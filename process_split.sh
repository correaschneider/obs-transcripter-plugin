#!/bin/bash
# Prepara UMA parte (split) de uma gravação do OBS — só o que não usa GPU, durante a call:
#   1. move o vídeo pra pasta da reunião (partNNN.<ext>) e grava partNNN.duration
#   2. copia as faixas de áudio pra partNNN.mka (sem recodificar)
#   3. extrai 1 frame a cada 5 s → frames_partNNN/fNNNN.jpg (candidatos a print)
#   4. APAGA o vídeo da parte (se DELETE_VIDEO=1) e marca partNNN.done
#
# Transcrição, momentos-chave e prints ficam pro pós-call (lib/postcall.py, chamado pelo
# finalize_meeting.sh): durante a call a GPU é do copiloto ao vivo (dois modelos davam OOM).
# Se o áudio ou os frames falharem, o vídeo é MANTIDO e fica partNNN.failed
# (dá pra reprocessar rodando este script de novo sobre o partNNN.<ext>).
#
# Modo plugin: o plugin passa MEETING_FOLDER e PART_NUMBER via env.
# Modo manual: parse do nome ("2026-05-20 12-15 (2).mkv" → pasta 2026-05-20_12-15, part 002).
#
# Uso: ./process_split.sh <arquivo.mkv|mp4>
#
# Env: MEETING_FOLDER, PART_NUMBER, DELETE_VIDEO (default 1)

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

echo "▶ Guardando o áudio (cópia das faixas, sem recodificar)..."
ffmpeg -y -loglevel error -i "$VIDEO_DEST" -map 0:a -c copy "${MARK}.mka" \
    && [[ -s "${MARK}.mka" ]] || fail "áudio da part${PART_NUM} falhou — vídeo mantido"
echo ""

echo "▶ Frames a cada 5 s (candidatos a print)..."
FRAMES_DIR="${TARGET_DIR}/frames_part${PART_NUM}"
mkdir -p "$FRAMES_DIR"
ffmpeg -y -loglevel error -i "$VIDEO_DEST" -vf "fps=1/5,scale='min(1280,iw)':-2" -q:v 3 \
    -start_number 0 "${FRAMES_DIR}/f%04d.jpg" \
    || fail "frames da part${PART_NUM} falharam — vídeo mantido"
echo "  $(ls "$FRAMES_DIR" | wc -l) frame(s)"
echo ""

if [[ "${DELETE_VIDEO:-1}" == "1" ]]; then
    SIZE=$(du -h "$VIDEO_DEST" | cut -f1)
    rm -f -- "$VIDEO_DEST"
    echo "🗑 Vídeo da part${PART_NUM} apagado (${SIZE} liberados)."
else
    echo "ℹ DELETE_VIDEO=0 — vídeo mantido."
fi

touch "${MARK}.done"
echo "✓ Split part${PART_NUM} preparado (transcrição e prints no pós-call)."
