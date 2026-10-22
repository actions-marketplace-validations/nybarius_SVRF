"""File locks: one train owner per state directory, and one gate per worktree slot.

All locks are `flock(2)` locks on files, so they are released when the holding process
dies. No process is ever found by matching command lines.
"""

from __future__ import annotations

import fcntl
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .errors import check_stop

_OWN_FDS: set[int] = set()


def _inherited(path: Path) -> bool:
    """True when this process holds a descriptor on the lock file that it did not open
    itself, e.g. one `flock(1)` took and handed down (`flock LOCK svrf run --once`)."""
    target = os.path.realpath(path)
    try:
        fds = os.listdir("/proc/self/fd")
    except OSError:
        return False
    for fd in fds:
        try:
            if int(fd) not in _OWN_FDS and os.path.realpath(f"/proc/self/fd/{fd}") == target:
                return True
        except (OSError, ValueError):
            continue
    return False


@contextmanager
def flocked(path: Path | str, block: bool = False, *, stop_requested=None):
    """An exclusive flock on `path`; yields whether it is held (always True when blocking)."""
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+", encoding="utf-8")
    _OWN_FDS.add(handle.fileno())
    try:
        while True:
            check_stop(stop_requested)
            try:
                flags = fcntl.LOCK_EX | (0 if block and stop_requested is None else fcntl.LOCK_NB)
                fcntl.flock(handle, flags)
                break
            except BlockingIOError:
                if not block or stop_requested is None:
                    yield False
                    return
                time.sleep(0.25)
        try:
            check_stop(stop_requested)
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        _OWN_FDS.discard(handle.fileno())
        handle.close()


@contextmanager
def owner_lock(path: Path | str):
    """The single owner: an exclusive, non-blocking flock (or one inherited from a
    wrapper). Yields whether this process owns the train; a second owner never waits."""
    path = Path(path).expanduser()
    if path.exists() and _inherited(path):
        yield True
        return
    with flocked(path) as owned:
        yield owned


class SlotPool:
    """Gate worktree slots, each exclusive by an flock on `<pool>/slot-<i>.lock`, so two
    trains on one host never share a slot."""

    def __init__(self, pool: Path | str, slots: int, poll: float = 5, sleep=time.sleep):
        self.pool = Path(pool).expanduser()
        self.slots = [self.pool / f"slot-{i}" for i in range(max(1, slots))]
        self.poll, self.sleep = poll, sleep
        self.held: dict[Path, object] = {}
        self.lock = threading.Lock()

    def acquire(self, block: bool = True, *, stop_requested=None) -> Path | None:
        self.pool.mkdir(parents=True, exist_ok=True)
        while True:
            check_stop(stop_requested)
            with self.lock:
                for slot in self.slots:
                    if slot in self.held:
                        continue
                    handle = open(slot.with_name(slot.name + ".lock"), "a+", encoding="utf-8")
                    try:
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        handle.close()
                        continue
                    self.held[slot] = handle
                    return slot
            if not block:
                return None
            self.sleep(self.poll)

    def release(self, slot: Path) -> None:
        with self.lock:
            handle = self.held.pop(slot, None)
        if handle is not None:
            fcntl.flock(handle, fcntl.LOCK_UN)
            handle.close()
