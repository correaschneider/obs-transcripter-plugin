#!/bin/bash
# Gera Ata e Lista de Tarefas a partir dos SRTs de uma reunião OBS.
# Adapta o prompt conforme disponibilidade das tracks:
#   - Tem track2 + track3 → distingue Lord vs Participantes
#   - Tem só track1_mix → trata como transcrição corrida sem distinção
#
# Uso: ./analyze_meeting.sh <pasta_gravacao>

set -euo pipefail

TARGET_DIR="${1:-}"

if [[ -z "$TARGET_DIR" || ! -d "$TARGET_DIR" ]]; then
    echo "Uso: $0 <pasta_da_gravacao>"
    exit 1
fi

TARGET_DIR=$(realpath "$TARGET_DIR")

SRT_MIX="${TARGET_DIR}/track1_mix.srt"
SRT_DESKTOP="${TARGET_DIR}/track2_desktop.srt"
SRT_MIC="${TARGET_DIR}/track3_mic.srt"
ATA="${TARGET_DIR}/ata.md"
TAREFAS="${TARGET_DIR}/tarefas.md"

# Detecta modo: multi-track vs single-track
MODE=""
if [[ -f "$SRT_DESKTOP" && -f "$SRT_MIC" ]]; then
    MODE="multitrack"
elif [[ -f "$SRT_MIX" ]]; then
    MODE="singletrack"
else
    echo "✗ Nenhum SRT encontrado em $TARGET_DIR"
    echo "  Esperado: track2_desktop.srt + track3_mic.srt OU track1_mix.srt"
    exit 1
fi

if [[ -f "$ATA" && -f "$TAREFAS" ]]; then
    echo "⊘ Ata e tarefas já existem, pulando geração."
    SKIP_ANALYSIS=true
else
    SKIP_ANALYSIS=false
fi

if [[ "$SKIP_ANALYSIS" == "false" ]]; then
    export CLAUDE_CONFIG_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"

    CLAUDE_BIN="${CLAUDE_BIN:-}"
    if [[ -z "$CLAUDE_BIN" ]]; then
        if command -v claude &>/dev/null; then
            CLAUDE_BIN=$(command -v claude)
        else
            for candidate in "$HOME/.local/bin/claude" "$HOME/.npm-global/bin/claude" "/usr/local/bin/claude"; do
                if [[ -x "$candidate" ]]; then
                    CLAUDE_BIN="$candidate"
                    break
                fi
            done
        fi
    fi

    if [[ -z "$CLAUDE_BIN" || ! -x "$CLAUDE_BIN" ]]; then
        echo "✗ claude não encontrado. PATH=$PATH"
        exit 1
    fi

    if [[ ! -d "$CLAUDE_CONFIG_DIR" ]]; then
        echo "✗ CLAUDE_CONFIG_DIR não existe: $CLAUDE_CONFIG_DIR"
        exit 1
    fi

    echo "→ Pasta:    $TARGET_DIR"
    echo "→ Modo:     $MODE"
    echo "→ Claude:   $CLAUDE_BIN"
    echo "→ Conta:    $CLAUDE_CONFIG_DIR"
    echo "▶ Gerando ata e tarefas..."
    echo ""

    # Frontmatter lido pelo lib/vault.py (publicação no Obsidian). data/hora/duração o script preenche.
    # Empresas e contexto vêm do empresas.json (não versionado) via `vault.py context`.
    VAULT_PY="$(dirname "$(realpath "$0")")/lib/vault.py"
    DONO=$(python3 "$VAULT_PY" context dono)
    EMPRESAS=$(python3 "$VAULT_PY" context empresas)
    CONTEXTO=$(python3 "$VAULT_PY" context contexto | sed 's/^/     /')
    FRONTMATTER=$(cat <<EOF2
   A ata DEVE começar com este frontmatter YAML (preencha os valores; nada antes do primeiro \`---\`):
   \`\`\`
   ---
   empresa: ${EMPRESAS}
   titulo: 3 a 8 palavras dizendo do que a reunião tratou (sem data, sem "Ata")
   participantes: [Nome, Nome]
   tags: [daily, suporte]
   ---
   \`\`\`
   - empresa (escolha uma):
${CONTEXTO}
   - participantes: só nomes próprios de quem falou (inclua "${DONO}" se ele falou), sem
     placeholder ("Participante remoto", "Lord", "Condutor"); lista vazia se não houver nome.
   - tags: 1 a 4, minúsculas, sem acento, com hífen.
EOF2
)

    # Constrói o prompt conforme o modo
    if [[ "$MODE" == "multitrack" ]]; then
        PROMPT=$(cat <<EOF
Você vai analisar duas transcrições (formato SRT, com timestamps) de uma reunião gravada via OBS:

1. **${SRT_DESKTOP}** — áudio que veio do computador (participantes remotos)
2. **${SRT_MIC}** — áudio do microfone (o usuário, Lord)

Os timestamps das duas faixas são sincronizados (gravadas simultaneamente).

**Suas tarefas:**

1. Leia os dois arquivos SRT acima.
2. Mescle as falas mentalmente em ordem cronológica usando os timestamps.
3. Identifique no diálogo: contexto, temas discutidos, decisões tomadas, action items.
4. Quando se referir a quem falou:
   - O conteúdo de **track3_mic** é sempre o **Lord** (primeira pessoa).
   - O conteúdo de **track2_desktop** são os **participantes** (chame de "Participante" ou pelo nome se aparecer claramente nas falas).

5. Crie dois arquivos markdown na pasta ${TARGET_DIR}:

   **a) ${ATA}** — Ata estruturada da reunião:
${FRONTMATTER}
   - Cabeçalho com data/hora (extraia do nome da pasta se possível)
   - Participantes identificados
   - Resumo executivo (2-3 parágrafos)
   - Temas discutidos (seções por assunto)
   - Decisões tomadas
   - Pontos em aberto / a definir

   **b) ${TAREFAS}** — Lista de action items:
   - Cada tarefa com: descrição clara, responsável (se mencionado), prazo (se mencionado), prioridade inferida (alta/média/baixa)
   - Formato de checklist markdown (- [ ] tarefa)
   - Agrupe por responsável quando possível

**Idioma:** Português brasileiro. **Tom:** Profissional, direto, sem floreios.

Use Read para ler os SRTs e Write para criar os MDs. Execute sem pedir confirmações.
EOF
)
    else
        # singletrack
        PROMPT=$(cat <<EOF
Você vai analisar uma transcrição (formato SRT, com timestamps) de uma reunião gravada via OBS:

1. **${SRT_MIX}** — áudio mixado da reunião (todos os participantes na mesma faixa, sem distinção de quem falou)

**Suas tarefas:**

1. Leia o arquivo SRT acima.
2. Identifique no diálogo: contexto, temas discutidos, decisões tomadas, action items.
3. Como não há distinção de speaker, infira pelo contexto quem pode ter dito o quê (mas sem afirmar com certeza nomes se não estiverem claros). Use linguagem como "foi mencionado", "foi decidido", "alguém comentou".
4. Se aparecerem nomes próprios nas falas (apresentações, vocativos), use-os para identificar participantes.

5. Crie dois arquivos markdown na pasta ${TARGET_DIR}:

   **a) ${ATA}** — Ata estruturada da reunião:
${FRONTMATTER}
   - Cabeçalho com data/hora (extraia do nome da pasta se possível)
   - Participantes identificados (se for possível inferir dos nomes citados)
   - Resumo executivo (2-3 parágrafos)
   - Temas discutidos (seções por assunto)
   - Decisões tomadas
   - Pontos em aberto / a definir

   **b) ${TAREFAS}** — Lista de action items:
   - Cada tarefa com: descrição clara, responsável (se mencionado), prazo (se mencionado), prioridade inferida (alta/média/baixa)
   - Formato de checklist markdown (- [ ] tarefa)
   - Agrupe por responsável quando possível; tarefas sem responsável claro vão num grupo "A definir"

**Idioma:** Português brasileiro. **Tom:** Profissional, direto, sem floreios.

Use Read para ler o SRT e Write para criar os MDs. Execute sem pedir confirmações.
EOF
)
    fi

    "$CLAUDE_BIN" \
        -p "$PROMPT" \
        --dangerously-skip-permissions \
        --output-format text

    echo ""
    echo "✓ Análise concluída."
fi

# Encadeia notificação Discord se configurado
if [[ -n "${NOTIFY_SCRIPT:-}" && -x "$NOTIFY_SCRIPT" ]]; then
    if [[ -z "${DISCORD_WEBHOOK_URL:-}" ]]; then
        echo "⚠ NOTIFY_SCRIPT setado mas DISCORD_WEBHOOK_URL ausente — pulando."
    else
        echo ""
        echo "→ Enviando notificações Discord..."
        "$NOTIFY_SCRIPT" "$TARGET_DIR"
    fi
fi

echo ""
echo "✓ Conteúdo final da pasta:"
ls -lh "$ATA" "$TAREFAS" 2>/dev/null || true