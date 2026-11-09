"""Pós-call: transcrição de todas as partes + momentos-chave e prints, em pipeline.

Durante a call o process_split.sh só guarda o áudio (partNNN.mka) e um frame a cada 5 s
(frames_partNNN/). Aqui, com a call encerrada:

  GPU (uma parte por vez):   [T1][T2][T3]...
  Claude + prints (até 2):       [M1][M2][M3]...   ← cada parte segue assim que a transcrição sai

T = lib/transcribe.py sobre o partNNN.mka (WhisperX, contexto da parte inteira). Se falhar, a parte
cai na transcrição ao vivo do copiloto (lib/live_transcript.py); sem nenhuma das duas, vira
partNNN.failed (o áudio fica pra reprocessar). M = lib/moments.py select + lib/frames.py sobre os
frames pré-extraídos.

Trava de GPU: antes de cada parte, se há uma captura ao vivo COM FALA RECENTE (outra call em
andamento: o transcript do copiloto mudou nos últimos GPU_BUSY_SECONDS), pede ao servidor WhisperX
que solte a VRAM e espera — o copiloto ao vivo tem prioridade. A captura parada da call que acabou
(overlay ainda aberto) não bloqueia.

Uso:  python3 postcall.py <pasta_reuniao> [--part NNN]   (--part reprocessa só uma parte)
Env:  MOMENTS_PER_PART (3), POSTCALL_WORKERS (2), GPU_BUSY_SECONDS (90), GPU_POLL_SECONDS (20),
      WHISPERX_SERVER_URL
Exit: 0 = todas as partes ok; 1 = alguma parte sem transcrição (partNNN.failed).
"""

from __future__ import annotations

import argparse
import glob
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

LIB = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, LIB)
from common import part_durations  # noqa: E402
from transcribe import live_captures_active, release_server  # noqa: E402

MOMENTS_PER_PART = os.environ.get("MOMENTS_PER_PART", "3")
WORKERS = int(os.environ.get("POSTCALL_WORKERS", "2"))
BUSY_S = float(os.environ.get("GPU_BUSY_SECONDS", "90"))
POLL_S = float(os.environ.get("GPU_POLL_SECONDS", "20"))
TRACKS = ("track1_mix", "track2_desktop", "track3_mic")
FAILED_TAG = "transcrição pós-call"


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def py(script: str, *args: str) -> int:
    return subprocess.run([sys.executable, os.path.join(LIB, script), *args]).returncode


def wait_gpu() -> None:
    """Espera não haver call ao vivo em andamento (o copiloto tem prioridade na GPU)."""
    first = True
    while True:
        active = live_captures_active()
        if not active:
            return
        if first:
            log(f"⏸ call ao vivo em andamento ({', '.join(os.path.basename(p) for p in active)}): "
                f"liberando a GPU e esperando")
            release_server()
            first = False
        time.sleep(POLL_S)


def transcribed(meeting: str, part: str) -> bool:
    return any(os.path.isfile(os.path.join(meeting, f"{t}_part{part}.json")) for t in TRACKS)


def transcribe_part(meeting: str, part: str) -> bool:
    if transcribed(meeting, part):
        log(f"⊘ part{part} já transcrita")
        return True
    # o áudio guardado no split; se o split falhou, o vídeo ainda está lá
    audio = next((p for p in [os.path.join(meeting, f"part{part}.mka")]
                  + sorted(glob.glob(os.path.join(meeting, f"part{part}.*")))
                  if os.path.isfile(p) and p.rsplit(".", 1)[-1].lower() in ("mka", "mkv", "mp4", "mov", "webm")), None)
    if audio:
        wait_gpu()
        t = time.time()
        if py("transcribe.py", audio, "--out-dir", meeting, "--part", part) == 0:
            log(f"✓ part{part} transcrita em {time.time() - t:.0f}s")
            return True
        log(f"⚠ WhisperX falhou na part{part}: tentando a transcrição ao vivo do copiloto")
    else:
        log(f"⚠ part{part} sem áudio: tentando a transcrição ao vivo do copiloto")
    for t in TRACKS:  # faixa que o WhisperX deixou pela metade não pode misturar com a ao vivo
        for ext in (".json", ".srt"):
            p = os.path.join(meeting, f"{t}_part{part}{ext}")
            if os.path.isfile(p):
                os.remove(p)
    return py("live_transcript.py", meeting, "--part", part) == 0


def moments_part(meeting: str, part: str) -> None:
    """Momentos-chave da parte (Claude) + prints a partir dos frames pré-extraídos."""
    t = time.time()
    if py("moments.py", "select", meeting, "--part", part, "--max", MOMENTS_PER_PART) != 0:
        log(f"⚠ momentos da part{part} falharam")
        return
    frames = os.path.join(meeting, f"frames_part{part}")
    src = frames if os.path.isdir(frames) else next(iter(glob.glob(os.path.join(meeting, f"part{part}.mkv"))), None)
    if src:
        py("frames.py", src, os.path.join(meeting, f"moments_part{part}.json"),
           "--out-dir", os.path.join(meeting, "moments"), "--prefix", f"part{part}")
    log(f"✓ part{part}: momentos e prints em {time.time() - t:.0f}s")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("meeting_dir")
    ap.add_argument("--part", help="só esta parte (reprocessamento)")
    a = ap.parse_args()
    meeting = os.path.realpath(a.meeting_dir)

    parts = sorted(part_durations(meeting))
    if a.part:
        parts = [p for p in parts if p == a.part]
    if not parts:
        log("⊘ nenhuma parte (partNNN.duration) na pasta")
        return 0
    log(f"▶ pós-call: {len(parts)} parte(s), momentos com até {WORKERS} em paralelo")

    failed = []
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        jobs = []
        for part in parts:  # GPU em série; cada parte transcrita já segue pros momentos
            mark = os.path.join(meeting, f"part{part}")
            if transcribe_part(meeting, part):
                # só apaga a marca que ESTE passo criou: a do split (vídeo mantido) segura a publicação
                if os.path.exists(f"{mark}.failed") and open(f"{mark}.failed").read().startswith(FAILED_TAG):
                    os.remove(f"{mark}.failed")
                jobs.append(pool.submit(moments_part, meeting, part))
            else:
                failed.append(part)
                with open(f"{mark}.failed", "w") as f:
                    f.write(f"{FAILED_TAG} falhou (WhisperX e ao vivo) — áudio mantido em part{part}.mka\n")
                log(f"✗ part{part} sem transcrição")
        for j in jobs:
            j.result()

    log(f"✓ pós-call concluído{f' — partes sem transcrição: {failed}' if failed else ''}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
