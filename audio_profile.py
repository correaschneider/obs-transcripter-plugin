"""Troca o fone Bluetooth entre modo call (Handsfree/HFP, com microfone) e modo normal
(Headset/A2DP, som bom sem microfone). Só stdlib + pactl (PipeWire/PulseAudio).

Uso:
  python3 audio_profile.py call     # HFP (mSBC se houver) + mic e saída do fone como padrão
  python3 audio_profile.py normal   # volta pro A2DP e restaura o microfone padrão anterior
  python3 audio_profile.py status

Env:
  BT_DEVICE_MATCH   trecho do nome do fone (default: vazio = primeiro fone Bluetooth conectado)
  AUDIO_STATE_FILE  onde guardar o estado anterior (default ~/.cache/obs-audio-profile.json)

Exit: 0 ok (inclusive "nenhum fone conectado" — não é erro), 1 falha ao trocar.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

MATCH = os.environ.get("BT_DEVICE_MATCH", "").lower()
STATE_FILE = os.path.expanduser(os.environ.get("AUDIO_STATE_FILE", "~/.cache/obs-audio-profile.json"))
# Ordem de preferência. mSBC (banda larga) soa bem menos "lata" que o CVSD.
CALL_PROFILES = ("headset-head-unit-msbc", "headset-head-unit", "headset-head-unit-cvsd")


def pactl(*args: str) -> str:
    r = subprocess.run(["pactl", *args], capture_output=True, text=True, timeout=15)
    if r.returncode != 0:
        raise RuntimeError(f"pactl {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout


def pactl_json(*args: str):
    return json.loads(pactl("-f", "json", *args) or "[]")


def find_card() -> dict | None:
    for c in pactl_json("list", "cards"):
        if not c["name"].startswith("bluez_card."):
            continue
        desc = str(c.get("properties", {}).get("device.description", ""))
        if not MATCH or MATCH in desc.lower() or MATCH in c["name"].lower():
            return c
    return None


def defaults() -> tuple[str, str]:
    info = pactl("info")
    get = lambda key: next((ln.split(":", 1)[1].strip() for ln in info.splitlines()  # noqa: E731
                            if ln.startswith(key)), "")
    return get("Default Sink"), get("Default Source")


def wait_for(kind: str, prefix: str, timeout: float = 8.0) -> str | None:
    """Espera o PipeWire recriar o sink/source do fone depois da troca de perfil."""
    end = time.time() + timeout
    while time.time() < end:
        for item in pactl_json("list", kind, "short"):
            name = item["name"]
            if name.startswith(prefix) and not name.endswith(".monitor"):
                return name
        time.sleep(0.3)
    return None


def move_captures(to_source: str, only_from: set[str] | None = None) -> int:
    """Move capturas já abertas (call no navegador, OBS) pro `to_source`. Mudar só o mic
    padrão não basta: app que abriu o microfone antes da troca continua no antigo.
    `only_from` = nomes de source de onde mover (None = de qualquer um, menos monitores)."""
    by_index = {s["index"]: s["name"] for s in pactl_json("list", "sources", "short")}
    moved = 0
    for out in pactl_json("list", "source-outputs"):
        current = by_index.get(out.get("source"), "")
        if not current or current == to_source or current.endswith(".monitor"):
            continue
        if only_from is not None and current not in only_from:
            continue
        app = out.get("properties", {}).get("application.name", "?")
        try:
            pactl("move-source-output", str(out["index"]), to_source)
            print(f"  ↪ captura de {app}: {current} → {to_source}")
            moved += 1
        except RuntimeError as e:
            print(f"  ⚠ não moveu captura de {app}: {e}")
    return moved


def available(card: dict, name: str) -> bool:
    p = card["profiles"].get(name)
    return bool(p and p.get("available"))


def load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(data: dict) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f)


def mac_of(card: dict) -> str:
    return card["name"].removeprefix("bluez_card.")


def cmd_call(card: dict) -> int:
    active = card.get("active_profile", "")
    if not active.startswith("headset-head-unit"):
        sink, source = defaults()
        save_state({"card": card["name"], "profile": active, "sink": sink, "source": source})
    profile = next((p for p in CALL_PROFILES if available(card, p)), None)
    if not profile:
        print(f"✗ {card['name']} não oferece perfil Handsfree")
        return 1
    if active != profile:
        pactl("set-card-profile", card["name"], profile)
    mac = mac_of(card)
    src = wait_for("sources", f"bluez_input.{mac}")
    sink = wait_for("sinks", f"bluez_output.{mac}")
    if not src:
        print(f"✗ perfil {profile} ativo, mas o microfone do fone não apareceu")
        return 1
    pactl("set-default-source", src)
    if sink:
        pactl("set-default-sink", sink)
    move_captures(src)
    print(f"✓ call: {profile} | mic={src} | saída={sink}")
    return 0


def cmd_normal(card: dict) -> int:
    state = load_state()
    wanted = state.get("profile") if state.get("card") == card["name"] else None
    if not (wanted and wanted.startswith("a2dp") and available(card, wanted)):
        a2dp = sorted((n for n, p in card["profiles"].items() if n.startswith("a2dp") and p.get("available")),
                      key=lambda n: card["profiles"][n].get("priority", 0), reverse=True)
        wanted = a2dp[0] if a2dp else None
    if not wanted:
        print(f"✗ {card['name']} não oferece perfil A2DP")
        return 1
    if card.get("active_profile") != wanted:
        pactl("set-card-profile", card["name"], wanted)
    sink = wait_for("sinks", f"bluez_output.{mac_of(card)}")
    if sink:
        pactl("set-default-sink", sink)
    # Microfone: volta o que era padrão antes da call, se ainda existir
    prev_src = state.get("source", "")
    if prev_src and not prev_src.startswith("bluez_input.") and wait_for("sources", prev_src, 1.0):
        pactl("set-default-source", prev_src)
        # O mic do fone some com o A2DP; o PipeWire realoca sozinho, mas garante o destino
        move_captures(prev_src, only_from={n["name"] for n in pactl_json("list", "sources", "short")
                                           if n["name"].startswith("bluez_input.")})
    print(f"✓ normal: {wanted} | saída={sink} | mic={defaults()[1]}")
    return 0


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in ("call", "normal", "status"):
        print(__doc__, file=sys.stderr)
        return 2
    try:
        card = find_card()
        if not card:
            print("⊘ nenhum fone Bluetooth conectado — nada a trocar")
            return 0
        if sys.argv[1] == "status":
            sink, source = defaults()
            print(f"{card['properties'].get('device.description')} ({card['name']})\n"
                  f"perfil={card.get('active_profile')}\nsaída padrão={sink}\nmic padrão={source}")
            return 0
        return cmd_call(card) if sys.argv[1] == "call" else cmd_normal(card)
    except Exception as e:  # noqa: BLE001
        print(f"✗ {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
