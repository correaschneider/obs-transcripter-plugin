# obs-transcripter-plugin

Pipeline de gravação de calls no OBS: grava em partes de 5 min, transcreve cada parte (WhisperX),
tira prints dos momentos-chave e apaga o vídeo. Quando a gravação termina, junta as transcrições,
gera ata + tarefas via `claude -p`, manda tudo ao Discord e publica a reunião no vault do Obsidian.

> ⚠️ **Repo público** (espelho no GitHub). Nada de nome de colega, cliente, empresa, webhook ou token
> no código: contexto de classificação vai em `empresas.json` (gitignored), segredo vai por env.

## Fluxo

```
OBS (obs_auto_pipeline_split.py, plugin Python carregado no OBS)
 ├─ início da gravação → on_call_start.sh  (audio_profile.py call + window_fit.py)
 ├─ a cada split       → process_split.sh <parte>
 │     lib/live_transcript.py (ou, sem sessão ao vivo, lib/transcribe.py)
 │       → lib/moments.py select → lib/frames.py → apaga vídeo → partNNN.done
 ├─ fim da gravação    → on_call_end.sh    (audio_profile.py normal)
 └─ depois do último   → finalize_meeting.sh <pasta_reuniao>
       lib/transcript.py concat/merge → analyze_meeting.sh (ata.md + tarefas.md)
       → lib/moments.py pick → lib/vault.py enrich/gallery → notify_discord.sh (+ lib/gallery.py)
       → lib/vault.py publish --move + index
```

Pasta da reunião = `<base>/YYYY-MM-DD_HH-MM[_N]/`. Faixas do OBS: 1 = mix, 2 = desktop
(participantes), 3 = mic (dono da gravação). Com ≥3 faixas só 2 e 3 são transcritas.

## Mapa de arquivos

| Arquivo | Papel |
|---|---|
| `obs_auto_pipeline_split.py` | Plugin OBS (ativo): dispara as partes, parada por microfone ocioso, reaponta capturas de janela |
| `process_split.sh` | Processa UMA parte; idempotente, reprocessável sobre `partNNN.<ext>` |
| `finalize_meeting.sh` | Fecha a reunião (passos 1–8 no cabeçalho do script) |
| `analyze_meeting.sh` | Prompt da ata/tarefas; frontmatter lido pelo `lib/vault.py` |
| `notify_discord.sh` | Ata/tarefas ao Discord (embed + anexo) |
| `backfill.sh` | Reprocessa gravações antigas em lote |
| `lib/*.py` | Uma etapa por arquivo, **só stdlib** (feitos para virar pacotes reaproveitáveis) |
| `audio_profile.py` | Fone Bluetooth: A2DP ↔ HFP (mSBC) e mic padrão (o OBS captura `default`) |
| `window_fit.py` | Tira do maximizado a janela do app de call (maximizada, a captura Xcomposite some) |
| `window_title_logger.sh` | Diagnóstico das macros do Advanced Scene Switcher |
| `obs_auto_extract.py`, `extract_obs_audio.sh`, `transcribe.sh`, `transcribe_split.sh`, `organize_existing.sh` | **Legado** (pipeline antigo sem split). `backfill.sh` ainda usa `extract_obs_audio.sh` |

## Configuração

- `empresas.json` (copiar de `empresas.example.json`): dono, empresas, `fallback`, `keywords`
  (regex → peso) e `contexto` do prompt. Lido por `lib/vault.py`; o shell pega pelo
  `python3 lib/vault.py context <dono|empresas|contexto>`. Caminho alternativo: `EMPRESAS_CONFIG`.
- Env principais: `DISCORD_WEBHOOK_URL`, `CLAUDE_CONFIG_DIR`, `WHISPERX_SERVER_URL`
  (default `http://127.0.0.1:8765`), `OBSIDIAN_VAULT`, `VAULT_PUBLISH`, `DELETE_VIDEO`,
  `MIN_MEETING_SECONDS` (60), `MOMENTS_PER_PART`, `MOMENTS_MAX`. No plugin, os campos do painel
  do script viram essas envs.

## Regras

- **Nunca perder vídeo em falha:** etapa falhou → `partNNN.failed` e o vídeo fica. Remoção só por
  caminho exato, nunca por glob. `vault.py cleanup` é dry-run sem `--yes`.
- **Testar em cópia** de uma pasta de reunião, não na original.
- **Transcrição: primeiro a ao vivo** (`lib/live_transcript.py`): se o copiloto de call
  (`COPILOT_TRANSCRIPTS`, um JSONL `{who,start,end,text}` por sessão, nascido junto com a gravação)
  cobriu a parte inteira, as faixas 2/3 saem dele, sem rodar Whisper — durante a call só um modelo
  usa a GPU. Cobertura = captura viva ou marca `# fim <epoch>` no `.log` da sessão depois do fim da
  parte; senão exit 3 e cai no WhisperX. Sincronia medida: ~0,2 s contra o WhisperX.
- **WhisperX (reserva) via servidor residente** (`:8765`); a CLI do whisperx só se o servidor estiver
  fora (com os dois carregados a GPU dá OOM).
- `copilot.md` (relatório do copiloto) entra no `publish` quando estiver na pasta da reunião.
- Editou o plugin `.py`? O OBS só pega depois de **↻ recarregar o script**; conferir
  `Loaded python script` no log com horário posterior à edição. Não editar `basic.ini`/cenas com o
  OBS aberto (ele sobrescreve ao fechar).
- Shell: `set -euo pipefail`; validar com `bash -n` e `python3 -m py_compile`.
- Commits em Conventional Commits, corpo em PT-BR. Push nos dois remotes:
  `git push origin main && git push github main` (`origin` = GitLab privado, `github` = público).

## Pegadinhas conhecidas

- OBS guarda o id da janela em **decimal**; `wmctrl -lx` devolve em **hex**.
- A captura de janela não reata sozinha quando o app reabre (id novo, título muda): o plugin reaponta
  pela **classe** a cada 5 s.
- Call em **aba do navegador** (Teams no Brave) tem a classe de todas as janelas do Brave: a classe não
  basta, o plugin escolhe pelo título da aba ativa (`CALL_TITLE_RE`). Sem isso a cena do Teams gravava
  a 1ª janela do Brave (WhatsApp/Spotify) de 02/10 a 14/10.
- Índice de tela do OBS (`xshm_input_v2`) muda entre sessões do X → cenas usam só captura de janela.
- `wmctrl -b` aceita no máximo **2** propriedades; o excedente é ignorado em silêncio.
- Whisper alucina "Obrigado." em silêncio na faixa do mic → filtro `HALLUCINATIONS` em
  `lib/transcribe.py`.
- Segments do servidor WhisperX vêm sem alinhamento fino (~30 s).
- **Outro processo na GPU** (ex.: transcrição ao vivo de call) faz o servidor responder HTTP 500
  `CUDA failed with error out of memory` e a parte vira `.failed`. O servidor não tenta de novo, e a
  finalização roda mesmo assim: gera ata parcial, manda ao Discord e **não publica no vault**
  (`FAILED` não vazio). Recuperar: `DELETE_VIDEO=0 ./process_split.sh <pasta>/partNNN.mkv` por parte →
  renomear `ata.md`/`tarefas.md` (o `analyze_meeting.sh` pula se os dois existem) →
  `finalize_meeting.sh` de novo (`NOTIFY_SCRIPT=/bin/true VAULT_PUBLISH=0` pra revisar antes de reenviar).
