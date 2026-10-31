"""Parse the machine block in a reviewer's verdict comment (skills/VERDICT.md)."""
import json
import re

BLOCK = re.compile(r"<!--\s*orgrove:verdict\s*(\{.*?\})\s*-->", re.S)
DECISIONS = {"qa": {"approve", "changes"}, "security": {"approve", "changes"}, "sanity": {"pass", "fail"}}


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
