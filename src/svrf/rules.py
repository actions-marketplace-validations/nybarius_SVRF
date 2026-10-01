"""The train's decision rules, as pure functions.

Nothing here reads git or GitHub. Every rule is listed in RULES with the theorem in
`proofs/` that states it and the function or method that enforces it;
`tests/test_proof_map.py` keeps that table honest.
"""

from __future__ import annotations

import re
from typing import Callable

from .merge_family import maximum_family

# rule name -> the Lean theorem that states it, and where the code enforces it
RULES: dict[str, dict[str, str]] = {
    "family-fold": {"lean": "fold", "check": "Train.plan"},
    "gate-green": {"lean": "landed_passes", "check": "Train.gate_family"},
    "tree-identity": {"lean": "checkLanding_iff", "check": "Train.land_family"},
    "outside-move-refused": {"lean": "checkLanding_refuses_moved", "check": "Train.land_family"},
    "bisect-lands-gated": {"lean": "segments_land_last_gated", "check": "Train.settle_red"},
    "prefix-ungated": {"lean": "prefix_is_fold", "check": "Train.land_family"},
    "one-gate-per-merge-commit": {"lean": "all_gated_iff_each_prefix", "check": "Train.land_family"},
    "land": {"lean": "landed_eq_gated", "check": "Train.land_family"},
    "hold": {"lean": "landed_passes", "check": "Train.settle_red"},
    "admit": {"lean": "admitted_iff", "check": "admission_decision"},
    "opt-out": {"lean": "admitted_iff", "check": "admission"},
    "already-merged": {"lean": "admitted_iff", "check": "admission"},
    "held-retry": {"lean": "retry_iff", "check": "retry_due"},
    "held-verdict-reuse": {"lean": "held_verdict_stands", "check": "held_retry"},
    "no-reopen-outside-watched-paths": {"lean": "no_reopen_outside_reads", "check": "held_retry"},
    "read-failure-never-holds": {"lean": "read_failure_never_holds", "check": "admission_decision"},
    "compatible-families": {"lean": "strands_comm", "check": "choose_families"},
    "interleaved-owners": {"lean": "interleaved_landing", "check": "choose_families"},
    "repair-mechanical": {"lean": "retry_iff", "check": "repair_class"},
    "ordered-reland": {"lean": "reland_tree", "check": "reland_class"},
    "union-merge": {"lean": "change_comm", "check": "union_lines"},
    "speculative-stacking": {"lean": "stack_lands_gated", "check": "Train.round"},
    "speculation-void": {"lean": "stackStatus_void_iff", "check": "Train.round"},
    "bisect-holds-exactly-red": {"lean": "settle_outcome", "check": "Train.settle_red"},
    "bisect-gate-bound": {"lean": "bisection_gates_le", "check": "Train.settle_red"},
    "maximal-family": {"lean": "bk_maximal", "check": "families"},
    "left-out-names-partner": {"lean": "chosen_family_maximal", "check": "choose_families"},
    "maximum-family": {"lean": "chosen_family_maximum", "check": "choose_families"},
    "stacked-retarget": {"lean": "retarget_after_parent", "check": "Daemon.stacked"},
}


# --------------------------------------------------------------------------- merging


def union_lines(*, theirs: str, ours: str) -> str:
    """A union-merged file: the base branch's lines in order, then the lines the pull
    request adds, each once. Suited to files whose lines are independent entries: import
    indexes, requirement lists, changelog bullets."""
    out = theirs.splitlines()
    seen = set(out)
    for line in ours.splitlines():
        if line not in seen:
            out.append(line)
            seen.add(line)
    return "\n".join(out) + "\n"


def chunk(items: list, size: int) -> list[list]:
    size = max(1, size)
    return [items[i:i + size] for i in range(0, len(items), size)]


# --------------------------------------------------------------------------- families

FAMILIES_BOUND = 64


def families(nodes: list[int], edges: list[tuple[int, int]], bound: int = FAMILIES_BOUND) -> list[list[int]]:
    """Every maximal set of pull requests that pairwise do not conflict, largest first
    (ties by number): the maximal independent sets of the conflict graph, enumerated as
    maximal cliques of its complement with Bron-Kerbosch and pivoting."""
    nodes = sorted(set(nodes))
    apart: dict[int, set[int]] = {n: set() for n in nodes}
    for a, b in edges:
        if a in apart and b in apart and a != b:
            apart[a].add(b)
            apart[b].add(a)
    compatible = {n: set(nodes) - apart[n] - {n} for n in nodes}
    found: list[list[int]] = []

    def extend(clique: set[int], candidates: set[int], excluded: set[int]) -> None:
        if not candidates and not excluded:
            found.append(sorted(clique))
            return
        pivot = max(candidates | excluded, key=lambda u: len(candidates & compatible[u]))
        for v in sorted(candidates - compatible[pivot]):
            extend(clique | {v}, candidates & compatible[v], excluded & compatible[v])
            candidates = candidates - {v}
            excluded = excluded | {v}

    if nodes:
        extend(set(), set(nodes), set())
    found.sort(key=lambda f: (-len(f), f))
    return found[:bound]


def choose_families(numbers: list[int], conflicts: list[dict], unreadable: list[dict],
                    is_union: Callable[[str], bool] = lambda p: False) -> tuple[list[int], dict]:
    """The largest pairwise-compatible set of pull requests and, for each one left out,
    the kept ones it conflicts with and on which paths. A conflict only on union-merged
    paths does not separate two pull requests; a pair whose merge could not be read does."""
    edges: list[tuple[int, int]] = []
    paths: dict[frozenset, set[str]] = {}
    for pair in conflicts:
        real = [p for p in pair.get("paths", []) if not is_union(p)]
        if real or not pair.get("paths"):
            edges.append((pair["a"], pair["b"]))
            paths[frozenset((pair["a"], pair["b"]))] = set(real)
    for pair in unreadable:
        edges.append((pair["a"], pair["b"]))
        paths[frozenset((pair["a"], pair["b"]))] = {f"UNREADABLE:{pair.get('reason', '')}"}
    chosen = maximum_family(numbers, edges)
    out: dict[int, dict] = {}
    for n in numbers:
        if n in chosen:
            continue
        partners = sorted(m for m in chosen if frozenset((n, m)) in paths)
        where = sorted(set().union(*(paths[frozenset((n, m))] for m in partners))) if partners else []
        out[n] = {"with": partners, "paths": where}
    return sorted(chosen), out


# --------------------------------------------------------------------------- admission


def retry_due(held: dict, now: dict) -> bool:
    """A held pull request is read again exactly when what its verdict depended on
    changed: its head sha, or the base branch's content over the paths the hold watched.
    Otherwise the verdict stands and no gate is spent on it again."""
    return (held.get("head"), held.get("watch_digest")) != (now.get("head"), now.get("watch_digest"))


def held_retry(held: dict | None, head_sha: str, watch_digest: str | None = None) -> bool:
    """`retry_due` for a snapshot row. A pull request never held is always read. Without a
    fresh read of the watched paths, the head alone decides."""
    if held is None:
        return True
    now = held.get("watch_digest") if watch_digest is None else watch_digest
    return retry_due(held, {"head": head_sha, "watch_digest": now})


def admission(row: dict, held: dict | None, watch_digest: str | None = None, carried: bool = False,
              base: str = "main", hold_label: str = "train:hold") -> tuple[str, str]:
    """What one pull-request row decides, with no further GitHub read:

    ALREADY_MERGED  its head is already contained in the base branch (checked first;
                    never gated, never held)
    HELD_UNCHANGED  held before, and nothing it depended on changed
    NOT_AGAINST_BASE, CROSS_REPOSITORY, DRAFT, OPT_OUT
    CANDIDATE       to be read by the admission check
    """
    head = row.get("headRefOid") or row.get("head_sha") or ""
    if carried and not row.get("isCrossRepository"):
        return "ALREADY_MERGED", head
    if not held_retry(held, head, watch_digest):
        return "HELD_UNCHANGED", head
    if (row.get("baseRefName") or base) != base:
        return "NOT_AGAINST_BASE", head
    if row.get("isCrossRepository"):
        return "CROSS_REPOSITORY", head
    if row.get("isDraft"):
        return "DRAFT", head
    labels = {(label.get("name") if isinstance(label, dict) else label) for label in row.get("labels") or []}
    if hold_label in labels:
        return "OPT_OUT", head
    return "CANDIDATE", head


MERGE_CONFLICT = "merge:CONFLICT:"
STALE_LINES = ("github:NOT_MERGEABLE",)
# `history:` is the built-in tests-first check's own prefix; `reland:` is the one an
# external admission.command uses to report the same order-only refusal (see
# admission.py's PASSTHROUGH_PREFIXES). Both name the identical class of refusal.
RELAND_LINE = re.compile(r"^(?:history|reland):REFUSED:(UNORDERED|MIXED)$")


def repair_class(lines: list[str], is_union: Callable[[str], bool]) -> str | None:
    """The mechanical hold class of a pull request's residual lines, or None:
    UNION_CONFLICT when every line is a merge conflict on a union-merged path, STALE_BASE
    when every line says GitHub will not merge a head that merges cleanly here."""
    if not lines:
        return None
    if all(line.startswith(MERGE_CONFLICT) for line in lines):
        paths = [line[len(MERGE_CONFLICT):] for line in lines]
        return "UNION_CONFLICT" if all(is_union(p) for p in paths) else None
    if all(line in STALE_LINES for line in lines):
        return "STALE_BASE"
    return None


def reland_class(lines: list[str]) -> str | None:
    """UNORDERED or MIXED when every residual is a history-order refusal of one class:
    the order of commits, not their content, which the train fixes by re-landing the same
    final tree as an ordered history. Any other residual stays held for a person."""
    if not lines:
        return None
    matches = [RELAND_LINE.match(line) for line in lines]
    if not all(matches):
        return None
    classes = {m.group(1) for m in matches}
    return classes.pop() if len(classes) == 1 else None


def admission_decision(value: dict, is_union: Callable[[str], bool] = lambda p: False,
                       reland: bool = True) -> tuple[str, str | None]:
    """The admission check's verdict as the train acts on it: ADMIT, ALREADY_MERGED,
    REPAIR (with its class), RELAND (with its class), HOLD, or RETRY (anything that is
    not a verdict: it never enters the held memory)."""
    verdict = value.get("verdict")
    if verdict == "MERGEABLE":
        return "ADMIT", None
    if verdict == "LANDED":
        return "ALREADY_MERGED", None
    if verdict == "HELD":
        residuals = list(value.get("residuals") or [])
        cls = repair_class(residuals, is_union)
        if cls:
            return "REPAIR", cls
        cls = reland_class(residuals)
        if cls and reland:
            return "RELAND", cls
        return "HOLD", None
    return "RETRY", value.get("reason") or f"VERDICT:{verdict}"


# --------------------------------------------------------------------------- gate reads

INFRA_PATTERN = re.compile(
    r"cannot lock ref|unable to update local ref|Could not resolve host|Connection (?:timed out|reset)|"
    r"No space left on device|Resource temporarily unavailable|Killed\b|failed to fetch", re.IGNORECASE)


def gate_infra_failure(text: str, extra: list[str] | None = None) -> str | None:
    """GATE_INFRA:<line> when a gate's output shows it could not run (not that the tree
    failed), else None. A gate that could not run is retried, never counted red."""
    patterns = [INFRA_PATTERN] + [re.compile(p) for p in (extra or [])]
    for line in (text or "").splitlines():
        if any(p.search(line) for p in patterns):
            return f"GATE_INFRA:{line.strip()[:160]}"
    return None
