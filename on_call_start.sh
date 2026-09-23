#!/bin/bash
# Gancho: roda quando a gravação começa (= call começou, via Advanced Scene Switcher).
# Acrescente aqui outras ações de início de call.
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
echo "=== início da call $(date '+%F %T') ==="
python3 "$SCRIPT_DIR/audio_profile.py" call
# App de call maximizado derruba a captura de janela do OBS (ver window_fit.py)
python3 "$SCRIPT_DIR/window_fit.py"
