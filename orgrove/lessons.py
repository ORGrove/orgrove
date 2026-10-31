"""Lessons register (orgrove#151): `lessons/<project>.md`, read by every role run and kept by the daily retro.

A lesson is a `## L-NNN · title` section with `Rule`, `Check`, `Learned from`, `Added` and `Seen` lines.
Seen holds comma-separated dates. A date after Added means the lesson didn't prevent the mistake.
"""
import re
from datetime import date

from orgrove import manifest

HEAD = re.compile(r"^## (L-\d{3}) · (.+)$", re.M)
FIELD = re.compile(r"^- \*\*(Added|Seen):\*\* (.*)$", re.M)
DAY = re.compile(r"\d{4}-\d\d-\d\d")


def path(project):
    return manifest.ROOT / "lessons" / f"{project}.md"


def text(project):
    p = path(project)
    return p.read_text() if p.exists() else ""


def parse(md):
    """[{id, title, added, seen: [dates]}] in file order."""
    heads = list(HEAD.finditer(md))
    out = []
    for i, h in enumerate(heads):
        body = md[h.end():heads[i + 1].start() if i + 1 < len(heads) else len(md)]
        fields = {k: v for k, v in FIELD.findall(body)}
        out.append({"id": h.group(1), "title": h.group(2).strip(), "added": fields.get("Added", "").strip(),
                    "seen": DAY.findall(fields.get("Seen", ""))})
    return out


def repeats(lesson):
    """Dates the mistake happened again after the lesson was written."""
    return [d for d in lesson["seen"] if lesson["added"] and d > lesson["added"]]


def record_seen(md, lesson_id, day):
    """Add `day` to a lesson's Seen line (idempotent). Returns the new text."""
    heads = list(HEAD.finditer(md))
    for i, h in enumerate(heads):
        if h.group(1) != lesson_id:
            continue
        end = heads[i + 1].start() if i + 1 < len(heads) else len(md)
        body = md[h.end():end]
        m = re.search(r"^(- \*\*Seen:\*\* )(.*)$", body, re.M)
        if not m or day in DAY.findall(m.group(2)):
            return md
        body = body[:m.start()] + m.group(1) + ", ".join(sorted(set(DAY.findall(m.group(2))) | {day})) + body[m.end():]
        return md[:h.end()] + body + md[end:]
    raise KeyError(lesson_id)


def append(md, title, rule, check, learned_from, day=None):
    """Add a new lesson with the next free id. Returns (new text, id)."""
    ids = [int(x["id"][2:]) for x in parse(md)]
    lid = f"L-{(max(ids) if ids else 0) + 1:03d}"
    day = day or date.today().isoformat()
    block = (f"\n## {lid} · {title.strip()}\n- **Rule:** {rule.strip()}\n- **Check:** {check.strip()}\n"
             f"- **Learned from:** {learned_from.strip()}\n- **Added:** {day}\n- **Seen:** {day}\n")
    return md.rstrip("\n") + "\n" + block, lid


def for_prompt(project):
    """The block every role prompt carries; empty when the project has no lessons yet."""
    md = text(project)
    if not md.strip():
        return ""
    return ("Lessons from past mistakes on this project (each one cost real money; follow the ones that apply to "
            "this ticket):\n```markdown\n" + md.strip() + "\n```")
