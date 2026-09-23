"""
OBS Script: Pipeline com suporte a Automatic File Splitting (polling-based).

Detecta novos splits via varredura do diretório de gravação a cada 2s.
OBS 32.x não expõe evento de file rotation, então o polling olha pro
filesystem direto.

O nome da pasta da reunião é fixado no PRIMEIRO arquivo da sessão e
mantido até RECORDING_STOPPED — splits que cruzem hora não fragmentam
a reunião (parser de nome iria pra outra pasta).

Eventos:
  RECORDING_STARTED → inicia sessão + timer de polling
  RECORDING_STOPPED → encerra timer, processa último split + finalize

Scripts encadeados (configurados na UI):
  1. process_split.sh     → transcreve direto do vídeo + momentos + prints; apaga o vídeo
  2. finalize_meeting.sh  → concat transcrições + ata/tarefas + melhores prints + Discord
  (analyze_meeting.sh / notify_discord.sh / lib/*.py são chamados por eles)

Recomendado: Settings → Output → Recording → "Automatically split file" a cada 5 min.
"""

import obspython as obs
import subprocess
import os
import re
import json
from datetime import datetime

# === Configurações (UI) ===
process_split_script = ""
call_start_script = ""
call_end_script = ""
finalize_script = ""
analyze_script = ""
notify_script = ""
claude_config_dir = ""
discord_webhook_url = ""
output_format = "m4a"
recording_dir_override = ""
delete_video = True
auto_stop_mic = True
mic_idle_seconds = 10
enabled = True

# === Estado da sessão ===
session_active = False
meeting_folder = None       # ex: "Acme_-_2026-05-27_14" — fixa pela sessão
current_file = None         # arquivo sendo gravado agora
processed_files = set()     # paths já despachados pra process_split
part_counter = 0
recording_dir = None
session_start_ts = 0.0
mic_armed = False           # já vimos a call usando o microfone nesta gravação?
mic_last_seen_ts = 0.0      # última vez que algum app (fora o OBS) capturava o mic
MIC_POLL_MS = 2000
WINDOW_POLL_MS = 5000
retarget_windows = True

POLL_INTERVAL_MS = 2000
VIDEO_EXTS = (".mkv", ".mp4", ".mov", ".webm")
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")


def parse_meeting_folder(video_path):
    """Deriva o folder da reunião do nome do PRIMEIRO arquivo da sessão.
    Strip de sufixo ' (N)' e espaços → underscore.
    """
    base = os.path.splitext(os.path.basename(video_path))[0]
    m = re.match(r"^(.+)\s+\(\d+\)$", base)
    if m:
        base = m.group(1)
    return base.replace(" ", "_")


def unique_meeting_folder(folder):
    """Se a pasta já existe (outra gravação no mesmo minuto), sufixa _2, _3…
    Resolvido aqui, uma vez por sessão, pra todas as partes irem pra mesma pasta."""
    if not recording_dir or not os.path.exists(os.path.join(recording_dir, folder)):
        return folder
    n = 2
    while os.path.exists(os.path.join(recording_dir, f"{folder}_{n}")):
        n += 1
    return f"{folder}_{n}"


def get_recording_dir():
    """Resolve o diretório de gravação.
    Ordem: override manual da UI > config OBS (mode-aware) > None.
    """
    if recording_dir_override and os.path.isdir(os.path.expanduser(recording_dir_override)):
        return os.path.expanduser(recording_dir_override)
    config = obs.obs_frontend_get_profile_config()
    if not config:
        return None
    mode = obs.config_get_string(config, "Output", "Mode") or "Simple"
    if mode == "Advanced":
        return obs.config_get_string(config, "AdvOut", "RecFilePath")
    return obs.config_get_string(config, "SimpleOutput", "FilePath")


def find_newest_video(folder, since_ts):
    """Vídeo mais recente em `folder` com mtime >= since_ts."""
    if not folder or not os.path.isdir(folder):
        return None
    newest = None
    newest_mtime = -1.0
    for entry in os.scandir(folder):
        if not entry.is_file():
            continue
        if not entry.name.lower().endswith(VIDEO_EXTS):
            continue
        st = entry.stat()
        if st.st_mtime >= since_ts and st.st_mtime > newest_mtime:
            newest_mtime = st.st_mtime
            newest = entry.path
    return newest


def build_env():
    env = os.environ.copy()
    home = os.path.expanduser("~")
    local_bin = f"{home}/.local/bin"
    if local_bin not in env.get("PATH", "").split(":"):
        env["PATH"] = f"{local_bin}:{env.get('PATH', '')}"
    if analyze_script and os.path.isfile(analyze_script):
        env["ANALYZE_SCRIPT"] = analyze_script
    if notify_script and os.path.isfile(notify_script):
        env["NOTIFY_SCRIPT"] = notify_script
    if claude_config_dir:
        env["CLAUDE_CONFIG_DIR"] = os.path.expanduser(claude_config_dir)
    if discord_webhook_url:
        env["DISCORD_WEBHOOK_URL"] = discord_webhook_url
    env["AUDIO_FORMAT"] = output_format
    env["DELETE_VIDEO"] = "1" if delete_video else "0"
    return env


def get_log_file(prefix):
    os.makedirs(LOG_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return os.path.join(LOG_DIR, f"{prefix}_{ts}.log")


def dispatch_process_split(video_path, part_num, is_final):
    """Dispara process_split.sh em background.
    Se is_final=True, encadeia finalize_meeting.sh após process_split.
    Passa MEETING_FOLDER e PART_NUMBER via env (override do parser interno).
    """
    env = build_env()
    env["MEETING_FOLDER"] = meeting_folder or ""
    env["PART_NUMBER"] = f"{part_num:03d}"

    log_prefix = f"{'final' if is_final else 'split'}_{meeting_folder}_part{part_num:03d}"
    log_file = get_log_file(log_prefix)

    try:
        log_fp = open(log_file, "w")
        header = "FINALIZE" if is_final else "SPLIT"
        log_fp.write(f"=== {header} part{part_num:03d} em {datetime.now()} ===\n")
        log_fp.write(f"Arquivo:  {video_path}\n")
        log_fp.write(f"Reunião:  {meeting_folder}\n")
        log_fp.write(f"Part:     {part_num:03d}\n")
        log_fp.write(f"PATH:     {env['PATH']}\n")
        log_fp.write("--- output ---\n")
        log_fp.flush()

        if is_final and finalize_script and os.path.isfile(finalize_script):
            meeting_dir = os.path.join(recording_dir, meeting_folder)
            # `;` e não `&&`: a última parte falhar não pode impedir ata/Discord das outras
            cmd = f'"{process_split_script}" "{video_path}"; "{finalize_script}" "{meeting_dir}"'
            subprocess.Popen(
                ["bash", "-c", cmd],
                stdout=log_fp, stderr=subprocess.STDOUT,
                start_new_session=True, env=env,
            )
        else:
            subprocess.Popen(
                [process_split_script, video_path],
                stdout=log_fp, stderr=subprocess.STDOUT,
                start_new_session=True, env=env,
            )

        suffix = " + finalize" if is_final else ""
        obs.script_log(
            obs.LOG_INFO,
            f"Dispatched part{part_num:03d}{suffix} → {os.path.basename(log_file)}",
        )
    except Exception as e:
        obs.script_log(obs.LOG_ERROR, f"Falha ao despachar: {e}")


def check_for_new_split():
    """Timer callback (2s). Detecta rotação de arquivo via filesystem."""
    global meeting_folder, current_file, part_counter

    if not session_active:
        return

    newest = find_newest_video(recording_dir, session_start_ts)
    if not newest:
        return

    # Primeira detecção da sessão: fixa meeting_folder a partir do nome
    if meeting_folder is None:
        meeting_folder = unique_meeting_folder(parse_meeting_folder(newest))
        current_file = newest
        obs.script_log(
            obs.LOG_INFO,
            f"Sessão fixada: folder={meeting_folder} primeiro={os.path.basename(newest)}",
        )
        return

    # Rotação? newest difere de current_file e current_file ainda não foi processado
    if newest != current_file and current_file and current_file not in processed_files:
        part_counter += 1
        processed_files.add(current_file)
        obs.script_log(
            obs.LOG_INFO,
            f"Split fechado: {os.path.basename(current_file)} → part{part_counter:03d}",
        )
        dispatch_process_split(current_file, part_counter, is_final=False)

    if newest != current_file:
        current_file = newest


def live_window(cls):
    """(id, título) da janela viva com essa classe X11, ou None."""
    try:
        out = subprocess.run(["wmctrl", "-lx"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return None
    for line in out.splitlines():
        parts = line.split(None, 4)
        if len(parts) >= 5 and parts[2].split(".")[0] == cls:
            # O OBS grava o id em DECIMAL; o wmctrl devolve em hexadecimal (0x...)
            return str(int(parts[0], 16)), parts[4].strip()
    return None


def retarget_window_captures():
    """Reaponta as fontes Window Capture (Xcomposite) pra janela VIVA da mesma classe.

    O OBS guarda "<id>\r\n<título>\r\n<classe>". O app de call fecha/reabre com id novo e
    muda o título ao entrar na reunião ("Google Meet" → "Google Meet - Meet: ..."), então a
    fonte pode não reencontrar a janela e a cena cai pro monitor de baixo. Reescrever o
    ajuste também força o OBS a recriar a imagem quando ela congela.
    """
    if not retarget_windows:
        return
    sources = obs.obs_enum_sources()
    try:
        for src in sources or []:
            if obs.obs_source_get_unversioned_id(src) != "xcomposite_input":
                continue
            settings = obs.obs_source_get_settings(src)
            try:
                current = obs.obs_data_get_string(settings, "capture_window") or ""
                parts = current.split("\r\n")
                if len(parts) != 3:
                    continue
                cls = parts[2]
                found = live_window(cls)
                if not found or found[0] == parts[0]:
                    continue
                wid, title = found
                new = f"{wid}\r\n{title}\r\n{cls}"
                obs.obs_data_set_string(settings, "capture_window", new)
                obs.obs_source_update(src, settings)
                obs.script_log(obs.LOG_INFO,
                               f"Captura '{obs.obs_source_get_name(src)}' reapontada: "
                               f"{parts[0]} → {wid} ({title[:40]})")
            finally:
                obs.obs_data_release(settings)
    finally:
        obs.source_list_release(sources)


def call_capture_apps(source_outputs, sources):
    """Apps (fora o OBS) capturando um microfone de verdade (não monitor de saída).
    Durante uma call o navegador/app segura o microfone; ao sair da call ele solta —
    diferente do título da janela, isso não muda ao trocar de aba."""
    names = {s.get("index"): s.get("name", "") for s in sources}
    apps = []
    for out in source_outputs:
        props = out.get("properties", {})
        binary = (props.get("application.process.binary") or "").lower()
        app = props.get("application.name") or binary or "?"
        if binary == "obs" or app == "OBS":
            continue
        if names.get(out.get("source"), "").endswith(".monitor"):
            continue
        apps.append(app)
    return apps


def list_call_capture_apps():
    try:
        outs = json.loads(subprocess.run(["pactl", "-f", "json", "list", "source-outputs"],
                                         capture_output=True, text=True, timeout=3).stdout or "[]")
        srcs = json.loads(subprocess.run(["pactl", "-f", "json", "list", "sources", "short"],
                                         capture_output=True, text=True, timeout=3).stdout or "[]")
    except Exception as e:
        obs.script_log(obs.LOG_WARNING, f"Checagem do microfone falhou: {e}")
        return None  # desconhecido: não conta como ocioso
    return call_capture_apps(outs, srcs)


def check_mic_idle():
    """Timer (2s). Para a gravação quando a call solta o microfone por mic_idle_seconds."""
    global mic_armed, mic_last_seen_ts
    if not obs.obs_frontend_recording_active():
        return
    apps = list_call_capture_apps()
    now = datetime.now().timestamp()
    if apps is None:
        return
    if apps:
        if not mic_armed:
            obs.script_log(obs.LOG_INFO, f"Call detectada pelo microfone ({', '.join(sorted(set(apps)))}) — parada automática armada")
        mic_armed = True
        mic_last_seen_ts = now
        return
    if mic_armed and now - mic_last_seen_ts >= mic_idle_seconds:
        obs.script_log(obs.LOG_INFO, f"Nenhum app usando o microfone há {mic_idle_seconds}s — parando a gravação")
        mic_armed = False
        obs.obs_frontend_recording_stop()


def run_hook(script, label):
    """Gancho de início/fim de call (ex.: trocar perfil do fone). Não bloqueia o OBS."""
    if not script or not os.path.isfile(script):
        return
    try:
        log_fp = open(get_log_file(f"hook_{label}"), "w")
        subprocess.Popen([script], stdout=log_fp, stderr=subprocess.STDOUT,
                         start_new_session=True, env=build_env())
        obs.script_log(obs.LOG_INFO, f"Gancho {label}: {os.path.basename(script)}")
    except Exception as e:
        obs.script_log(obs.LOG_ERROR, f"Falha no gancho {label}: {e}")


def on_recording_started():
    global session_active, meeting_folder, current_file, processed_files
    global part_counter, recording_dir, session_start_ts

    # Ganchos e parada automática rodam mesmo com o pipeline desabilitado
    run_hook(call_start_script, "inicio")
    global mic_armed, mic_last_seen_ts
    mic_armed, mic_last_seen_ts = False, 0.0
    if auto_stop_mic:
        obs.timer_remove(check_mic_idle)
        obs.timer_add(check_mic_idle, MIC_POLL_MS)
    retarget_window_captures()
    obs.timer_remove(retarget_window_captures)
    obs.timer_add(retarget_window_captures, WINDOW_POLL_MS)
    if not enabled:
        return
    if not process_split_script or not os.path.isfile(process_split_script):
        obs.script_log(obs.LOG_WARNING, f"process_split_script inválido: {process_split_script}")
        return

    recording_dir = get_recording_dir()
    if not recording_dir:
        obs.script_log(obs.LOG_WARNING, "Não consegui ler recording_dir da config OBS")
        return

    session_active = True
    meeting_folder = None
    current_file = None
    processed_files = set()
    part_counter = 0
    session_start_ts = datetime.now().timestamp()

    obs.script_log(obs.LOG_INFO, f"Sessão iniciada — polling {recording_dir} a cada 2s")
    obs.timer_add(check_for_new_split, POLL_INTERVAL_MS)


def on_recording_stopped():
    global session_active, part_counter, current_file

    obs.timer_remove(check_mic_idle)
    obs.timer_remove(retarget_window_captures)
    run_hook(call_end_script, "fim")
    if not session_active:
        return

    obs.timer_remove(check_for_new_split)

    # Recovery: se o timer nunca rodou (gravação muito curta), tenta achar arquivo agora
    if current_file is None or meeting_folder is None:
        newest = find_newest_video(recording_dir, session_start_ts)
        if newest:
            if meeting_folder is None:
                # Não temos chance de processar — sem folder, nada a fazer
                # (parse aqui pra emergência)
                from_path = parse_meeting_folder(newest)
                globals()["meeting_folder"] = unique_meeting_folder(from_path)
            current_file = newest

    # Processa o último arquivo conhecido + finalize encadeado
    if current_file and current_file not in processed_files:
        part_counter += 1
        processed_files.add(current_file)
        obs.script_log(
            obs.LOG_INFO,
            f"Último split: {os.path.basename(current_file)} → part{part_counter:03d} (+finalize)",
        )
        dispatch_process_split(current_file, part_counter, is_final=True)
    else:
        obs.script_log(obs.LOG_WARNING, "Stop sem arquivo a processar (sessão vazia?)")

    session_active = False


def on_event(event):
    if event == obs.OBS_FRONTEND_EVENT_RECORDING_STARTED:
        on_recording_started()
    elif event == obs.OBS_FRONTEND_EVENT_RECORDING_STOPPED:
        on_recording_stopped()


# === UI ===

def script_description():
    return (
        "<b>OBS Auto Pipeline (Split Recording, polling-based)</b><br>"
        "Output → Recording → <b>Automatically split file</b>: por tempo, 5 min.<br>"
        "Filename Formatting: <code>%CCYY-%MM-%DD_%hh-%mm</code> · Overwrite if file exists: <b>desligado</b>.<br>"
        "Cada parte: transcrição direto do vídeo + prints dos momentos-chave; depois o vídeo é apagado.<br>"
        f"Logs em <code>{LOG_DIR}</code>"
    )


def script_properties():
    props = obs.obs_properties_create()
    obs.obs_properties_add_bool(props, "enabled", "Habilitado")
    obs.obs_properties_add_bool(props, "delete_video", "Apagar vídeo de cada parte após processar")
    obs.obs_properties_add_bool(props, "retarget_windows",
                                "Reapontar captura de janela (Teams/Meet) durante a gravação")
    obs.obs_properties_add_path(
        props, "process_split_script", "1. process_split.sh",
        obs.OBS_PATH_FILE, "Shell scripts (*.sh)", None,
    )
    obs.obs_properties_add_path(
        props, "finalize_script", "2. finalize_meeting.sh",
        obs.OBS_PATH_FILE, "Shell scripts (*.sh)", None,
    )
    obs.obs_properties_add_path(
        props, "analyze_script", "3. analyze_meeting.sh",
        obs.OBS_PATH_FILE, "Shell scripts (*.sh)", None,
    )
    obs.obs_properties_add_path(
        props, "notify_script", "4. notify_discord.sh",
        obs.OBS_PATH_FILE, "Shell scripts (*.sh)", None,
    )
    obs.obs_properties_add_bool(
        props, "auto_stop_mic", "Parar gravação quando a call soltar o microfone",
    )
    obs.obs_properties_add_int(
        props, "mic_idle_seconds", "  … depois de quantos segundos sem microfone", 5, 600, 5,
    )
    obs.obs_properties_add_path(
        props, "call_start_script", "Gancho: início da call (on_call_start.sh)",
        obs.OBS_PATH_FILE, "Shell scripts (*.sh)", None,
    )
    obs.obs_properties_add_path(
        props, "call_end_script", "Gancho: fim da call (on_call_end.sh)",
        obs.OBS_PATH_FILE, "Shell scripts (*.sh)", None,
    )
    obs.obs_properties_add_text(
        props, "recording_dir_override",
        "Diretório de gravação (override, ex: /data/shared/calls)",
        obs.OBS_TEXT_DEFAULT,
    )
    obs.obs_properties_add_text(
        props, "claude_config_dir",
        "Conta Claude (CLAUDE_CONFIG_DIR)", obs.OBS_TEXT_DEFAULT,
    )
    obs.obs_properties_add_text(
        props, "discord_webhook_url",
        "Discord Webhook URL", obs.OBS_TEXT_PASSWORD,
    )
    fmt_list = obs.obs_properties_add_list(
        props, "output_format", "Formato de áudio",
        obs.OBS_COMBO_TYPE_LIST, obs.OBS_COMBO_FORMAT_STRING,
    )
    obs.obs_property_list_add_string(fmt_list, "m4a (rápido)", "m4a")
    obs.obs_property_list_add_string(fmt_list, "wav (PCM)", "wav")
    return props


def script_defaults(settings):
    obs.obs_data_set_default_bool(settings, "enabled", True)
    obs.obs_data_set_default_bool(settings, "delete_video", True)
    obs.obs_data_set_default_bool(settings, "auto_stop_mic", True)
    obs.obs_data_set_default_bool(settings, "retarget_windows", True)
    obs.obs_data_set_default_int(settings, "mic_idle_seconds", 10)
    obs.obs_data_set_default_string(settings, "output_format", "m4a")
    obs.obs_data_set_default_string(settings, "claude_config_dir", "~/.claude")


def script_update(settings):
    global process_split_script, finalize_script, delete_video
    global call_start_script, call_end_script, auto_stop_mic, mic_idle_seconds
    global retarget_windows
    global analyze_script, notify_script
    global claude_config_dir, discord_webhook_url, output_format
    global recording_dir_override, enabled
    enabled = obs.obs_data_get_bool(settings, "enabled")
    delete_video = obs.obs_data_get_bool(settings, "delete_video")
    auto_stop_mic = obs.obs_data_get_bool(settings, "auto_stop_mic")
    retarget_windows = obs.obs_data_get_bool(settings, "retarget_windows")
    mic_idle_seconds = obs.obs_data_get_int(settings, "mic_idle_seconds") or 10
    process_split_script = obs.obs_data_get_string(settings, "process_split_script")
    call_start_script = obs.obs_data_get_string(settings, "call_start_script")
    call_end_script = obs.obs_data_get_string(settings, "call_end_script")
    finalize_script = obs.obs_data_get_string(settings, "finalize_script")
    analyze_script = obs.obs_data_get_string(settings, "analyze_script")
    notify_script = obs.obs_data_get_string(settings, "notify_script")
    claude_config_dir = obs.obs_data_get_string(settings, "claude_config_dir")
    discord_webhook_url = obs.obs_data_get_string(settings, "discord_webhook_url")
    output_format = obs.obs_data_get_string(settings, "output_format")
    recording_dir_override = obs.obs_data_get_string(settings, "recording_dir_override")


def script_load(settings):
    obs.obs_frontend_add_event_callback(on_event)


def script_unload():
    global session_active
    if session_active:
        try:
            obs.timer_remove(check_for_new_split)
        except Exception:
            pass
        session_active = False
    for cb in (check_mic_idle, retarget_window_captures):
        try:
            obs.timer_remove(cb)
        except Exception:
            pass
    obs.obs_frontend_remove_event_callback(on_event)
