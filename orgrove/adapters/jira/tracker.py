"""JiraTracker: the `Tracker` port on Jira Cloud REST v3 (#276, epic #46).

Stage modes (orgrove.stage_modes): `workflow` maps each stage to its own Jira status; `hybrid` maps several stages to
a coarse status and keeps the fine stage as a label. `labels` mode has nothing to map and is refused here.
Flags are Jira labels, with `/` written `-` (Jira labels can't hold a slash): one table, `label_for` / `flag_for`.

A status change always asks Jira for that issue's transitions and posts the one that reaches the mapped status; ids
are never cached across issues, because workflows differ per issue type. Every call is limited to one project key.
"""
import re
from urllib.parse import quote

from orgrove import stage_modes, stages
from orgrove.adapters.jira.http import Client, JiraError
from orgrove.ports import Capability, Item, NotFound, PortError

PROJECT = re.compile(r"[A-Z][A-Z0-9_]{1,9}")
ISSUE_KEY = re.compile(r"[A-Z][A-Z0-9_]{1,9}-[0-9]+")
JQL_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _.:()-]{0,79}")    # a status or label: no quote, backslash, or operator
FIELDS = ["summary", "status", "labels", "parent", "assignee", "project"]
PAGE = 100


def _jql(value, pattern=JQL_WORD):
    """A value as a quoted JQL string, only if it matches the strict pattern; titles and bodies never come here."""
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f"not safe to put in a JQL query: {value!r}")
    return f'"{value}"'


def _adf(text):
    """Plain text as an Atlassian Document Format document, one paragraph per line."""
    paragraphs = [{"type": "paragraph", "content": [{"type": "text", "text": line}]} if line else
                  {"type": "paragraph"} for line in text.split("\n")]
    return {"type": "doc", "version": 1, "content": paragraphs}


def _adf_text(node):
    if not isinstance(node, dict):
        return ""
    if node.get("type") == "text":
        return str(node.get("text", ""))
    if node.get("type") == "hardBreak":
        return "\n"
    parts = [_adf_text(c) for c in node.get("content") or []]
    return "\n".join(parts) if node.get("type") in ("doc", "bulletList", "orderedList") else "".join(parts)


def _object(payload, where):
    if not isinstance(payload, dict):
        raise JiraError(f"{where}: unexpected answer shape")
    return payload


class JiraTracker:
    def __init__(self, transport, project, mode, stage_map, stages_cfg=None, clock=None, sleep=None,
                 issue_type="Task", subtask_type="Subtask"):
        if not isinstance(project, str) or not PROJECT.fullmatch(project):
            raise ValueError("Jira project key must look like ORG")
        self._cfg = stages_cfg or stages.load()
        problems = stage_modes.validate(self._cfg, mode, stage_map)
        if mode == "labels":
            problems.append("the Jira tracker needs stage_mode = workflow or hybrid")
        if problems:
            raise ValueError("; ".join(problems))
        self._project, self._mode, self._map = project, mode, dict(stage_map)
        self._issue_type, self._subtask_type = issue_type, subtask_type
        kwargs = {} if sleep is None else {"sleep": sleep}
        self._client = Client(transport, **kwargs)
        self._keys = {}                 # id -> issue key, for links and messages
        self._init_tables()

    # --- label tables (one place, tested both ways) ---
    def _init_tables(self):
        self._flag_to_label = {f: f.replace("/", "-") for f in self._cfg["flags"]}
        self._stage_to_label = {s["id"]: s["label"].replace("/", "-") for s in self._cfg["stage"]}
        both = list(self._flag_to_label.values()) + list(self._stage_to_label.values())
        if len(set(both)) != len(both):
            raise ValueError("two flags or stages would share one Jira label")
        self._label_to_flag = {v: k for k, v in self._flag_to_label.items()}
        self._label_to_stage = {v: k for k, v in self._stage_to_label.items()}

    def label_for(self, flag):
        return self._flag_to_label[flag]

    def flag_for(self, label):
        return self._label_to_flag.get(label)

    def key_of(self, item_id):
        """The issue key (ORG-12) of an item this tracker has read or created, for links."""
        return self._keys.get(item_id)

    # --- Tracker ---
    def capabilities(self):
        return frozenset({Capability.NATIVE_STATES, Capability.SUB_ITEMS, Capability.ASSIGNEES})

    def ensure_schema(self, stages_cfg):
        """Labels need no setup in Jira; check that every mapped status exists in the project."""
        self._cfg = stages_cfg
        self._init_tables()
        path = f"/rest/api/3/project/{self._project}/statuses"
        known = {s.get("name") for kind in self._list(self._client.call("GET", path), path)
                 for s in _object(kind, path).get("statuses", [])}
        missing = sorted(set(self._map.values()) - known)
        if missing:
            raise ValueError(f"project {self._project} has no Jira status {missing}")

    def list(self, stage=None):
        if stage is not None and stage not in self._map:
            raise ValueError(f"unknown stage {stage!r}")
        jql = f"project = {_jql(self._project, PROJECT)}"
        if stage is not None:
            jql += f" AND status = {_jql(self._map[stage])}"
        return [i.id for i in self._search(jql + " ORDER BY id ASC") if stage is None or i.stage == stage]

    def get(self, item_id):
        item = self._item(self._issue(item_id))
        if item is None:
            raise PortError(f"issue {self._keys.get(item_id, item_id)} is in a status the stage map doesn't cover")
        return item

    def set_stage(self, item_id, stage):
        if stage not in self._map:
            raise ValueError(f"unknown stage {stage!r}")
        issue = self._issue(item_id)
        fields = issue["fields"]
        current, target = fields["status"]["name"], self._map[stage]
        if current != target:
            self._transition(item_id, issue["key"], current, target)
        if self._mode == "hybrid":
            labels = set(fields.get("labels", []))
            keep = self._stage_to_label[stage] if stage not in stage_modes.OPTIONAL_STAGES else None
            ops = [{"remove": x} for x in labels & set(self._label_to_stage) if x != keep]
            if keep and keep not in labels:
                ops.append({"add": keep})
            if ops:
                self._put(item_id, {"update": {"labels": ops}})

    def flags(self, item_id):
        return self._flags_of(self._issue(item_id)["fields"].get("labels", []))

    def add_flag(self, item_id, flag):
        self._label_op(item_id, flag, "add")

    def remove_flag(self, item_id, flag):
        self._label_op(item_id, flag, "remove")

    def comment(self, item_id, body):
        self._issue(item_id)
        self._client.call("POST", f"/rest/api/3/issue/{item_id}/comment", {"body": _adf(body)})

    def comments(self, item_id):
        self._issue(item_id)
        out, start = [], 0
        while True:
            path = f"/rest/api/3/issue/{item_id}/comment?orderBy=created&startAt={start}&maxResults={PAGE}"
            page = _object(self._client.call("GET", path), path)
            found = page.get("comments", [])
            out += [_adf_text(c.get("body")) for c in found]
            start += len(found)
            if not found or start >= page.get("total", 0):
                return out

    def create_item(self, title, body="", parent=None):
        fields = {"project": {"key": self._project}, "summary": title,
                  "issuetype": {"name": self._subtask_type if parent is not None else self._issue_type}}
        if body:
            fields["description"] = _adf(body)
        if parent is not None:
            fields["parent"] = {"key": self._issue(parent)["key"]}
        made = _object(self._client.call("POST", "/rest/api/3/issue", {"fields": fields}), "create issue")
        try:
            item_id = int(made["id"])
        except (KeyError, TypeError, ValueError):
            raise JiraError("POST /rest/api/3/issue: the answer has no issue id") from None
        self._keys[item_id] = made.get("key")
        return item_id

    def children(self, item_id):
        key = self._issue(item_id)["key"]
        return [i.id for i in self._search(f"project = {_jql(self._project, PROJECT)} AND parent = {_jql(key, ISSUE_KEY)}")]

    def assign(self, item_id, login):
        key = self._issue(item_id)["key"]
        path = f"/rest/api/3/user/assignable/search?query={quote(login, safe='')}&issueKey={key}"
        users = self._list(self._client.call("GET", path), path)
        want = login.lower()
        hits = [u for u in users if isinstance(u, dict) and want in (
            str(u.get("accountId", "")).lower(), str(u.get("emailAddress", "")).lower(),
            str(u.get("displayName", "")).lower())]
        if len(hits) != 1:
            raise PortError(f"{len(hits)} assignable Jira users match {login!r} on {key}; need exactly one")
        self._client.call("PUT", f"/rest/api/3/issue/{item_id}/assignee", {"accountId": hits[0]["accountId"]})

    def native_state(self, item_id):
        """The issue's Jira status name."""
        return self._issue(item_id)["fields"]["status"]["name"]

    # --- helpers ---
    @staticmethod
    def _list(payload, where):
        if not isinstance(payload, list):
            raise JiraError(f"{where}: unexpected answer shape")
        return payload

    def _search(self, jql):
        found, token = [], None
        while True:
            body = {"jql": jql, "fields": FIELDS, "maxResults": PAGE}
            if token:
                body["nextPageToken"] = token
            page = _object(self._client.call("POST", "/rest/api/3/search/jql", body), "search")
            issues = [i for i in page.get("issues", []) if self._in_project(i)]    # a result from another project is dropped
            found += [i for i in map(self._item, issues) if i is not None]
            token = page.get("nextPageToken")
            if not token or page.get("isLast"):
                return found

    def _in_project(self, issue):
        fields = issue.get("fields") if isinstance(issue, dict) else None
        project = fields.get("project") if isinstance(fields, dict) else None
        return isinstance(project, dict) and project.get("key") == self._project

    def _issue(self, item_id):
        """The raw issue, refused unless it is in this tracker's project."""
        if not isinstance(item_id, int) or isinstance(item_id, bool) or item_id <= 0:
            raise NotFound(f"item {item_id!r} is not a Jira issue id")
        path = f"/rest/api/3/issue/{item_id}?fields={','.join(FIELDS)}"
        issue = _object(self._client.call("GET", path), path)
        fields = _object(issue.get("fields"), path)
        if _object(fields.get("project"), path).get("key") != self._project:
            raise NotFound(f"item {item_id} is not in project {self._project}")
        self._keys[item_id] = issue["key"]
        return issue

    def _flags_of(self, labels):
        return frozenset(f for f in map(self.flag_for, labels) if f)

    def _stage_of(self, state, labels):
        if self._mode == "hybrid":
            for label in labels:
                stage = self._label_to_stage.get(label)
                if stage and self._map.get(stage) == state:
                    return stage
        for stage in ("backlog", "done", *self._map):
            if self._map.get(stage) == state:
                return stage
        return None

    def _item(self, issue):
        fields = issue["fields"]
        labels = fields.get("labels", [])
        stage = self._stage_of(fields["status"]["name"], labels)
        if stage is None:
            return None
        item_id = int(issue["id"])
        self._keys[item_id] = issue.get("key")
        parent = (fields.get("parent") or {}).get("id")
        who = fields.get("assignee") or {}
        return Item(id=item_id, title=fields.get("summary", ""), stage=stage, flags=self._flags_of(labels),
                    parent=int(parent) if parent else None,
                    assignee=who.get("displayName") or who.get("accountId"))

    def _put(self, item_id, body):
        self._client.call("PUT", f"/rest/api/3/issue/{item_id}", body)

    def _label_op(self, item_id, flag, op):
        if flag not in self._flag_to_label:
            raise ValueError(f"unknown flag {flag!r}")
        self._issue(item_id)
        self._put(item_id, {"update": {"labels": [{op: self._flag_to_label[flag]}]}})

    def _transition(self, item_id, key, current, target):
        path = f"/rest/api/3/issue/{item_id}/transitions"
        found = _object(self._client.call("GET", path), path).get("transitions", [])
        hits = [t for t in found if isinstance(t, dict) and (t.get("to") or {}).get("name") == target]
        if not hits:
            raise PortError(f"issue {key}: no Jira transition reaches {target!r} from {current!r}")
        self._client.call("POST", path, {"transition": {"id": hits[0]["id"]}})
