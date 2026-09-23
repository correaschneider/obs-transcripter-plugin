"""Escolha dos momentos-chave (as partes que exigem julgamento vão pro `claude -p`).

Subcomandos:
  select <pasta_reuniao> --part 001 [--max 3]
      Lê as transcrições da parte, pede ao Claude até N momentos e grava
      moments_part001.json ({"part","duration","moments":[{t,label,quote,speaker}]}).
      `t` é LOCAL à parte. Se o Claude falhar, cai pra 1 momento no meio da parte —
      assim o vídeo ainda pode ser apagado sem perder o registro visual.
  pick <pasta_reuniao> [--max 10]
      Junta os moments_part*.json com imagem, converte `t` pra tempo global da reunião
      e, se passar de N, pede ao Claude os melhores (com a ata como contexto).
      Grava moments.json.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import claude_bin, fmt_ts, load_json, part_durations, part_offsets, run, save_json  # noqa: E402

SPEAKERS = {"track2_desktop": "Participantes", "track3_mic": "Lord", "track1_mix": None}

MOMENTS_SCHEMA = json.dumps({
    "type": "object",
    "properties": {"moments": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "t": {"type": "number", "description": "segundos, = MM:SS da linha convertido"},
            "label": {"type": "string", "description": "3-6 palavras, o que acontece"},
            "quote": {"type": "string", "description": "texto da linha"},
        },
        "required": ["t", "label", "quote"],
    }}},
    "required": ["moments"],
})

PICK_SCHEMA = json.dumps({
    "type": "object",
    "properties": {"indices": {"type": "array", "items": {"type": "integer"}}},
    "required": ["indices"],
})

SELECT_PROMPT = """Abaixo está a transcrição de um trecho de {duration} de uma reunião gravada (é a
parte {part} de uma gravação dividida em partes), um segment por linha no formato
`[MM:SS] Quem: texto` ("Lord" é o dono da gravação; "Participantes" são os demais).
Escolha até {max_moments} momentos (menos se o trecho for monótono; 0 se não houver nada
relevante), priorizando nesta ordem:
1. mudança de tópico / abertura de assunto novo;
2. decisão, definição ou acordo (prazo, número, nome específico);
3. demonstração ou apresentação de tela ("olha aqui", "esse é o…", "essa tela…");
4. pergunta-chave que orientou o resto;
5. menção a nome próprio importante (pessoa, task, MR, cliente, valor).
Evite saudações, despedidas, filler e pausas; espace os momentos.
Para cada momento: `t` = o MM:SS da linha convertido em segundos, `label` curta em PT-BR
dizendo o que ESTÁ ACONTECENDO (não parafraseie a fala), `quote` = o texto da linha.

TRANSCRIÇÃO:
{lines}"""

PICK_PROMPT = """Estes são momentos-chave pré-selecionados de uma reunião gravada, cada um com um
print de tela. Escolha os {max_moments} mais relevantes para quem não assistiu à reunião —
decisões, demonstrações de tela e mudanças de assunto — cobrindo a reunião inteira e sem
repetir o mesmo assunto. Devolva os números (índices) escolhidos.

{ata}MOMENTOS:
{lines}"""

MIN_GAP = 20.0  # s: prints mais próximos que isso costumam ser a mesma cena (borda de parte)

DISALLOWED = ["Bash", "Edit", "Write", "NotebookEdit", "WebFetch", "WebSearch", "Agent"]


def ask_claude(prompt: str, schema: str, cwd: str) -> dict:
    """Chamada curta, sem ferramentas, com saída estruturada. `--disallowedTools "*"` não
    serve: a saída do --json-schema é uma tool interna e seria negada junto."""
    bin_ = claude_bin()
    if not bin_:
        raise RuntimeError("claude não encontrado no PATH")
    r = run([bin_, "-p", "--output-format", "json", "--json-schema", schema,
             "--permission-mode", "dontAsk", "--disallowedTools", *DISALLOWED, "--", prompt],
            timeout=300, cwd=cwd)
    if r.returncode != 0:
        raise RuntimeError(f"claude exit {r.returncode}: {(r.stderr or r.stdout)[-300:]}")
    data = json.loads(r.stdout)
    out = data.get("structured_output")
    if not isinstance(out, dict):
        raise RuntimeError(f"claude sem structured_output: {r.stdout[:200]}")
    return out


def part_lines(meeting_dir: str, part: str) -> str:
    rows = []
    for track, speaker in SPEAKERS.items():
        data = load_json(os.path.join(meeting_dir, f"{track}_part{part}.json"), {}) or {}
        for s in data.get("segments", []):
            text = str(s.get("text", "")).strip()
            if text:
                rows.append((float(s.get("start", 0)), speaker, text))
    rows.sort(key=lambda r: r[0])
    return "\n".join(f"[{fmt_ts(t)[3:] if t < 3600 else fmt_ts(t)}] "
                     f"{(sp + ': ') if sp else ''}{txt}" for t, sp, txt in rows)


def speaker_at(meeting_dir: str, part: str, t: float) -> str | None:
    """Autor da fala mais próxima de `t`. O Claude devolve o MM:SS da linha (segundo
    truncado), então a fala começa em [t, t+1) — tolera 1,5 s pra cada lado."""
    best, best_diff = None, 1.5
    for track, speaker in SPEAKERS.items():
        data = load_json(os.path.join(meeting_dir, f"{track}_part{part}.json"), {}) or {}
        for s in data.get("segments", []):
            diff = abs(float(s.get("start", -99)) - t)
            if diff < best_diff:
                best, best_diff = speaker, diff
    return best


def cmd_select(a) -> int:
    part = a.part
    duration = part_durations(a.dir).get(part, 0.0)
    out_path = os.path.join(a.dir, f"moments_part{part}.json")
    lines = part_lines(a.dir, part)
    result = {"part": part, "duration": duration, "moments": []}

    if not lines:
        print(f"⊘ part{part} sem fala — sem momentos")
        save_json(out_path, result)
        return 0
    try:
        prompt = SELECT_PROMPT.format(duration=fmt_ts(duration), part=int(part),
                                      max_moments=a.max, lines=lines)
        raw = ask_claude(prompt, MOMENTS_SCHEMA, a.dir).get("moments") or []
        seen, moments = set(), []
        for m in raw:
            try:
                t = float(m.get("t"))
            except (TypeError, ValueError):
                continue
            if not (0 <= t <= max(duration, 0.5)) or int(t) in seen:
                continue
            seen.add(int(t))
            moments.append({"t": t, "label": str(m.get("label", "")).strip()[:60],
                            "quote": str(m.get("quote", "")).strip()[:500],
                            "speaker": speaker_at(a.dir, part, t)})
        result["moments"] = sorted(moments, key=lambda m: m["t"])[: a.max]
        print(f"✓ part{part}: {len(result['moments'])} momento(s) escolhidos")
    except Exception as e:  # noqa: BLE001
        print(f"⚠ part{part}: seleção pelo Claude falhou ({e}) — fallback: meio da parte")
        result["moments"] = [{"t": round(duration / 2, 3), "label": f"Parte {int(part)}",
                              "quote": "", "speaker": None}]
        result["fallback"] = True
    save_json(out_path, result)
    return 0


def spread(items: list, n: int) -> list:
    step = len(items) / n
    return [items[int(i * step)] for i in range(n)]


def cmd_pick(a) -> int:
    offsets = part_offsets(a.dir)
    all_m = []
    for path in sorted(glob.glob(os.path.join(a.dir, "moments_part*.json"))):
        data = load_json(path, {}) or {}
        part = data.get("part") or re.search(r"part(\d+)", path).group(1)
        for m in data.get("moments", []):
            if m.get("image") and os.path.isfile(m["image"]):
                all_m.append({**m, "part": part, "t": round(offsets.get(part, 0.0) + float(m["t"]), 3)})
    all_m.sort(key=lambda m: m["t"])
    # Dedupe temporal: das falas muito próximas, fica a primeira
    spaced = []
    for m in all_m:
        if not spaced or m["t"] - spaced[-1]["t"] >= MIN_GAP:
            spaced.append(m)
    if len(spaced) < len(all_m):
        print(f"  {len(all_m) - len(spaced)} momento(s) a menos de {MIN_GAP:.0f}s de outro — descartados")
    all_m = spaced

    chosen = all_m
    if len(all_m) > a.max:
        ata = ""
        ata_path = os.path.join(a.dir, "ata.md")
        if os.path.isfile(ata_path):
            with open(ata_path, encoding="utf-8") as f:
                ata = f"ATA DA REUNIÃO (contexto):\n{f.read()[:6000]}\n\n"
        lines = "\n".join(f"{i}. [{fmt_ts(m['t'])}] {m['label']} — \"{m['quote'][:200]}\""
                          for i, m in enumerate(all_m))
        try:
            idx = ask_claude(PICK_PROMPT.format(max_moments=a.max, ata=ata, lines=lines),
                             PICK_SCHEMA, a.dir).get("indices") or []
            idx = sorted({i for i in idx if isinstance(i, int) and 0 <= i < len(all_m)})[: a.max]
            chosen = [all_m[i] for i in idx] if idx else spread(all_m, a.max)
        except Exception as e:  # noqa: BLE001
            print(f"⚠ escolha final pelo Claude falhou ({e}) — espalhando uniformemente")
            chosen = spread(all_m, a.max)

    save_json(os.path.join(a.dir, "moments.json"), {"moments": chosen})
    print(f"✓ {len(chosen)} de {len(all_m)} momento(s) com print selecionados")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("select")
    s.add_argument("dir")
    s.add_argument("--part", required=True)
    s.add_argument("--max", type=int, default=3)
    p = sub.add_parser("pick")
    p.add_argument("dir")
    p.add_argument("--max", type=int, default=10)
    a = ap.parse_args()
    return cmd_select(a) if a.cmd == "select" else cmd_pick(a)


if __name__ == "__main__":
    sys.exit(main())
