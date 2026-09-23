#!/bin/bash
# Processa UMA parte (split) de uma gravação do OBS:
#   1. move o vídeo pra pasta da reunião (partNNN.<ext>) e grava partNNN.duration
#   2. transcreve direto do vídeo (desktop + mic; ou a faixa única) → *_partNNN.json/.srt
#   3. Claude escolhe os momentos-chave da parte → moments_partNNN.json
#   4. extrai os prints desses momentos → moments/partNNN_mMM.jpg
#   5. APAGA o vídeo da parte (se DELETE_VIDEO=1) e marca partNNN.done
#
# Se a transcrição ou os prints falharem, o vídeo é MANTIDO e fica partNNN.failed
# (dá pra reprocessar rodando este script de novo sobre o partNNN.<ext>).
#
# Modo plugin: o plugin passa MEETING_FOLDER e PART_NUMBER via env.
# Modo manual: parse do nome ("2026-05-20 12-15 (2).mkv" → pasta 2026-05-20_12-15, part 002).
#
# Uso: ./process_split.sh <arquivo.mkv|mp4>
#
# Env: MEETING_FOLDER, PART_NUMBER, DELETE_VIDEO (default 1), MOMENTS_PER_PART (default 3),
#      CLAUDE_CONFIG_DIR, WHISPERX_SERVER_URL, WHISPERX_LANGUAGE

set -uo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
LIB="$SCRIPT_DIR/lib"
INPUT="${1:-}"

if [[ -z "$INPUT" || ! -f "$INPUT" ]]; then
    echo "Uso: $0 <arquivo.mkv|mp4>"
    exit 1
fi

INPUT=$(realpath "$INPUT")
INPUT_EXT_LOWER="${INPUT##*.}"
INPUT_EXT_LOWER="${INPUT_EXT_LOWER,,}"
DIR=$(dirname "$INPUT")
FILENAME=$(basename "$INPUT")
BASENAME="${FILENAME%.*}"

if [[ "$BASENAME" =~ ^part([0-9]{3})$ ]]; then
    # Reprocessamento de uma parte que já está na pasta da reunião
    TARGET_DIR="$DIR"
    PART_NUM="${BASH_REMATCH[1]}"
elif [[ -n "${MEETING_FOLDER:-}" && -n "${PART_NUMBER:-}" ]]; then
    TARGET_DIR="${DIR}/${MEETING_FOLDER}"
    PART_NUM="$PART_NUMBER"
else
    if [[ "$BASENAME" =~ ^(.+)\ \(([0-9]+)\)$ ]]; then
        BASE="${BASH_REMATCH[1]}"
        PART_NUM=$(printf "%03d" "${BASH_REMATCH[2]}")
    else
        BASE="$BASENAME"
        PART_NUM="001"
    fi
    TARGET_DIR="${DIR}/${BASE// /_}"
fi

mkdir -p "$TARGET_DIR"
MARK="${TARGET_DIR}/part${PART_NUM}"
VIDEO_DEST="${MARK}.${INPUT_EXT_LOWER}"

if [[ "$INPUT" != "$VIDEO_DEST" ]]; then
    if [[ -e "$VIDEO_DEST" || -e "${MARK}.done" ]]; then
        echo "✗ part${PART_NUM} já existe em $TARGET_DIR — não sobrescrevo."
        exit 1
    fi
    mv "$INPUT" "$VIDEO_DEST"
fi

fail() {
    echo "✗ $1"
    echo "$1" > "${MARK}.failed"
    exit 1
}

rm -f "${MARK}.failed"

echo "→ Reunião:  $(basename "$TARGET_DIR")"
echo "→ Split:    part${PART_NUM}"
echo "→ Vídeo:    $VIDEO_DEST"
echo ""

DURATION=$(python3 "$LIB/common.py" duration "$VIDEO_DEST") || fail "não consegui ler a duração"
echo "$DURATION" > "${MARK}.duration"
echo "→ Duração:  ${DURATION}s"
echo ""

echo "▶ Transcrevendo direto do vídeo..."
python3 "$LIB/transcribe.py" "$VIDEO_DEST" --out-dir "$TARGET_DIR" --part "$PART_NUM" \
    || fail "transcrição da part${PART_NUM} falhou — vídeo mantido"
echo ""

echo "▶ Escolhendo momentos-chave..."
python3 "$LIB/moments.py" select "$TARGET_DIR" --part "$PART_NUM" --max "${MOMENTS_PER_PART:-3}" \
    || fail "seleção de momentos da part${PART_NUM} falhou — vídeo mantido"
echo ""

echo "▶ Extraindo prints..."
python3 "$LIB/frames.py" "$VIDEO_DEST" "${TARGET_DIR}/moments_part${PART_NUM}.json" \
    --out-dir "${TARGET_DIR}/moments" --prefix "part${PART_NUM}" \
    || fail "prints da part${PART_NUM} falharam — vídeo mantido"
echo ""

if [[ "${DELETE_VIDEO:-1}" == "1" ]]; then
    SIZE=$(du -h "$VIDEO_DEST" | cut -f1)
    rm -f -- "$VIDEO_DEST"
    echo "🗑 Vídeo da part${PART_NUM} apagado (${SIZE} liberados)."
else
    echo "ℹ DELETE_VIDEO=0 — vídeo mantido."
fi

touch "${MARK}.done"
echo "✓ Split part${PART_NUM} processado."
