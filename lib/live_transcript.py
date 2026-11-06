"""Transcrição de UMA parte a partir da transcrição AO VIVO do call-copilot (sem rodar Whisper).

O copiloto de call (outro projeto) transcreve a call em tempo real, com quem fala separado
("eles" = som do sistema, "eu" = microfone), num JSONL por sessão:
  <COPILOT_TRANSCRIPTS>/AAAA-MM-DD_HHMMSS.jsonl   {"who","start","end","text"} (epoch)
A sessão nasce junto com a gravação (a macro do OBS dispara os dois), então o nome dá o início da
gravação com precisão de segundo. Esta etapa recorta a janela da parte e grava os mesmos arquivos do
transcribe.py: track2_desktop_partNNN (eles) e track3_mic_partNNN (eu), .json + .srt, com o tempo
relativo ao início da parte. Assim só UM modelo de voz roda na GPU durante a call.

Só usa a sessão se ela COBRIU a parte inteira (processo de captura vivo, ou fim registrado depois do
fim da parte). Senão sai com 3 e o process_split.sh cai no transcribe.py (WhisperX) como reserva.

Uso:  python3 live_transcript.py <pasta_reuniao> --part 001
Env:  COPILOT_TRANSCRIPTS (default /data/projects/call-copilot/transcripts; vazio desliga)
      LIVE_TAIL_WAIT (default 20 s: espera a fala que atravessa o fim da parte ser escrita)
Exit: 0 = partes gravadas, 3 = sem sessão ao vivo que cubra a parte (usar o WhisperX), 1 = erro.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import part_durations, save_json  # noqa: E402
from transcribe import segments_to_srt  # noqa: E402

SESSIONS = os.environ.get("COPILOT_TRANSCRIPTS", "/data/projects/call-copilot/transcripts")
TAIL_WAIT = float(os.environ.get("LIVE_TAIL_WAIT", "20"))
FOLDER_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})(?:_\d+)?$")
SESSION_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{6})\.jsonl$")
TRACK = {"eles": "track2_desktop", "eu": "track3_mic"}
END_MARK = re.compile(r"^# fim (\d+(?:\.\d+)?)", re.M)
NOT_COVERED = 3


def log(msg: str) -> None:
    print(msg, flush=True)


def find_session(meeting_dir: str) -> tuple[str, float] | None:
    """Sessão do copiloto que começou junto com a gravação: no minuto da pasta (ou até 1 min depois)."""
    m = FOLDER_RE.match(os.path.basename(os.path.normpath(meeting_dir)))
    if not m or not SESSIONS or not os.path.isdir(SESSIONS):
        return None
    folder_t = datetime.strptime(f"{m.group(1)} {m.group(2)}:{m.group(3)}", "%Y-%m-%d %H:%M").timestamp()
    best = None
    for path in glob.glob(os.path.join(SESSIONS, "*.jsonl")):
        sm = SESSION_RE.match(os.path.basename(path))
        if not sm:
            continue  # .copilot.jsonl e afins
        t = datetime.strptime(f"{sm.group(1)} {sm.group(2)}", "%Y-%m-%d %H%M%S").timestamp()
        if folder_t - 5 <= t < folder_t + 120 and (best is None or t < best[1]):
            best = (path, t)
    return best


def capture_alive(session: str) -> bool:
    """O listen.py do copiloto ainda está gravando nesta sessão?"""
    name = os.path.basename(session)
    r = subprocess.run(["pgrep", "-f", f"listen.py --out .*{re.escape(name)}"], capture_output=True, text=True)
    return bool(r.stdout.strip())


def session_end(session: str) -> float | None:
    """Fim da sessão: marca "# fim <epoch>" que o run.sh do copiloto grava no log ao encerrar."""
    logf = session[:-len(".jsonl")] + ".log"
    try:
        with open(logf, encoding="utf-8", errors="replace") as f:
            hits = END_MARK.findall(f.read())
    except OSError:
        return None
    return float(hits[-1]) if hits else None


def read_lines(session: str) -> list[dict]:
    rows = []
    with open(session, encoding="utf-8") as f:
        for ln in f:
            try:
                r = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if r.get("who") in TRACK and str(r.get("text", "")).strip():
                rows.append(r)
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("meeting_dir")
    ap.add_argument("--part", required=True)
    a = ap.parse_args()

    found = find_session(a.meeting_dir)
    if not found:
        log("⊘ sem sessão ao vivo do copiloto para esta gravação")
        return NOT_COVERED
    session, rec_start = found
    durations = part_durations(a.meeting_dir)
    if a.part not in durations:
        log(f"✗ part{a.part}.duration ausente")
        return 1
    offset = sum(d for p, d in durations.items() if p < a.part)
    part_start = rec_start + offset
    part_end = part_start + durations[a.part]

    alive = capture_alive(session)
    end = session_end(session)
    if not alive and (end is None or end < part_end - 5):
        fim = datetime.fromtimestamp(end).strftime("%H:%M:%S") if end else "sem registro"
        log(f"⊘ sessão {os.path.basename(session)} não cobre a part{a.part} (captura encerrada, fim {fim})")
        return NOT_COVERED

    # A fala que atravessa o fim da parte só é escrita quando acaba (o VAD espera o silêncio):
    # com a captura viva, espera aparecer fala depois do fim da parte, ou o tempo-limite.
    deadline = time.time() + TAIL_WAIT
    while alive and time.time() < deadline:
        if any(r["start"] >= part_end for r in read_lines(session)):
            break
        time.sleep(2)
        alive = capture_alive(session)

    lines = [r for r in read_lines(session) if part_start <= r["start"] < part_end]
    for who, track in TRACK.items():
        segs = [{"start": round(max(0.0, r["start"] - part_start), 3),
                 "end": round(max(0.0, min(r["end"], part_end) - part_start), 3),
                 "text": str(r["text"]).strip()} for r in lines if r["who"] == who]
        base = os.path.join(a.meeting_dir, f"{track}_part{a.part}")
        save_json(f"{base}.json", {"segments": segs, "source": f"call-copilot {os.path.basename(session)}"})
        with open(f"{base}.srt", "w", encoding="utf-8") as f:
            f.write(segments_to_srt(segs))
        log(f"✓ {track} part{a.part}: {len(segs)} falas da transcrição ao vivo")
    return 0


if __name__ == "__main__":
    sys.exit(main())
