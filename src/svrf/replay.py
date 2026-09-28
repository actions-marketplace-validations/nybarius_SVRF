"""Replay a historical snapshot: given already-merged pull request numbers, reconstruct
each one's head and the base it merged onto from its own merge commit (an ordinary
two-parent merge -- a squash or rebase merge has no reconstructable head and is
reported, not guessed), open them as pull requests against a local stand-in for GitHub
(`LocalHub`) carrying the same commit graph, and run the ordinary admission read and
family planning -- and, with `gate=True`, the ordinary gate -- exactly as a live round
would.

Nothing here lands: replay never pushes, merges, or comments against the real remote;
every write happens in a throwaway local origin this module owns.

    svrf replay --merged 101,102,103 [--base <sha-or-ref>] [--gate]

`base` is the round every listed pull request is replayed against. Left out, every
listed pull request's own reconstructed base (its merge commit's first parent) must be
identical -- true for the sole member of a round, refused otherwise, since the train
model has one current base per round. To replay several pull requests as the family
they actually landed together, pass their shared original round base explicitly:
prefer the first (lowest-numbered, earliest-landed) member's own reconstructed base
over a receipt's `family["base"]` -- for every family after the first in a round that
speculatively stacked families on each other's unlanded folds, that field can be a
"train preview" commit that was never pushed anywhere durable and will not be present
in any real clone.

A pull request that was pushed to more than once before it merged is reconstructed only
at the state its own merge commit carries, not any earlier, since-superseded push: a
head repaired or relanded along the way and then merged cleanly is read here only as
"merged cleanly", the same way a live snapshot only ever sees the current push.
"""

from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .admission import Admission
from .app import kind_of
from .errors import ReadFailed
from .gate import CommandGate
from .git import RealGit
from .localhub import LocalHub
from .rules import admission_decision, chunk, choose_families
from .train import Train


class ReplayError(ValueError):
    pass


@dataclass
class Reconstructed:
    number: int
    base_sha: str
    head_sha: str
    merge_sha: str


def reconstruct(git: RealGit, merge_commit, numbers: list[int]) -> tuple[list["Reconstructed"], dict[int, str]]:
    """`merge_commit(number)` names the merge commit sha the hub recorded for that pull
    request, or None (never merged, or merged some other way -- a squash, a rebase --
    that recorded no such commit). Every number that cannot be reconstructed this way is
    reported in the second return value instead of guessed."""
    found: list[Reconstructed] = []
    problems: dict[int, str] = {}
    for n in numbers:
        sha = merge_commit(n)
        if not sha:
            problems[n] = "NOT_MERGED"
            continue
        try:
            fetch_commit = getattr(git, "fetch_commit", None)
            if fetch_commit is not None:
                fetch_commit(sha)
            parents = git.parents(sha)
        except ReadFailed as failure:
            problems[n] = f"UNREADABLE:{failure.reason}"
            continue
        if len(parents) != 2:
            problems[n] = f"NOT_AN_ORDINARY_MERGE:{len(parents)}_PARENTS"
            continue
        found.append(Reconstructed(number=n, base_sha=parents[0], head_sha=parents[1], merge_sha=sha))
    return found, problems


def _resolve_base(found: list[Reconstructed], base: str | None) -> str:
    if base is not None:
        return base
    anchor = found[0].base_sha
    mismatched = [f.number for f in found if f.base_sha != anchor]
    if mismatched:
        raise ReplayError(
            f"DIFFERENT_BASES:{mismatched} do not share #{found[0].number}'s base {anchor[:12]}; "
            "pass an explicit base (a receipt's family[\"base\"], or one member's own base) to replay them together")
    return anchor


def stage(source_root: Path, found: list[Reconstructed], workdir: Path, *, base_branch: str, base: str | None,
         repo: str = "replay/local") -> tuple[Path, LocalHub, str]:
    """A throwaway local origin under `workdir` (a fast local clone of `source_root`, so
    every reconstructed commit is already present) with `base_branch` forced to the
    replay's shared base and one branch + open pull request per reconstructed head.
    Returns (origin_path, hub, base_sha); the caller owns `workdir`'s cleanup."""
    if not found:
        raise ReplayError("NOTHING_TO_REPLAY")
    base_sha = _resolve_base(found, base)
    origin = workdir / "origin.git"
    subprocess.run(["git", "-C", str(workdir), "clone", "-q", "--bare", "--local", str(source_root), str(origin)],
                   check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(origin), "branch", "-f", base_branch, base_sha], check=True,
                   capture_output=True, text=True)
    hub = LocalHub(origin, base=base_branch, repo=repo)
    for item in found:
        ref = f"replay-{item.number}"
        subprocess.run(["git", "-C", str(origin), "branch", "-f", ref, item.head_sha], check=True,
                       capture_output=True, text=True)
        hub.open_pr(ref, base_branch, title=f"replay #{item.number}", author="replay", number=item.number)
    return origin, hub, base_sha


def run(config, found: list[Reconstructed], *, base: str | None = None, gate: bool = False) -> dict:
    """Admission and planning (and, with `gate=True`, the gate) against the historical
    snapshot `found` reconstructs. Always a dry run: nothing lands, nothing is pushed to
    the real remote, and the throwaway origin and working clone this creates are removed
    before returning."""
    with tempfile.TemporaryDirectory(prefix="svrf-replay-") as tmp:
        origin, hub, base_sha = stage(Path(config.clone), found, Path(tmp), base_branch=config.base, base=base,
                                      repo=f"replay/{config.repo}")
        work = Path(tmp) / "clone"
        subprocess.run(["git", "-C", tmp, "clone", "-q", str(origin), str(work)], check=True,
                       capture_output=True, text=True)
        git = RealGit(work, base=config.base, union=config.union_paths)
        kind = kind_of(config)
        admission_check = Admission(git, order=config.history.order, kind=kind,
                                    refactor_prefixes=tuple(config.history.refactor_prefixes),
                                    command=config.admission_command,
                                    timeout=config.admission_timeout_minutes * 60)
        admission: dict[int, dict] = {}
        for item in found:
            try:
                value = admission_check(item.head_sha, base_sha)
            except ReadFailed as failure:
                admission[item.number] = {"decision": "RETRY", "class": None, "reason": failure.reason}
                continue
            decision, cls = admission_decision(value, config.union_paths, reland=config.history.reland)
            admission[item.number] = {"decision": decision, "class": cls, "verdict": value.get("verdict"),
                                      "residuals": value.get("residuals", [])}
        queue = [item.number for item in found if admission[item.number]["decision"] == "ADMIT"]
        by_number = {item.number: item for item in found}
        rows = [{"number": n, "headRefOid": by_number[n].head_sha, "headRefName": f"replay-{n}"} for n in queue]
        receipts = Path(tmp) / "receipts"
        gate_cmd = CommandGate(work, Path(tmp) / "worktrees", config.train.jobs, config.gate.commands,
                               setup=config.gate.setup, logs=Path(tmp) / "logs",
                               timeout_minutes=config.gate.timeout_minutes,
                               infra_patterns=config.gate.infra_patterns,
                               failing_pattern=config.gate.failing_pattern, env=config.gate.env)
        train = Train(git, hub, gate_cmd, receipts=receipts, jobs=config.train.jobs,
                      family_size=config.train.family_size, max_rounds=1, dry_run=True, comment=False,
                      is_union=config.union_paths)
        present = train.snapshot(queue, rows)
        pairs = train.git.pairs([{"number": n, "head_sha": train.rows[n]["head_sha"]} for n in present], base_sha)
        chosen, out = choose_families(present, pairs.get("conflicts", []), pairs.get("unreadable", []),
                                      config.union_paths)
        families: list[dict] = []
        if gate:
            train.round(chosen)
            families = train.receipt["families"]
        else:
            acc = base_sha
            for part in chunk(chosen, config.train.family_size):
                family = train.plan(part, acc)
                if family is not None:
                    families.append(family)
                    acc = family["commit"]
        return {"requested": [item.number for item in found], "base": base_sha,
                "admission": admission, "pairs": pairs, "out": out, "families": families,
                "gates": train.receipt["gates"]}
