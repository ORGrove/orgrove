"""The ports: what the core needs from a platform, as `typing.Protocol` classes.

Adapters live under `orgrove/adapters/<platform>/` (later tickets). Ids are ints for items and PRs,
strings for runs, branches and lock keys. Time is epoch seconds from the clock the adapter was given,
so leases and `merged_since` are testable without sleeping.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from orgrove.ports.capabilities import Capability


@dataclass(frozen=True)
class Item:
    id: int
    title: str
    stage: str              # a stage id from config/stages.toml; "backlog" when no stage label is set
    flags: frozenset
    parent: int | None = None
    assignee: str | None = None


@dataclass(frozen=True)
class PullRequest:
    number: int
    branch: str
    state: str              # "open" | "closed" | "merged"
    head_sha: str


@dataclass(frozen=True)
class LedgerTotals:
    reserved_usd: float     # reserved and not yet settled
    spent_usd: float        # settled actuals


@runtime_checkable
class Tracker(Protocol):
    def capabilities(self) -> frozenset[Capability]: ...
    def ensure_schema(self, stages_cfg: dict) -> None:
        """Make the stages and flags in `stages_cfg` (the shape of `stages.load()`) exist. Idempotent."""
    def list(self, stage: str | None = None) -> list[int]: ...
    def get(self, item_id: int) -> Item: ...
    def set_stage(self, item_id: int, stage: str) -> None:
        """One stage at a time: the previous stage is removed. Unknown stage: ValueError."""
    def flags(self, item_id: int) -> frozenset[str]: ...
    def add_flag(self, item_id: int, flag: str) -> None: ...
    def remove_flag(self, item_id: int, flag: str) -> None: ...
    def comment(self, item_id: int, body: str) -> None: ...
    def comments(self, item_id: int) -> list[str]: ...
    def create_item(self, title: str, body: str = "", parent: int | None = None) -> int: ...
    def children(self, item_id: int) -> list[int]: ...
    def assign(self, item_id: int, login: str) -> None: ...


@runtime_checkable
class CodeHost(Protocol):
    def capabilities(self) -> frozenset[Capability]: ...
    def head_sha(self, branch: str) -> str: ...
    def diff(self, pr: int) -> str: ...
    def open_pr(self, branch: str, title: str, body: str = "", base: str = "main") -> int: ...
    def request_review(self, pr: int, reviewers: list[str]) -> None: ...
    def pr_state(self, pr: int) -> PullRequest: ...
    def merged_since(self, since: float) -> list[int]:
        """Numbers of PRs merged at or after `since` (epoch seconds), oldest first."""


@runtime_checkable
class Store(Protocol):
    def capabilities(self) -> frozenset[Capability]: ...
    def reserve(self, run_id: str, usd: float) -> None:
        """Reserve a run's full limit. A run id already seen: LedgerError."""
    def settle(self, run_id: str, actual_usd: float) -> None:
        """Settle a reservation once. Unknown run or already settled: LedgerError."""
    def totals(self) -> LedgerTotals: ...
    def acquire(self, key: str, holder: str, lease_s: float) -> bool:
        """True if `holder` now holds `key`. False while another holder's lease is live. Re-acquire renews."""
    def release(self, key: str, holder: str) -> bool:
        """True if `holder` held the lock and it is now free; False otherwise (never frees another's lock)."""


@runtime_checkable
class BoardMirror(Protocol):
    """Optional: mirrors stage and dates onto a board. Does nothing where the board is the tracker."""
    def capabilities(self) -> frozenset[Capability]: ...
    def mirror(self, item_id: int, stage: str) -> None: ...


@runtime_checkable
class Runner(Protocol):
    """Runs agent jobs on a CI system or a local process."""
    def capabilities(self) -> frozenset[Capability]: ...
    def submit(self, role: str, item_id: int, inputs: dict) -> str: ...
    def status(self, run_id: str) -> str: ...
    def cancel(self, run_id: str) -> None: ...
    def fetch_result(self, run_id: str) -> dict: ...


@runtime_checkable
class Notifier(Protocol):
    def capabilities(self) -> frozenset[Capability]: ...
    def notify(self, item_id: int, recipients: list[str], message: str) -> None: ...
