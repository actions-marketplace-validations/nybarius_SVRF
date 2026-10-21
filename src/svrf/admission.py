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

import json
import os
import re
import subprocess
import sys
import time
import tempfile
from pathlib import Path, PurePosixPath
from typing import Callable

from . import history, rules
from .errors import ReadFailed
from .locks import SlotPool
from .redact import redact

UNAVAILABLE_EXITS = (126, 127)
# The projected reason a command names on its output before exiting 126: an unobserved IO charge, a full IO
# pool, a budgeted intake that yielded part-way, or an output it could not observe. Each is a read not made
# (retry_later), never a verdict.
UNAVAILABLE_CLASS = re.compile(
    r"^(IO_CHARGE_UNOBSERVED:\S+|IO_POOL_CAPACITY|INTAKE_BUDGET_YIELD:\d+/\d+|INTAKE_OUTPUT_UNOBSERVED:\S+)$")


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("DUPLICATE_KEY")
        result[key] = value
    return result


def _read_basis(path: Path, head: str, base: str) -> list[dict] | None:
    """Optional command evidence; a supplied but unreadable basis cannot seed a hold."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None  # Existing commands have no read-evidence protocol.
    except (OSError, UnicodeError) as error:
        raise ReadFailed("ADMISSION_READS_UNAVAILABLE") from error
    try:
        value = json.loads(text, object_pairs_hook=_unique)
        if (not isinstance(value, dict) or value.get("schema") != "svrf.admission-reads/1"
                or value.get("head") != head or value.get("base") != base
                or not isinstance(value.get("consumed"), list)):
            raise ValueError("OCCURRENCE")
        for row in value["consumed"]:
            if (not isinstance(row, dict) or set(row) != {"kind", "id"}
                    or row["kind"] not in ("PATH", "DIR", "CODE", "COMMIT", "BASE_NYMISH_PAYLOAD")
                    or not isinstance(row["id"], str) or not row["id"]
                    or any(ord(c) < 32 for c in row["id"])):
                raise ValueError("ROW")
            if row["kind"] == "COMMIT":
                continue
            if row["kind"] == "BASE_NYMISH_PAYLOAD":
                if not rules.is_base_payload_id(row["id"]):
                    raise ValueError("PAYLOAD")
                continue
            name = row["id"]
            if row["kind"] == "DIR":
                if not name.startswith("dir:"):
                    raise ValueError("DIRECTORY")
                name = name[4:]
            if (not name or name.startswith(":") or PurePosixPath(name).is_absolute()
                    or ".." in PurePosixPath(name).parts):
                raise ValueError("PATH")
        return value["consumed"]
    except (ValueError, TypeError, RecursionError) as error:
        raise ReadFailed("ADMISSION_READS_INVALID") from error


def _stop_command(proc: subprocess.Popen) -> None:
    """Ask the command's surviving owner to stop and reap its whole family."""
    import signal
    try:
        proc.send_signal(signal.SIGTERM)
    except ProcessLookupError:
        pass
    while True:
        try:
            proc.communicate(timeout=1)
            return
        except subprocess.TimeoutExpired:
            # A detached test can outlive its direct parent. Never kill the
            # subreaper or release the slot while it still owns child processes.
            if proc.poll() is not None:
                # The owner has reaped its family; an outside pipe holder cannot
                # keep a cancelled command's output read alive indefinitely.
                for stream in (proc.stdout, proc.stderr):
                    if stream is not None:
                        stream.close()
                return
        except ValueError:
            proc.wait()
            return


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

    def __call__(self, head: str, base: str, pr: int | None = None, *, cancelled=None) -> dict:
        if cancelled is not None and cancelled.is_set():
            raise ReadFailed("ADMISSION_CANCELLED")
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
        consumed = None
        if self.command:
            lines, consumed = self._command(head, base, pr, cancelled=cancelled)
            residuals += lines
        value = {"verdict": "HELD" if residuals else "MERGEABLE", "residuals": residuals, "changed": changed}
        if consumed is not None:
            value["consumed"] = consumed
        return value

    def _command(self, head: str, base: str, pr: int | None = None, *, cancelled=None) -> tuple[list[str], list[dict] | None]:
        if self.pool is None:
            return self._run_command(head, base, self.git.root, pr, cancelled=cancelled)
        slot = self.pool.acquire()
        try:
            return self._run_command(head, base, self._slot_root(slot), pr, cancelled=cancelled)
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

    def _run_command(self, head: str, base: str, root: Path, pr: int | None = None, *, cancelled=None) -> tuple[list[str], list[dict] | None]:
        with tempfile.TemporaryDirectory(prefix="svrf-admission-reads-") as scratch:
            return self._execute_command(head, base, root, Path(scratch) / "reads.json", pr, cancelled=cancelled)

    def _execute_command(self, head: str, base: str, root: Path,
                         reads: Path, pr: int | None = None, *, cancelled=None) -> tuple[list[str], list[dict] | None]:
        if cancelled is not None and cancelled.is_set():
            raise ReadFailed("ADMISSION_CANCELLED")
        env = {**os.environ, "SVRF_HEAD": head, "SVRF_BASE": base, "SVRF_CLONE": str(root),
               "SVRF_ADMISSION_READS": str(reads)}
        if pr is not None:
            env["SVRF_PR"] = str(pr)
        # A dedicated Linux subreaper owns this command, including test processes
        # that detach into their own sessions or outlive a worker-thread parent.
        # Its exit follows complete family reaping, so the original guards retain
        # physical custody through both cancellation and command timeout.
        supervisor = str(Path(__file__).with_name("_admission_process.py").resolve())
        proc = subprocess.Popen([sys.executable, "-I", supervisor, self.command], cwd=str(root), env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = max(0, deadline - time.monotonic())
            try:
                stdout, stderr = proc.communicate(
                    timeout=min(remaining, 0.25) if cancelled is not None else remaining)
                break
            except subprocess.TimeoutExpired:
                # Communication must finish, not just the command leader: a child
                # can keep inherited pipes open after its parent exits. Completed
                # output keeps its original result even if cancellation raced it.
                if cancelled is not None and cancelled.is_set():
                    _stop_command(proc)
                    raise ReadFailed("ADMISSION_CANCELLED")
                if time.monotonic() >= deadline:
                    _stop_command(proc)
                    raise ReadFailed("ADMISSION_COMMAND_TIMEOUT")
        done = subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)
        if done.returncode in UNAVAILABLE_EXITS:
            if done.returncode == 126:
                named = [line[:200] for line in (ln.strip() for ln in redact(done.stdout + done.stderr, env).splitlines())
                         if UNAVAILABLE_CLASS.match(line[:200])]
                if named:
                    raise ReadFailed(named[-1])
            raise ReadFailed(f"ADMISSION_COMMAND_UNAVAILABLE:{done.returncode}")
        consumed = _read_basis(reads, head, base)
        if done.returncode == 0:
            return [], consumed
        output = redact(done.stdout + done.stderr, env)
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        # A line the command already shaped as a reland refusal (`reland:REFUSED:<class>`)
        # is passed through as-is, so reland_class reads it exactly like the built-in
        # tests-first check's own `history:REFUSED:<class>`; every other line is a plain
        # diagnostic and is wrapped under `check:`.
        residuals = [line[:200] if rules.RELAND_LINE.match(line[:200]) else f"check:{line[:200]}"
                     for line in lines[-5:]]
        return residuals or [f"check:exit={done.returncode}"], consumed
