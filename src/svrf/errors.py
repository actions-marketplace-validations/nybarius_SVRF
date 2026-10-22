"""Read failures.

A read that could not be made (a network error, a rate limit, a gate whose machine
fell over) is never a verdict about a pull request. It is retried later and never
turns into a hold.
"""

from __future__ import annotations

import re

from .redact import redact


class ReadFailed(Exception):
    """A read that could not be made. Never a conflict, never a hold: retry later."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class StopRequested(ReadFailed):
    """An owner retired before a new operation began; leave it pending."""

    def __init__(self):
        super().__init__("STOP_REQUESTED")


def check_stop(stop_requested) -> None:
    if stop_requested is not None and stop_requested():
        raise StopRequested()


class RateLimited(ReadFailed):
    """GitHub refused the call for its rate limit; `reset` is when it said to come back."""

    def __init__(self, message: str, reset: float | None = None, env: dict | None = None):
        first = (redact(message, env).strip().splitlines() or [""])[0][:160]
        super().__init__(f"RATE_LIMITED:{first}")
        self.reset = reset


RATE_PATTERN = re.compile(r"rate limit|secondary rate|abuse detection|HTTP 429", re.IGNORECASE)


def classify_gh_failure(rc: int, stderr: str, env: dict | None = None) -> None:
    """Raise the read failure a non-zero `gh` exit is: rate limited, or failed. `stderr`
    is redacted first: it is `gh`'s own output and could echo an inherited token back."""
    stderr = redact(stderr or "", env)
    if RATE_PATTERN.search(stderr):
        raise RateLimited(stderr, env=env)
    first = (stderr.strip().splitlines() or [""])[0][:160]
    raise ReadFailed(f"GH_FAILED:{rc}:{first}")
