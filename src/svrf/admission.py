"""The admission check: the read that decides whether a pull request enters the train.

It returns a verdict dict:

    {"verdict": "MERGEABLE" | "HELD" | "LANDED", "residuals": [...], "changed": [...]}

Residual lines name why a head is held:

    merge:CONFLICT:<path>       git cannot merge it onto the base branch
    history:REFUSED:<class>     the history-order check refused it (if enabled)
    check:<line>                the configured admission command failed

A read that cannot be made raises `ReadFailed`; the train retries it next round and
never holds the pull request for it.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Callable

from . import history, rules
from .errors import ReadFailed
from .locks import SlotPool
from .redact import redact

UNAVAILABLE_EXITS = (126, 127)


class Admission:
    """`pool`/`slots`: when given, the admission command runs in its own worktree slot
    (the same shape as the gate's `CommandGate`) instead of the clone's single shared
    working tree, so concurrent admission reads for different heads never race each
    other's `git checkout`. Without a pool the command runs directly in `git.root`,
    exactly as before -- the historical single-threaded callers (`replay`) need no
    worktree of their own."""

    def __init__(self, git, *, order: str = "off", kind: Callable[[str], str] | None = None,
                 refactor_prefixes: tuple[str, ...] = ("refactor", "perf"), command: str = "",
                 timeout: float = 600, pool: Path | str | None = None, slots: int = 1):
        self.git, self.order, self.kind = git, order, kind
        self.refactor_prefixes, self.command, self.timeout = refactor_prefixes, command, timeout
        self.pool = SlotPool(Path(pool), slots) if pool is not None else None

    def __call__(self, head: str, base: str) -> dict:
        if self.git.is_ancestor(head, base):
            return {"verdict": "LANDED", "residuals": [], "changed": []}
        residuals: list[str] = []
        merge = self.git.merge_preview(base, head)
        if merge["status"] == "FAILED":
            raise ReadFailed(merge.get("reason") or "MERGE_PREVIEW_FAILED")
        residuals += [f"merge:CONFLICT:{p}" for p in merge["conflicts"]]
        changed = self.git.changed_paths(base, head)
        if self.order == "tests-first" and self.kind is not None:
            result = history.verdict(self.git, base, head, self.kind, self.refactor_prefixes)
            if result != "CLEAN":
                residuals.append(f"history:{result}")
        if self.command:
            residuals += self._command(head, base)
        return {"verdict": "HELD" if residuals else "MERGEABLE", "residuals": residuals, "changed": changed}

    def _command(self, head: str, base: str) -> list[str]:
        if self.pool is None:
            return self._run_command(head, base, self.git.root)
        slot = self.pool.acquire()
        try:
            return self._run_command(head, base, self._slot_root(slot))
        finally:
            self.pool.release(slot)

    def _slot_root(self, slot: Path) -> Path:
        """The slot's own worktree of `git.root`, created once and reused: a `git
        worktree` shares the object database, so no fetch is needed and every commit
        the shared clone already has is checked out-able here too."""
        if not (slot / ".git").exists():
            slot.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "-C", str(self.git.root), "worktree", "prune"], capture_output=True)
            subprocess.run(["git", "-C", str(self.git.root), "worktree", "add", "-q", "--detach", "-f",
                            str(slot), "HEAD"], check=True, capture_output=True)
        return slot

    def _run_command(self, head: str, base: str, root: Path) -> list[str]:
        env = {**os.environ, "SVRF_HEAD": head, "SVRF_BASE": base, "SVRF_CLONE": str(root)}
        try:
            done = subprocess.run(["bash", "-c", self.command], cwd=str(root), env=env,
                                  capture_output=True, text=True, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            raise ReadFailed("ADMISSION_COMMAND_TIMEOUT")
        if done.returncode in UNAVAILABLE_EXITS:
            raise ReadFailed(f"ADMISSION_COMMAND_UNAVAILABLE:{done.returncode}")
        if done.returncode == 0:
            return []
        output = redact(done.stdout + done.stderr, env)
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        # A line the command already shaped as a reland refusal (`reland:REFUSED:<class>`)
        # is passed through as-is, so reland_class reads it exactly like the built-in
        # tests-first check's own `history:REFUSED:<class>`; every other line is a plain
        # diagnostic and is wrapped under `check:`.
        residuals = [line[:200] if rules.RELAND_LINE.match(line[:200]) else f"check:{line[:200]}"
                     for line in lines[-5:]]
        return residuals or [f"check:exit={done.returncode}"]
