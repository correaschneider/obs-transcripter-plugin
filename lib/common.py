"""Utilitários compartilhados pelas etapas do pipeline (só stdlib).

CLI:
  python3 common.py duration <video>   → imprime a duração em segundos
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

VIDEO_EXTS = (".mkv", ".mp4", ".mov", ".webm")


def fmt_ts(seconds: float) -> str:
    s = max(0, int(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}"


def run(cmd: list[str], timeout: float = 600, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)


def media_duration(path: str) -> float:
    """Duração do container; se ausente (MKV não finalizado), o pts do último pacote."""
    r = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path], timeout=60)
    try:
        return float(r.stdout.strip())
    except ValueError:
        pass
    r = run(["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "packet=pts_time", "-of", "csv=p=0", path], timeout=600)
    times = []
    for line in r.stdout.splitlines():
        try:
            times.append(float(line.strip().rstrip(",")))
        except ValueError:
            continue
    return max(times) if times else 0.0


def audio_track_count(path: str) -> int:
    r = run(["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=index", "-of", "csv=p=0", path], timeout=60)
    return len([ln for ln in r.stdout.splitlines() if ln.strip()])


def load_json(path: str, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def save_json(path: str, data) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def part_durations(meeting_dir: str) -> dict[str, float]:
    """{"001": 300.0, ...} a partir dos `partNNN.duration` gravados antes de apagar o vídeo."""
    out: dict[str, float] = {}
    for name in os.listdir(meeting_dir):
        if name.startswith("part") and name.endswith(".duration"):
            try:
                with open(os.path.join(meeting_dir, name)) as f:
                    out[name[4:-9]] = float(f.read().strip() or 0)
            except (OSError, ValueError):
                continue
    return out


def part_offsets(meeting_dir: str) -> dict[str, float]:
    """Offset global de cada parte = soma das durações das partes anteriores."""
    offsets, acc = {}, 0.0
    for part, dur in sorted(part_durations(meeting_dir).items()):
        offsets[part] = acc
        acc += dur
    return offsets


def claude_bin() -> str | None:
    cand = os.environ.get("CLAUDE_BIN")
    if cand and os.access(cand, os.X_OK):
        return cand
    from shutil import which
    found = which("claude")
    if found:
        return found
    for c in ("~/.local/bin/claude", "~/.npm-global/bin/claude", "/usr/local/bin/claude"):
        c = os.path.expanduser(c)
        if os.access(c, os.X_OK):
            return c
    return None


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "duration":
        print(f"{media_duration(sys.argv[2]):.3f}")
    else:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
