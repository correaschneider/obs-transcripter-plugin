# obs-plugins

Grava calls no OBS e entrega, sem intervenção, **transcrição, ata, lista de tarefas e prints dos
momentos-chave**, publicados no Discord e num vault do Obsidian.

- Grava em partes de 5 min e processa cada parte enquanto a call continua: transcreve com WhisperX,
  escolhe os momentos-chave com o Claude, tira os prints e **apaga o vídeo** (o disco não enche).
- Separa quem falou pelas faixas de áudio do OBS: desktop = participantes, microfone = você.
- Para a gravação sozinho quando o app de call solta o microfone.
- No fim, gera `ata.md` + `tarefas.md` com `claude -p`, classifica a reunião por empresa/projeto
  e move tudo para `<vault>/Empresas/<Empresa>/Calls/<pasta>/`, com índice por empresa.

> Feito para uso pessoal em Linux (X11 + PipeWire). Funciona, mas assume o ambiente descrito abaixo;
> adapte caminhos e ganchos ao seu.

## Como funciona

```
OBS grava ──split 5 min──▶ process_split.sh (por parte)
                              transcreve (WhisperX) → momentos (Claude) → prints → apaga vídeo
Gravação para ───────────▶ finalize_meeting.sh
                              junta transcrições → ata + tarefas (Claude) → melhores prints
                              → Discord → vault do Obsidian
```

Cada parte deixa um marcador `partNNN.done` ou `partNNN.failed`. Em falha o vídeo **fica** e dá
para reprocessar rodando `process_split.sh` de novo sobre ele.

## Requisitos

- **OBS Studio** com scripting Python e gravação com 3 faixas de áudio:
  1 = mix, 2 = áudio do desktop, 3 = microfone.
- `python3` (só stdlib), `ffmpeg`/`ffprobe`, `curl`, `flock`.
- [Claude Code](https://claude.com/claude-code) (`claude` no `PATH`, logado).
- **WhisperX**: de preferência um servidor HTTP local (`WHISPERX_SERVER_URL`, default
  `http://127.0.0.1:8765`) com `GET /health` e `POST /transcribe` recebendo
  `{"path": "<wav>", "language": "pt"}` e devolvendo `{"segments": [...]}`. Sem servidor, cai para
  o `whisperx-cli` (`WHISPERX_BIN`).
- Opcionais: ImageMagick `identify` (descarta prints vazios), `wmctrl`/`xprop` (ajuste de janela e
  reapontamento da captura), `pactl` (parada por microfone e perfil do fone Bluetooth).

## Instalação

```bash
git clone https://github.com/correaschneider/obs-plugins.git
cd obs-plugins
cp empresas.example.json empresas.json   # ajuste empresas, palavras-chave e contexto
```

No OBS:

1. **Configurações → Saída → Gravação**: 3 faixas de áudio (mix, desktop, mic) e
   *Automatically split file* a cada 5 min.
2. **Ferramentas → Scripts → +** → `obs_auto_pipeline_split.py`.
3. No painel do script, aponte `process_split.sh`, `finalize_meeting.sh`, `analyze_meeting.sh`,
   `notify_discord.sh` e (opcional) os ganchos `on_call_start.sh`/`on_call_end.sh`; preencha o
   webhook do Discord e o diretório de gravação.
4. Depois de editar qualquer `.py` do plugin, clique em **↻** para recarregar o script.

Iniciar/parar a gravação pelo título da janela do app de call fica a cargo de outra ferramenta
(ex.: plugin *Advanced Scene Switcher*); a parada por microfone ocioso já vem no script.

## Configuração

`empresas.json` (não versionado; formato em [`empresas.example.json`](empresas.example.json)):

| Campo | Uso |
|---|---|
| `dono` | Seu nome, para a lista de participantes |
| `empresas` | Destinos possíveis no vault |
| `fallback` | Empresa usada quando nada bate |
| `keywords` | `regex → peso` por empresa (classificação sem IA) |
| `contexto` | Texto que vai no prompt da ata para o Claude decidir a empresa |

Variáveis de ambiente mais usadas:

| Variável | Default | Para quê |
|---|---|---|
| `DISCORD_WEBHOOK_URL` | — | Sem ela, nada vai ao Discord |
| `CLAUDE_CONFIG_DIR` | `~/.claude` | Conta do Claude usada pelo `claude -p` |
| `WHISPERX_SERVER_URL` | `http://127.0.0.1:8765` | Servidor de transcrição |
| `OBSIDIAN_VAULT` | `/data/projects/Obsidian` | Vault de destino |
| `VAULT_PUBLISH` | `1` | `0` mantém a reunião só na pasta de gravação |
| `DELETE_VIDEO` | `1` | `0` mantém o vídeo de cada parte |
| `MIN_MEETING_SECONDS` | `60` | Abaixo disso não gera ata (gravação acidental) |
| `EMPRESAS_CONFIG` | `./empresas.json` | Caminho alternativo da config |

## Uso manual

```bash
./process_split.sh "/caminho/2026-05-20 12-15 (2).mkv"   # uma parte
./finalize_meeting.sh /caminho/2026-05-20_12-15          # fecha a reunião
./backfill.sh /caminho/gravacoes                          # reprocessa gravações antigas
python3 lib/vault.py index                               # regera os índices do vault
python3 lib/vault.py move <pasta_no_vault> <Empresa>     # corrige a empresa
python3 lib/vault.py cleanup                             # lista o que dá para apagar (--yes apaga)
```

## Estrutura

| Caminho | O que é |
|---|---|
| `obs_auto_pipeline_split.py` | Script do OBS |
| `process_split.sh`, `finalize_meeting.sh` | Orquestração por parte e por reunião |
| `analyze_meeting.sh`, `notify_discord.sh` | Ata/tarefas e envio ao Discord |
| `lib/` | Uma etapa por arquivo: `transcribe`, `moments`, `frames`, `transcript`, `gallery`, `vault` |
| `on_call_start.sh`, `on_call_end.sh`, `audio_profile.py`, `window_fit.py` | Ganchos de início/fim de call |
| `obs_auto_extract.py`, `extract_obs_audio.sh`, `transcribe*.sh`, `organize_existing.sh` | Pipeline antigo, sem split (legado) |
