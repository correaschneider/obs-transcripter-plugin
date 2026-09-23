#!/bin/bash
# Migra arquivos OBS já gravados na pasta atual pro novo layout (pasta por gravação).
#
# Uso: ./organize_existing.sh [diretório]
#   (sem argumento → usa o diretório atual)
#
# Detecta cada arquivo "YYYY-MM-DD HH-MM-SS.mkv" e move ele + seus tracks correspondentes
# pra uma pasta dedicada.

set -euo pipefail

BASE_DIR="${1:-.}"
cd "$BASE_DIR"

shopt -s nullglob

for mkv in *.mkv; do
    BASENAME="${mkv%.mkv}"
    FOLDER_NAME="${BASENAME// /_}"

    echo "→ Processando: $mkv"
    mkdir -p "$FOLDER_NAME"

    # Move o vídeo
    mv -n "$mkv" "$FOLDER_NAME/video.mkv"

    # Move tracks correspondentes (se existirem)
    for track in 1_mix 2_desktop 3_mic; do
        for ext in m4a wav; do
            src="${BASENAME}_track${track}.${ext}"
            if [[ -f "$src" ]]; then
                mv -n "$src" "$FOLDER_NAME/track${track}.${ext}"
            fi
        done
    done

    echo "  ✓ Movido para: $FOLDER_NAME/"
done

echo ""
echo "✓ Migração concluída. Layout atual:"
ls -la