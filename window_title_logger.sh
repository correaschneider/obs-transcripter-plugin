#!/bin/bash
# Diagnóstico das macros do Advanced Scene Switcher: registra (só leitura) o título da janela
# ativa e de toda janela Teams/Meet sempre que mudam. Uso: ./window_title_logger.sh [segundos]
DUR="${1:-1800}"
LOG="$(cd "$(dirname "$0")" && pwd)/logs/window_titles_$(date +%Y%m%d_%H%M%S).log"
END=$(( $(date +%s) + DUR ))
last_active=""; last_calls=""
echo "registrando em $LOG por ${DUR}s"
while [[ $(date +%s) -lt $END ]]; do
    id=$(xprop -root _NET_ACTIVE_WINDOW 2>/dev/null | awk '{print $NF}')
    active=$(xprop -id "$id" _NET_WM_NAME 2>/dev/null | sed -E 's/^[^=]*= "(.*)"$/\1/')
    calls=$(wmctrl -l 2>/dev/null | cut -d' ' -f5- | grep -iE "teams|meet" | sort)
    if [[ "$active" != "$last_active" ]]; then
        echo "$(date +%T) FOCO: $active" >> "$LOG"; last_active="$active"
    fi
    if [[ "$calls" != "$last_calls" ]]; then
        echo "$(date +%T) JANELAS Teams/Meet:" >> "$LOG"
        [[ -n "$calls" ]] && sed 's/^/           /' <<<"$calls" >> "$LOG" || echo "           (nenhuma)" >> "$LOG"
        last_calls="$calls"
    fi
    sleep 0.5
done
echo "$(date +%T) fim" >> "$LOG"
