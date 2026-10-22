"""A bounded retry delay belongs to actual failed reads of one source occurrence."""
from __future__ import annotations

import hashlib
import marshal
import math
import os
from pathlib import Path
import re
import sys

from .admission_attempt import _canonical, _digest
from .globs import PathSet


def _bound_value(value, depth=0):
    """Encode supported captured values exactly; an opaque value has no guessed identity."""
    if depth > 12:
        raise ValueError("CAPTURE_DEPTH")
    kind = type(value)
    if value is None or kind in (bool, int, str):
        return [kind.__name__, value]
    if kind is float and math.isfinite(value):
        return ["float", value.hex()]
    if kind is bytes:
        return ["bytes", value.hex()]
    if kind in (list, tuple, set, frozenset):
        rows = [_bound_value(item, depth + 1) for item in value]
        return [kind.__name__, sorted(rows, key=_canonical) if kind in (set, frozenset) else rows]
    if kind is dict:
        rows = [[_bound_value(key, depth + 1), _bound_value(item, depth + 1)] for key, item in value.items()]
        return ["dict", sorted(rows, key=_canonical)]
    if kind is PathSet:
        regex = value._regex
        if regex is not None and type(regex) is not re.Pattern:
            raise TypeError("MATCHER_UNOBSERVED")
        return ["PathSet", _bound_value(value.patterns, depth + 1),
                None if regex is None else [regex.pattern, regex.flags],
                hashlib.sha256(marshal.dumps(PathSet.__call__.__code__, 2)).hexdigest()]
    raise TypeError("CAPTURE_UNOBSERVED")


def _callable_identity(value, *, bind_receiver=False) -> dict:
    method = value if hasattr(value, "__code__") else getattr(value, "__call__", None)
    code = getattr(method, "__code__", None)
    result = {
        "module": getattr(value, "__module__", type(value).__module__),
        "name": getattr(value, "__qualname__", type(value).__qualname__),
        # Format 2 records code values without reference-count-dependent sharing flags.
        "code": hashlib.sha256(marshal.dumps(code, 2)).hexdigest() if code is not None else None,
        "bindings_status": "UNOBSERVED",
    }
    if code is None:
        return result
    try:
        captured = {
            "defaults": _bound_value(getattr(method, "__defaults__", None)),
            "keyword_defaults": _bound_value(getattr(method, "__kwdefaults__", None)),
            "closure": {name: _bound_value(cell.cell_contents) for name, cell in
                        zip(code.co_freevars, getattr(method, "__closure__", None) or (), strict=True)},
        }
        if bind_receiver:
            receiver = getattr(value, "__self__", None)
            if receiver is not None:
                captured["receiver"] = _bound_value(receiver)
            elif not hasattr(value, "__code__"):
                captured["receiver"] = _bound_value(value)
        result.update(bindings_status="OBSERVED", bindings_sha256=_digest(captured))
    except (AttributeError, TypeError, ValueError, RecursionError):
        pass
    return result


def source_identity(admission, pr: int, head: str, base: str, inputs: dict | None = None) -> dict:
    """Hash consumed configuration and environment without retaining their secrets."""
    callback = _callable_identity(admission)
    configuration = {
        "callable": callback,
        "command": getattr(admission, "command", None),
        "order": getattr(admission, "order", None),
        "timeout": getattr(admission, "timeout", None),
        "refactor_prefixes": list(getattr(admission, "refactor_prefixes", ())),
        "kind": (_callable_identity(admission.kind, bind_receiver=True)
                 if getattr(admission, "kind", None) is not None else None),
        "root": str(getattr(getattr(admission, "git", None), "root", "")),
    }
    command = _digest(configuration)
    mask = None
    try:
        mask = next(line.split(":", 1)[1].strip() for line in Path("/proc/self/status").read_text().splitlines()
                    if line.startswith("Umask:"))
    except (OSError, StopIteration):
        pass
    environment = _digest({
        "variables": {key: value for key, value in os.environ.items() if key != "SVRF_ADMISSION_PROGRESS_FILE"},
        "python": sys.executable, "version": list(sys.version_info), "umask": mask,
    })
    watch = (inputs or {}).get("admission_watch")
    observed = (callback["code"] is not None and callback["bindings_status"] == "OBSERVED"
                and mask is not None and (configuration["kind"] is None
                                         or configuration["kind"]["bindings_status"] == "OBSERVED"))
    if watch is not None and isinstance(watch.get("value"), dict) and "unobserved" in watch["value"]:
        observed = False
    readset = {"pr": pr, "head": head, "base": base, "command": command,
               "environment": environment, "watch": watch}
    return {
        "source_status": "OBSERVED" if observed else "UNOBSERVED",
        "occurrence_key": _digest(readset), "readset_fingerprint": _digest(readset),
        "command_fingerprint": command, "environment_fingerprint": environment,
    }


def retain_failure(state: dict, attempt: dict) -> None:
    """Count one terminal UUID once; unobserved work never proves no progress."""
    rows = state.setdefault("admission_retries", {})
    number, attempt_id = str(attempt["pr"]), attempt["attempt_id"]
    previous = rows.get(number) or {}
    if previous.get("attempt_id") == attempt_id:
        return
    if not attempt.get("read_failed"):
        rows.pop(number, None)
        return
    reason = (attempt.get("read_failure") or {}).get("reason")
    same = previous.get("occurrence_key") == attempt.get("occurrence_key")
    if not same:
        previous = {}
    progress = attempt.get("progress") or {}
    summary = progress.get("summary") or {}
    complete = (
        attempt.get("source_status") == "OBSERVED"
        and progress.get("status") == "OBSERVED"
        and summary.get("terminal_disposition") in ("UNOBSERVED", "EXECUTION_FAILED")
        and summary.get("terminal_at") is not None
        and isinstance(summary.get("completed_work_ids"), list)
        and (attempt.get("exit") is None or summary.get("exit") == attempt["exit"])
    )
    known = set(previous.get("completed_work_ids") or [])
    count, retry_at, current_digest = None, None, None
    unknown_count, unknown_reason, backoff_kind = 0, None, None
    if complete:
        current = set(summary["completed_work_ids"])
        useful = bool(current - known)
        same_failure = previous.get("reason") == reason
        prior_count = previous.get("no_progress_failures") if same_failure else 0
        count = 0 if useful else (prior_count or 0) + 1
        known.update(current)
        current_digest = summary.get("coverage_digest")
        if count >= 3:
            retry_at = attempt["terminal_at"] + min(900, 30 * 2 ** min(count - 3, 5))
            backoff_kind = "NO_USEFUL_PROGRESS"
    else:
        unknown_reason = progress.get("reason") or (
            "PROGRESS_TERMINAL_UNOBSERVED" if summary.get("terminal_at") is None
            else "PROGRESS_TERMINAL_NOT_QUALIFIED")
        same_unknown = (previous.get("reason") == reason
                        and previous.get("coverage_status") == "UNOBSERVED"
                        and previous.get("unknown_reason") == unknown_reason)
        unknown_count = (previous.get("unobserved_failures", 0) if same_unknown else 0) + 1
        if unknown_count >= 3 and attempt.get("source_status") == "OBSERVED":
            retry_at = attempt["terminal_at"] + min(900, 30 * 2 ** min(unknown_count - 3, 5))
            backoff_kind = "REPEATED_UNOBSERVED"
    keys = ("pr", "head", "base", "occurrence_key", "readset_fingerprint",
            "command_fingerprint", "environment_fingerprint", "source_status")
    rows[number] = {
        **{key: attempt.get(key) for key in keys},
        "attempt_id": attempt_id, "reason": reason,
        "last_terminal_at": attempt.get("terminal_at"), "clock": "unix",
        "read_failure_count": previous.get("read_failure_count", 0) + 1,
        "coverage_status": "OBSERVED" if complete else "UNOBSERVED",
        "completed_work_ids": sorted(known), "coverage_digest": _digest(sorted(known)),
        "current_coverage_digest": current_digest,
        "no_progress_failures": count, "retry_at": retry_at, "backoff_kind": backoff_kind,
        "unobserved_failures": unknown_count, "unknown_reason": unknown_reason,
    }
