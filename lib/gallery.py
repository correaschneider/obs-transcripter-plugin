"""Envia a galeria de prints dos momentos-chave ao Discord via webhook.

Uso:
  python3 gallery.py <pasta_reuniao>

Lê moments.json (gerado por `moments.py pick`). Cada print vira um embed com a imagem anexada
(`attachment://`), título `[HH:MM:SS] label` e a fala como descrição. O webhook aceita até 10
embeds/anexos por mensagem; os lotes também respeitam o limite de upload.

Env: DISCORD_WEBHOOK_URL (obrigatório), DISCORD_USERNAME, DISCORD_AVATAR_URL,
     DISCORD_MAX_ATTACH_BYTES (default 9000000)
Exit: 0 ok / nada a enviar, 1 falha de envio.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import fmt_ts, load_json  # noqa: E402

MAX_FILES = 10
COLOR = 0xE67E22


def batches(moments: list[dict], max_bytes: int) -> list[list[dict]]:
    out, cur, size = [], [], 0
    for m in moments:
        b = os.path.getsize(m["image"])
        if cur and (len(cur) >= MAX_FILES or size + b > max_bytes):
            out.append(cur)
            cur, size = [], 0
        cur.append(m)
        size += b
    if cur:
        out.append(cur)
    return out


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    meeting_dir = os.path.realpath(sys.argv[1])
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not url:
        print("✗ DISCORD_WEBHOOK_URL não definido")
        return 1
    data = load_json(os.path.join(meeting_dir, "moments.json"), {}) or {}
    moments = [m for m in data.get("moments", []) if m.get("image") and os.path.isfile(m["image"])]
    if not moments:
        print("⊘ nenhum print pra enviar")
        return 0

    max_bytes = int(os.environ.get("DISCORD_MAX_ATTACH_BYTES", "9000000"))
    groups = batches(moments, max_bytes)
    meeting = os.path.basename(meeting_dir)
    failed = 0
    for gi, group in enumerate(groups, 1):
        embeds, files = [], []
        for i, m in enumerate(group):
            name = f"m{gi:02d}_{i:02d}.jpg"
            quote = m.get("quote") or ""
            speaker = m.get("speaker")
            desc = f"> {quote[:350]}" + (f"\n— {speaker}" if speaker and quote else "")
            embeds.append({"title": f"[{fmt_ts(float(m['t']))}] {m.get('label', '')}"[:256],
                           "description": desc or None, "color": COLOR,
                           "image": {"url": f"attachment://{name}"}})
            files += ["-F", f"files[{i}]=@{m['image']};filename={name};type=image/jpeg"]
        suffix = f" ({gi}/{len(groups)})" if len(groups) > 1 else ""
        payload = {"username": os.environ.get("DISCORD_USERNAME", "OBS Pipeline"),
                   "content": f"🖼️ **Momentos da reunião{suffix}** — `{meeting}`",
                   "embeds": embeds}
        if os.environ.get("DISCORD_AVATAR_URL"):
            payload["avatar_url"] = os.environ["DISCORD_AVATAR_URL"]

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as pf, \
                tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as rf:
            json.dump(payload, pf, ensure_ascii=False)
        try:
            r = subprocess.run(["curl", "-sS", "-o", rf.name, "-w", "%{http_code}", "-X", "POST", url,
                                "-F", f"payload_json=<{pf.name};type=application/json", *files],
                               capture_output=True, text=True, timeout=120)
            code = r.stdout.strip()
            if code.startswith("2"):
                print(f"  ✓ galeria{suffix}: {len(group)} print(s) (HTTP {code})")
            else:
                failed += 1
                with open(rf.name, encoding="utf-8", errors="replace") as f:
                    print(f"  ✗ galeria{suffix}: HTTP {code} {r.stderr.strip()} {f.read()[:500]}")
        finally:
            os.remove(pf.name)
            os.remove(rf.name)
        time.sleep(1)  # rate limit do webhook
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
