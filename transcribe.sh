#!/bin/bash
# Transcreve as 3 audio tracks de uma pasta de gravação OBS usando WhisperX.
# Opcionalmente encadeia análise da reunião (ata + tarefas) via Claude Code.
#
# Uso: ./transcribe.sh <pasta_gravacao>
#
# Variáveis de ambiente:
#   ANALYZE_SCRIPT → caminho pro analyze_meeting.sh (se setado, dispara automático)

set -euo pipefail

TARGET_DIR="${1:-}"

if [[ -z "$TARGET_DIR" || ! -d "$TARGET_DIR" ]]; then
    echo "Uso: $0 <pasta_da_gravacao>"
    exit 1
fi

# Localiza whisperx-cli
WHISPERX="${WHISPERX_BIN:-$HOME/.local/bin/whisperx-cli}"
if [[ ! -x "$WHISPERX" ]]; then
    if command -v whisperx-cli &>/dev/null; then
        WHISPERX=$(command -v whisperx-cli)
    elif command -v whisperx &>/dev/null; then
        WHISPERX=$(command -v whisperx)
    else
        echo "✗ whisperx-cli não encontrado."
        exit 1
    fi
fi

MODEL="${WHISPERX_MODEL:-large-v3}"
DEVICE="${WHISPERX_DEVICE:-cuda}"
COMPUTE_TYPE="${WHISPERX_COMPUTE_TYPE:-int8}"
LANGUAGE="${WHISPERX_LANGUAGE:-pt}"
BATCH_SIZE="${WHISPERX_BATCH:-8}"
OUTPUT_FORMAT="${WHISPERX_FORMAT:-srt}"

echo "→ Pasta: $TARGET_DIR"
echo "→ Modelo: $MODEL | Device: $DEVICE | Idioma: $LANGUAGE"
echo ""

run_whisperx() {
    local audio="$1"
    local label="$2"

    if [[ ! -f "$audio" ]]; then
        echo "⊘ Pulando $label (arquivo não existe)"
        return
    fi

    local audio_base="${audio%.*}"
    if [[ -f "${audio_base}.srt" ]]; then
        echo "⊘ Pulando $label (já transcrito)"
        return
    fi

    echo "▶ Transcrevendo $label..."
    "$WHISPERX" "$audio" \
        --model "$MODEL" \
        --device "$DEVICE" \
        --compute_type "$COMPUTE_TYPE" \
        --language "$LANGUAGE" \
        --batch_size "$BATCH_SIZE" \
        --output_format "$OUTPUT_FORMAT" \
        --output_dir "$TARGET_DIR"
    echo "✓ $label concluído"
    echo ""
}

run_whisperx "${TARGET_DIR}/track1_mix.m4a"     "track1 (mix)"
run_whisperx "${TARGET_DIR}/track2_desktop.m4a" "track2 (desktop)"
run_whisperx "${TARGET_DIR}/track3_mic.m4a"     "track3 (mic)"

echo "✓ Transcrição concluída."

# Encadeia análise da reunião se configurado
if [[ -n "${ANALYZE_SCRIPT:-}" && -x "$ANALYZE_SCRIPT" ]]; then
    echo ""
    echo "→ Iniciando análise da reunião..."
    "$ANALYZE_SCRIPT" "$TARGET_DIR"
else
    echo "ℹ Análise não configurada (defina ANALYZE_SCRIPT pra encadear)."
fi

echo ""
echo "✓ Conteúdo final da pasta:"
ls -lh "$TARGET_DIR"