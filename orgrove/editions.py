"""ORGrove's editions (#159): the one table of what only the paid editions have.

Enterprise (owner, 2026-11-20) is every additional floor, customisation and voice. It ships in the same Pro build:
a Pro licence grants the Pro parts, an Enterprise licence every part (orgrove/licence.py, `bin/licence --parts`).

ORGrove Pro is all of ORGrove. ORGrove Community is the same commit with every part below removed (bin/package), so
the two never drift apart: one source, one set of skills, and this one table. Everything that differs between them
reads it:

- bin/package: the files a Community copy loses, and what each copy's Office must serve at every address below;
- the Office: what it hides (edition.js, data-pro="<part>"), and at a missing part's address, a page saying what the
  part is and where to get it, never a bare 404 (owner, 2026-10-31);
- the installer and doctor: what to skip when a part isn't there;
- tests: every Pro-only file and page belongs to exactly one part.

A new Pro part is one entry here, with its files and addresses: hiding it, its Pro page and its checks follow.
"""
from dataclasses import dataclass
from pathlib import Path

from orgrove import licence

ROOT = Path(__file__).resolve().parent.parent
PRO_URL = "https://orgrove.com/#waitlist"
ENTERPRISE_URL = "mailto:info@orgrove.com?subject=ORGrove%20Enterprise"


@dataclass(frozen=True)
class Part:
    name: str                       # what the Office's Pro page calls it
    about: str                      # one line on what it does
    files: tuple                    # what only Pro ships, relative to the repo
    pages: tuple = ()               # addresses a person opens: Community shows the Pro page there
    api: tuple = ()                 # addresses the Office's pages call: Community answers a short JSON note
    edition: str = "Pro"            # the edition it is sold in: Pro, or Enterprise (every additional floor; owner, 2026-11-20)

    def get(self):
        """(where to get it, the button's words) for the page at a missing part's address."""
        if self.edition == "Enterprise":
            return ENTERPRISE_URL, "Talk to us about ORGrove Enterprise"
        return PRO_URL, "Join the ORGrove Pro waitlist"


PARTS = {
    "meetings": Part(
        "Meetings with your agents",
        "Call the team into the meeting room and talk a decision through with them.",
        ("orgrove/meeting.py", "orgrove/voice.py", "orgrove/office_static/meeting.html", "config/meeting.toml"),
        pages=("/meeting", "/meeting.html"), api=("/api/meeting", "/api/transcribe")),
    "finance": Part(
        "Finance reports",
        "What the team costs, from Claude to GitHub and cloud bills, measured and checked against your caps.",
        ("orgrove/finance.py", "orgrove/office_static/finance.html", "config/costs.toml",
         "infra/finance-ce-readonly.yaml"),
        pages=("/finance", "/finance.html"), api=("/api/finance",)),
    "new_look": Part(
        "The new 3D office",
        "A detailed 3D office, where the team takes breaks and walks around between tickets.",
        ("orgrove/office_static/office3d_new.js", "orgrove/office_static/layout_new.js",
         "orgrove/office_static/preview3d.html", "orgrove/office_static/preview3d.js", "orgrove/office_static/models"),
        pages=("/preview3d", "/preview3d.html")),
    "news": Part(
        "News chats",
        "On a break, the team chats about the day's headlines.",
        ("orgrove/office_news.py",), api=("/api/news",)),
    "monitoring": Part(
        "The Monitoring floor",
        "A second floor where a team watches your production systems, with a local AI model so client data stays on your side.",
        ("orgrove/monitoring.py", "orgrove/checks.py", "orgrove/awsread.py", "orgrove/incidents.py", "orgrove/rca.py", "orgrove/monitor_setup.py",
         "infra/monitor-readonly.yaml", "bin/orgrove-monitor-setup", "orgrove/office_static/floor2.js", "orgrove/office_static/layout_floor2.js",
         "orgrove/office_static/floor2.html", "config/monitoring.toml"),
        pages=("/floor2", "/floor2.html"), api=("/api/floor2",), edition="Enterprise"),
    "wiki": Part(
        "The project wiki",
        "Nightly pages on the team's work, with your to-do list.",
        ("orgrove/wiki.py", "infra/support-cases-readonly.yaml")),
}


def files():
    """Every file only ORGrove Pro ships."""
    return [rel for part in PARTS.values() for rel in part.files]


def installed(part, root=ROOT):
    """Whether the part's files are here: all of them (ORGrove Pro), or none (Community)."""
    return all((root / rel).exists() for rel in PARTS[part].files)


def has(part, root=ROOT):
    """Whether this copy runs the part: its files are here and the licence grants it (orgrove/licence.py, #426).
    ORGrove's own source checkout needs no licence; an installed copy does."""
    return installed(part, root) and licence.status(root).grants(part)


def present(root=ROOT):
    return {name: has(name, root) for name in PARTS}


def unlicensed(part, root=ROOT):
    """The licence Status when the part's files are here but the licence doesn't let it run, else None."""
    if not installed(part, root):
        return None
    st = licence.status(root)
    return None if st.grants(part) else st


def missing_part(route, parts=None):
    """The part a route belongs to when this copy doesn't have it, else None."""
    parts = present() if parts is None else parts
    for name, part in PARTS.items():
        if not parts[name] and any(route == a or route.startswith(a + "/") for a in part.pages + part.api):
            return name
    return None
