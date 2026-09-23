"""Tira do maximizado as janelas dos apps de call (Teams/Meet PWA) no início da gravação.

Por quê: o app PWA não tem barra de título, então maximizado ele cobre o monitor inteiro e o
GNOME passa a desenhá-lo direto na tela (sem composição). A fonte Window Capture (Xcomposite)
do OBS depende da composição e perde a imagem depois de um Alt+Tab — a gravação cai pro
monitor por baixo e os prints dos momentos saem da janela errada. Com a janela uns pixels
menor que o monitor a captura não cai (validado em 17/09).

Uso:
  python3 window_fit.py          # ajusta as janelas dos apps de call que cobrem um monitor inteiro
  python3 window_fit.py status   # só mostra estado/geometria, sem mexer

Env:
  CALL_WINDOW_CLASSES  classes X11 separadas por vírgula (default: Teams PWA + Meet PWA)
  CALL_WINDOW_MARGIN   folga em px de cada lado (default 12)

Exit: 0 sempre que não houver erro de X11 (sem janela aberta também é 0).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time

CLASSES = [c.strip() for c in os.environ.get(
    "CALL_WINDOW_CLASSES",
    "crx_cifhbcnohmdccbgoicgdjpfamggdegmo,crx_kjgfgldnnfoeklkmfkjfagphfepbbdan",
).split(",") if c.strip()]
MARGIN = int(os.environ.get("CALL_WINDOW_MARGIN", "12"))


def sh(*cmd: str) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout


def monitors() -> list[tuple[int, int, int, int]]:
    """[(x, y, w, h)] de `xrandr --listmonitors` (linha: 1920/527x1080/296+1920+0)."""
    out = []
    for m in re.finditer(r"(\d+)/\d+x(\d+)/\d+\+(\d+)\+(\d+)", sh("xrandr", "--listmonitors")):
        w, h, x, y = map(int, m.groups())
        out.append((x, y, w, h))
    return out


def call_windows() -> list[tuple[str, str]]:
    """[(id, classe)] das janelas de call abertas."""
    wins = []
    for line in sh("wmctrl", "-lx").splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[2].split(".")[0] in CLASSES:
            wins.append((parts[0], parts[2].split(".")[0]))
    return wins


def geometry(wid: str) -> tuple[int, int, int, int]:
    info = sh("xwininfo", "-id", wid)
    get = lambda k: int(re.search(rf"{k}:\s+(-?\d+)", info).group(1))  # noqa: E731
    return get("Absolute upper-left X"), get("Absolute upper-left Y"), get("Width"), get("Height")


def frame_extents(wid: str) -> tuple[int, int, int, int]:
    """Sombra/borda desenhada pelo app (left, right, top, bottom) — fica fora da área visível."""
    m = re.search(r"=\s*(\d+),\s*(\d+),\s*(\d+),\s*(\d+)", sh("xprop", "-id", wid, "_GTK_FRAME_EXTENTS"))
    return tuple(map(int, m.groups())) if m else (0, 0, 0, 0)


def state(wid: str) -> str:
    return sh("xprop", "-id", wid, "_NET_WM_STATE").split("=", 1)[-1].strip()


def visible_rect(wid: str) -> tuple[int, int, int, int]:
    x, y, w, h = geometry(wid)
    l, r, t, b = frame_extents(wid)
    return x + l, y + t, w - l - r, h - t - b


def monitor_of(rect, mons):
    cx, cy = rect[0] + rect[2] // 2, rect[1] + rect[3] // 2
    for m in mons:
        if m[0] <= cx < m[0] + m[2] and m[1] <= cy < m[1] + m[3]:
            return m
    return mons[0] if mons else None


def covers_monitor(rect, mon) -> bool:
    return rect[2] >= mon[2] - 2 and rect[3] >= mon[3] - 2


def fit(wid: str, cls: str, mons) -> None:
    st = state(wid)
    rect = visible_rect(wid)
    mon = monitor_of(rect, mons)
    maximized = "MAXIMIZED" in st or "FULLSCREEN" in st
    if not mon or not (maximized or covers_monitor(rect, mon)):
        print(f"⊘ {cls[:12]} {wid}: não cobre o monitor ({rect[2]}x{rect[3]}) — nada a fazer")
        return
    for attempt in range(3):
        # `wmctrl -b` aceita no máximo 2 propriedades: com 3 ele ignora o comando em silêncio
        sh("wmctrl", "-i", "-r", wid, "-b", "remove,maximized_vert,maximized_horz")
        sh("wmctrl", "-i", "-r", wid, "-b", "remove,fullscreen")
        time.sleep(1.0)  # animação de desmaximizar do GNOME; com 0,6 s ele re-maximiza
        # A janela X INTEIRA (conteúdo + sombra desenhada pelo app) fica dentro do monitor, com
        # folga. Se a sombra passar da borda, o GNOME trata como maximizada na horizontal e a
        # captura volta a cair. Mover+redimensionar num comando só (wmctrl -e).
        x, y = mon[0] + MARGIN, mon[1] + MARGIN
        w, h = mon[2] - 2 * MARGIN, mon[3] - 2 * MARGIN
        sh("wmctrl", "-i", "-r", wid, "-e", f"0,{x},{y},{w},{h}")
        time.sleep(1.0)  # animação de desmaximizar do GNOME; com 0,6 s ele re-maximiza
        if "MAXIMIZED" not in state(wid) and not covers_monitor(visible_rect(wid), mon):
            break
    after = visible_rect(wid)
    print(f"✓ {cls[:12]} {wid}: {rect[2]}x{rect[3]} (maximizada={maximized}) → "
          f"{after[2]}x{after[3]} em {after[0]},{after[1]} (monitor {mon[2]}x{mon[3]}+{mon[0]}+{mon[1]})")


def main() -> int:
    wins = call_windows()
    if not wins:
        print("⊘ nenhuma janela de call (Teams/Meet app) aberta")
        return 0
    mons = monitors()
    for wid, cls in wins:
        if len(sys.argv) > 1 and sys.argv[1] == "status":
            print(f"{cls[:12]} {wid} state=[{state(wid)}] visível={visible_rect(wid)} "
                  f"monitor={monitor_of(visible_rect(wid), mons)}")
        else:
            fit(wid, cls, mons)
    return 0


if __name__ == "__main__":
    sys.exit(main())
