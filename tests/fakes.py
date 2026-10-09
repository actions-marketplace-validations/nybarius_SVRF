"""In-memory fakes of the clone, GitHub and the gate.

A commit carries a set of lanes; a tree is the sorted lane set, so a fold of heads is
the union of their lanes and tree identity is set equality. Lane 0 is the base.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from svrf.errors import RateLimited, ReadFailed  # noqa: E402
from svrf.git import Reland, Step  # noqa: E402

UNION = "index.txt"


def is_union(path: str) -> bool:
    return path == UNION


class FakeRepo:
    def __init__(self, prs, *, conflicts=(), main_conflicts=None, union_only=()):
        self.commits = {"c0": {"lanes": frozenset({0}), "parents": ()}}
        self.main = "c0"
        self.heads = {}
        self.branches = {}
        for n in prs:
            sha = f"h{n}"
            self.commits[sha] = {"lanes": frozenset({0, n}), "parents": ("c0",)}
            self.heads[n] = sha
            self.branches[f"pr-{n}"] = sha
        self.conflicts = {frozenset(pair): paths for pair, paths in conflicts}
        self.union_only = {frozenset(pair) for pair in union_only}
        self.main_conflicts = dict(main_conflicts or {})
        self.pushes = []
        self.push_refusals: set[str] = set()
        self.fetches = 0
        self.counter = 0
        self.lock = threading.Lock()

    def _new(self, lanes, parents):
        with self.lock:
            self.counter += 1
            sha = f"m{self.counter}"
            self.commits[sha] = {"lanes": frozenset(lanes), "parents": tuple(parents)}
            return sha

    def tree(self, commit):
        return "T" + ",".join(str(n) for n in sorted(self.commits[commit]["lanes"]))

    def lanes(self, commit):
        return self.commits[commit]["lanes"]

    def fetch(self, numbers):
        self.fetches += 1

    def main_sha(self):
        return self.main

    def parents(self, commit):
        return list(self.commits[commit]["parents"])

    def is_ancestor(self, a, b):
        return self.lanes(a) <= self.lanes(b)

    def pairs(self, rows, base):
        numbers = [r["number"] for r in rows]
        conflicts = []
        for i, a in enumerate(numbers):
            for b in numbers[i + 1:]:
                key = frozenset({a, b})
                if key in self.conflicts:
                    conflicts.append({"a": a, "b": b, "paths": list(self.conflicts[key])})
                elif key in self.union_only:
                    conflicts.append({"a": a, "b": b, "paths": [UNION]})
        return {"read": len(numbers) * (len(numbers) - 1) // 2, "conflicts": conflicts, "unreadable": []}

    def union_step(self, acc, head, message=""):
        acc_lanes, head_lanes = self.lanes(acc), self.lanes(head)
        for lane in head_lanes - {0}:
            if lane in self.main_conflicts and acc == self.main:
                return Step("CONFLICT", conflicts=list(self.main_conflicts[lane]))
            for other in acc_lanes:
                key = frozenset({lane, other})
                if key in self.conflicts:
                    return Step("CONFLICT", conflicts=list(self.conflicts[key]))
        if head_lanes <= acc_lanes:
            return Step("CLEAN", commit=acc, tree=self.tree(acc))
        sha = self._new(acc_lanes | head_lanes, (head, acc))
        return Step("CLEAN", commit=sha, tree=self.tree(sha))

    def push_branch(self, commit, branch):
        if branch in self.push_refusals:
            raise ReadFailed(f"PUSH_REFUSED:{branch}")
        self.pushes.append((branch, commit))
        self.branches[branch] = commit


class FakeGitHub:
    def __init__(self, repo, *, drafts=(), rate_limited_first=0, merge_failures=None,
                 outside_after=None, mismatch_on=None, clock=None, remaining=5000):
        self.repo = repo
        self.drafts = set(drafts)
        self.rate_limited_first = rate_limited_first
        self.merge_failures = dict(merge_failures or {})
        self.outside_after = outside_after
        self.mismatch_on = mismatch_on
        self.clock = clock
        self.remaining = remaining
        self.merged = []
        self.readied = []
        self.comments = []
        self.comment_rows = []
        self.statuses = {}
        self.closed = []
        self.calls = {"graphql": 0, "rest": 0}
        self._next_comment_id = 1

    def rate_limit(self):
        now = self.clock() if self.clock else 0
        return {"graphql": {"remaining": self.remaining, "reset": now + 30},
                "core": {"remaining": 5000, "reset": now + 30}}

    def snapshot(self):
        self.calls["graphql"] += 1
        if self.rate_limited_first:
            self.rate_limited_first -= 1
            raise RateLimited("GraphQL: API rate limit already exceeded for user ID 1.",
                              reset=(self.clock() if self.clock else 0) + 60)
        return [{"number": n, "headRefName": f"pr-{n}", "headRefOid": sha,
                 "isDraft": n in self.drafts, "title": f"lane {n}"}
                for n, sha in sorted(self.repo.heads.items())]

    def pull(self, number):
        self.calls["rest"] += 1
        return {"number": number, "head_sha": self.repo.branches[f"pr-{number}"],
                "mergeable": True, "state": "open", "draft": number in self.drafts,
                "labels": [{"name": name} for name in getattr(self, "labels", {}).get(number, [])]}

    def ready(self, number):
        self.calls["graphql"] += 1
        self.readied.append(number)
        self.drafts.discard(number)

    def merge(self, number, sha):
        self.calls["rest"] += 1
        if number in self.merge_failures:
            raise self.merge_failures.pop(number)
        lanes = self.repo.lanes(self.repo.main) | self.repo.lanes(sha)
        if self.mismatch_on == number:
            lanes = lanes | {99}
        merged = self.repo._new(lanes, (self.repo.main, sha))
        self.repo.main = merged
        self.merged.append(number)
        if self.outside_after == number:
            self.outside_after = None
            self.repo.main = self.repo._new(self.repo.lanes(self.repo.main) | {77}, (self.repo.main,))
        return merged

    def comment(self, number, body):
        self.calls["rest"] += 1
        self.comments.append((number, body))
        cid = self._next_comment_id
        self._next_comment_id += 1
        self.comment_rows.append({"id": cid, "number": number, "body": body})

    def list_comments(self, number):
        self.calls["rest"] += 1
        return [{"id": row["id"], "body": row["body"]} for row in self.comment_rows if row["number"] == number]

    def update_comment(self, comment_id, body):
        self.calls["rest"] += 1
        for row in self.comment_rows:
            if row["id"] == comment_id:
                row["body"] = body
                return
        raise ReadFailed(f"NO_SUCH_COMMENT:{comment_id}")

    def set_status(self, sha, state, description, context="svrf", target_url=None):
        self.calls["rest"] += 1
        self.statuses.setdefault(sha, []).append({"state": state, "description": description,
                                                   "context": context, "target_url": target_url})

    def close(self, number):
        self.calls["rest"] += 1
        self.closed.append(number)


class FakeGate:
    """Green unless the gated tree carries a bad lane; records trees and concurrency."""

    def __init__(self, repo, bad=(), delay=0.0):
        self.repo = repo
        self.bad = set(bad)
        self.delay = delay
        self.trees = []
        self.running = 0
        self.peak = 0
        self.lock = threading.Lock()

    def run(self, base, commit, label=""):
        with self.lock:
            self.running += 1
            self.peak = max(self.peak, self.running)
        try:
            if self.delay:
                time.sleep(self.delay)
            lanes = self.repo.lanes(commit)
            tree = self.repo.tree(commit)
            with self.lock:
                self.trees.append(tree)
            bad = sorted(lanes & self.bad)
            return {"green": not bad, "tree": tree,
                    "failing": [f"FAILED tests/test_lane_{n}.py::test_it" for n in bad]}
        finally:
            with self.lock:
                self.running -= 1


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


# --------------------------------------------------------------------------- daemon fakes


class DaemonRepo(FakeRepo):
    """Adds the repair step, head moves, watched-path digests and a fake re-land."""

    def __init__(self, prs, *, repair_conflicts=None, reland_mismatch=False, restack_mismatch=False, **kw):
        super().__init__(prs, **kw)
        self.repair_conflicts = dict(repair_conflicts or {})
        self.repairs = []
        self.reland_mismatch = reland_mismatch
        self.reland_calls = []
        self.restack_mismatch = restack_mismatch
        self.restack_calls = []

    def repair_step(self, acc, head, message=""):
        self.repairs.append((acc, head))
        for lane in self.lanes(head) - {0}:
            if lane in self.repair_conflicts:
                return Step("CONFLICT", conflicts=list(self.repair_conflicts[lane]))
        sha = self._new(self.lanes(acc) | self.lanes(head), (head, acc))
        return Step("CLEAN", commit=sha, tree=self.tree(sha))

    def watch_digest(self, commit, paths):
        """The base over watched paths: a watched path `lane:N` sees lane N."""
        return ",".join(str(lane) for lane in sorted(self.lanes(commit)) if f"lane:{lane}" in paths)

    def move_head(self, n):
        sha = self._new(self.lanes(self.heads[n]), (self.heads[n],))
        self.heads[n] = sha
        self.branches[f"pr-{n}"] = sha
        return sha

    def reland(self, base, tree, title, kind=None):
        self.reland_calls.append((base, tree, title))
        if self.reland_mismatch:
            return Reland("MISMATCH", tree="Tmismatch", reason="RELAND_TREE_MISMATCH")
        lanes = frozenset(int(x) for x in tree[1:].split(",") if x) if tree.startswith("T") else frozenset()
        sha = self._new(lanes, (base,))
        return Reland("CLEAN", commits=[sha], tree=self.tree(sha))

    def restack(self, old_parent, child, onto):
        """The child's own lanes replayed onto `onto`; a mismatch rebuilds a different tree."""
        self.restack_calls.append((old_parent, child, onto))
        lanes = self.lanes(child) | self.lanes(onto)
        sha = self._new(lanes - {max(lanes)} if self.restack_mismatch else lanes, (onto,))
        return Reland("CLEAN", commits=[sha], tree=self.tree(sha))


class DaemonGitHub(FakeGitHub):
    def __init__(self, repo, *, labels=None, bases=None, forks=(), rate_limited_list=0, bodies=None, **kw):
        super().__init__(repo, **kw)
        self.labels = {n: list(v) for n, v in (labels or {}).items()}
        self.bases = dict(bases or {})
        self.forks = set(forks)
        self.rate_limited_list = rate_limited_list
        self.rate_reads = 0
        self.bodies = dict(bodies or {})
        self.opened = []
        self._next_pr = 9000
        self.parents: dict = {}
        self.retargeted: list = []

    def rate_limit(self):
        self.rate_reads += 1
        return super().rate_limit()

    def snapshot(self):
        if self.rate_limited_list:
            self.calls["graphql"] += 1
            self.rate_limited_list -= 1
            raise RateLimited("GraphQL: API rate limit already exceeded for user ID 1.")
        rows = super().snapshot()
        for row in rows:
            n = row["number"]
            row["baseRefName"] = self.bases.get(n, "main")
            row["labels"] = [{"name": name} for name in self.labels.get(n, [])]
            row["isCrossRepository"] = n in self.forks
            row["body"] = self.bodies.get(n, "")
        return rows

    def open_pr(self, head, base, title, body):
        self.calls["rest"] += 1
        self._next_pr += 1
        number = self._next_pr
        self.opened.append({"number": number, "head": head, "base": base, "title": title, "body": body})
        return number

    def pulls_with_head(self, branch):
        self.calls["rest"] += 1
        return list(self.parents.get(branch, []))

    def retarget(self, number, base):
        self.calls["rest"] += 1
        self.retargeted = self.retargeted + [(number, base)]
        self.bases[number] = base

    def merge(self, number, sha):
        merged = super().merge(number, sha)
        self.repo.heads.pop(number, None)
        return merged


class Admission:
    """The admission verdict by head sha; a head it has no verdict for is MERGEABLE.
    `delay` and `running`/`peak` let a test observe whether concurrent admission reads
    actually overlap in wall time, the same shape as FakeGate's own peak tracking."""

    def __init__(self, verdicts=None, failures=None, delay=0.0):
        self.verdicts = dict(verdicts or {})
        self.failures = dict(failures or {})
        self.delay = delay
        self.calls = []
        self.running = 0
        self.peak = 0
        self.lock = threading.Lock()

    def __call__(self, head, base):
        with self.lock:
            self.calls.append((head, base))
            self.running += 1
            self.peak = max(self.peak, self.running)
        try:
            if self.delay:
                time.sleep(self.delay)
            if head in self.failures:
                raise self.failures.pop(head)
            return self.verdicts.get(head, {"verdict": "MERGEABLE", "residuals": []})
        finally:
            with self.lock:
                self.running -= 1


def git(cwd, *args, env=None):
    import subprocess

    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True,
                          env={**os.environ, **IDENT, **(env or {})}).stdout.strip()


IDENT = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
