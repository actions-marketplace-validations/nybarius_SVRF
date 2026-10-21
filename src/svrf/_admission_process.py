"""Run one Linux admission command and retain custody of all its descendants.

This file is executed in a separate, single-threaded interpreter. Only this
process waits for its children; command output uses the inherited descriptors.
"""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


_PR_SET_CHILD_SUBREAPER = 36
_PR_GET_CHILD_SUBREAPER = 37
_INTERRUPT_GRACE = 0.25
_POLL_INTERVAL = 0.01


def _children() -> set[int]:
    # The supervisor has one thread. Reparented descendants join this exact
    # ownership list even when their creator ran in another thread or session.
    names = Path(f"/proc/self/task/{os.getpid()}/children").read_text(encoding="ascii").split()
    children = {int(name) for name in names}
    if any(pid <= 0 or pid == os.getpid() for pid in children):
        raise ValueError("invalid child ownership")
    return children


def _establish_custody() -> None:
    if sys.platform != "linux":
        raise OSError("Linux child ownership is required")
    libc = ctypes.CDLL(None, use_errno=True)
    prctl = libc.prctl
    prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    prctl.restype = ctypes.c_int
    if prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "PR_SET_CHILD_SUBREAPER")
    enabled = ctypes.c_int()
    address = ctypes.cast(ctypes.byref(enabled), ctypes.c_void_p).value
    if prctl(_PR_GET_CHILD_SUBREAPER, address, 0, 0, 0) != 0 or enabled.value != 1:
        raise OSError(ctypes.get_errno(), "PR_GET_CHILD_SUBREAPER")
    if _children():
        raise OSError("unexpected children before command launch")


def _unavailable(reason: str) -> int:
    print("INTAKE_OUTPUT_UNOBSERVED:ADMISSION_PROCESS_" + reason, file=sys.stderr, flush=True)
    return 126


def _command_status(returncode: int) -> int:
    if returncode >= 0:
        return returncode
    # All children have been reaped. Preserve a signal exit as a signal exit,
    # rather than changing the original command's status into a shell number.
    signum = -returncode
    if signum not in (signal.SIGKILL, signal.SIGSTOP):
        signal.signal(signum, signal.SIG_DFL)
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signum})
    os.kill(os.getpid(), signum)
    return 128 + signum  # defensive fallback if the signal did not terminate us


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        return _unavailable("ARGUMENT")
    cancelled = False

    def cancel(_signum, _frame):
        nonlocal cancelled
        cancelled = True

    try:
        signal.signal(signal.SIGTERM, cancel)
        signal.signal(signal.SIGINT, cancel)
        # An inherited SIG_IGN would auto-reap children and invalidate the PID
        # ownership interval between reading the list and signaling its members.
        signal.signal(signal.SIGCHLD, signal.SIG_DFL)
        _establish_custody()
    except (OSError, ValueError, AttributeError):
        return _unavailable("OWNERSHIP_UNAVAILABLE")
    if cancelled:
        return _unavailable("CANCELLED_BEFORE_LAUNCH")
    try:
        command = subprocess.Popen(["bash", "-c", args[0]], start_new_session=True)
    except OSError:
        return _unavailable("LAUNCH_UNAVAILABLE")

    known = {command.pid}
    interrupted: set[int] = set()
    deadline: float | None = None
    all_reaped = False
    custody_error = False
    while not all_reaped:
        try:
            if cancelled:
                if deadline is None:
                    deadline = time.monotonic() + _INTERRUPT_GRACE
                try:
                    children = _children()
                    known.update(children)
                except (OSError, ValueError):
                    # A later failed ownership read must not release live children.
                    # Keep known, unreaped children in custody and retry the read.
                    children = known.copy()
                signum = signal.SIGINT if time.monotonic() < deadline else signal.SIGKILL
                for pid in children:
                    if signum == signal.SIGINT and pid in interrupted:
                        continue
                    try:
                        os.kill(pid, signum)
                    except OSError:
                        # ESRCH is harmless for an exited child. Other failures also
                        # leave custody here: only ECHILD below can finish the owner.
                        pass
                    else:
                        if signum == signal.SIGINT:
                            interrupted.add(pid)
                # No wait/reap occurs between observing IDs and signaling them.
                # Even an exited child's PID cannot be reused during that interval.

            while True:
                try:
                    pid, status = os.waitpid(-1, os.WNOHANG)
                except InterruptedError:
                    continue
                except ChildProcessError:
                    all_reaped = True
                    break
                if pid == 0:
                    break
                known.discard(pid)
                interrupted.discard(pid)
                if pid == command.pid:
                    command.returncode = os.waitstatus_to_exitcode(status)
        except Exception:
            # An unexpected post-launch error cannot turn owner exit into a
            # false completion. Switch to cleanup and keep custody until ECHILD.
            custody_error = True
            cancelled = True
        if not all_reaped:
            time.sleep(_POLL_INTERVAL)
    if custody_error or command.returncode is None:
        return _unavailable("CUSTODY_UNAVAILABLE")
    return _command_status(command.returncode)



if __name__ == "__main__":
    raise SystemExit(main())
