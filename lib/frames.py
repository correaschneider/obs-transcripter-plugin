"""Extrai 1 print por momento de um vídeo, descartando frames sem conteúdo visual.

Lógica portada do claude-telegram-bot-v2 (tools/video.py): métrica = desvio-padrão do
ImageMagick (0–65535; preto=0, slide com texto≈8000, screenshot≈6500). Entropia NÃO serve
(slide branco com texto dá 0,05). Frame uniforme → tenta t+5, t+10, t-5 e fica o melhor.

Uso:
  python3 frames.py <video> <moments.json> --out-dir <pasta> [--prefix part001]

Atualiza o moments.json in-place: cada momento ganha "image" (caminho absoluto) ou null.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import load_json, media_duration, run, save_json  # noqa: E402

STDDEV_GOOD = 655.0   # ≥1 %: aceita na hora
STDDEV_MIN = 200.0    # abaixo disso o frame é descartado
JPG_MIN_BYTES = 20 * 1024  # fallback sem ImageMagick
RETRY_OFFSETS = (0.0, 5.0, 10.0, -5.0)


def score(path: str) -> float:
    if not os.path.isfile(path):
        return 0.0
    if shutil.which("identify"):
        r = run(["identify", "-format", "%[standard-deviation]", path], timeout=30)
        if r.returncode == 0:
            try:
                return float(r.stdout.strip())
            except ValueError:
                pass
    return 0.0 if os.path.getsize(path) < JPG_MIN_BYTES else STDDEV_GOOD


def grab(video: str, t: float, out: str) -> float:
    r = run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{max(t, 0):.3f}", "-i", video,
             "-frames:v", "1", "-q:v", "2", out], timeout=60)
    return score(out) if r.returncode == 0 else 0.0


def best_frame(video: str, t: float, duration: float, out: str) -> str | None:
    best, best_score = None, 0.0
    for off in RETRY_OFFSETS:
        tt = t + off
        if not (0 <= tt <= duration):
            continue
        cand = f"{out[:-4]}_try{int(off):+d}.jpg"
        s = grab(video, tt, cand)
        if s > best_score:
            if best:
                os.remove(best)
            best, best_score = cand, s
        elif os.path.exists(cand):
            os.remove(cand)
        if best_score >= STDDEV_GOOD:
            break
    if best and best_score >= STDDEV_MIN:
        os.replace(best, out)
        return out
    if best:
        os.remove(best)
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("moments_json")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--prefix", default="moment")
    a = ap.parse_args()

    data = load_json(a.moments_json)
    if not isinstance(data, dict):
        print(f"✗ moments inválido: {a.moments_json}")
        return 1
    moments = data.get("moments") or []
    if not moments:
        print("⊘ nenhum momento — sem prints")
        return 0
    os.makedirs(a.out_dir, exist_ok=True)
    duration = float(data.get("duration") or 0) or media_duration(a.video)

    def one(i_m):
        i, m = i_m
        out = os.path.abspath(os.path.join(a.out_dir, f"{a.prefix}_m{i:02d}.jpg"))
        m["image"] = best_frame(a.video, float(m["t"]), duration, out)

    with ThreadPoolExecutor(max_workers=4) as ex:
        list(ex.map(one, enumerate(moments, 1)))
    save_json(a.moments_json, data)
    ok = sum(1 for m in moments if m.get("image"))
    print(f"✓ {ok}/{len(moments)} prints úteis")
    return 0


if __name__ == "__main__":
    sys.exit(main())
