"""Activity log on the `ledger` branch: one JSONL file per local day under events/.

Writes are optimistic: reset to the remote tip, read, decide, append, commit,
push. A rejected push means someone else wrote first, so the whole step is
retried on fresh data. That makes reserve-then-check atomic across parallel runs
without any lock. The branch is protected against force-push and deletion.
"""
import json
import subprocess
from pathlib import Path


class LedgerConflict(RuntimeError):
    pass


class GitLedger:
    def __init__(self, workdir, remote="origin", branch="ledger"):
        self.dir = Path(workdir)
        self.remote = remote
        self.branch = branch

    def _git(self, *args, check=True):
        return subprocess.run(
            ["git", "-C", str(self.dir), *args], check=check, capture_output=True, text=True
        )

    def sync(self):
        self._git("fetch", "-q", self.remote, self.branch)
        self._git("reset", "-q", "--hard", f"{self.remote}/{self.branch}")

    def events(self, month=None):
        """All events, or only those in files for one YYYY-MM month."""
        pattern = f"{month}-*.jsonl" if month else "*.jsonl"
        out = []
        for path in sorted((self.dir / "events").glob(pattern)):
            out += [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        return out

    def _append_local(self, day, event, subdir="events"):
        path = self.dir / subdir / f"{day}.jsonl"
        path.parent.mkdir(exist_ok=True)
        with path.open("a") as f:
            f.write(json.dumps(event, sort_keys=True) + "\n")
        self._git("add", str(path))
        self._git("commit", "-q", "-m", f"{event['type']} {event.get('run_id', '')}".strip())

    def transact(self, step, retries=8, subdir="events"):
        """Run step(ledger) -> (day, event | None, result) atomically against the remote.

        The event is appended to <subdir>/<day>.jsonl (events/ by default; shadow/ for dispatcher shadow diffs)."""
        for _ in range(retries):
            self.sync()
            day, event, result = step(self)
            if event is None:
                return result
            self._append_local(day, event, subdir)
            if self._git("push", "-q", self.remote, f"HEAD:{self.branch}", check=False).returncode == 0:
                return result
        raise LedgerConflict(f"could not write to {self.branch} after {retries} attempts")
