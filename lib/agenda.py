"""Tarefas do dono da gravação → lista de tarefas (Google Tasks), sem duplicar.

Subcomandos:
  propose <pasta_reuniao> --ledger <arquivo.json> [--out <agenda.md>]
      Lê a seção do dono no tarefas.md, compara com as tarefas já registradas no ledger
      (abertas e concluídas) e decide, item a item, com o `claude -p`:
        criar     → tarefa nova (título "<ID> - ..." ou "<sigla> - ..." quando não há ID)
        ja_existe → já está no ledger (aberta ou concluída), não cria de novo
        concluir  → item marcado [x] na reunião que fecha uma tarefa aberta
        ignorar   → item [x] sem tarefa aberta correspondente (nada a fazer)
      Grava agenda.md (relatório) e atualiza o ledger com o que seria criado/concluído.
      Hoje é só simulação: o ledger faz o papel da lista do Google Tasks.

Sigla da empresa: `siglas` no empresas.json ({"Empresa": "X"}); sem ela, a inicial.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import load_json, save_json  # noqa: E402
from moments import ask_claude  # noqa: E402
from vault import _CFG, DONO, split_frontmatter  # noqa: E402

# IDs de task dos trackers (Jira, ClickUp…): vem do empresas.json (`task_id_re`), que é local;
# sem ele, qualquer "SIGLA-123".
TASK_ID_RE = re.compile(_CFG.get("task_id_re") or r"\b[A-Z][A-Z0-9]{1,9}-\d+\b", re.I)
ITEM_RE = re.compile(r"^\s*[-*]\s+\[([ xX])\]\s+(.*)$")

SCHEMA = json.dumps({
    "type": "object",
    "properties": {"itens": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "n": {"type": "integer", "description": "número do item na lista recebida"},
            "acao": {"type": "string", "enum": ["criar", "ja_existe", "concluir", "ignorar"]},
            "titulo": {"type": "string", "description": "só p/ criar: título no formato pedido"},
            "ref": {"type": "string", "description": "id da tarefa do ledger (ja_existe/concluir)"},
            "motivo": {"type": "string", "description": "uma frase curta"},
        },
        "required": ["n", "acao", "motivo"],
    }}},
    "required": ["itens"],
})

PROMPT = """Você mantém a lista de tarefas pessoais de {dono}. Uma reunião acabou de gerar action
items para ele. Decida, para CADA item abaixo, o que fazer na lista de tarefas.

Reunião: {data} · empresa {empresa} (sigla "{sigla}") · {titulo}

Itens de {dono} nesta reunião ([x] = já feito, segundo a própria reunião):
{itens}

Tarefas já existentes na lista (id · status · título · origem):
{ledger}

Regras:
- "ja_existe": o item é o MESMO trabalho de uma tarefa existente (aberta ou concluída), mesmo
  com outra redação. Dailies repetem o mesmo trabalho dia após dia — não duplique. Informe `ref`.
- "concluir": item marcado [x] que corresponde a uma tarefa ABERTA. Informe `ref`.
- "ignorar": item [x] sem tarefa aberta correspondente.
- "criar": trabalho novo e ainda aberto ([ ]). Mesma task de tracker (mesmo ID) com etapa
  claramente diferente (ex.: investigar × fazer deploy) pode virar tarefa nova; na dúvida, ja_existe.
- Título de "criar": se o item cita um ID de task (ex.: PRJ-123, OPS-45), comece com ele;
  senão comece com a sigla da empresa. Formato: "<ID ou sigla> - <verbo no infinitivo + objeto>",
  curto (até ~70 caracteres), sem prioridade/prazo, sem nome do próprio {dono}.
  Ex.: "PRJ-330 - Ajudar a Carla a subir o ambiente", "{sigla} - Cobrar acesso ao Redis de PRD".
"""


def sigla_de(empresa: str) -> str:
    return (_CFG.get("siglas") or {}).get(empresa) or (empresa[:1].upper() if empresa else "?")


def dono_items(tarefas_md: str) -> list[dict]:
    """Itens de checklist das seções `## ...` cujo título cita o dono."""
    items, in_dono = [], False
    for line in tarefas_md.splitlines():
        if line.startswith("#"):
            in_dono = line.startswith("## ") and DONO.lower() in line.lower()
            continue
        if not in_dono:
            continue
        m = ITEM_RE.match(line)
        if m:
            items.append({"feito": m.group(1).lower() == "x", "texto": m.group(2).strip()})
        elif items and line.startswith(("  ", "\t")) and line.strip():
            items[-1]["texto"] += " " + line.strip()
    return items


def fmt_ledger(ledger: list[dict]) -> str:
    if not ledger:
        return "(nenhuma)"
    return "\n".join(f"{t['id']} · {t['status']} · {t['titulo']} · {t['origem']}" for t in ledger)


def cmd_propose(a) -> int:
    mdir = os.path.abspath(a.dir)
    with open(os.path.join(mdir, "tarefas.md"), encoding="utf-8") as f:
        items = dono_items(f.read())
    fm: dict = {}
    if os.path.isfile(os.path.join(mdir, "ata.md")):
        with open(os.path.join(mdir, "ata.md"), encoding="utf-8") as f:
            fm, _ = split_frontmatter(f.read())
    empresa = fm.get("empresa") or ""
    sigla = sigla_de(empresa)
    origem = os.path.basename(mdir)
    ledger = load_json(a.ledger, []) or []

    decisions: list[dict] = []
    if items:
        listing = "\n".join(f"{i}. [{'x' if it['feito'] else ' '}] {it['texto']}"
                            for i, it in enumerate(items, 1))
        out = ask_claude(PROMPT.format(dono=DONO, data=fm.get("data", origem), empresa=empresa,
                                       sigla=sigla, titulo=fm.get("titulo", ""), itens=listing,
                                       ledger=fmt_ledger(ledger)), SCHEMA, mdir)
        decisions = sorted(out.get("itens") or [], key=lambda d: d.get("n", 0))

    by_id = {t["id"]: t for t in ledger}
    rows = []
    for d in decisions:
        n = d.get("n", 0)
        if not 1 <= n <= len(items):
            continue
        it, acao, ref = items[n - 1], d["acao"], (d.get("ref") or "").strip()
        titulo = (d.get("titulo") or "").strip()
        if acao == "criar":
            if not titulo:
                titulo = f"{sigla} - {it['texto'][:70]}"
            ref = f"t{len(ledger) + 1:03d}"
            ledger.append({"id": ref, "status": "aberta", "titulo": titulo, "origem": origem,
                           "empresa": empresa, "ids": sorted({m.upper() for m in
                                                              TASK_ID_RE.findall(it["texto"])}),
                           "notas": it["texto"]})
            by_id[ref] = ledger[-1]
        elif acao == "concluir" and ref in by_id:
            by_id[ref]["status"] = "concluida"
            by_id[ref]["concluida_em"] = origem
            titulo = by_id[ref]["titulo"]
        elif ref in by_id:
            titulo = by_id[ref]["titulo"]
        rows.append((acao, ref, titulo, it, d.get("motivo", "")))

    if a.ledger:
        save_json(a.ledger, ledger)

    icon = {"criar": "➕", "ja_existe": "⊘", "concluir": "✅", "ignorar": "·"}
    lines = [f"# Agenda — {origem} ({empresa or 'sem empresa'})", "",
             f"Simulação gerada em {date.today().isoformat()}. "
             f"{len(items)} item(ns) de {DONO}.", ""]
    if not rows:
        lines.append(f"_Nenhum item de {DONO} nesta reunião._")
    else:
        lines += ["| | Ação | Tarefa | Item da reunião | Motivo |", "|---|---|---|---|---|"]
        for acao, ref, titulo, it, motivo in rows:
            cell = it["texto"].replace("|", "\\|")
            lines.append(f"| {icon.get(acao, '?')} | {acao} | `{ref}` {titulo} | "
                         f"{'[x] ' if it['feito'] else ''}{cell} | {motivo} |")
    out_path = a.out or os.path.join(mdir, "agenda.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    counts = {k: sum(1 for r in rows if r[0] == k) for k in icon}
    print(f"{origem}: " + " ".join(f"{k}={v}" for k, v in counts.items()) + f" → {out_path}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("propose")
    s.add_argument("dir")
    s.add_argument("--ledger", required=True)
    s.add_argument("--out")
    a = p.parse_args()
    return {"propose": cmd_propose}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
