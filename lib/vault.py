"""Publicação das reuniões no vault do Obsidian (só stdlib).

Subcomandos:
  meta <pasta_reuniao> [--empresa X]
      Garante o frontmatter do ata.md. Campos determinísticos (data, hora, duração) saem
      do nome da pasta e das durações; titulo/empresa/participantes/tags vêm do frontmatter
      que o Claude escreveu (analyze_meeting.sh) e, se faltarem (atas antigas), do próprio
      texto: título do H1, empresa por palavra-chave, participantes da seção "Participantes".
      Empresa sem sinal claro → `fallback` do empresas.json (e avisa, pra conferir).
  enrich <pasta_reuniao> [--force]
      Atas sem frontmatter do Claude (as antigas): 1 chamada curta (Haiku, sem ferramentas)
      que lê a ata e devolve empresa, título, participantes e tags. Depois roda o `meta`.
  gallery <pasta_reuniao>
      (Re)escreve a seção "Momentos" do ata.md a partir do moments.json, com links
      relativos pros prints. Idempotente (entre marcadores).
  publish <pasta_reuniao> [--move]
      Copia ata, tarefas, transcrição, SRTs finais, áudios e prints pra
      <vault>/Empresas/<empresa>/Calls/<pasta>/, confere tamanho de cada arquivo e,
      com --move, apaga a pasta de origem inteira (o resto é intermediário do pipeline).
  move <pasta_no_vault> <Empresa>
      Corrige a empresa de uma call já publicada: ajusta frontmatter/tags, move a pasta e
      regera o índice.
  cleanup [--base /data/shared/calls] [--min-seconds 60] [--yes]
      Limpa a pasta de gravação. Por padrão só LISTA (dry-run); com --yes apaga, pasta a pasta
      pelo caminho exato. Apaga: (a) reunião com ata cuja cópia no vault (ou na lixeira do
      Obsidian) tem todos os arquivos com o mesmo tamanho; (b) gravação sem ata mais curta que
      --min-seconds (teste/disparo acidental). MANTÉM: ata sem cópia conferida no vault, parte
      com vídeo pendente/.failed, gravação longa sem ata e vídeo solto na raiz.
  context <dono|empresas|contexto>
      Imprime um campo do empresas.json (usado pelo analyze_meeting.sh no prompt da ata).
  index
      Regera do zero `Empresas/<E>/Calls/Calls <E>.md` a partir do frontmatter das atas.
      Não usa Claude; pode rodar quantas vezes quiser.

Env: OBSIDIAN_VAULT (default /data/projects/Obsidian), EMPRESAS_CONFIG (default ../empresas.json)
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import claude_bin, fmt_ts, load_json, media_duration, part_durations, run  # noqa: E402

VAULT = os.environ.get("OBSIDIAN_VAULT", "/data/projects/Obsidian")
FIELDS = ("tipo", "data", "hora", "duracao_min", "empresa", "titulo", "aliases", "participantes", "tags")
GALLERY_START = "<!-- momentos:inicio -->"
GALLERY_END = "<!-- momentos:fim -->"
DIR_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})(?:_(\d+))?$")

# Empresas, palavras-chave e contexto do prompt vêm de empresas.json (não versionado);
# sem ele, cai no empresas.example.json. EMPRESAS_CONFIG sobrescreve o caminho.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_config() -> dict:
    path = os.environ.get("EMPRESAS_CONFIG") or os.path.join(_ROOT, "empresas.json")
    if not os.path.isfile(path):
        print(f"⚠ {path} não existe — usando empresas.example.json", file=sys.stderr)
        path = os.path.join(_ROOT, "empresas.example.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


_CFG = _load_config()
DONO = _CFG["dono"]
EMPRESAS = tuple(_CFG["empresas"])
FALLBACK_EMPRESA = _CFG["fallback"]
# Peso por ocorrência. Termos fortes (nome de produto/empresa) decidem; os fracos desempatam.
KEYWORDS = _CFG["keywords"]
EMPRESAS_CONTEXTO = "\n".join(_CFG["contexto"])


# ---------- frontmatter ----------

def split_frontmatter(text: str) -> tuple[dict, str]:
    m = re.match(r"^---\n(.*?)\n---\n?", text, re.S)
    if not m:
        return {}, text
    data, key = {}, None
    for line in m.group(1).splitlines():
        item = re.match(r"^\s+-\s+(.*)$", line)
        if item and key:
            data.setdefault(key, [])
            if isinstance(data[key], list):
                data[key].append(unquote(item.group(1)))
            continue
        kv = re.match(r"^([A-Za-z_]+):\s*(.*)$", line)
        if not kv:
            continue
        key, val = kv.group(1), kv.group(2).strip()
        if val.startswith("[") and val.endswith("]"):
            data[key] = [unquote(v) for v in val[1:-1].split(",") if v.strip()]
        elif val == "":
            data[key] = []
        else:
            data[key] = unquote(val)
    return data, text[m.end():]


def unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def yaml_scalar(v) -> str:
    s = str(v)
    if s == "" or re.search(r"[:#\[\]{},&*!|>'\"%@`]", s) or s.strip() != s or re.fullmatch(r"[\d.:-]+", s):
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


def render_frontmatter(data: dict) -> str:
    lines = ["---"]
    keys = [k for k in FIELDS if k in data] + [k for k in data if k not in FIELDS]
    for k in keys:
        v = data[k]
        if isinstance(v, list):
            lines.append(f"{k}: [{', '.join(yaml_scalar(x) for x in v)}]")
        elif k == "duracao_min" and str(v).isdigit():
            lines.append(f"{k}: {v}")
        else:
            lines.append(f"{k}: {yaml_scalar(v)}")
    lines.append("---")
    return "\n".join(lines) + "\n"


# ---------- campos derivados ----------

def srt_end(path: str) -> float:
    last = 0.0
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = re.match(r".*-->\s*(\d+):(\d+):(\d+)[,.](\d+)", line)
                if m:
                    h, mi, s, ms = map(int, m.groups())
                    last = max(last, h * 3600 + mi * 60 + s + ms / 1000)
    except OSError:
        pass
    return last


def duration_seconds(meeting_dir: str) -> float:
    parts = part_durations(meeting_dir)
    if parts:
        return sum(parts.values())
    for name in ("track1_mix.m4a", "track2_desktop.m4a", "track3_mic.m4a"):
        p = os.path.join(meeting_dir, name)
        if os.path.isfile(p):
            d = media_duration(p)
            if d > 0:
                return d
    return max((srt_end(p) for p in glob.glob(os.path.join(meeting_dir, "track*.srt"))), default=0.0)


def classify(text: str) -> tuple[str | None, dict]:
    low = text.lower()
    scores = {e: sum(w * len(re.findall(p, low)) for p, w in kw.items()) for e, kw in KEYWORDS.items()}
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    (top, s1), (_, s2) = ranked[0], ranked[1]
    if s1 >= 3 and s1 >= 2 * s2:
        return top, scores
    return None, scores


def clean_title(body: str, fallback: str) -> str:
    m = re.search(r"^#\s+(.+)$", body, re.M)
    if not m:
        return fallback
    t = re.sub(r"^Ata(\s+de\s+Reuni[ãa]o)?\s*[—–-]\s*", "", m.group(1).strip())
    return t or fallback


def parse_participants(body: str) -> list[str]:
    m = re.search(r"^##+\s*Participantes.*?\n(.*?)(?=^##?\s|\Z)", body, re.S | re.M)
    if not m:
        return []
    names = []
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line.startswith(("-", "*", "|")) or re.match(r"^\|[\s:-]+\|", line):
            continue
        bold = re.search(r"\*\*([^*]+)\*\*", line)
        if bold:
            name = bold.group(1)
        elif line.startswith("|"):
            name = line.strip("|").split("|")[0]
        else:
            name = re.split(r"\s[—–-]\s|:|\(", line.lstrip("-* "), maxsplit=1)[0]
        name = re.sub(r"\s*\(.*?\)", "", name).strip(" .*")
        if name and name.lower() not in ("pessoa", "nome", "participante") and len(name) <= 40:
            names.append(name)
    seen, out = set(), []
    for n in names:
        if n.lower() not in seen:
            seen.add(n.lower())
            out.append(n)
    return out[:15]


def normalize_empresa(v) -> str | None:
    if not v:
        return None
    for e in EMPRESAS:
        if str(v).strip().lower() == e.lower():
            return e
    return None


ENRICH_PROMPT = """Abaixo está a ata de uma reunião gravada (pasta {name}).

{contexto}

Devolva:
- empresa: {empresas};
- titulo: 3 a 8 palavras em PT-BR dizendo do que a reunião tratou (sem data, sem "Ata");
- participantes: nomes próprios das pessoas que FALARAM na reunião (inclua "{dono}" se ele
  participou); nada de placeholder ("Participante remoto", "Condutor", "Lord"); vazio se não
  houver nome;
- tags: 1 a 4 tags curtas, minúsculas, sem acento, com hífen (ex.: daily, suporte, seguranca).

ATA:
{ata}"""

ENRICH_SCHEMA = json.dumps({
    "type": "object",
    "properties": {
        "empresa": {"type": "string", "enum": list(EMPRESAS)},
        "titulo": {"type": "string"},
        "participantes": {"type": "array", "items": {"type": "string"}},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["empresa", "titulo", "participantes", "tags"],
})


def ask_haiku(prompt: str, schema: str, cwd: str) -> dict:
    bin_ = claude_bin()
    if not bin_:
        raise RuntimeError("claude não encontrado no PATH")
    r = run([bin_, "-p", "--model", os.environ.get("VAULT_ENRICH_MODEL", "haiku"),
             "--output-format", "json", "--json-schema", schema, "--permission-mode", "dontAsk",
             "--disallowedTools", "Bash", "Edit", "Write", "NotebookEdit", "WebFetch", "WebSearch",
             "Agent", "--", prompt], timeout=300, cwd=cwd)
    if r.returncode != 0:
        raise RuntimeError(f"claude exit {r.returncode}: {(r.stderr or r.stdout)[-300:]}")
    out = json.loads(r.stdout).get("structured_output")
    if not isinstance(out, dict):
        raise RuntimeError(f"claude sem structured_output: {r.stdout[:200]}")
    return out


# ---------- subcomandos ----------

def cmd_enrich(a) -> int:
    ata_path = os.path.join(a.dir, "ata.md")
    if not os.path.isfile(ata_path):
        print(f"✗ sem ata.md em {a.dir}")
        return 1
    with open(ata_path, encoding="utf-8") as f:
        fm, body = split_frontmatter(f.read())
    if not a.force and normalize_empresa(fm.get("empresa")) and fm.get("titulo"):
        print("⊘ frontmatter já completo")
        return cmd_meta(a)
    name = os.path.basename(os.path.normpath(a.dir))
    out = ask_haiku(ENRICH_PROMPT.format(name=name, contexto=EMPRESAS_CONTEXTO, dono=DONO,
                                    empresas=" ou ".join(EMPRESAS), ata=body[:15000]),
                    ENRICH_SCHEMA, a.dir)
    fm["empresa"] = normalize_empresa(out.get("empresa")) or FALLBACK_EMPRESA
    fm["titulo"] = out.get("titulo", "").strip() or fm.get("titulo") or name
    fm["participantes"] = [p.strip() for p in out.get("participantes", []) if p.strip()]
    fm["tags"] = [re.sub(r"[^a-z0-9-]", "", t.lower().replace(" ", "-")) for t in out.get("tags", [])]
    with open(ata_path, "w", encoding="utf-8") as f:
        f.write(render_frontmatter(fm) + "\n" + body.lstrip("\n"))
    return cmd_meta(a)


def cmd_meta(a) -> int:
    ata_path = os.path.join(a.dir, "ata.md")
    if not os.path.isfile(ata_path):
        print(f"✗ sem ata.md em {a.dir}")
        return 1
    with open(ata_path, encoding="utf-8") as f:
        fm, body = split_frontmatter(f.read())

    name = os.path.basename(os.path.normpath(a.dir))
    m = DIR_RE.match(name)
    if m:
        fm["data"] = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        fm["hora"] = f"{m.group(4)}:{m.group(5)}"
    fm["tipo"] = "call"
    dur = duration_seconds(a.dir)
    if dur > 0:
        fm["duracao_min"] = str(max(1, round(dur / 60)))

    if not fm.get("titulo"):
        fm["titulo"] = clean_title(body, name)

    empresa = normalize_empresa(a.empresa) or normalize_empresa(fm.get("empresa"))
    if not empresa:
        extra = ""
        tarefas = os.path.join(a.dir, "tarefas.md")
        if os.path.isfile(tarefas):
            with open(tarefas, encoding="utf-8") as f:
                extra = f.read()
        empresa, scores = classify(body + "\n" + extra)
        if not empresa:
            print(f"⚠ {name}: empresa ambígua {scores} → {FALLBACK_EMPRESA} (conferir)")
            empresa = FALLBACK_EMPRESA
    fm["empresa"] = empresa
    # Toda ata se chama ata.md: o alias é o que aparece no quick switcher e nos links.
    fm["aliases"] = [f"Call {fm.get('data', name)} {fm['titulo']}"]

    parts = fm.get("participantes")
    if not isinstance(parts, list) or not parts:
        fm["participantes"] = parse_participants(body)

    tags = fm.get("tags") if isinstance(fm.get("tags"), list) else []
    tags = [t.lstrip("#") for t in tags if t]
    for t in ("call", empresa.lower()):
        if t not in tags:
            tags.insert(0 if t == "call" else 1, t)
    fm["tags"] = tags

    with open(ata_path, "w", encoding="utf-8") as f:
        f.write(render_frontmatter(fm) + "\n" + body.lstrip("\n"))
    print(f"✓ {name}: {fm['empresa']} · {fm['titulo']} · {fm.get('duracao_min', '?')} min")
    return 0


def cmd_gallery(a) -> int:
    ata_path = os.path.join(a.dir, "ata.md")
    if not os.path.isfile(ata_path):
        print(f"✗ sem ata.md em {a.dir}")
        return 1
    with open(ata_path, encoding="utf-8") as f:
        text = f.read()
    text = re.sub(rf"\n*{re.escape(GALLERY_START)}.*?{re.escape(GALLERY_END)}\n*", "\n", text, flags=re.S)

    data = load_json(os.path.join(a.dir, "moments.json"), {}) or {}
    blocks = []
    for mo in data.get("moments", []):
        img = mo.get("image") or ""
        rel = os.path.join("moments", os.path.basename(img))
        if not os.path.isfile(os.path.join(a.dir, rel)):
            continue
        quote = re.sub(r"\s+", " ", mo.get("quote", "")).strip()
        if len(quote) > 280:
            quote = quote[:277].rstrip() + "…"
        who = f" — {mo['speaker']}" if mo.get("speaker") else ""
        blocks.append(f"### [{fmt_ts(float(mo.get('t', 0)))}] {mo.get('label', '')}\n\n"
                      f"![{mo.get('label', '')}]({rel.replace(' ', '%20')})\n\n> {quote}{who}\n")
    if blocks:
        text = (text.rstrip("\n") + f"\n\n{GALLERY_START}\n## Momentos\n\n"
                + "\n".join(blocks) + f"{GALLERY_END}\n")
    with open(ata_path, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"✓ galeria com {len(blocks)} print(s)")
    return 0


def files_to_publish(src: str) -> list[str]:
    rels = []
    for name in ("ata.md", "tarefas.md", "transcricao.txt",
                 "track1_mix.srt", "track2_desktop.srt", "track3_mic.srt"):
        if os.path.isfile(os.path.join(src, name)):
            rels.append(name)
    rels += sorted(os.path.basename(p) for p in glob.glob(os.path.join(src, "*.m4a")))
    for p in sorted(glob.glob(os.path.join(src, "moments", "*"))):
        if p.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
            rels.append(os.path.join("moments", os.path.basename(p)))
    return rels


def dest_dir(src: str) -> str:
    with open(os.path.join(src, "ata.md"), encoding="utf-8") as f:
        fm, _ = split_frontmatter(f.read())
    empresa = normalize_empresa(fm.get("empresa"))
    if not empresa:
        raise SystemExit(f"✗ {src}: ata sem empresa no frontmatter — rode `meta` antes")
    return os.path.join(VAULT, "Empresas", empresa, "Calls", os.path.basename(os.path.normpath(src)))


def cmd_publish(a) -> int:
    src = os.path.realpath(a.dir)
    if not os.path.isfile(os.path.join(src, "ata.md")):
        print(f"✗ sem ata.md em {src}")
        return 1
    dst = dest_dir(src)
    rels = files_to_publish(src)
    for rel in rels:
        os.makedirs(os.path.dirname(os.path.join(dst, rel)), exist_ok=True)
        shutil.copy2(os.path.join(src, rel), os.path.join(dst, rel))
    bad = [r for r in rels if os.path.getsize(os.path.join(src, r)) != os.path.getsize(os.path.join(dst, r))]
    if bad:
        print(f"✗ cópia divergente, origem mantida: {bad}")
        return 1
    print(f"✓ {len(rels)} arquivo(s) → {dst}")
    if a.move:
        shutil.rmtree(src)
        print(f"🗑 origem removida: {src}")
    return 0


def cmd_move(a) -> int:
    src = os.path.realpath(a.dir)
    empresa = normalize_empresa(a.empresa)
    if not empresa or not os.path.isfile(os.path.join(src, "ata.md")):
        print(f"✗ uso: move <pasta_da_call_no_vault> <{'|'.join(EMPRESAS)}>")
        return 1
    ata_path = os.path.join(src, "ata.md")
    with open(ata_path, encoding="utf-8") as f:
        fm, body = split_frontmatter(f.read())
    old = normalize_empresa(fm.get("empresa"))
    fm["empresa"] = empresa
    tags = [t for t in (fm.get("tags") or []) if t != (old or "").lower()]
    fm["tags"] = tags[:1] + [empresa.lower()] + tags[1:] if tags[:1] == ["call"] else ["call", empresa.lower()] + tags
    with open(ata_path, "w", encoding="utf-8") as f:
        f.write(render_frontmatter(fm) + "\n" + body.lstrip("\n"))
    dst = os.path.join(VAULT, "Empresas", empresa, "Calls", os.path.basename(src))
    if os.path.exists(dst):
        print(f"✗ destino já existe: {dst}")
        return 1
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.move(src, dst)
    print(f"✓ {os.path.basename(src)}: {old} → {empresa}")
    return cmd_index(a)


def vault_copy(name: str) -> str | None:
    """Pasta da call no vault (Empresas/*/Calls) ou na lixeira do Obsidian (.trash/*/*)."""
    for pattern in (os.path.join(VAULT, "Empresas", "*", "Calls", name),
                    os.path.join(VAULT, ".trash", "*", "*", name)):
        hits = [h for h in glob.glob(pattern) if os.path.isfile(os.path.join(h, "ata.md"))]
        if hits:
            return hits[0]
    return None


def dir_size(path: str) -> int:
    return sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(path) for f in fs)


def human(n: float) -> str:
    for unit in ("B", "K", "M", "G"):
        if n < 1024 or unit == "G":
            return f"{n:.0f}{unit}" if unit in ("B", "K") else f"{n:.1f}{unit}"
        n /= 1024
    return str(n)


def cleanup_verdict(src: str, min_seconds: float) -> tuple[bool, str]:
    name = os.path.basename(src)
    if glob.glob(os.path.join(src, "part[0-9][0-9][0-9].failed")) or any(
            glob.glob(os.path.join(src, f"part[0-9][0-9][0-9].{ext}")) for ext in ("mkv", "mp4", "mov", "webm")):
        return False, "parte com vídeo pendente/.failed — reprocessar"
    if os.path.isfile(os.path.join(src, "ata.md")):
        dst = vault_copy(name)
        if not dst:
            return False, "ata sem cópia no vault — rodar publish"
        missing = [r for r in files_to_publish(src)
                   if not os.path.isfile(os.path.join(dst, r))
                   or os.path.getsize(os.path.join(dst, r)) != os.path.getsize(os.path.join(src, r))]
        # a ata do vault pode ter sido editada (empresa corrigida, galeria): só exige que exista
        missing = [r for r in missing if r != "ata.md"]
        if missing:
            return False, f"cópia no vault incompleta ({len(missing)} arquivo(s), ex.: {missing[0]})"
        where = "lixeira do Obsidian" if "/.trash/" in dst else os.path.relpath(dst, VAULT)
        return True, f"já está em {where}"
    dur = duration_seconds(src)
    if dur < min_seconds:
        return True, f"sem ata, {dur:.0f}s (< {min_seconds:.0f}s)"
    return False, f"sem ata e {dur / 60:.0f} min — processar (backfill)"


def cmd_cleanup(a) -> int:
    base = os.path.realpath(a.base)
    delete, keep, total = [], [], 0
    for name in sorted(os.listdir(base)):
        path = os.path.join(base, name)
        if os.path.isdir(path) and DIR_RE.match(name):
            ok, why = cleanup_verdict(path, a.min_seconds)
            size = dir_size(path)
            (delete if ok else keep).append((name, size, why))
            total += size if ok else 0
        elif os.path.isfile(path) and path.lower().endswith((".mkv", ".mp4", ".mov", ".webm")):
            keep.append((name, os.path.getsize(path), "vídeo solto — processar (process_split/backfill)"))
    for title, rows in (("APAGAR", delete), ("MANTER", keep)):
        print(f"\n== {title} ({len(rows)}) ==")
        for name, size, why in rows:
            print(f"  {name:22} {human(size):>7}  {why}")
    print(f"\nTotal a liberar: {human(total)} em {len(delete)} pasta(s).")
    if not a.yes:
        print("(dry-run — nada apagado; rode com --yes pra apagar)")
        return 0
    for name, _, _ in delete:
        shutil.rmtree(os.path.join(base, name))
    print(f"🗑 {len(delete)} pasta(s) apagada(s).")
    return 0


def cmd_index(a) -> int:
    for empresa in EMPRESAS:
        calls_dir = os.path.join(VAULT, "Empresas", empresa, "Calls")
        rows = []
        for ata in glob.glob(os.path.join(calls_dir, "*", "ata.md")):
            with open(ata, encoding="utf-8") as f:
                fm, _ = split_frontmatter(f.read())
            folder = os.path.basename(os.path.dirname(ata))
            rows.append((fm.get("data", ""), fm.get("hora", ""), folder, fm))
        if not rows:
            continue
        rows.sort(key=lambda r: (r[0], r[1], r[2]), reverse=True)
        total_min = sum(int(r[3].get("duracao_min") or 0) for r in rows)
        out = [f"# 📞 Calls — {empresa}", "",
               f"> Gerado por `obs-transcripter-plugin/lib/vault.py index` — **não editar à mão** (é reescrito a cada call).",
               f"> {len(rows)} call(s), ~{total_min // 60}h{total_min % 60:02d} gravadas.", ""]
        month = None
        for data, hora, folder, fm in rows:
            if data[:7] != month:
                month = data[:7]
                out += ["", f"## {month}", "",
                        "| Data | Hora | Min | Call | Participantes | Tarefas |",
                        "|---|---|---:|---|---|---|"]
            title = str(fm.get("titulo", folder)).replace("|", "\\|")
            parts = fm.get("participantes") if isinstance(fm.get("participantes"), list) else []
            who = ", ".join(parts[:6]) + (f" +{len(parts) - 6}" if len(parts) > 6 else "")
            base = f"Empresas/{empresa}/Calls/{folder}"
            tarefas = f"[[{base}/tarefas\\|tarefas]]" if os.path.isfile(os.path.join(calls_dir, folder, "tarefas.md")) else ""
            day = f"{data[8:10]}/{data[5:7]}" if len(data) == 10 else data
            out.append(f"| {day} | {hora} | {fm.get('duracao_min', '')} | [[{base}/ata\\|{title}]] "
                       f"| {who.replace('|', '/')} | {tarefas} |")
        path = os.path.join(calls_dir, f"Calls {empresa}.md")
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n")
        os.replace(path + ".tmp", path)
        print(f"✓ {path} ({len(rows)} calls)")
    return 0


def cmd_context(a) -> int:
    """Imprime um campo do empresas.json para os scripts shell montarem o prompt."""
    print({"dono": DONO, "empresas": " | ".join(EMPRESAS), "contexto": EMPRESAS_CONTEXTO}[a.field])
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("meta")
    s.add_argument("dir")
    s.add_argument("--empresa")
    s = sub.add_parser("enrich")
    s.add_argument("dir")
    s.add_argument("--force", action="store_true")
    s.set_defaults(empresa=None)
    s = sub.add_parser("gallery")
    s.add_argument("dir")
    s = sub.add_parser("publish")
    s.add_argument("dir")
    s.add_argument("--move", action="store_true")
    s = sub.add_parser("move")
    s.add_argument("dir")
    s.add_argument("empresa")
    s = sub.add_parser("cleanup")
    s.add_argument("--base", default="/data/shared/calls")
    s.add_argument("--min-seconds", type=float, default=float(os.environ.get("MIN_MEETING_SECONDS", "60")))
    s.add_argument("--yes", action="store_true")
    sub.add_parser("index")
    s = sub.add_parser("context")
    s.add_argument("field", choices=("dono", "empresas", "contexto"))
    a = ap.parse_args()
    return {"meta": cmd_meta, "enrich": cmd_enrich, "gallery": cmd_gallery, "move": cmd_move, "publish": cmd_publish, "index": cmd_index, "cleanup": cmd_cleanup, "context": cmd_context}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
