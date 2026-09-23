# Instalação

Guia testado em **Ubuntu 24.04** (sessão Xorg, PipeWire), GPU NVIDIA com 8 GB de VRAM. Em outras
distros os nomes de pacote mudam, mas os passos são os mesmos.

## Dependências

| Dependência | Obrigatória? | Usada por | Pacote (Ubuntu) |
|---|---|---|---|
| OBS Studio ≥ 30 com scripting Python | sim | gravação + `obs_auto_pipeline_split.py` | `obs-studio` (PPA oficial) |
| Python ≥ 3.10 | sim | `lib/*.py`, ganchos, plugin (só stdlib) | `python3` |
| ffmpeg / ffprobe | sim | duração das partes, extração de áudio e de frames | `ffmpeg` |
| curl | sim | `notify_discord.sh` | `curl` |
| flock | sim | serializa transcrições na GPU | `util-linux` |
| Claude Code (`claude`) | sim | momentos-chave, ata, tarefas, classificação | instalador oficial |
| WhisperX | sim | transcrição | venv próprio (abaixo) |
| ImageMagick (`identify`) | recomendado | descarta prints sem conteúdo (tela preta/vazia) | `imagemagick` |
| `pactl` com saída JSON | recomendado | parada da gravação pelo microfone, perfil do fone | `pulseaudio-utils` (≥ 16) |
| `wmctrl`, `xprop` | opcional | `window_fit.py` e reapontamento da captura de janela (X11) | `wmctrl`, `x11-utils` |
| Advanced Scene Switcher | opcional | iniciar a gravação quando a janela da call abre | plugin do OBS |
| Webhook do Discord | opcional | envio da ata e da galeria | — |
| Vault do Obsidian | opcional | publicação das reuniões | — |

## 1. Pacotes do sistema

```bash
sudo add-apt-repository ppa:obsproject/obs-studio
sudo apt update
sudo apt install obs-studio python3 python3-venv ffmpeg curl util-linux \
    imagemagick pulseaudio-utils wmctrl x11-utils
```

Confira se o OBS enxerga o Python: **Ferramentas → Scripts → aba Configurações do Python** deve
mostrar a versão carregada. Se estiver vazio, instale `libpython3-dev` e reabra o OBS.

## 2. Claude Code

```bash
curl -fsSL https://claude.ai/install.sh | bash
claude          # faça login uma vez, de forma interativa
```

O pipeline chama `claude -p` sem interação. Para usar outra conta que não a padrão (`~/.claude`),
defina `CLAUDE_CONFIG_DIR` (ou o campo *Conta Claude* no painel do script).

## 3. WhisperX

Em um venv separado (as dependências de CUDA são pesadas e não devem ir para o Python do sistema):

```bash
python3 -m venv ~/whisperx/.venv
~/whisperx/.venv/bin/pip install whisperx
~/whisperx/.venv/bin/whisperx --help   # confere a instalação
```

Sem GPU NVIDIA, use `WHISPERX_DEVICE=cpu` e `WHISPERX_COMPUTE_TYPE=int8` (bem mais lento).

### 3a. Servidor residente (recomendado)

Carregar o modelo custa ~10 s por chamada; com um servidor que mantém o modelo na memória, cada
parte transcreve em segundos. O pipeline fala com ele em `WHISPERX_SERVER_URL`
(default `http://127.0.0.1:8765`):

- `GET /health` → 200
- `POST /transcribe` com `{"path": "/abs/audio.wav", "language": "pt"}` → `{"segments": [{"start", "end", "text"}]}`

Uma implementação pronta (só stdlib + whisperx, com descarga do modelo por ociosidade) está em
[`claude-telegram-bot-v2/deploy`](https://github.com/correaschneider/claude-telegram-bot-v2/tree/main/deploy):

```bash
git clone https://github.com/correaschneider/claude-telegram-bot-v2.git ~/src/claude-telegram-bot-v2
cp ~/src/claude-telegram-bot-v2/deploy/whisperx-server.service ~/.config/systemd/user/
# ajuste o caminho do ExecStart para ~/src/claude-telegram-bot-v2/deploy/whisperx_server.py
systemctl --user daemon-reload
systemctl --user enable --now whisperx-server
curl -s http://127.0.0.1:8765/health
```

### 3b. Sem servidor (CLI)

Se o servidor não responder, `lib/transcribe.py` chama o executável do WhisperX a cada parte:

```bash
export WHISPERX_BIN=~/whisperx/.venv/bin/whisperx
```

Não rode os dois ao mesmo tempo numa GPU de 8 GB: com o servidor carregado, a CLI dá OOM.

## 4. Este repositório

```bash
git clone https://github.com/correaschneider/obs-transcripter-plugin.git ~/obs-transcripter-plugin
cd ~/obs-transcripter-plugin
cp empresas.example.json empresas.json
chmod +x *.sh
```

Edite `empresas.json` com suas empresas/projetos, palavras-chave e o contexto que o Claude usa para
classificar cada reunião (detalhes no [README](README.md#configuração)).

## 5. OBS

1. **Configurações → Saída → Modo avançado → Gravação**
   - Formato `mkv`, faixas de áudio **1, 2 e 3** marcadas.
   - *Automatically split file* por tempo, **5 min**.
2. **Configurações → Áudio / Mixer avançado**: roteie as fontes para que a faixa 1 seja a mix,
   a 2 só o áudio do desktop e a 3 só o microfone.
3. **Ferramentas → Scripts → +** → `obs_auto_pipeline_split.py`. No painel do script:
   - aponte `process_split.sh`, `finalize_meeting.sh`, `analyze_meeting.sh` e `notify_discord.sh`;
   - (opcional) ganchos `on_call_start.sh` / `on_call_end.sh`;
   - diretório de gravação, conta Claude e webhook do Discord.
4. (Opcional) Instale o [Advanced Scene Switcher](https://github.com/WarmUpTill/SceneSwitcher/releases)
   e crie uma macro que inicia a gravação quando a janela da call aparece. A parada fica com o
   script (microfone ocioso por `mic_idle_seconds`).
5. Em cada cena, prefira **Captura de janela (Xcomposite)** do app de call em vez da tela inteira:
   o índice de monitor do OBS muda entre sessões e pode gravar a tela errada.

Sempre que editar um `.py` do plugin, clique em **↻** em Ferramentas → Scripts.

## 6. Variáveis de ambiente (opcional)

Os scripts funcionam só com o painel do OBS. Para rodar à mão ou ajustar defaults, exporte no shell
(ou em `~/.profile`):

```bash
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
export OBSIDIAN_VAULT="$HOME/Obsidian"
export VAULT_PUBLISH=1          # 0 = não move a reunião para o vault
export MIN_MEETING_SECONDS=60   # gravações menores não geram ata
```

Lista completa no [README](README.md#configuração).

## 7. Teste

```bash
curl -s http://127.0.0.1:8765/health                    # servidor WhisperX
python3 lib/vault.py context empresas                   # config lida
```

Grave 1–2 min no OBS com algum áudio e pare. Em alguns minutos a pasta
`<diretório de gravação>/YYYY-MM-DD_HH-MM/` deve ter `part001.done`, `transcricao.txt`, `ata.md`
e `tarefas.md`. Logs do plugin ficam em `logs/`; erros de uma parte, em `partNNN.failed`.
