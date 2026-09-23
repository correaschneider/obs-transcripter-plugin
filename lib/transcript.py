"""Monta as transcrições da reunião inteira a partir das partes.

Subcomandos:
  concat <pasta_reuniao>
      Para cada faixa, junta <faixa>_partNNN.json aplicando o offset da parte (soma das
      durações anteriores, lidas de partNNN.duration — o vídeo já pode ter sido apagado).
      Grava <faixa>.srt e <faixa>.json.
  merge <pasta_reuniao>
      Intercala as faixas da reunião em ordem cronológica com o autor da fala e grava
      transcricao.txt ("[HH:MM:SS] Lord: ...").
"""

from __future__ import annotations

import glob
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import fmt_ts, load_json, part_durations, part_offsets, save_json  # noqa: E402
from transcribe import segments_to_srt  # noqa: E402

TRACKS = {"track1_mix": None, "track2_desktop": "Participantes", "track3_mic": "Lord"}


def concat(meeting_dir: str) -> None:
    offsets = part_offsets(meeting_dir)
    for track in TRACKS:
        files = sorted(glob.glob(os.path.join(meeting_dir, f"{track}_part*.json")))
        if not files:
            continue
        segments = []
        for path in files:
            part = re.search(r"_part(\d+)\.json$", path).group(1)
            if part not in offsets:
                print(f"  ⚠ {os.path.basename(path)} sem partNNN.duration — offset 0")
            off = offsets.get(part, 0.0)
            for s in (load_json(path, {}) or {}).get("segments", []):
                segments.append({"start": round(float(s["start"]) + off, 3),
                                 "end": round(float(s["end"]) + off, 3), "text": s["text"]})
        save_json(os.path.join(meeting_dir, f"{track}.json"), {"segments": segments})
        with open(os.path.join(meeting_dir, f"{track}.srt"), "w", encoding="utf-8") as f:
            f.write(segments_to_srt(segments))
        print(f"  → {track}.srt ({len(files)} partes, {len(segments)} segments)")
    durations = part_durations(meeting_dir)
    print(f"    {len(durations)} parte(s), duração total ~{sum(durations.values()) / 60:.1f}min")


def merge(meeting_dir: str) -> None:
    rows = []
    for track, speaker in TRACKS.items():
        data = load_json(os.path.join(meeting_dir, f"{track}.json"), {}) or {}
        for s in data.get("segments", []):
            text = str(s.get("text", "")).strip()
            if text:
                rows.append((float(s["start"]), speaker, text))
    rows.sort(key=lambda r: r[0])
    out = os.path.join(meeting_dir, "transcricao.txt")
    with open(out, "w", encoding="utf-8") as f:
        for t, sp, text in rows:
            f.write(f"[{fmt_ts(t)}] {(sp + ': ') if sp else ''}{text}\n")
    print(f"  → transcricao.txt ({len(rows)} falas)")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] in ("concat", "merge"):
        (concat if sys.argv[1] == "concat" else merge)(sys.argv[2])
    else:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
