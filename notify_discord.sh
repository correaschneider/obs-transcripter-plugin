#!/bin/bash
# Envia ata.md e tarefas.md pro Discord via webhook.
# Cada arquivo vai como embed (preview) + anexo (.md completo).
#
# Uso: ./notify_discord.sh <pasta_gravacao>
#
# Variáveis de ambiente:
#   DISCORD_WEBHOOK_URL  → URL do webhook (obrigatório)
#   DISCORD_USERNAME     → nome do bot (default: "OBS Pipeline")
#   DISCORD_AVATAR_URL   → avatar opcional

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

TARGET_DIR="${1:-}"

if [[ -z "$TARGET_DIR" || ! -d "$TARGET_DIR" ]]; then
    echo "Uso: $0 <pasta_da_gravacao>"
    exit 1
fi

TARGET_DIR=$(realpath "$TARGET_DIR")
ATA="${TARGET_DIR}/ata.md"
TAREFAS="${TARGET_DIR}/tarefas.md"
TRANSCRICAO="${TARGET_DIR}/track1_mix.srt"
TRANSCRICAO_TXT="${TARGET_DIR}/transcricao.txt"
MEETING_NAME=$(basename "$TARGET_DIR")

if [[ -z "${DISCORD_WEBHOOK_URL:-}" ]]; then
    echo "✗ DISCORD_WEBHOOK_URL não definido"
    exit 1
fi

if [[ ! -f "$ATA" || ! -f "$TAREFAS" ]]; then
    echo "✗ Arquivos ausentes em $TARGET_DIR"
    [[ ! -f "$ATA" ]] && echo "  faltando: ata.md"
    [[ ! -f "$TAREFAS" ]] && echo "  faltando: tarefas.md"
    exit 1
fi

USERNAME="${DISCORD_USERNAME:-OBS Pipeline}"
AVATAR="${DISCORD_AVATAR_URL:-}"
# Limite de upload do webhook (10MB no tier default); folga pro multipart.
MAX_ATTACH_BYTES="${DISCORD_MAX_ATTACH_BYTES:-9000000}"

# Arquivo temp pro payload (cleanup automático)
PAYLOAD_FILE=$(mktemp --suffix=.json)
trap 'rm -f "$PAYLOAD_FILE" /tmp/discord_response.txt' EXIT

# Gera o payload_json e grava no arquivo temp
# args: title color_dec file_path [preview_mode: md|srt]
build_payload_file() {
    local title="$1"
    local color_dec="$2"
    local file_path="$3"
    local preview_mode="${4:-md}"

    python3 - "$title" "$color_dec" "$file_path" "$MEETING_NAME" "$USERNAME" "$AVATAR" "$PAYLOAD_FILE" "$preview_mode" <<'PYEOF'
import json
import re
import sys
from datetime import datetime, timezone

title, color_dec, file_path, meeting_name, username, avatar, out_path, preview_mode = sys.argv[1:9]

with open(file_path, "r", encoding="utf-8") as f:
    content = f.read()

# Frontmatter e galeria (lib/vault.py) são pro Obsidian; no Discord os prints vão à parte.
content = re.sub(r"\A---\n.*?\n---\n+", "", content, flags=re.S)
content = re.sub(r"\n*<!-- momentos:inicio -->.*?<!-- momentos:fim -->\n*", "\n", content, flags=re.S)

if preview_mode == "srt":
    # Preview legível: descarta índice e linha de timestamp, junta as falas.
    lines = []
    for line in content.splitlines():
        line = line.strip()
        if not line or "-->" in line or line.isdigit():
            continue
        lines.append(line)
    content = " ".join(lines)

LIMIT = 3900
if len(content) > LIMIT:
    content = content[:LIMIT] + "\n\n...\n_(conteúdo truncado — veja o arquivo anexo)_"

payload = {
    "username": username,
    "embeds": [
        {
            "title": title,
            "description": content,
            "color": int(color_dec),
            "footer": {"text": f"Reunião: {meeting_name}"},
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    ],
}

if avatar:
    payload["avatar_url"] = avatar

with open(out_path, "w", encoding="utf-8") as f:
    json.dump(payload, f, ensure_ascii=False)
PYEOF
}

# Envia uma mensagem
# args: title color_hex file_path [preview_mode: md|srt]
send_discord_message() {
    local title="$1"
    local color_hex="$2"
    local file_path="$3"
    local preview_mode="${4:-md}"

    local color_dec=$((16#${color_hex}))
    build_payload_file "$title" "$color_dec" "$file_path" "$preview_mode"

    echo "▶ Enviando: $title"

    # Anexo acima do limite do webhook: manda só o embed (preview) e avisa.
    local curl_attach=(-F "file1=@${file_path}")
    local size_bytes
    size_bytes=$(stat -c %s "$file_path")
    if (( size_bytes > MAX_ATTACH_BYTES )); then
        echo "  ⚠ $(basename "$file_path") tem $((size_bytes / 1024 / 1024))MB (> $((MAX_ATTACH_BYTES / 1024 / 1024))MB) — enviando só o preview."
        curl_attach=()
    fi

    local http_code
    http_code=$(curl -sS -o /tmp/discord_response.txt -w "%{http_code}" \
        -X POST "$DISCORD_WEBHOOK_URL" \
        -F "payload_json=<${PAYLOAD_FILE};type=application/json" \
        "${curl_attach[@]}")

    if [[ "$http_code" =~ ^2 ]]; then
        echo "  ✓ Enviado (HTTP $http_code)"
    else
        echo "  ✗ Falha (HTTP $http_code)"
        cat /tmp/discord_response.txt
        echo ""
        return 1
    fi

    # Webhook rate limit: 5 req/2s
    sleep 1
}

echo "→ Pasta: $TARGET_DIR"
echo "→ Reunião: $MEETING_NAME"
echo ""

send_discord_message "📋 Ata da reunião"         "3498DB" "$ATA"
send_discord_message "✅ Tarefas / Action Items" "2ECC71" "$TAREFAS"

# Transcrição é opcional: pode faltar (falha do WhisperX) ou vir vazia.
# Pipeline de split grava transcricao.txt (Lord × Participantes intercalados);
# gravações antigas só têm track1_mix.srt.
if [[ -s "$TRANSCRICAO_TXT" ]]; then
    send_discord_message "📝 Transcrição" "9B59B6" "$TRANSCRICAO_TXT"
elif [[ -s "$TRANSCRICAO" ]]; then
    send_discord_message "📝 Transcrição (mix)" "9B59B6" "$TRANSCRICAO" "srt"
else
    echo "⊘ Sem transcrição não-vazia — não enviada."
fi

# Galeria de prints dos momentos-chave (moments.json, gerado no finalize)
if [[ -f "${TARGET_DIR}/moments.json" ]]; then
    echo "▶ Enviando: 🖼️ Momentos da reunião"
    python3 "$SCRIPT_DIR/lib/gallery.py" "$TARGET_DIR"
fi

echo ""
echo "✓ Notificações enviadas com sucesso."