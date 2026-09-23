#!/bin/bash
# Transcreve UM split (partN) de uma reunião OBS via WhisperX.
# Idempotente: pula tracks que já têm SRT.
# OOM-resiliente: retry com batch decrescente (8 → 4 → 2) ao detectar OOM.
# Serial: usa flock global pra evitar transcrições concorrentes (preserva VRAM).
#
# Uso: ./transcribe_split.sh <pasta_reuniao> <part_num_padded>
#
# Variáveis de ambiente (todas opcionais):
#   WHISPERX_BIN          → caminho pro whisperx-cli
#   WHISPERX_MODEL        → default: large-v3
#   WHISPERX_DEVICE       → default: cuda
#   WHISPERX_COMPUTE_TYPE → default: int8 (seguro pra 8GB VRAM)
#   WHISPERX_LANGUAGE     → default: pt
#   WHISPERX_BATCH        → default: 8 (entrada do retry decrescente)
#   WHISPERX_FORMAT       → default: srt
#   WHISPERX_LOCK         → default: /tmp/whisperx-pipeline.lock

set -uo pipefail

TARGET_DIR="${1:-}"
PART_NUM="${2:-}"

if [[ -z "$TARGET_DIR" || ! -d "$TARGET_DIR" || -z "$PART_NUM" ]]; then
    echo "Uso: $0 <pasta_reuniao> <part_num_padded>"
    exit 1
fi

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
INITIAL_BATCH="${WHISPERX_BATCH:-8}"
OUTPUT_FORMAT="${WHISPERX_FORMAT:-srt}"
LOCK_FILE="${WHISPERX_LOCK:-/tmp/whisperx-pipeline.lock}"

echo "→ Pasta:    $TARGET_DIR"
echo "→ Part:     $PART_NUM"
echo "→ Modelo:   $MODEL | Device: $DEVICE | Lang: $LANGUAGE"
echo "→ Compute:  $COMPUTE_TYPE | Batch inicial: $INITIAL_BATCH"
echo "→ Lock:     $LOCK_FILE"
echo ""

# Retry com batch decrescente: 8 → 4 → 2 (mínimo). Só desce se detectar OOM.
run_one_track() {
    local audio="$1"
    local label="$2"

    if [[ ! -f "$audio" ]]; then
        echo "⊘ $label (arquivo não existe)"
        return 0
    fi

    local audio_base="${audio%.*}"
    if [[ -f "${audio_base}.srt" ]]; then
        echo "⊘ $label (já transcrito)"
        return 0
    fi

    local batch="$INITIAL_BATCH"
    while (( batch >= 2 )); do
        echo "▶ $label (batch=$batch)..."
        local stderr_file
        stderr_file=$(mktemp)
        if "$WHISPERX" "$audio" \
            --model "$MODEL" \
            --device "$DEVICE" \
            --compute_type "$COMPUTE_TYPE" \
            --language "$LANGUAGE" \
            --batch_size "$batch" \
            --output_format "$OUTPUT_FORMAT" \
            --output_dir "$TARGET_DIR" \
            2> >(tee "$stderr_file" >&2); then
            rm -f "$stderr_file"
            echo "✓ $label concluído (batch=$batch)"
            return 0
        fi

        if grep -qiE "out of memory|cuda failed|cublasstatus|cudnn failure" "$stderr_file"; then
            local new_batch=$((batch / 2))
            rm -f "$stderr_file"
            if (( new_batch < 2 )); then
                echo "✗ $label: OOM mesmo com batch=2."
                return 1
            fi
            echo "⚠ OOM em batch=$batch — retry com batch=$new_batch"
            batch=$new_batch
            sleep 2
            continue
        fi

        echo "✗ $label: falha não-OOM (stderr acima)."
        rm -f "$stderr_file"
        return 1
    done

    return 1
}

(
    flock -x 9 || { echo "✗ Não consegui adquirir lock"; exit 1; }
    echo "🔒 Lock adquirido."

    failed=0
    for track in track1_mix track2_desktop track3_mic; do
        audio="${TARGET_DIR}/${track}_part${PART_NUM}.m4a"
        if ! run_one_track "$audio" "${track} part${PART_NUM}"; then
            failed=$((failed + 1))
        fi
    done

    echo "🔓 Liberando lock."
    exit "$failed"
) 9>"$LOCK_FILE"

exit_code=$?
echo ""
if (( exit_code > 0 )); then
    echo "⚠ $exit_code track(s) falharam no part${PART_NUM}."
    exit 1
fi
echo "✓ Transcrição do part${PART_NUM} concluída."
