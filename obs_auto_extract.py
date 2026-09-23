"""
OBS Script: Pipeline completo após stop recording
  1. extract_obs_audio.sh   (separa as 3 tracks de áudio)
  2. transcribe.sh          (WhisperX → SRTs)
  3. analyze_meeting.sh     (Claude Code → ata.md + tarefas.md)
  4. notify_discord.sh      (Webhook → mensagens com embed + anexo)
"""

import obspython as obs
import subprocess
import os
from datetime import datetime

extract_script = ""
transcribe_script = ""
analyze_script = ""
notify_script = ""
claude_config_dir = ""
discord_webhook_url = ""
output_format = "m4a"
enabled = True


def on_event(event):
    if event == obs.OBS_FRONTEND_EVENT_RECORDING_STOPPED:
        if not enabled:
            return
        handle_recording_stopped()


def handle_recording_stopped():
    last_file = obs.obs_frontend_get_last_recording()
    if not last_file or not os.path.isfile(last_file):
        obs.script_log(obs.LOG_WARNING, f"Arquivo inválido: {last_file}")
        return

    if not extract_script or not os.path.isfile(extract_script):
        obs.script_log(obs.LOG_WARNING, f"Extract script inválido: {extract_script}")
        return

    log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    os.makedirs(log_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = os.path.join(log_dir, f"pipeline_{timestamp}.log")

    env = os.environ.copy()
    home = os.path.expanduser("~")
    local_bin = f"{home}/.local/bin"
    if local_bin not in env.get("PATH", "").split(":"):
        env["PATH"] = f"{local_bin}:{env.get('PATH', '')}"

    if transcribe_script and os.path.isfile(transcribe_script):
        env["TRANSCRIBE_SCRIPT"] = transcribe_script
    if analyze_script and os.path.isfile(analyze_script):
        env["ANALYZE_SCRIPT"] = analyze_script
    if notify_script and os.path.isfile(notify_script):
        env["NOTIFY_SCRIPT"] = notify_script
    if claude_config_dir:
        env["CLAUDE_CONFIG_DIR"] = os.path.expanduser(claude_config_dir)
    if discord_webhook_url:
        env["DISCORD_WEBHOOK_URL"] = discord_webhook_url

    obs.script_log(obs.LOG_INFO, f"Gravação finalizada: {last_file}")
    obs.script_log(obs.LOG_INFO, f"Log do pipeline: {log_file}")

    try:
        log_fp = open(log_file, "w")
        log_fp.write(f"=== Pipeline iniciado em {datetime.now()} ===\n")
        log_fp.write(f"Arquivo: {last_file}\n")
        log_fp.write(f"Extract:    {extract_script}\n")
        log_fp.write(f"Transcribe: {transcribe_script or '(não configurado)'}\n")
        log_fp.write(f"Analyze:    {analyze_script or '(não configurado)'}\n")
        log_fp.write(f"Notify:     {notify_script or '(não configurado)'}\n")
        log_fp.write(f"CLAUDE_CONFIG_DIR: {env.get('CLAUDE_CONFIG_DIR', '(não setado)')}\n")
        log_fp.write(f"DISCORD_WEBHOOK_URL: {'(setado)' if discord_webhook_url else '(não setado)'}\n")
        log_fp.write(f"PATH: {env['PATH']}\n")
        log_fp.write(f"--- output ---\n")
        log_fp.flush()

        subprocess.Popen(
            [extract_script, last_file, output_format],
            stdout=log_fp,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=env,
        )
        obs.script_log(obs.LOG_INFO, "Pipeline iniciado em background.")
    except Exception as e:
        obs.script_log(obs.LOG_ERROR, f"Falha ao executar: {e}")


def script_description():
    return (
        "<b>OBS Auto Pipeline</b><br>"
        "Stop Recording → extrai áudio → transcreve → ata+tarefas → Discord.<br>"
        "Logs em logs/, ao lado deste script."
    )


def script_properties():
    props = obs.obs_properties_create()
    obs.obs_properties_add_bool(props, "enabled", "Habilitado")
    obs.obs_properties_add_path(
        props, "extract_script", "1. Script de extração",
        obs.OBS_PATH_FILE, "Shell scripts (*.sh)", None
    )
    obs.obs_properties_add_path(
        props, "transcribe_script", "2. Script de transcrição (opcional)",
        obs.OBS_PATH_FILE, "Shell scripts (*.sh)", None
    )
    obs.obs_properties_add_path(
        props, "analyze_script", "3. Script de análise (opcional)",
        obs.OBS_PATH_FILE, "Shell scripts (*.sh)", None
    )
    obs.obs_properties_add_path(
        props, "notify_script", "4. Script de notificação (opcional)",
        obs.OBS_PATH_FILE, "Shell scripts (*.sh)", None
    )
    obs.obs_properties_add_text(
        props, "claude_config_dir",
        "Conta Claude (CLAUDE_CONFIG_DIR, ex: ~/.claude-personal)",
        obs.OBS_TEXT_DEFAULT
    )
    obs.obs_properties_add_text(
        props, "discord_webhook_url",
        "Discord Webhook URL",
        obs.OBS_TEXT_PASSWORD
    )
    fmt_list = obs.obs_properties_add_list(
        props, "output_format", "Formato de áudio",
        obs.OBS_COMBO_TYPE_LIST, obs.OBS_COMBO_FORMAT_STRING
    )
    obs.obs_property_list_add_string(fmt_list, "m4a (rápido)", "m4a")
    obs.obs_property_list_add_string(fmt_list, "wav (PCM)", "wav")
    return props


def script_defaults(settings):
    obs.obs_data_set_default_bool(settings, "enabled", True)
    obs.obs_data_set_default_string(settings, "output_format", "m4a")
    obs.obs_data_set_default_string(settings, "claude_config_dir", "~/.claude-personal")


def script_update(settings):
    global extract_script, transcribe_script, analyze_script, notify_script
    global claude_config_dir, discord_webhook_url, output_format, enabled
    enabled = obs.obs_data_get_bool(settings, "enabled")
    extract_script = obs.obs_data_get_string(settings, "extract_script")
    transcribe_script = obs.obs_data_get_string(settings, "transcribe_script")
    analyze_script = obs.obs_data_get_string(settings, "analyze_script")
    notify_script = obs.obs_data_get_string(settings, "notify_script")
    claude_config_dir = obs.obs_data_get_string(settings, "claude_config_dir")
    discord_webhook_url = obs.obs_data_get_string(settings, "discord_webhook_url")
    output_format = obs.obs_data_get_string(settings, "output_format")


def script_load(settings):
    obs.obs_frontend_add_event_callback(on_event)


def script_unload():
    obs.obs_frontend_remove_event_callback(on_event)