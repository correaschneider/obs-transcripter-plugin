#!/bin/bash
# Processa em batch gravações antigas (.mkv ou .mp4 solto, ou pastas parciais).
# Encadeia todo o pipeline: extract → transcribe → analyze → notify → vault.

set -euo pipefail

BASE_DIR="${1:-/data/shared/calls}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXTRACT_SCRIPT="${EXTRACT_SCRIPT:-$SCRIPT_DIR/extract_obs_audio.sh}"

if [[ ! -d "$BASE_DIR" ]]; then
    echo "✗ Diretório não existe: $BASE_DIR"
    exit 1
fi

if [[ ! -x "$EXTRACT_SCRIPT" ]]; then
    echo "✗ Extract script não encontrado: $EXTRACT_SCRIPT"
    exit 1
fi

cd "$BASE_DIR"

echo "═══════════════════════════════════════════"
echo "  Backfill OBS Pipeline"
echo "  Base: $BASE_DIR"
echo "═══════════════════════════════════════════"
echo ""

shopt -s nullglob

# === 1. Gravações .mkv/.mp4 soltas (pipeline completo) ===
echo "═══ Fase 1: Gravações soltas (.mkv/.mp4) ═══"
loose_count=0
for video in *.mkv *.MKV *.mp4 *.MP4 *.mov *.webm; do
    [[ -f "$video" ]] || continue
    echo ""
    echo "▶ Processando: $video"
    "$EXTRACT_SCRIPT" "$BASE_DIR/$video" || {
        echo "✗ Falhou: $video (continuando...)"
    }
    loose_count=$((loose_count + 1))
done
[[ $loose_count -eq 0 ]] && echo "  (nenhuma)"

# === 2. Pastas com video.* mas sem tracks ===
echo ""
echo "═══ Fase 2: Pastas sem tracks de áudio ═══"
partial_count=0
for dir in */; do
    # Encontra o vídeo (qualquer extensão suportada)
    video=""
    for ext in mkv mp4 mov webm; do
        if [[ -f "${dir}video.${ext}" ]]; then
            video="${dir}video.${ext}"
            break
        fi
    done

    if [[ -n "$video" && ! -f "${dir}track1_mix.m4a" ]]; then
        echo ""
        echo "▶ Extraindo tracks: $dir"

        # Detecta codec pra decidir copy vs reencode
        audio_codec=$(ffprobe -v error -select_streams a:0 -show_entries stream=codec_name -of csv=p=0 "$video" 2>/dev/null || echo "unknown")
        if [[ "$audio_codec" == "aac" ]]; then
            codec_args=(-c:a copy)
        else
            codec_args=(-c:a aac -b:a 192k)
        fi

        # Detecta quantas tracks
        num_tracks=$(ffprobe -v error -select_streams a -show_entries stream=index -of csv=p=0 "$video" | wc -l)
        track_names=("track1_mix" "track2_desktop" "track3_mic")

        ffmpeg_args=(-hide_banner -loglevel warning -stats -i "$video")
        for i in 0 1 2; do
            if [[ "$i" -lt "$num_tracks" ]]; then
                ffmpeg_args+=(-map "0:a:$i" "${codec_args[@]}" "${dir}${track_names[$i]}.m4a")
            fi
        done

        ffmpeg "${ffmpeg_args[@]}" || true
        partial_count=$((partial_count + 1))
    fi
done
[[ $partial_count -eq 0 ]] && echo "  (nenhuma)"

# === 3. Pastas sem SRTs ===
echo ""
echo "═══ Fase 3: Pastas sem transcrição ═══"
if [[ -n "${TRANSCRIBE_SCRIPT:-}" && -x "$TRANSCRIBE_SCRIPT" ]]; then
    trans_count=0
    for dir in */; do
        if [[ -f "${dir}track1_mix.m4a" && ! -f "${dir}track1_mix.srt" ]]; then
            echo ""
            echo "▶ Transcrevendo: $dir"
            "$TRANSCRIBE_SCRIPT" "$BASE_DIR/${dir%/}" || {
                echo "✗ Falhou: $dir (continuando...)"
            }
            trans_count=$((trans_count + 1))
        fi
    done
    [[ $trans_count -eq 0 ]] && echo "  (nenhuma)"
else
    echo "  ⊘ TRANSCRIBE_SCRIPT não setado, pulando."
fi

# === 4. Pastas sem ata/tarefas ===
echo ""
echo "═══ Fase 4: Pastas sem ata/tarefas ═══"
if [[ -n "${ANALYZE_SCRIPT:-}" && -x "$ANALYZE_SCRIPT" ]]; then
    analyze_count=0
    for dir in */; do
        if [[ -f "${dir}track2_desktop.srt" && -f "${dir}track3_mic.srt" ]]; then
            if [[ ! -f "${dir}ata.md" || ! -f "${dir}tarefas.md" ]]; then
                echo ""
                echo "▶ Analisando: $dir"
                "$ANALYZE_SCRIPT" "$BASE_DIR/${dir%/}" || {
                    echo "✗ Falhou: $dir (continuando...)"
                }
                analyze_count=$((analyze_count + 1))
            fi
        fi
    done
    [[ $analyze_count -eq 0 ]] && echo "  (nenhuma)"
else
    echo "  ⊘ ANALYZE_SCRIPT não setado, pulando."
fi

# === 5. Notificações Discord (com confirmação) ===
echo ""
echo "═══ Fase 5: Notificações Discord pendentes ═══"
if [[ -n "${NOTIFY_SCRIPT:-}" && -x "$NOTIFY_SCRIPT" && -n "${DISCORD_WEBHOOK_URL:-}" ]]; then
    pending=()
    for dir in */; do
        if [[ -f "${dir}ata.md" && -f "${dir}tarefas.md" ]]; then
            pending+=("$dir")
        fi
    done

    if [[ ${#pending[@]} -eq 0 ]]; then
        echo "  (nenhuma)"
    else
        echo "  Vai enviar ${#pending[@]} reunião(ões) pro Discord:"
        for d in "${pending[@]}"; do
            echo "    - $d"
        done
        read -p "  Confirma envio? [y/N] " ans
        if [[ "$ans" == "y" || "$ans" == "Y" ]]; then
            for d in "${pending[@]}"; do
                echo ""
                echo "▶ Notificando: $d"
                "$NOTIFY_SCRIPT" "$BASE_DIR/${d%/}" || true
            done
        else
            echo "  Cancelado."
        fi
    fi
else
    echo "  ⊘ NOTIFY_SCRIPT ou DISCORD_WEBHOOK_URL ausentes, pulando."
fi

# === 6. Publicação no vault (move a pasta; ver lib/vault.py) ===
echo ""
echo "═══ Fase 6: Reuniões com ata ainda fora do vault ═══"
if [[ "${VAULT_PUBLISH:-1}" == "1" ]]; then
    to_publish=()
    for dir in */; do
        [[ -f "${dir}ata.md" && -f "${dir}tarefas.md" ]] && to_publish+=("$dir")
    done
    if [[ ${#to_publish[@]} -eq 0 ]]; then
        echo "  (nenhuma)"
    else
        echo "  Vai mover ${#to_publish[@]} reunião(ões) pro vault (${OBSIDIAN_VAULT:-/data/projects/Obsidian}):"
        for d in "${to_publish[@]}"; do echo "    - $d"; done
        read -p "  Confirma? [y/N] " ans
        if [[ "$ans" == "y" || "$ans" == "Y" ]]; then
            for d in "${to_publish[@]}"; do
                echo ""
                echo "▶ Vault: $d"
                python3 "$SCRIPT_DIR/lib/vault.py" enrich "$BASE_DIR/${d%/}" \
                    && python3 "$SCRIPT_DIR/lib/vault.py" gallery "$BASE_DIR/${d%/}" \
                    && python3 "$SCRIPT_DIR/lib/vault.py" publish "$BASE_DIR/${d%/}" --move \
                    || echo "✗ Falhou: $d (continuando...)"
            done
            python3 "$SCRIPT_DIR/lib/vault.py" index || true
        else
            echo "  Cancelado."
        fi
    fi
else
    echo "  ⊘ VAULT_PUBLISH=0, pulando."
fi

echo ""
echo "═══════════════════════════════════════════"
echo "  ✓ Backfill concluído"
echo "═══════════════════════════════════════════"