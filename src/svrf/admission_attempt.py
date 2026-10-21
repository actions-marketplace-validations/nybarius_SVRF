"""Durable observations of an admission's source, command and completion.

The records describe owned work. They never decide an admission or stop a process.
"""
from __future__ import annotations

from contextvars import ContextVar
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
from threading import RLock
import time
import uuid

_CURRENT = ContextVar("svrf_admission_attempt", default=None)
MAX_PROGRESS_BYTES = 8 * 1024 * 1024


def process_identity(pid: int) -> dict:
    value = {"pid": pid}
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
        value["start_ticks"] = int(text[text.rfind(")") + 2:].split()[19])
        value["boot_id"] = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except (OSError, ValueError, IndexError) as error:
        value["read_failure"] = type(error).__name__
    return value


def read_progress(path: Path) -> dict:
    """Read a bounded original event file; absence is an unobserved read."""
    result = {"path": str(path), "status": "UNOBSERVED"}
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return {**result, "reason": "PROGRESS_NOT_REGULAR"}
            data = stream.read(MAX_PROGRESS_BYTES + 1)
    except FileNotFoundError:
        return {**result, "reason": "PROGRESS_FILE_ABSENT"}
    except OSError as error:
        return {**result, "reason": "PROGRESS_READ_FAILED:" + type(error).__name__}
    if len(data) > MAX_PROGRESS_BYTES:
        return {**result, "reason": "PROGRESS_READ_LIMIT"}
    result.update(sha256=hashlib.sha256(data).hexdigest(), bytes=len(data))
    try:
        if not data or not data.endswith(b"\n"):
            raise ValueError("PROGRESS_INCOMPLETE")
        last = json.loads(data.splitlines()[-1])
        if not isinstance(last, dict):
            raise ValueError("PROGRESS_EVENT_INVALID")
    except (UnicodeError, ValueError) as error:
        return {**result, "reason": type(error).__name__}
    return {**result, "status": "OBSERVED", "last_event": last}


class AdmissionAttempt:
    def __init__(self, state_dir: Path, pr: int, head: str, base: str, *, clock=time.time, persist=True):
        self.clock, self.persist = clock, persist
        self._lock = RLock()
        self._finished = None
        self._started = time.monotonic()
        root = Path(state_dir).resolve()
        attempt_id = uuid.uuid4().hex
        self.path = root / "admission-attempts" / f"{attempt_id}.json"
        self.progress_file = root / "admission-progress" / f"pr-{pr}" / f"{attempt_id}.jsonl"
        self._data = {
            "schema_version": "svrf.admission-attempt/1", "attempt_id": attempt_id,
            "pr": pr, "head": head, "base": base, "owner": process_identity(os.getpid()),
            "started_at": clock(), "started_monotonic": self._started, "stage": "queued",
            "record_file": str(self.path), "progress_file": str(self.progress_file),
        }
        self.snapshot()  # Persist the source before an executor can start it.

    def _write(self, value: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                             prefix="." + self.path.stem + "-", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(value, stream, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self.path)
            temporary = None
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def snapshot(self) -> dict:
        with self._lock:
            value = deepcopy(self._data)
            value["observed_at"] = self.clock()
            value["elapsed_seconds"] = round(max(0.0, (self._finished or time.monotonic()) - self._started), 6)
            value["progress"] = read_progress(self.progress_file)
            if self.persist:
                try:
                    self._write(value)
                except (OSError, TypeError, ValueError) as error:
                    value["telemetry_failure"] = "ATTEMPT_RECORD_UNAVAILABLE:" + type(error).__name__
            return value

    def update(self, **fields) -> None:
        with self._lock:
            self._data.update(fields)
            self.snapshot()

    def run(self, function, *args):
        token = _CURRENT.set(self)
        try:
            self.update(stage="admission")
            value, failure, seconds = function(*args)
            with self._lock:
                self._finished = time.monotonic()
                self.update(stage="complete", finished_at=self.clock(), result=value,
                            read_failure=None if failure is None else {
                                "type": type(failure).__name__,
                                "reason": str(getattr(failure, "reason", failure)),
                            }, admission_seconds=seconds)
            return value, failure, seconds
        except BaseException as error:
            with self._lock:
                self._finished = time.monotonic()
                self.update(stage="complete", finished_at=self.clock(), result=None,
                            read_failure={"type": type(error).__name__, "reason": type(error).__name__})
            raise
        finally:
            _CURRENT.reset(token)


def command_environment() -> dict:
    attempt = _CURRENT.get()
    return {"SVRF_ADMISSION_PROGRESS_FILE": str(attempt.progress_file)} if attempt and attempt.persist else {}


def command_started(proc, root: Path) -> None:
    attempt = _CURRENT.get()
    if attempt is not None:
        attempt.update(stage="command", process=process_identity(proc.pid), slot=str(root))


def command_finished(returncode: int) -> None:
    attempt = _CURRENT.get()
    if attempt is not None:
        with attempt._lock:
            process = dict(attempt._data.get("process", {}))
            process["returncode"] = returncode
            attempt.update(process=process)
