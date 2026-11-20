"""Parse the machine block in a reviewer's verdict comment (skills/VERDICT.md)."""
import json
import re

BLOCK = re.compile(r"<!--\s*orgrove:verdict\s*(\{.*?\})\s*-->", re.S)
DECISIONS = {"qa": {"approve", "changes"}, "security": {"approve", "changes"}, "sanity": {"pass", "fail"}}


def problem(v):
    """Why a verdict breaks the rules, in plain words, or None: the same rules as parse, with the reason."""
    if not isinstance(v, dict):
        return "the verdict must be an object"
    role, decision = v.get("role"), v.get("decision")
    if role not in DECISIONS:
        return f"role {role!r} is not one of {', '.join(sorted(DECISIONS))}"
    if decision not in DECISIONS[role]:
        return f"decision {decision!r} is not one of {', '.join(sorted(DECISIONS[role]))} for {role}"
    findings = [f for f in v.get("findings", []) if isinstance(f, dict)]
    blocking = [(k, f) for k, f in enumerate(findings, 1) if f.get("severity") == "blocking"]
    for k, f in blocking:
        if not f.get("sub_issue"):
            return (f"finding {k} ({str(f.get('title', ''))[:60]}) is blocking, so it needs `sub_item` (its place in this "
                    "result's sub_items, starting at 0) or the `sub_issue` number of an issue that already exists")
    if decision in {"changes", "fail"} and not blocking:
        return f"a {decision} verdict needs at least one blocking finding"
    if decision == "approve" and blocking:
        return "an approve verdict cannot have a blocking finding"
    return None


def parse(body, expected_head=None):
    """The verdict dict, or None if missing, malformed, inconsistent or for another head."""
    blocks = BLOCK.findall(body or "")
    if len(blocks) != 1:
        return None
    try:
        v = json.loads(blocks[0])
    except json.JSONDecodeError:
        return None
    if v.get("decision") not in DECISIONS.get(v.get("role"), set()):
        return None
    if expected_head and v.get("head") != expected_head:
        return None
    blocking = [f for f in v.get("findings", []) if f.get("severity") == "blocking"]
    if any(not f.get("sub_issue") for f in blocking):
        return None
    if v["decision"] in {"changes", "fail"} and not blocking:
        return None
    if v["decision"] == "approve" and blocking:
        return None
    return v
