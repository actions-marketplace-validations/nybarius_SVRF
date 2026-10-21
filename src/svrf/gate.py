"""The gate: your own commands, run on one folded tree in a worktree slot.

A family's folded commit is checked out (detached) in a slot of a pool of worktrees of
the train's clone; each slot is exclusive by a file lock, so two trains on one host
never share one. Untracked build caches in the slot are kept between gates (only
`git clean -fd` runs), so incremental builds stay incremental.

Environment each command sees:

    SVRF_BASE            the base commit the family was folded onto
    SVRF_COMMIT          the folded commit being gated
    SVRF_CHANGED_FILES   a file listing the paths the family changes, one per line
    SVRF_LABEL           the family id (F1, F2, ...)

`setup` commands run first, under one lock shared by every slot (use it for dependency
downloads into a shared cache). The gate is green iff every command exits 0. A gate
whose output shows it could not run (a network error, a full disk, a killed process, a
timeout) is a read failure: retried later, never counted red.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .locks import SlotPool, flocked
from .redact import redact
from .rules import gate_infra_failure

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def meminfo_available_gb() -> float:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / (1024 * 1024)
    except OSError:
        pass
    return 0.0


class MemoryGuard:
    """Admit a gate while the memory available, less what the gates already running may
    still take (`need_gb` each), covers one more gate plus a reserve."""

    def __init__(self, need_gb: float = 10, reserve_gb: float = 8, available_gb=meminfo_available_gb,
                 sleep=time.sleep, poll: float = 15):
        self.need_gb, self.reserve_gb = need_gb, reserve_gb
        self.available_gb, self.sleep, self.poll = available_gb, sleep, poll
        self.running = 0
        self.lock = threading.Lock()

    def fits(self, running: int) -> bool:
        return self.available_gb() - self.need_gb * running >= self.need_gb + self.reserve_gb

    @contextmanager
    def admit(self):
        while True:
            with self.lock:
                if self.fits(self.running):
                    self.running += 1
                    break
            self.sleep(self.poll)
        try:
            yield
        finally:
            with self.lock:
                self.running -= 1


class CommandGate:
    def __init__(self, clone: Path | str, pool: Path | str, slots: int, commands: list[str], *,
                 setup: list[str] = (), logs: Path | str, timeout_minutes: float = 60,
                 infra_patterns: list[str] = (), failing_pattern: str = r"error|FAIL|Traceback",
                 env: dict | None = None):
        self.clone = Path(clone).expanduser().resolve()
        self.pool = Path(pool).expanduser()
        self.logs = Path(logs).expanduser()
        self.commands, self.setup = list(commands), list(setup)
        self.timeout = timeout_minutes * 60 if timeout_minutes else None
        self.infra_patterns = list(infra_patterns)
        self.failing = re.compile(failing_pattern)
        self.slots = SlotPool(self.pool, slots)
        self.env = {**os.environ, **(env or {})}
        self.setup_lock = self.pool / "setup.lock"

    def _git(self, *args, cwd: Path | None = None) -> str:
        root = cwd or self.clone
        return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()

    def _prepare(self, slot: Path, commit: str) -> None:
        if not (slot / ".git").exists():
            slot.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "-C", str(self.clone), "worktree", "prune"], capture_output=True)
            self._git("worktree", "add", "-q", "--detach", "-f", str(slot), commit)
        # Index flags can hide changed bytes from status and skip-worktree can
        # also suppress force-checkout. Remove those flags under the slot lock;
        # a masked slot must be restored even when status previously looked clean.
        entries = subprocess.run(["git", "-C", str(slot), "ls-files", "-v", "-z"],
                                 check=True, capture_output=True).stdout.split(b"\0")
        assumed = [e[2:] for e in entries if e[:1].islower()]
        skipped = [e[2:] for e in entries if e[:1].upper() == b"S"]
        for option, paths in (("--no-assume-unchanged", assumed), ("--no-skip-worktree", skipped)):
            if paths:
                subprocess.run(["git", "-C", str(slot), "update-index", option, "-z", "--stdin"],
                               input=b"\0".join(paths) + b"\0", check=True, capture_output=True)
        head = subprocess.run(["git", "-C", str(slot), "rev-parse", "HEAD"],
                              capture_output=True, text=True)
        tracked = subprocess.run(["git", "-C", str(slot), "-c", "core.fsmonitor=false",
                                  "-c", "core.ignorestat=false", "-c", "core.trustctime=true",
                                  "status", "--porcelain", "--untracked-files=no"],
                                 capture_output=True, text=True)
        if not (not assumed and not skipped
                and head.returncode == 0 and head.stdout.strip() == commit
                and tracked.returncode == 0 and not tracked.stdout):
            self._git("checkout", "-q", "--detach", "-f", commit, cwd=slot)
        self._git("clean", "-fdq", cwd=slot)

    def _step(self, command: str, cwd: Path, log: Path, env: dict) -> tuple[int, str, float]:
        started = time.monotonic()
        with open(log, "w", encoding="utf-8") as out:
            try:
                done = subprocess.run(["bash", "-c", command], cwd=cwd, stdout=out, stderr=subprocess.STDOUT,
                                      env=env, timeout=self.timeout)
                rc = done.returncode
            except subprocess.TimeoutExpired:
                rc = None
        text = redact(log.read_text(encoding="utf-8", errors="replace"), env)
        log.write_text(text, encoding="utf-8")  # the log on disk is redacted too, not only what we return
        return rc, text, round(time.monotonic() - started, 3)

    def run(self, base: str, commit: str, label: str = "") -> dict:
        slot = self.slots.acquire()
        try:
            self._prepare(slot, commit)
            tree = self._git("rev-parse", "HEAD^{tree}", cwd=slot)
            logs = self.logs / f"{label or 'gate'}-{commit[:12]}"
            logs.mkdir(parents=True, exist_ok=True)
            changed = self._git("diff", "--name-only", "--no-renames", base, commit, cwd=slot)
            (logs / "changed.txt").write_text(changed + ("\n" if changed else ""), encoding="utf-8")
            env = {**self.env, "SVRF_BASE": base, "SVRF_COMMIT": commit, "SVRF_LABEL": label,
                   "SVRF_CHANGED_FILES": str(logs / "changed.txt")}
            steps: list[dict] = []
            result = {"green": False, "read_failure": None, "tree": tree, "slot": slot.name, "logs": str(logs),
                      "steps": steps, "failing": []}
            plan = [("setup", c) for c in self.setup] + [("gate", c) for c in self.commands]
            for index, (phase, command) in enumerate(plan):
                log = logs / f"{index:02d}-{phase}.log"
                if phase == "setup":
                    with flocked(self.setup_lock, block=True):
                        rc, text, seconds = self._step(command, slot, log, env)
                else:
                    rc, text, seconds = self._step(command, slot, log, env)
                lines = [ANSI.sub("", line) for line in text.splitlines()]
                steps.append({"phase": phase, "command": command, "rc": rc, "seconds": seconds,
                              "tail": (lines or [""])[-1][:200]})
                if rc is None:
                    result["read_failure"] = f"GATE_TIMEOUT:{command[:80]}"
                    return result
                if rc != 0:
                    result["read_failure"] = gate_infra_failure(text, self.infra_patterns)
                    result["failing"] = [line for line in lines if self.failing.search(line)][:12] or lines[-8:]
                    return result
            result["green"] = True
            return result
        finally:
            self.slots.release(slot)
