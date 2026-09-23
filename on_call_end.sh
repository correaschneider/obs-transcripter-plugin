#!/bin/bash
# Gancho: roda quando a gravação para (= call terminou).
# Acrescente aqui outras ações de fim de call.
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
echo "=== fim da call $(date '+%F %T') ==="
python3 "$SCRIPT_DIR/audio_profile.py" normal
