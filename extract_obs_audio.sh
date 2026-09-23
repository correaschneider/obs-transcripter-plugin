#!/bin/bash
# Extrai as 3 audio tracks de uma gravação OBS (.mkv ou .mp4),
# organiza tudo numa pasta dedicada e (opcionalmente) dispara transcribe/analyze/notify.
#
# Uso: ./extract_obs_audio.sh <arquivo.mkv|mp4> [m4a|wav]
#
# Variáveis de ambiente:
#   TRANSCRIBE_SCRIPT  → encadeia transcribe.sh
#   ANALYZE_SCRIPT     → propagado pro transcribe
#   NOTIFY_SCRIPT      → propagado pro analyze
#   CLAUDE_CONFIG_DIR  → conta Claude
#   DISCORD_WEBHOOK_URL → webhook

set -euo pipefail

INPUT="${1:-}"
FORMAT="${2:-m4a}"

if [[ -z "$INPUT" || ! -f "$INPUT" ]]; then
    echo "Uso: $0 <arquivo.mkv|mp4> [m4a|wav]"
    exit 1
fi

# Valida extensão
INPUT_EXT="${INPUT##*.}"
INPUT_EXT_LOWER="${INPUT_EXT,,}"
case "$INPUT_EXT_LOWER" in
    mkv|mp4|mov|webm)
        VIDEO_EXT="$INPUT_EXT_LOWER"
        ;;
    *)
        echo "✗ Extensão não suportada: $INPUT_EXT"
        echo "  Suportadas: mkv, mp4, mov, webm"
        exit 1
        ;;
esac

DIR=$(dirname "$INPUT")
FILENAME=$(basename "$INPUT")
BASENAME="${FILENAME%.*}"
FOLDER_NAME="${BASENAME// /_}"
TARGET_DIR="${DIR}/${FOLDER_NAME}"

# Template do OBS tem precisão de hora — duas gravações na mesma hora colidem.
# Se já há um video.<ext> na pasta, anexa sufixo _2, _3, … pra preservar a anterior.
if [[ -d "$TARGET_DIR" && -f "${TARGET_DIR}/video.${VIDEO_EXT}" ]]; then
    n=2
    while [[ -d "${DIR}/${FOLDER_NAME}_${n}" && -f "${DIR}/${FOLDER_NAME}_${n}/video.${VIDEO_EXT}" ]]; do
        n=$((n + 1))
    done
    TARGET_DIR="${DIR}/${FOLDER_NAME}_${n}"
    echo "ℹ Colisão detectada; usando pasta: $TARGET_DIR"
fi

mkdir -p "$TARGET_DIR"

VIDEO_DEST="${TARGET_DIR}/video.${VIDEO_EXT}"
if [[ "$INPUT" != "$VIDEO_DEST" ]]; then
    mv "$INPUT" "$VIDEO_DEST"
fi

echo "→ Pasta:    $TARGET_DIR"
echo "→ Vídeo:    $VIDEO_DEST"
echo "→ Formato áudio: $FORMAT"
echo ""

# Verifica quantas tracks de áudio o arquivo tem
NUM_AUDIO_TRACKS=$(ffprobe -v error -select_streams a -show_entries stream=index -of csv=p=0 "$VIDEO_DEST" | wc -l)
echo "→ Tracks de áudio detectadas: $NUM_AUDIO_TRACKS"

if [[ "$NUM_AUDIO_TRACKS" -lt 3 ]]; then
    echo "⚠ Esperava 3 tracks (mix/desktop/mic), encontrei $NUM_AUDIO_TRACKS."
    echo "  Provavelmente esta gravação não foi feita com multi-track habilitado no OBS."
fi
echo ""

# Configura codec por formato de saída
if [[ "$FORMAT" == "wav" ]]; then
    CODEC=(-c:a pcm_s16le)
    EXT="wav"
else
    # m4a: tenta copy primeiro. Se o source não for AAC (MP4 raramente, MKV sempre), reencoda.
    CODEC=(-c:a aac -b:a 192k)
    EXT="m4a"

    # Detecta codec da primeira track de áudio
    AUDIO_CODEC=$(ffprobe -v error -select_streams a:0 -show_entries stream=codec_name -of csv=p=0 "$VIDEO_DEST" || echo "unknown")
    if [[ "$AUDIO_CODEC" == "aac" ]]; then
        CODEC=(-c:a copy)
        echo "→ Áudio em AAC, usando -c copy (sem reencode)"
    else
        echo "→ Áudio em $AUDIO_CODEC, reencodando pra AAC 192k"
    fi
    echo ""
fi

# Monta args do ffmpeg dinamicamente (caso tenha menos de 3 tracks)
FFMPEG_ARGS=(-hide_banner -loglevel warning -n -stats -i "$VIDEO_DEST")

TRACK_NAMES=("track1_mix" "track2_desktop" "track3_mic")
for i in 0 1 2; do
    if [[ "$i" -lt "$NUM_AUDIO_TRACKS" ]]; then
        FFMPEG_ARGS+=(-map "0:a:$i" "${CODEC[@]}" "${TARGET_DIR}/${TRACK_NAMES[$i]}.${EXT}")
    fi
done

ffmpeg "${FFMPEG_ARGS[@]}"

echo ""
echo "✓ Extração concluída."

# Encadeia transcribe
if [[ -n "${TRANSCRIBE_SCRIPT:-}" && -x "$TRANSCRIBE_SCRIPT" ]]; then
    echo ""
    echo "→ Iniciando transcrição..."
    "$TRANSCRIBE_SCRIPT" "$TARGET_DIR"
else
    echo "ℹ Transcrição não configurada (defina TRANSCRIBE_SCRIPT)."
fi

echo ""
echo "✓ Pipeline finalizado. Conteúdo da pasta:"
ls -lh "$TARGET_DIR"