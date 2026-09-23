"""Transcreve UMA parte de gravação do OBS direto do vídeo (sem .m4a persistido).

Com ≥3 faixas (layout do OBS: 1=mix, 2=desktop, 3=mic) transcreve só desktop e mic — a
faixa 1 é a soma das duas e só dobraria o trabalho. Com menos faixas, transcreve a 1ª.

Motor: servidor WhisperX residente (o mesmo do bot, que segura o flock global). Só cai pro
whisperx-cli se o servidor estiver FORA — com o servidor de pé e o modelo carregado, subir
um segundo modelo na GPU de 8 GB dá CUDA OOM.

Uso:
  python3 transcribe.py <video> --out-dir <pasta> --part 001 [--language pt]

Saídas: <out-dir>/<faixa>_part001.json ({"segments":[{start,end,text}]}) e .srt
Exit: 0 = todas as faixas ok (fala vazia conta como ok), 1 = alguma falhou.
"""

from __future__ import annotations

import argparse
import fcntl
import glob
import json
import os
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import audio_track_count, run, save_json  # noqa: E402

SERVER = os.environ.get("WHISPERX_SERVER_URL", "http://127.0.0.1:8765").rstrip("/")
CLI = os.environ.get("WHISPERX_BIN") or os.path.expanduser("~/.local/bin/whisperx-cli")
MODEL = os.environ.get("WHISPERX_MODEL", "large-v3")
COMPUTE = os.environ.get("WHISPERX_COMPUTE_TYPE", "int8")
LOCK = os.environ.get("WHISPERX_LOCK", "/tmp/whisperx-pipeline.lock")
OOM_MARKERS = ("out of memory", "cuda failed", "cublasstatus", "cudnn failure")


def log(msg: str) -> None:
    print(msg, flush=True)


def server_up() -> bool:
    if not SERVER:
        return False
    try:
        with urllib.request.urlopen(f"{SERVER}/health", timeout=3) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError):
        return False


def via_server(wav: str, language: str) -> list[dict]:
    body = json.dumps({"path": wav, "language": language}).encode()
    req = urllib.request.Request(f"{SERVER}/transcribe", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=3600) as r:
            return json.load(r).get("segments", [])
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"servidor whisperx HTTP {e.code}: {e.read()[:300]!r}") from e


def via_cli(wav: str, language: str) -> list[dict]:
    if not os.access(CLI, os.X_OK):
        raise RuntimeError(f"whisperx-cli não encontrado em {CLI}")
    out_dir = tempfile.mkdtemp(prefix="wx-")
    lock_fd = os.open(LOCK, os.O_CREAT | os.O_RDWR, 0o666)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        batch = int(os.environ.get("WHISPERX_BATCH", "8"))
        while batch >= 2:
            r = run([CLI, wav, "--model", MODEL, "--device", "cuda", "--compute_type", COMPUTE,
                     "--language", language, "--batch_size", str(batch),
                     "--output_format", "json", "--output_dir", out_dir], timeout=3600)
            if r.returncode == 0:
                found = glob.glob(os.path.join(out_dir, "*.json"))
                if not found:
                    raise RuntimeError("whisperx-cli não produziu JSON")
                with open(found[0], encoding="utf-8") as f:
                    return json.load(f).get("segments", [])
            if any(m in r.stderr.lower() for m in OOM_MARKERS):
                batch //= 2
                log(f"  ⚠ OOM — retry com batch={batch}")
                time.sleep(2)
                continue
            raise RuntimeError(f"whisperx-cli exit {r.returncode}: {r.stderr[-400:]}")
        raise RuntimeError("OOM mesmo com batch=2")
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
        shutil.rmtree(out_dir, ignore_errors=True)


def srt_ts(seconds: float) -> str:
    ms = int(round(max(0.0, seconds) * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def segments_to_srt(segments: list[dict]) -> str:
    blocks = []
    for i, s in enumerate((s for s in segments if str(s.get("text", "")).strip()), 1):
        blocks.append(f"{i}\n{srt_ts(float(s['start']))} --> {srt_ts(float(s['end']))}\n"
                      f"{str(s['text']).strip()}\n")
    return "\n".join(blocks)


# Alucinações clássicas do Whisper em trecho de silêncio (a faixa do mic fica muda enquanto os
# outros falam). Só descarta o segment se ele for INTEIRO uma dessas frases.
HALLUCINATIONS = {
    "obrigado", "obrigada", "obrigado por assistir", "obrigada por assistir",
    "legendas pela comunidade amaraorg", "inscrevase no canal", "tchau tchau",
}


# Palavras que o Whisper repete sozinhas sobre silêncio/ruído ("Música Música", "Obrigado. Obrigado.")
HALLUCINATION_WORDS = {"música", "musica", "obrigado", "obrigada", "aplausos", "risos"}


def is_hallucination(text: str) -> bool:
    norm = " ".join("".join(c for c in text.lower() if c.isalnum() or c == " ").split())
    return norm in HALLUCINATIONS or (bool(norm) and set(norm.split()) <= HALLUCINATION_WORDS)


def clean(segments: list[dict]) -> list[dict]:
    out = []
    for s in segments:
        text = str(s.get("text", "")).strip()
        if not text or is_hallucination(text):
            continue
        out.append({"start": round(float(s.get("start", 0)), 3),
                    "end": round(float(s.get("end", 0)), 3), "text": text})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--part", required=True)
    ap.add_argument("--language", default=os.environ.get("WHISPERX_LANGUAGE", "pt"))
    a = ap.parse_args()

    n = audio_track_count(a.video)
    tracks = [(1, "track2_desktop"), (2, "track3_mic")] if n >= 3 else [(0, "track1_mix")]
    if n == 0:
        log("⊘ vídeo sem trilha de áudio — nada a transcrever")
        return 0
    use_server = server_up()
    log(f"→ {n} faixa(s) de áudio; transcrevendo {[t for _, t in tracks]} "
        f"via {'servidor residente' if use_server else 'whisperx-cli'}")

    failed = 0
    for idx, name in tracks:
        base = os.path.join(a.out_dir, f"{name}_part{a.part}")
        if os.path.isfile(f"{base}.json"):
            log(f"⊘ {name} part{a.part} já transcrito")
            continue
        wav = os.path.join(a.out_dir, f".{name}_part{a.part}.wav")
        try:
            r = run(["ffmpeg", "-y", "-loglevel", "error", "-i", a.video, "-map", f"0:a:{idx}",
                     "-ac", "1", "-ar", "16000", wav], timeout=600)
            if r.returncode != 0:
                raise RuntimeError(f"ffmpeg exit {r.returncode}: {r.stderr[-300:]}")
            t = time.time()
            segments = clean(via_server(wav, a.language) if use_server else via_cli(wav, a.language))
            save_json(f"{base}.json", {"segments": segments})
            with open(f"{base}.srt", "w", encoding="utf-8") as f:
                f.write(segments_to_srt(segments))
            log(f"✓ {name} part{a.part}: {len(segments)} segments em {time.time() - t:.1f}s")
        except Exception as e:  # noqa: BLE001
            failed += 1
            log(f"✗ {name} part{a.part}: {e}")
        finally:
            if os.path.exists(wav):
                os.remove(wav)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
