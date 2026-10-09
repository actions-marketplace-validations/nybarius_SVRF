"""The batched train: fold a family of ready pull requests onto the base branch, gate the
folded tree once, land the family one merge commit at a time, and check that the base
branch ends on exactly the tree that was gated.

One run over a list of pull-request numbers:

1. One pull-request list snapshot (the rate budget read first, and waited out below
   `rate_floor`).
2. The pairwise conflict read: which heads conflict with which, on which paths. A
   conflict only on union-merged paths does not separate two heads. The largest
   pairwise-compatible set is kept; a head that conflicts with it is OUT for this run,
   named with its partners and paths.
3. The kept heads are folded onto the base in order with the union step (git's merge of
   the base into the head, union paths resolved by union), which is the same step the
   landing performs, and cut into families of `family_size`. Family k is gated on the
   fold of families 1..k (speculative stacking), so families gate in parallel (`jobs`)
   and every gated tree is still exactly a tree the base branch will hold.
4. A green family lands in order. For each pull request the base is re-read, the union
   step is recomputed on the current base and must equal the planned tree, is pushed to
   the pull request's branch by refspec, mergeability is read, and the pull request is
   merged with a merge commit pinned to that sha. The merged tree is fetched and compared
   with the planned tree; a mismatch alerts and stops the train. A commit landing on the
   base from outside between two merges stops the family, and the rest is regated.
5. A red family is bisected: each half is replanned on the current base and gated; a
   green half lands, a red half is split again, and a red single pull request is held
   with its failing lines.
6. A failed read is never a conflict or a hold: it is retried later. A rate limit is
   waited out until its reported reset.

What the gate covers: the tree each family (or bisected segment) ends on. The
intermediate trees between two merges of one family are prefix folds that were not
gated; a family stopped midway is recorded PREFIX_LANDED_UNGATED.

The receipt (one JSON file per run) is rewritten after every event, so an interrupted
run leaves its last state on disk.
"""

from __future__ import annotations

import itertools
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from . import pr_surface
from .errors import RateLimited, ReadFailed
from .rules import choose_families, chunk
from .tree_provider import reason_class

SCHEMA = "svrf.receipt/1"

# A per-process counter for receipt filenames. `id(self)` is not a safe discriminator:
# CPython may reuse a freed Train instance's address for the next one, so two runs
# started within the same wall-clock second (the timestamp's own resolution) and the
# same pid could otherwise produce the identical receipt filename, and the second
# `_save()` would silently overwrite the first run's receipt.
_receipt_serial = itertools.count()


class Train:
    def __init__(self, git, github, gate, *, receipts: Path, jobs: int = 1, family_size: int | str = 8,
                 family_cap: int = 8,
                 memory=None, clock=time.time, sleep=time.sleep, rate_floor: int = 200, rate_retries: int = 6,
                 poll_seconds: float = 5, poll_tries: int = 36, max_rounds: int = 8, dry_run: bool = False,
                 comment: bool = True, is_union: Callable[[str], bool] = lambda p: False,
                 pr_comments: bool = True, status_checks: bool = True, dashboard_url: str = "",
                 hold_label: str = "train:hold", tree_provider=None):
        self.git, self.gh, self.gate = git, github, gate
        self.jobs, self.memory = max(1, jobs), memory
        self.family_cap = max(1, family_cap)
        self.family_size = "adaptive" if family_size == "adaptive" else max(1, family_size)
        self.receipts_dir = Path(receipts).expanduser()
        self.clock, self.sleep = clock, sleep
        self.rate_floor, self.rate_retries = rate_floor, rate_retries
        self.poll_seconds, self.poll_tries = poll_seconds, poll_tries
        self.max_rounds, self.dry_run, self.comment = max_rounds, dry_run, comment
        self.is_union = is_union
        self.hold_label = hold_label
        self.tree_provider = tree_provider
        self.pr_comments, self.status_checks, self.dashboard_url = pr_comments, status_checks, dashboard_url
        self.lock = threading.RLock()
        self.rows: dict[int, dict] = {}
        self.stopped = False
        self.red_trees: dict[str, int] = {}
        self._families = 0
        self._queue_position: dict[int, int] = {}
        self.surface_history: dict[int, list[str]] = {}
        self._surface_calls = {"status": 0, "comment_write": 0, "comment_skip": 0}
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(clock()))
        receipts = Path(receipts).expanduser()
        receipts.mkdir(parents=True, exist_ok=True)
        self.path = receipts / f"train-{stamp}-{os.getpid()}-{next(_receipt_serial):04d}.json"
        self.receipt: dict = {
            "schema_version": SCHEMA, "started": stamp, "finished": None,
            "config": {"jobs": self.jobs, "family_size": self.family_size, "family_cap": self.family_cap,
                       "dry_run": dry_run,
                       "rate_floor": rate_floor, "max_rounds": max_rounds,
                       "memory": None if memory is None else {"need_gb": memory.need_gb,
                                                              "reserve_gb": memory.reserve_gb}},
            "requested": [], "prs": {}, "pairs": None, "out": {}, "rounds": [], "families": [], "gates": [],
            "merges": [], "holds": [], "retry_later": [], "pending": [], "alerts": [], "rate_waits": [],
            "batching": [],
            "api_calls": {}, "surface": dict(self._surface_calls), "stopped": False}
        if tree_provider is not None:
            self.receipt["tree_provider"] = {"consulted": 0, "provider": 0, "git": 0, "reasons": {}}

    # ---- receipt

    def _save(self) -> None:
        with self.lock:
            self.receipt["api_calls"] = dict(getattr(self.gh, "calls", {}))
            self.receipt["surface"] = dict(self._surface_calls)
            self.receipt["stopped"] = self.stopped
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.receipt, indent=1, sort_keys=True, default=str) + "\n", encoding="utf-8")
            tmp.replace(self.path)

    def _add(self, key: str, row: dict) -> None:
        with self.lock:
            self.receipt[key].append(row)
        self._save()

    def hold(self, number: int, reason: str, paths=(), failing=(), *, batch: str | None = None,
             gate_seconds: float | None = None, conflict_with: list[int] | None = None) -> None:
        paths, failing = list(paths), list(failing)
        self._add("holds", {"number": number, "reason": reason, "paths": paths, "failing": failing})
        self._surface_event(number, "HELD", batch=batch, gate_seconds=gate_seconds, failing=failing,
                            conflict_with=conflict_with, conflict_paths=paths if conflict_with else None,
                            reason_line=self._reason_line(reason, paths, failing, conflict_with))

    def retry_later(self, number: int | None, reason: str) -> None:
        self._add("retry_later", {"number": number, "reason": reason})

    # ---- the pull-request surface: a commit status and a living comment

    def _reason_line(self, reason: str, paths: list[str], failing: list[str],
                     conflict_with: list[int] | None) -> str:
        """A one-line, human reason for a hold: what a developer reads in the status and
        the comment, not the internal reason code."""
        if conflict_with:
            where = f" on {paths[0]}" if paths else ""
            return "conflicts with " + " ".join(f"#{p}" for p in conflict_with) + where
        if reason == "CONFLICT" and paths:
            return "conflicts on " + ", ".join(paths[:3])
        if failing:
            return pr_surface.one_line(str(failing[0]))
        return reason.replace("_", " ").lower()

    def _surface_event(self, number: int, phase: str, **fields) -> None:
        """Render and post the commit status and/or the living comment for one pull
        request's phase transition. Best-effort: a failed read never turns a surface
        update into a train failure, and nothing is posted for a pull request whose head
        sha is not yet known (never gated, no commit to attach a status to)."""
        if not (self.status_checks or self.pr_comments):
            return
        history = self.surface_history.setdefault(number, [])
        event = {"number": number, "phase": phase, "position": self._queue_position.get(number),
                 "history": list(history)}
        event.update({k: v for k, v in fields.items() if v is not None})
        if phase not in history:
            history.append(phase)
        sha = self.rows.get(number, {}).get("head_sha")
        if not sha:
            return
        if self.status_checks:
            state, description = pr_surface.status_line(event)
            try:
                self.call(self.gh.set_status, sha, state, description, pr_surface.CONTEXT,
                         self.dashboard_url or None)
                self._surface_calls["status"] += 1
            except ReadFailed:
                pass
        if self.pr_comments:
            self._upsert_comment(number, event)

    def _upsert_comment(self, number: int, event: dict) -> None:
        body = pr_surface.render_comment(event)
        digest = pr_surface.comment_hash(body)
        try:
            comments = self.call(self.gh.list_comments, number)
        except ReadFailed:
            return
        existing = next((c for c in comments if pr_surface.is_surface_comment(c.get("body") or "")), None)
        if existing is not None and pr_surface.comment_hash(existing.get("body") or "") == digest:
            self._surface_calls["comment_skip"] += 1
            return
        try:
            if existing is not None:
                self.call(self.gh.update_comment, existing["id"], body)
            else:
                self.call(self.gh.comment, number, body)
            self._surface_calls["comment_write"] += 1
        except ReadFailed:
            pass

    def alert(self, alert: str, **detail) -> None:
        self._add("alerts", {"alert": alert, **detail})

    # ---- GitHub, frugally

    def _wait_until(self, reset: float | None, reason: str) -> None:
        now = self.clock()
        until = reset if reset is not None else now + 300
        self._add("rate_waits", {"reason": reason, "at": now, "until": until})
        self.sleep(max(1.0, until - now))

    def call(self, fn, *args):
        """A GitHub call; a rate limit is waited out until the reported reset."""
        for _ in range(self.rate_retries):
            try:
                return fn(*args)
            except RateLimited as limited:
                reset = limited.reset
                if reset is None:
                    try:
                        budget = self.gh.rate_limit()
                        reset = max(float(v.get("reset", 0)) for v in budget.values())
                    except ReadFailed:
                        reset = None
                self._wait_until(reset, limited.reason)
        raise ReadFailed("RATE_LIMITED:RETRIES_EXHAUSTED")

    def rate_guard(self) -> None:
        """Before a round: pause while any GitHub budget is below the floor."""
        for _ in range(self.rate_retries):
            try:
                budget = self.gh.rate_limit()
            except ReadFailed as failure:
                self._wait_until(self.clock() + 60, failure.reason)
                continue
            low = [(k, v) for k, v in sorted(budget.items()) if int(v.get("remaining", 0)) < self.rate_floor]
            if not low:
                return
            kind, value = low[0]
            self._wait_until(float(value.get("reset", self.clock() + 300)),
                             f"RATE_FLOOR:{kind}:{value.get('remaining')}<{self.rate_floor}")

    # ---- the merge step

    def merge_step(self, acc: str, head: str, message: str):
        """The union step, as always. With a tree provider, a step that git merged into a
        new commit is also offered to the provider; its tree is used only when it is
        git's own tree. Returns the step and, with a provider, what to record about it."""
        step = self.git.union_step(acc, head, message)
        if self.tree_provider is None or step.status != "CLEAN" or step.commit in (acc, head):
            return step, {}
        try:
            proposed, reason = self.tree_provider.propose(head, acc)
        except Exception as error:  # a provider failure is never a landing failure
            proposed, reason = None, f"PROVIDER_FAILED:{type(error).__name__}"
        if reason is None and proposed != step.tree:
            reason = "PROVIDER_MISMATCH"
        record = {"tree_source": "git" if reason else "provider"}
        if reason:
            record["tree_provider"] = reason
            if proposed is not None:
                record["proposed_tree"] = proposed
        with self.lock:
            counts = self.receipt["tree_provider"]
            counts["consulted"] += 1
            counts[record["tree_source"]] += 1
            if reason:
                key = reason_class(reason)
                counts["reasons"][key] = counts["reasons"].get(key, 0) + 1
        return step, record

    # ---- planning and gating

    def plan(self, numbers: list[int], base: str, parent: str | None = None) -> dict | None:
        """Fold the heads onto `base` with the union step. A conflict is held with its
        paths, a failed read is retried later; the rest form one family."""
        acc, steps = base, []
        for n in numbers:
            row = self.rows[n]
            step, record = self.merge_step(acc, row["head_sha"], f"train preview #{n}")
            if step.status == "CONFLICT":
                self.hold(n, "CONFLICT", paths=step.conflicts)
            elif step.status != "CLEAN":
                self.retry_later(n, step.reason or "UNION_STEP_FAILED")
            elif step.commit == acc:
                self.retry_later(n, "ALREADY_MERGED")
            else:
                steps.append({"number": n, "commit": step.commit, "tree": step.tree, **record})
                acc = step.commit
        if not steps:
            return None
        with self.lock:
            self._families += 1
            ident = f"F{self._families}"
        family = {"id": ident, "parent": parent, "prs": [s["number"] for s in steps], "base": base,
                  "commit": steps[-1]["commit"], "tree": steps[-1]["tree"], "steps": steps,
                  "status": "PLANNED", "gate": None}
        self._add("families", family)
        for n in family["prs"]:
            self._surface_event(n, "QUEUED", batch=ident, members=family["prs"])
        return family

    def gate_family(self, family: dict, base: str) -> dict:
        """One gate of the family's folded tree, admitted by the memory guard."""
        for n in family["prs"]:
            self._surface_event(n, "GATING", batch=family["id"], members=family["prs"])
        with self.lock:
            known = self.red_trees.get(family["tree"])
        if known is not None:
            # a tree already gated red is never landed: reuse the red result
            row = {**self.receipt["gates"][known], "family": family["id"], "prs": family["prs"], "base": base,
                   "commit": family["commit"], "reused": known, "seconds": 0.0}
            with self.lock:
                family["gate"] = len(self.receipt["gates"])
                family["status"] = "RED"
            self._add("gates", row)
            return row
        started = self.clock()
        wall = time.monotonic()
        try:
            if self.memory is not None:
                with self.memory.admit():
                    result = self.gate.run(base, family["commit"], family["id"])
            else:
                result = self.gate.run(base, family["commit"], family["id"])
        except Exception as error:  # a gate that could not run is retried, not red
            result = {"green": False, "read_failure": f"GATE_FAILED:{type(error).__name__}:{error}"}
        result = dict(result)
        if result.get("tree") not in (None, family["tree"]):
            result["green"] = False
            result.setdefault("failing", []).append(
                f"GATED_TREE_MISMATCH gated {result.get('tree')} planned {family['tree']}")
        row = {"family": family["id"], "prs": family["prs"], "base": base, "commit": family["commit"],
               "started": started, "seconds": round(time.monotonic() - wall, 3), **result}
        row["tree"] = family["tree"]
        with self.lock:
            family["gate"] = len(self.receipt["gates"])
            family["status"] = "GREEN" if row.get("green") else ("GATE_RETRY" if row.get("read_failure") else "RED")
            if not row.get("green") and not row.get("read_failure"):
                self.red_trees.setdefault(family["tree"], family["gate"])
        self._add("gates", row)
        return row

    # ---- landing

    def _stop_family(self, family: dict, index: int, status: str) -> None:
        family["status"] = "PREFIX_LANDED_UNGATED" if index else status
        self._save()

    def _publication_withheld(self, pull: dict, family: dict, index: int) -> bool:
        labels = pull.get("labels")
        if (type(labels) is not list
                or any(type(row) is not dict or type(row.get("name")) is not str for row in labels)):
            raise ReadFailed("PULL_LABELS_UNOBSERVED")
        if any(row["name"] == self.hold_label for row in labels):
            self.retry_later(family["steps"][index]["number"], f"HOLD_LABEL:{self.hold_label}")
            self._stop_family(family, index, "WITHHELD")
            return True
        return False

    def land_family(self, family: dict, expected: str, gate_seconds: float = 0.0) -> tuple[bool, list[int]]:
        """Land the family's pull requests in order, checking every landed tree against the
        planned one. Returns whether the whole family landed and what to requeue."""
        steps = family["steps"]
        for index, planned in enumerate(steps):
            n = planned["number"]
            rest = [s["number"] for s in steps[index:]]
            row = self.rows[n]
            try:
                current = self.git.main_sha()
                if current != expected:
                    self.alert("BASE_MOVED_EXTERNALLY", family=family["id"], before=n,
                               expected=expected, observed=current)
                    self._stop_family(family, index, "REQUEUED")
                    return False, rest
                pull = self.call(self.gh.pull, n)
                if self._publication_withheld(pull, family, index):
                    return False, rest[1:]
                if pull.get("head_sha") != row["head_sha"]:
                    self.alert("HEAD_MOVED", family=family["id"], number=n, snapshot=row["head_sha"],
                               observed=pull.get("head_sha"))
                    self._stop_family(family, index, "REQUEUED")
                    return False, rest
                prepared, record = self.merge_step(current, row["head_sha"], f"Merge base into {row['head_ref']}")
                if prepared.status != "CLEAN" or prepared.tree != planned["tree"]:
                    self.alert("PREPARE_DIVERGED", family=family["id"], number=n, status=prepared.status,
                               planned=planned["tree"], prepared=prepared.tree)
                    self._stop_family(family, index, "REQUEUED")
                    return False, rest
                if prepared.commit != row["head_sha"]:
                    self.git.push_branch(prepared.commit, row["head_ref"])
                pull = self._await_mergeable(n, prepared.commit)
                if self._publication_withheld(pull, family, index):
                    return False, rest[1:]
                if pull.get("draft"):
                    self.call(self.gh.ready, n)
                merged = self.call(self.gh.merge, n, prepared.commit)
            except ReadFailed as failure:
                self.retry_later(n, failure.reason)
                self._stop_family(family, index, "RETRY")
                return False, rest
            row["head_sha"] = prepared.commit
            observed, parents, unread = self._read_landed(merged)
            if observed is None:
                # the merge happened and its tree could not be read: never a mismatch
                self.retry_later(n, f"LANDED_TREE_UNREAD:{unread}")
                self._add("merges", {"number": n, "family": family["id"], "head": prepared.commit, "merge": merged,
                                     "parents": [], "gated_tree": planned["tree"], "observed_tree": None,
                                     "identity": None, "at": self.clock(), **record})
                family["status"] = "LANDED_TREE_UNREAD"
                self._save()
                return False, [s["number"] for s in steps[index + 1:]]
            identity = observed == planned["tree"]
            self._add("merges", {"number": n, "family": family["id"], "head": prepared.commit, "merge": merged,
                                 "parents": parents, "gated_tree": planned["tree"], "observed_tree": observed,
                                 "identity": identity, "at": self.clock(), **record})
            if not identity:
                self.alert("TREE_MISMATCH", family=family["id"], number=n, planned=planned["tree"],
                           observed=observed, merge=merged)
                family["status"] = "TREE_MISMATCH"
                self.stopped = True
                self._save()
                return False, []
            if self.comment:
                try:
                    self.call(self.gh.comment, n,
                              f"Merged by SVRF in family {family['id']} {family['prs']}, gated once on tree "
                              f"{family['tree'][:12]}; the landed tree {observed[:12]} was checked against the "
                              f"planned tree. Receipt: {self.path.name}.")
                except ReadFailed as failure:
                    self.retry_later(n, f"COMMENT:{failure.reason}")
            self._surface_event(n, "LANDED", batch=family["id"], gate_seconds=gate_seconds)
            expected = merged
        family["status"] = "LANDED"
        family["landed"] = expected
        self._save()
        return True, []

    def _read_landed(self, merged: str, tries: int = 3) -> tuple[str | None, list[str], str | None]:
        """The merge commit's tree and parents, after fetching that commit by sha."""
        failure = None
        for _ in range(tries):
            try:
                fetch_commit = getattr(self.git, "fetch_commit", None)
                if fetch_commit is not None:
                    fetch_commit(merged)
                else:
                    self.git.fetch([])
                return self.git.tree(merged), self.git.parents(merged), None
            except ReadFailed as error:
                failure = error.reason
                self.sleep(self.poll_seconds)
        return None, [], failure

    def _await_mergeable(self, n: int, commit: str) -> dict:
        for _ in range(self.poll_tries):
            pull = self.call(self.gh.pull, n)
            if pull.get("head_sha") == commit and pull.get("mergeable") is True:
                return pull
            if pull.get("head_sha") == commit and pull.get("mergeable") is False:
                raise ReadFailed("GITHUB_NOT_MERGEABLE")
            self.sleep(self.poll_seconds)
        raise ReadFailed("MERGEABILITY_UNREAD")

    # ---- bisection

    def settle_red(self, numbers: list[int], result: dict, parent: str) -> list[int]:
        """A red family: a single pull request is held with its failing lines; otherwise
        each half is replanned on the current base and gated, a green half lands and a red
        half is split again. Returns what to requeue."""
        if len(numbers) == 1:
            self.hold(numbers[0], "GATE_RED", failing=list(result.get("failing") or [])[:12], batch=parent,
                     gate_seconds=result.get("seconds"))
            return []
        half = len(numbers) // 2
        requeue: list[int] = []
        for part in (numbers[:half], numbers[half:]):
            if self.stopped:
                requeue.extend(part)
                continue
            try:
                base = self.git.main_sha()
            except ReadFailed as failure:
                self.alert("BASE_UNREAD", reason=failure.reason)
                requeue.extend(part)
                continue
            family = self.plan(part, base, parent=parent)
            if family is None:
                continue
            gated = self.gate_family(family, base)
            if gated.get("read_failure"):
                for n in family["prs"]:
                    self.retry_later(n, gated["read_failure"])
                continue
            if gated.get("green"):
                _, rest = self.land_family(family, base, gated.get("seconds", 0.0))
                requeue.extend(rest)
            else:
                family["status"] = "BISECTED" if len(family["prs"]) > 1 else "HELD"
                requeue.extend(self.settle_red(family["prs"], gated, family["id"]))
        return requeue

    # ---- rounds

    def snapshot(self, numbers: list[int], rows: list[dict] | None = None) -> list[int]:
        """One pull-request list read (or the caller's own read of it this round)."""
        if rows is None:
            self.rate_guard()
            rows = self.call(self.gh.snapshot)
        by_number = {int(r["number"]): r for r in rows}
        present = []
        for n in numbers:
            r = by_number.get(n)
            if r is None:
                self.retry_later(n, "NOT_OPEN_IN_SNAPSHOT")
                continue
            self.rows[n] = {"number": n, "head_sha": r.get("headRefOid") or r.get("head_sha"),
                            "head_ref": r.get("headRefName") or r.get("head_ref"),
                            "draft": bool(r.get("isDraft")), "title": r.get("title", "")}
            present.append(n)
        with self.lock:
            self.receipt["prs"].update({str(n): dict(self.rows[n]) for n in present})
        self.git.fetch(present)
        return present

    def round(self, queue: list[int]) -> list[int]:
        self._queue_position = {n: i + 1 for i, n in enumerate(queue)}
        base = self.git.main_sha()
        families = []
        size = self.family_size
        if size == "adaptive":
            from .batching import choose
            choice = choose(self.receipts_dir, self.family_cap)
            size = choice["family_size"]
            self._add("batching", choice)
        for part in chunk(queue, size):
            # a family boundary is a planning boundary; the fold continues across it
            family = self.plan(part, families[-1]["commit"] if families else base)
            if family is not None:
                families.append(family)
        self._add("rounds", {"base": base, "queue": list(queue), "families": [f["id"] for f in families]})
        if not families:
            return []
        executor = ThreadPoolExecutor(max_workers=self.jobs, thread_name_prefix="gate")
        futures = [executor.submit(self.gate_family, f, base) for f in families]
        requeue: list[int] = []
        expected = base
        try:
            for index, (family, future) in enumerate(zip(families, futures)):
                later = [n for f in families[index + 1:] for n in f["prs"]]
                gated = future.result()
                if self.dry_run:
                    family["status"] = "GATED_DRY" if gated.get("green") else "RED_DRY"
                    self._save()
                    continue
                if self.stopped:
                    requeue.extend(family["prs"] + later)
                    break
                if gated.get("green"):
                    ok, rest = self.land_family(family, expected, gated.get("seconds", 0.0))
                    if not ok:
                        requeue.extend(rest + later)
                        break
                    expected = family["landed"]
                    continue
                for f in families[index + 1:]:
                    f["status"] = "SPECULATION_VOID"
                if gated.get("read_failure"):
                    for n in family["prs"]:
                        self.retry_later(n, gated["read_failure"])
                    self.receipt["pending"].extend(family["prs"])
                    requeue.extend(later)
                    break
                family["status"] = "BISECTED" if len(family["prs"]) > 1 else "HELD"
                requeue.extend(self.settle_red(family["prs"], gated, family["id"]))
                requeue.extend(later)
                break
        finally:
            for future in futures:
                future.cancel()
            executor.shutdown(wait=False, cancel_futures=True)
            self._save()
        return requeue

    def run(self, numbers: list[int], rows: list[dict] | None = None) -> dict:
        self.receipt["requested"] = list(numbers)
        try:
            queue = self.snapshot(list(numbers), rows)
            try:
                base = self.git.main_sha()
                pairs = self.git.pairs([{"number": n, "head_sha": self.rows[n]["head_sha"]} for n in queue], base)
            except ReadFailed as failure:
                self.alert("PAIRS_UNREAD", reason=failure.reason)
                pairs = {"read": 0, "conflicts": [], "unreadable": []}
            self.receipt["pairs"] = pairs
            chosen, out = choose_families(queue, pairs.get("conflicts", []), pairs.get("unreadable", []),
                                          self.is_union)
            self.receipt["out"] = {str(n): v for n, v in out.items()}
            self._save()
            for n, info in out.items():
                # left out of this round by a pairwise conflict, not a hold: never recorded
                # in receipt["holds"] or the daemon's held memory, just shown as held here
                partners, paths = info.get("with") or [], info.get("paths") or []
                self._surface_event(n, "HELD", conflict_with=partners, conflict_paths=paths if partners else None,
                                    reason_line=self._reason_line("CONFLICT", paths, [], partners))
            queue = chosen
            rounds = 0
            while queue and not self.stopped and rounds < self.max_rounds:
                rounds += 1
                if rounds > 1:
                    queue = self.snapshot(queue)
                queue = self.round(queue)
                if self.dry_run:
                    break
            self.receipt["pending"] = sorted(set(self.receipt["pending"]) | set(queue))
        except ReadFailed as failure:
            self.alert("RUN_UNREAD", reason=failure.reason)
        self.receipt["finished"] = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(self.clock()))
        self._save()
        return self.receipt
