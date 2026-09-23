#!/bin/bash
# Finaliza uma reunião gravada em partes:
#   1. aguarda as partes pendentes (vídeo presente sem partNNN.done/.failed)
#   2. concatena as transcrições com offset (durações de partNNN.duration)
#   3. monta transcricao.txt intercalando Lord × Participantes
#   4. gera ata + tarefas (analyze_meeting.sh)
#   5. escolhe os melhores prints da reunião (moments.json)
#   6. frontmatter + galeria de prints na ata (lib/vault.py)
#   7. envia tudo ao Discord (notify_discord.sh)
#   8. move a reunião pro vault do Obsidian e regera o índice (lib/vault.py)
#
# Uso: ./finalize_meeting.sh <pasta_reuniao>
#
# Env: ANALYZE_SCRIPT / NOTIFY_SCRIPT (default: irmãos deste script), DISCORD_WEBHOOK_URL,
#      MOMENTS_MAX (default 10), FINALIZE_MAX_WAIT (default 3600s),
#      MIN_MEETING_SECONDS (default 60: gravação mais curta não gera ata nem vai pro Discord),
#      VAULT_PUBLISH (default 1; 0 = deixa a reunião na pasta de gravação), OBSIDIAN_VAULT

set -uo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
LIB="$SCRIPT_DIR/lib"
ANALYZE_SCRIPT="${ANALYZE_SCRIPT:-$SCRIPT_DIR/analyze_meeting.sh}"
NOTIFY_SCRIPT="${NOTIFY_SCRIPT:-$SCRIPT_DIR/notify_discord.sh}"
TARGET_DIR="${1:-}"

if [[ -z "$TARGET_DIR" || ! -d "$TARGET_DIR" ]]; then
    echo "Uso: $0 <pasta_reuniao>"
    exit 1
fi
TARGET_DIR=$(realpath "$TARGET_DIR")
echo "→ Pasta: $TARGET_DIR"
echo ""

# 1. Aguarda partes pendentes
echo "▶ Verificando partes pendentes..."
MAX_WAIT="${FINALIZE_MAX_WAIT:-3600}"
WAITED=0
shopt -s nullglob
while true; do
    PENDING=0
    for video in "$TARGET_DIR"/part[0-9][0-9][0-9].{mkv,mp4,mov,webm}; do
        mark="${video%.*}"
        [[ -f "${mark}.done" || -f "${mark}.failed" ]] || PENDING=$((PENDING + 1))
    done
    if [[ "$PENDING" -eq 0 ]]; then
        echo "✓ Nenhuma parte pendente."
        break
    fi
    if [[ "$WAITED" -ge "$MAX_WAIT" ]]; then
        echo "⚠ Timeout (${MAX_WAIT}s) com $PENDING parte(s) pendente(s) — seguindo."
        break
    fi
    echo "  ⏳ Aguardando $PENDING parte(s)... (${WAITED}s)"
    sleep 10
    WAITED=$((WAITED + 10))
done
FAILED=("$TARGET_DIR"/part*.failed)
shopt -u nullglob
echo ""

# 2-3. Transcrições da reunião inteira
echo "▶ Concatenando transcrições..."
python3 "$LIB/transcript.py" concat "$TARGET_DIR"
python3 "$LIB/transcript.py" merge "$TARGET_DIR"
echo ""

# Gravação curta (teste, disparo acidental): mantém transcrição e prints na pasta,
# mas não gasta Claude nem polui o Discord.
MIN_SECONDS="${MIN_MEETING_SECONDS:-60}"
TOTAL_SECONDS=$(cat "$TARGET_DIR"/part[0-9][0-9][0-9].duration 2>/dev/null | awk '{s+=$1} END {printf "%d", s}')
if (( TOTAL_SECONDS < MIN_SECONDS )); then
    echo "⊘ Gravação com ${TOTAL_SECONDS}s (< ${MIN_SECONDS}s) — sem ata, sem prints finais e sem Discord."
    echo "✓ Finalização concluída (gravação curta)."
    exit 0
fi

# 4. Ata + tarefas (sem notificar ainda: os prints vêm antes)
if [[ -x "$ANALYZE_SCRIPT" ]]; then
    echo "▶ Iniciando análise..."
    NOTIFY_SCRIPT="" "$ANALYZE_SCRIPT" "$TARGET_DIR" || echo "⚠ Análise falhou."
else
    echo "ℹ ANALYZE_SCRIPT não encontrado: $ANALYZE_SCRIPT"
fi
echo ""

# 5. Melhores prints da reunião
echo "▶ Selecionando prints da reunião..."
python3 "$LIB/moments.py" pick "$TARGET_DIR" --max "${MOMENTS_MAX:-10}"
echo ""

# 6. Frontmatter (empresa/título/participantes vêm do Claude na ata; Haiku só se faltar) + galeria
if [[ -f "$TARGET_DIR/ata.md" ]]; then
    echo "▶ Frontmatter e galeria da ata..."
    python3 "$LIB/vault.py" enrich "$TARGET_DIR" || echo "⚠ Frontmatter falhou."
    python3 "$LIB/vault.py" gallery "$TARGET_DIR" || echo "⚠ Galeria falhou."
    echo ""
fi

# 7. Discord
if [[ -x "$NOTIFY_SCRIPT" && -n "${DISCORD_WEBHOOK_URL:-}" ]]; then
    echo "▶ Enviando ao Discord..."
    "$NOTIFY_SCRIPT" "$TARGET_DIR" || echo "⚠ Envio ao Discord falhou."
else
    echo "ℹ Discord não configurado (NOTIFY_SCRIPT/DISCORD_WEBHOOK_URL) — nada enviado."
fi

# 8. Vault: move a reunião (ata, tarefas, transcrição, SRTs, áudios, prints) e regera o índice.
#    Parte com falha mantém o vídeo pra reprocessar → aí a pasta fica onde está.
echo ""
if [[ "${VAULT_PUBLISH:-1}" == "1" && -f "$TARGET_DIR/ata.md" && ${#FAILED[@]} -eq 0 ]]; then
    echo "▶ Publicando no vault..."
    if python3 "$LIB/vault.py" publish "$TARGET_DIR" --move; then
        python3 "$LIB/vault.py" index || echo "⚠ Índice do vault falhou."
        echo "✓ Finalização concluída."
        exit 0
    fi
    echo "⚠ Publicação no vault falhou — reunião mantida em $TARGET_DIR."
fi

echo ""
if [[ ${#FAILED[@]} -gt 0 ]]; then
    echo "⚠ Partes com falha (vídeo mantido pra reprocessar):"
    for f in "${FAILED[@]}"; do echo "   $(basename "$f"): $(cat "$f")"; done
fi
echo "✓ Finalização concluída."
ls -lh "$TARGET_DIR" | head -40
