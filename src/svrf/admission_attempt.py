"""Durable observations of an admission's source, command and completion.

The records describe owned work. They never decide an admission or stop a process.
"""
from __future__ import annotations

from contextvars import ContextVar
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import tempfile
from threading import RLock
import time
import uuid

from .errors import ReadFailed

_CURRENT = ContextVar("svrf_admission_attempt", default=None)
MAX_PROGRESS_BYTES = 4 * 1024 * 1024
MAX_EVENT_BYTES = 32768


def process_identity(pid: int) -> dict:
    value = {"pid": pid}
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
        fields = text[text.rfind(")") + 2:].split()
        value["start_ticks"] = int(fields[19])
        value["state"] = fields[0]
        value["boot_id"] = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except (OSError, ValueError, IndexError) as error:
        value["read_failure"] = type(error).__name__
    return value


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _digest(value) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def _number(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _decode_progress(data: bytes) -> list[dict]:
    """Validate the source's own schema and chain before projecting any facts."""
    if not data or not data.endswith(b"\n"):
        raise ValueError("PROGRESS_INCOMPLETE")
    events, previous, schema = [], None, None
    for index, line in enumerate(data.splitlines()):
        if len(line) > MAX_EVENT_BYTES:
            raise ValueError("PROGRESS_EVENT_LIMIT")
        event = json.loads(line)
        if not isinstance(event, dict):
            raise ValueError("PROGRESS_EVENT_INVALID")
        if index == 0:
            schema = event.get("schema")
            if not isinstance(schema, str) or not schema or event.get("kind") != "begin":
                raise ValueError("PROGRESS_BEGIN_INVALID")
        if (event.get("schema") != schema or type(event.get("sequence")) is not int
                or event["sequence"] != index or event.get("previous") != previous
                or not _number(event.get("wall")) or not _number(event.get("monotonic"))
                or type(event.get("pid")) is not int or event["pid"] < 1
                or not isinstance(event.get("lane"), str) or not event["lane"]):
            raise ValueError("PROGRESS_CHAIN_INVALID")
        body = {key: value for key, value in event.items() if key != "digest"}
        if event.get("digest") != _digest(body):
            raise ValueError("PROGRESS_DIGEST_MISMATCH")
        kind = event.get("kind")
        if events and events[-1]["kind"] == "terminal":
            raise ValueError("PROGRESS_AFTER_TERMINAL")
        if kind == "begin":
            if (index != 0 or type(event.get("pr")) is not int or event["pr"] < 1
                    or not all(isinstance(event.get(key), str) and event[key] for key in ("head", "base"))
                    or not isinstance(event.get("owner"), dict)):
                raise ValueError("PROGRESS_BEGIN_INVALID")
        elif kind in ("start", "stage"):
            if not isinstance(event.get("stage"), str) or not event["stage"]:
                raise ValueError("PROGRESS_STAGE_INVALID")
            if kind == "start" and (not isinstance(event.get("span"), str) or not event["span"]):
                raise ValueError("PROGRESS_SPAN_INVALID")
            for key in ("obligation", "blocking_resource"):
                if event.get(key) is not None and not isinstance(event[key], str):
                    raise ValueError("PROGRESS_STAGE_INVALID")
        elif kind == "end":
            if not isinstance(event.get("span"), str) or not event["span"]:
                raise ValueError("PROGRESS_SPAN_INVALID")
        elif kind == "progress":
            if (not all(isinstance(event.get(key), str) and event[key] for key in ("work_id", "obligation"))
                    or type(event.get("outcome")) is not int
                    or event.get("provenance") not in ("COMPUTED", "REUSED")):
                raise ValueError("PROGRESS_WORK_INVALID")
        elif kind == "terminal":
            if (event.get("disposition") not in ("ADMITTED", "HELD", "UNOBSERVED", "EXECUTION_FAILED")
                    or type(event.get("exit")) is not int):
                raise ValueError("PROGRESS_TERMINAL_INVALID")
        else:
            raise ValueError("PROGRESS_KIND_UNOBSERVED")
        events.append(event)
        previous = event["digest"]
    return events


def _summarize_progress(events: list[dict], wall: float, monotonic: float) -> dict:
    first, active, completed, last_progress, terminal = events[0], {}, set(), None, None
    for event in events[1:]:
        kind = event["kind"]
        if kind == "start":
            active[event["span"]] = event
        elif kind == "end":
            active.pop(event["span"], None)
        elif kind == "stage":
            active["stage:" + event["lane"]] = event
        elif kind == "progress" and event["work_id"] not in completed:
            completed.add(event["work_id"])
            last_progress = event
        elif kind == "terminal":
            terminal = event
    observer = process_identity(os.getpid())
    same_boot = bool(observer.get("boot_id")) and observer.get("boot_id") == first["owner"].get("boot_id")
    clock = "monotonic" if same_boot else "wall"
    endpoint = terminal or {"wall": wall, "monotonic": monotonic}
    ordered = sorted(active.values(), key=lambda row: row["sequence"], reverse=True)
    current = ordered[0] if ordered else {}
    elapsed = endpoint[clock] - first[clock]
    fields = ("stage", "obligation", "blocking_resource", "pid", "lane", "wall", "monotonic")
    return {
        "pr": first["pr"], "head": first["head"], "base": first["base"], "owner": first["owner"],
        "started_at": first["wall"], "observed_at": wall,
        "stage": "TERMINAL" if terminal else current.get("stage", "STARTING"),
        "stage_started_at": None if terminal else current.get("wall"),
        "elapsed_seconds": elapsed if elapsed >= 0 else None, "clock": clock,
        "stage_elapsed_seconds": (max(0.0, endpoint[clock] - current[clock])
                                  if current and not terminal else None),
        "last_progress": last_progress,
        "seconds_without_progress": max(0.0, endpoint[clock] - (last_progress or first)[clock]),
        "blocking_resource": None if terminal else current.get("blocking_resource"),
        "active_stages": [] if terminal else [{key: event.get(key) for key in fields} for event in ordered],
        "terminal_disposition": terminal.get("disposition") if terminal else None,
        "terminal_at": terminal["wall"] if terminal else None, "exit": terminal["exit"] if terminal else None,
        "completed_obligations": len(completed), "completed_work_ids": sorted(completed),
        "coverage_digest": _digest(sorted(completed)), "observer_sha256": first.get("observer_sha256"),
        "event_count": len(events),
    }


def read_progress(path: Path, *, expected: dict | None = None,
                  wall: float | None = None, monotonic: float | None = None) -> dict:
    """Read one bounded original chain. Invalid or absent work stays unobserved."""
    wall = time.time() if wall is None else wall
    monotonic = time.monotonic() if monotonic is None else monotonic
    result = {"path": str(path), "status": "UNOBSERVED", "observed_at": wall}
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
        events = _decode_progress(data)
        if expected is not None and any(events[0].get(key) != value for key, value in expected.items()):
            raise ValueError("PROGRESS_SOURCE_MISMATCH")
        summary = _summarize_progress(events, wall, monotonic)
    except (UnicodeError, ValueError, TypeError, KeyError, OverflowError) as error:
        reason = str(error) if isinstance(error, ValueError) and str(error).startswith("PROGRESS_") else type(error).__name__
        return {**result, "reason": reason}
    return {**result, "status": "OBSERVED", "last_event": events[-1], "summary": summary}


class AdmissionAttempt:
    def __init__(self, state_dir: Path, pr: int, head: str, base: str, *, clock=time.time, persist=True):
        self.clock, self.persist = clock, persist
        self._lock = RLock()
        self._finished = None
        self._terminal_saved = False
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
        sync = self._finished is not None and not self._terminal_saved
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                             prefix="." + self.path.stem + "-", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(value, stream, sort_keys=True)
                stream.write("\n")
                stream.flush()
                if sync:
                    os.fsync(stream.fileno())
            temporary.replace(self.path)
            temporary = None
            if sync:
                directory = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
                self._terminal_saved = True
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def snapshot(self) -> dict:
        with self._lock:
            value = deepcopy(self._data)
            wall, monotonic = self.clock(), time.monotonic()
            value["observed_at"] = wall
            value["elapsed_seconds"] = round(max(0.0, (self._finished or monotonic) - self._started), 6)
            value["progress"] = read_progress(
                self.progress_file, expected={key: value[key] for key in ("pr", "head", "base")},
                wall=wall, monotonic=monotonic)
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
                verdict = value.get("verdict") if isinstance(value, dict) else None
                disposition = ("UNOBSERVED" if isinstance(failure, ReadFailed) else
                               "EXECUTION_FAILED" if failure is not None else
                               "ADMITTED" if verdict in ("MERGEABLE", "LANDED") else
                               "HELD" if verdict == "HELD" else "UNOBSERVED")
                finished = self.clock()
                self.update(stage="complete", finished_at=finished, terminal_at=finished,
                            terminal_disposition=disposition,
                            exit=self._data.get("process", {}).get("returncode"),
                            read_failed=isinstance(failure, ReadFailed), result=value,
                            read_failure=None if failure is None else {
                                "type": type(failure).__name__,
                                "reason": str(getattr(failure, "reason", failure)),
                            }, admission_seconds=seconds)
            return value, failure, seconds
        except BaseException as error:
            with self._lock:
                self._finished = time.monotonic()
                finished = self.clock()
                self.update(stage="complete", finished_at=finished, terminal_at=finished,
                            terminal_disposition="EXECUTION_FAILED",
                            exit=self._data.get("process", {}).get("returncode"),
                            read_failed=isinstance(error, ReadFailed), result=None,
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
