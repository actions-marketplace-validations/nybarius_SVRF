"""Adaptive family size, read from the train's own retained receipts.

A family of `k` pull requests costs one gate when every member is green; when any member
is red the family is halved and each half is gated in turn (the train's bisection). With a
per-pull-request red probability `p` the expected gate runs for a family of `k` are

    E(1) = 1,    E(k) = 1 + (1 - (1-p)^k) * (E(floor(k/2)) + E(ceil(k/2)))

and the size chosen is the one minimising E(k)/k for k in 1..cap (ties keep the larger k).

`p` is the maximum-likelihood estimate from each retained round's first family: its size
and whether it landed whole. No receipts means `p` is unknown and the cap is kept; nothing
here invents a rate."""

from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path


def expected_gates_per_pr(k: int, p: float) -> float:
    @lru_cache(maxsize=None)
    def gates(n: int) -> float:
        if n <= 1:
            return 1.0
        a = n // 2
        return 1.0 + (1.0 - (1.0 - p) ** n) * (gates(a) + gates(n - a))
    return gates(k) / k


def optimal_family_size(p: float | None, cap: int) -> int:
    cap = max(1, int(cap))
    if p is None:
        return cap
    best_k, best = cap, expected_gates_per_pr(cap, p)
    for k in range(cap - 1, 0, -1):
        cost = expected_gates_per_pr(k, p)
        if cost < best - 1e-12:
            best_k, best = k, cost
    return best_k


def _observations(receipts: Path) -> list[tuple[int, bool]]:
    rows = []
    for path in sorted(Path(receipts).glob("train-*.json")):
        try:
            families = json.loads(path.read_text()).get("families") or []
        except (OSError, ValueError, AttributeError):
            continue
        if not families:
            continue
        first = families[0]
        size = len(first.get("prs") or [])
        if size:
            rows.append((size, first.get("status") == "LANDED"))
    return rows


def red_probability(receipts: Path) -> tuple[float | None, int]:
    rows = _observations(receipts)
    if not rows:
        return None, 0
    if all(green for _, green in rows):
        return 0.0, len(rows)
    if not any(green for _, green in rows):
        return 1.0, len(rows)

    def log_likelihood(p: float) -> float:
        total = 0.0
        for size, green in rows:
            g = (1.0 - p) ** size
            total += math.log(g) if green else math.log(max(1.0 - g, 1e-300))
        return total

    lo, hi = 1e-9, 1.0 - 1e-9          # golden-section search: the likelihood is unimodal in p
    ratio = (math.sqrt(5) - 1) / 2
    for _ in range(200):
        a, b = hi - ratio * (hi - lo), lo + ratio * (hi - lo)
        if log_likelihood(a) < log_likelihood(b):
            lo = a
        else:
            hi = b
    return (lo + hi) / 2, len(rows)


def choose(receipts: Path, cap: int) -> dict:
    p, n = red_probability(receipts)
    size = optimal_family_size(p, cap)
    return {"family_size": size, "cap": int(cap), "observations": n,
            "red_probability": None if p is None else round(p, 6),
            "expected_gates_per_pr": None if p is None else round(expected_gates_per_pr(size, p), 6)}
