"""The ticket brief: what the next agent needs from a ticket's thread, built by code (plan
2026-11-05-agent-context-budget, step 3).

A long ticket's thread is read again by every PM, Dev and DevOps run: 48 comments and 51,000 characters on one client
ticket, most of it dispatcher notes and earlier review rounds. The brief keeps what decides the work and drops the rest:
  - the ticket body and **every owner comment, word for word**: the goal, the acceptance criteria and the owner's
    decisions are where the sense of the task lives, so they are never summarised (only the oldest owner comments
    are left out when they pass the cap, and the brief says so);
  - the latest review of each reviewer, from its machine block (skills/VERDICT.md): round, head, decision, findings;
  - the latest agent handoffs, word for word, and the dispatcher's last moves in one line each.
No model writes or summarises it, so it can't drift or invent. A run reads a full comment only when the brief points
at one or says it cut something.

    python -m orgrove.ticket_brief --repo owner/repo --issue 540     # prints the brief ("" if it can't read the ticket)
"""
import argparse
import json
import re
import subprocess

from orgrove import verdict

BODY_MAX, OWNER_MAX, HANDOFF_MAX, HANDOFFS, MOVES = 10000, 8000, 2500, 2, 4
ROLES = ("dev", "devops", "pm")         # pm covers pm-orgrove. QA and Security get the body and diff only; Delivery
                                        # mediates, so it reads every comment itself.
DISPATCHER = re.compile(r"^\s*orgrove dispatcher:", re.I)
REFUSED = "<!-- orgrove:result-refused"      # cli.REFUSED_MARKER: the note on a result that was refused
HIDDEN = re.compile(r"<!--.*?-->", re.S)


def for_role(role):
    return role in ROLES or role.startswith("pm")


def _fence(text):
    fence = "````" if "```" in text else "```"
    return f"{fence}markdown\n{text.strip()}\n{fence}"


def _cut(text, limit):
    text = text.strip()
    return text if len(text) <= limit else text[:limit].rsplit("\n", 1)[0] + "\n[... cut here: read the full comment in the thread]"


def build(issue, comments, owners, bot_login):
    """The brief for one ticket. issue: {number, title, body, labels: [{name}]}; comments oldest first."""
    owners = {o.lower() for o in owners}
    bot = bot_login.lower().removesuffix("[bot]")
    who = lambda c: ((c.get("user") or {}).get("login") or "").lower()  # noqa: E731
    is_bot = lambda c: who(c).removesuffix("[bot]") == bot  # noqa: E731
    labels = ", ".join(sorted(x["name"] for x in issue.get("labels", []))) or "none"
    lines = ["## Ticket brief", "",
             f"Made by ORGrove from #{issue['number']}'s thread by code; no model wrote or summarised it. It replaces "
             f"reading the thread ({len(comments)} comments, {sum(len(c.get('body') or '') for c in comments):,} "
             "characters): open a full comment only when this brief points at one or says it cut something.", "",
             f"**Ticket:** #{issue['number']} {issue.get('title', '')} · labels: {labels}", "",
             "**Body** (the goal and acceptance criteria, word for word):", _fence(_cut(issue.get("body") or "(empty)", BODY_MAX))]

    said = [c for c in comments if who(c) in owners]
    kept, size = [], 0
    for c in reversed(said):                       # the newest owner words matter most when the cap is reached
        if size + len(c.get("body") or "") > OWNER_MAX and kept:
            break
        kept.insert(0, c)
        size += len(c.get("body") or "")
    lines += ["", f"**Every owner comment, word for word** ({len(said)}):" if said else "**Owner comments:** none."]
    if len(kept) < len(said):
        lines.append(f"_{len(said) - len(kept)} older owner comment(s) left out for size: read them in the thread._")
    for c in kept:
        lines += [f"- {c['created_at'][:16].replace('T', ' ')} UTC, @{(c.get('user') or {}).get('login')}:", _fence(c.get("body") or "")]

    latest = {}
    for c in comments:
        v = verdict.parse(c.get("body") or "") if is_bot(c) else None
        if v:
            latest[v["role"]] = (c, v)
    if latest:
        lines += ["", "**Latest review per reviewer:**"]
        for role, (c, v) in sorted(latest.items()):
            found = "; ".join(f"{f.get('severity', '?')}: {f.get('title', '')}" + (f" ({f['file']})" if f.get("file") else "")
                              + (f" → #{f['sub_issue']}" if f.get("sub_issue") else "") for f in v.get("findings", []))
            lines.append(f"- {role}, round {v.get('round')}, head `{str(v.get('head'))[:7]}`: **{v['decision']}**"
                         + (f". Findings: {found}" if found else ", no findings") + f" ({c['created_at'][:10]})")

    agent = [c for c in comments if is_bot(c) and not DISPATCHER.match(c.get("body") or "")
             and not verdict.BLOCK.search(c.get("body") or "") and "<!-- ticket-cost -->" not in (c.get("body") or "")
             and REFUSED not in (c.get("body") or "")]
    refused = [c for c in comments if is_bot(c) and REFUSED in (c.get("body") or "")]
    if refused and not [c for c in agent if c["created_at"] > refused[-1]["created_at"]]:   # nothing done since: still to fix
        lines += ["", "**Your last result was refused, so nothing in it was done. Fix exactly this** (word for word):",
                  _fence(_cut(HIDDEN.sub("", refused[-1].get("body") or ""), HANDOFF_MAX))]
    if agent:
        lines += ["", f"**Latest agent handoff{'s' if len(agent[-HANDOFFS:]) > 1 else ''}** (word for word, newest last):"]
        for c in agent[-HANDOFFS:]:
            lines += [f"- {c['created_at'][:16].replace('T', ' ')} UTC:", _fence(_cut(HIDDEN.sub("", c.get("body") or ""), HANDOFF_MAX))]

    moves = [c for c in comments if is_bot(c) and DISPATCHER.match(c.get("body") or "")]
    if moves:
        lines += ["", "**Dispatcher's last moves:**"]
        lines += [f"- {c['created_at'][:16].replace('T', ' ')}: {(c.get('body') or '').strip().splitlines()[0][:200]}"
                  for c in moves[-MOVES:]]
    return "\n".join(lines)


def _gh(*args):
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True, timeout=120).stdout


def main(argv=None):
    from orgrove import manifest, office, org   # late: office imports half the package
    p = argparse.ArgumentParser()
    p.add_argument("--repo", required=True)
    p.add_argument("--issue", type=int, required=True)
    a = p.parse_args(argv)
    try:
        issue = json.loads(_gh("issue", "view", str(a.issue), "-R", a.repo, "--json", "number,title,body,labels"))
        comments = office.issue_comments(a.repo, a.issue)
    except (subprocess.SubprocessError, OSError, ValueError) as e:   # no brief: the run reads the thread as before
        print(f"ticket brief: not built ({e})", file=__import__("sys").stderr)
        return 0
    print(build(issue, comments, manifest.owners_for_repo(a.repo), org.BOT_LOGIN))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
