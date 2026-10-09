"""The automatic train: one owner, one round per tick, candidates discovered from one
pull-request list snapshot, admitted by the admission check, landed by the batched train.

A round:

1. The owner lock (an flock on `<state_dir>/svrf.lock`). A second owner reads nothing
   and returns LOCKED.
2. The rate budget and one pull-request list. A failed or rate-limited read ends the
   round as RETRY and changes no memory.
3. Each open pull request is read from its snapshot row. First, a local ancestry read of
   its head against the base branch: a head the base already contains is ALREADY_MERGED
   (never gated, never held): its branch is fast-forwarded to the base so GitHub records
   it merged, or, if that push is refused, it gets one comment and is closed. A held pull
   request is not read again until its head, or the base branch's content over the paths
   its hold depended on, changes. Forks, drafts and the hold label are skipped. A stacked
   pull request (based on another branch) waits while its parent is open, is retargeted
   to the base once its parent merged, and is held if its parent closed unmerged.
4. The candidates are fetched and read by the admission check. MERGEABLE is admitted. A
   mechanical hold (conflicts only on union-merge paths; GitHub refusing a head that
   merges cleanly here) is repaired by a union merge pushed to the branch, and the new
   head is read next round. A history-order refusal (if that check is on) is re-landed:
   the head's final tree is rebuilt as tests -> code -> docs commits on the base, pushed
   to `<branch>-ordered`, and a new pull request supersedes the original, provided the
   rebuilt tree is identical and the new history passes the original admission check
   before publication. Anything else is held
   with its residual lines. A failed read is retried next round.
5. Completed admission groups go to the batched train (`Train.run`, reusing the round's
   snapshot) while other admissions continue. A stopped train stops later groups too.
6. `<state_dir>/state.json` (the held memory) and `<state_dir>/held.json` (every held pull
   request with its head, reason, failing lines and paths) are rewritten.
"""

from __future__ import annotations

import json
import math
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Callable

from . import rules
from .errors import RateLimited, ReadFailed
from .locks import owner_lock
from .train import Train

STATE_SCHEMA = "svrf.state/1"
HELD_SCHEMA = "svrf.held/1"


def _observed_reset(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _stamp(clock) -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(clock()))


def _strip_trailers(text: str) -> str:
    """A body copied onto a superseding pull request keeps its prose, not its trailers."""
    return "\n".join(line for line in text.splitlines() if not line.lower().startswith("co-authored-by:"))


class Daemon:
    def __init__(self, git, github, gate, admission, *, state_dir: Path, receipts: Path, base: str = "main",
                 clock=time.time, sleep=time.sleep, train_options: dict | None = None, dry_run: bool = False,
                 rate_floor: int = 200, lock_path: Path | None = None, hold_label: str = "train:hold",
                 is_union: Callable[[str], bool] = lambda p: False, repair: bool = True, reland: bool = True,
                 kind: Callable[[str], str] | None = None, history_verdict: Callable[[str, str], str] | None = None,
                 admission_watch: list[str] | None = None, jobs: int = 1, memory=None, demand=None,
                 admission_jobs: int | None = None):
        self.git, self.gh, self.gate, self.admission = git, github, gate, admission
        self.state_dir = Path(state_dir).expanduser()
        self.receipts = Path(receipts).expanduser()
        self.base = base
        self.clock, self.sleep = clock, sleep
        self.train_options = {"max_rounds": 3, **(train_options or {})}
        if memory is not None:
            self.train_options["memory"] = memory
        self.dry_run, self.rate_floor = dry_run, rate_floor
        self.lock_path = Path(lock_path).expanduser() if lock_path else self.state_dir / "svrf.lock"
        self.hold_label, self.is_union = hold_label, is_union
        self.repair_enabled, self.reland_enabled = repair, reland
        self.kind = kind or (lambda p: "code")
        # Paths every hold depends on whatever it changed: the environment its verdict ran in.
        self.admission_watch = list(admission_watch or [])
        self.history_verdict = history_verdict
        # A slow admission command (one PR's package suite) must never block another
        # PR's admission read: up to `jobs` heads are read at once, admitted by the same
        # memory guard the gate uses (both can run the same heavy suite), the way
        # `Train.round` already gates independent families concurrently.
        self.jobs, self.memory = max(1, jobs), memory
        # admission reads overlap up to their own count; None borrows the gate's jobs as before
        self.admission_jobs = max(1, admission_jobs) if admission_jobs else self.jobs
        self.demand = demand
        self.round_rows, self.round_inputs = {}, {}

    # ---- memory

    @property
    def state_path(self) -> Path:
        return self.state_dir / "state.json"

    def load_state(self) -> dict:
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
            if value.get("schema_version") == STATE_SCHEMA and isinstance(value.get("held"), dict):
                for key in ("merged_elsewhere", "relanded", "bases"):
                    value.setdefault(key, {})
                return value
        except (OSError, ValueError):
            pass
        return {"schema_version": STATE_SCHEMA, "held": {}, "merged_elsewhere": {}, "relanded": {}, "bases": {}}

    def _write(self, path: Path, value: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(value, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(path)

    def save(self, state: dict, summary: dict) -> None:
        if self.dry_run:
            return
        state["last_tick"] = {k: summary[k] for k in (
            "tick", "at", "receipt", "receipts", "stopped", "reason", "retry_at",
            "admitted", "held", "merged", "skipped", "retry_later", "reasons", "tree_provider",
        ) if k in summary}
        state["last_tick"]["admission_sources"] = {
            str(n): {"head": inputs["row"].get("headRefOid"), "base": inputs["admission_base"]}
            for n, inputs in self.round_inputs.items() if "admission_base" in inputs
        }
        self._write(self.state_path, state)
        rows = [{"number": int(n), **entry} for n, entry in sorted(state["held"].items(), key=lambda kv: int(kv[0]))]
        self._write(self.state_dir / "held.json", {"schema_version": HELD_SCHEMA, "generated": summary["at"],
                                                   "held": rows})

    def forget(self, numbers: list[int]) -> dict:
        with owner_lock(self.lock_path) as owned:
            if not owned:
                return {"tick": "LOCKED"}
            state = self.load_state()
            dropped = [n for n in numbers if state["held"].pop(str(n), None) is not None]
            summary = {"tick": "FORGOT", "at": _stamp(self.clock), "forgot": dropped}
            self.save(state, summary)
            released = getattr(self.demand, "released", None)
            if callable(released) and not self.dry_run:
                released(self, numbers)
            return summary

    # ---- one round

    def tick(self) -> dict:
        with owner_lock(self.lock_path) as owned:
            if not owned:
                return {"tick": "LOCKED", "at": _stamp(self.clock)}
            if self.demand is not None:
                return self.demand.drain(self)
            return self._tick()

    def _retry(self, summary: dict, n: int, reason: str) -> None:
        if n not in summary["retry_later"]:
            summary["retry_later"].append(n)
        summary["reasons"][str(n)] = reason

    def _read_admission(self, row: dict, base: str) -> tuple[dict | None, Exception | None]:
        """One admission read, run on the executor: admitted by the memory guard the gate
        also uses (the admission command can run the same heavy suite), so this and a
        running gate never together exceed the host's reserved memory."""
        head = row.get("headRefOid")
        try:
            if self.memory is not None:
                with self.memory.admit():
                    return self.admission(head, base), None
            return self.admission(head, base), None
        except Exception as error:  # surfaced to the caller, which sorts ReadFailed from the rest
            return None, error

    def _tick(self, requested: set[int] | None = None, changed: dict | None = None) -> dict:
        self.round_rows, self.round_inputs = {}, {}
        summary = {"tick": "IDLE", "at": _stamp(self.clock), "admitted": [], "held": [], "held_unchanged": [],
                   "repaired": [], "would_repair": [], "retargeted": [], "would_retarget": [], "skipped": {},
                   "retry_later": [], "reasons": {}, "merged": [], "merged_elsewhere": [],
                   "would_close_merged": [], "held_rows": [], "relanded": [], "would_reland": [], "restacked": [], "would_restack": [],
                   "out": [],
                   "receipt": None, "receipts": [], "stopped": False, "dry_run": self.dry_run}
        state = self.load_state()
        try:
            budget = self.gh.rate_limit()
            low = [(k, v) for k, v in sorted(budget.items()) if int(v.get("remaining", 0)) < self.rate_floor]
            if low:
                kind, value = low[0]
                summary.update(tick="RATE_FLOOR", reason=f"RATE_FLOOR:{kind}:{value.get('remaining')}<{self.rate_floor}")
                resets = [v.get("reset") for _, v in low if _observed_reset(v.get("reset"))]
                if resets:
                    summary["retry_at"] = max(resets)
                self.save(state, summary)
                return summary
            rows = self.gh.snapshot()
        except ReadFailed as failure:
            summary.update(tick="RETRY", reason=failure.reason)
            if isinstance(failure, RateLimited) and _observed_reset(failure.reset):
                summary["retry_at"] = failure.reset
            self.save(state, summary)
            return summary
        by_number = {int(r["number"]): r for r in rows}
        self.round_rows = by_number
        stacked_on = {r.get("baseRefName") for r in rows}
        for memory in (state["held"], state["bases"], state["merged_elsewhere"], state["relanded"]):
            for n in [n for n in memory if int(n) not in by_number]:
                if memory is state["relanded"] and memory[n].get("from_ref") in stacked_on:
                    continue  # a pull request still stacked on the re-landed branch reads this record
                del memory[n]
        heads = {r.get("headRefName") for r in rows}
        candidates: list[dict] = []
        base_now: dict = {"sha": None, "fetched": False}
        try:
            base_sha = self.git.main_sha() if by_number else None
        except ReadFailed:
            base_sha = None
        for n in sorted(by_number):
            if requested is not None and n not in requested:
                continue
            row = by_number[n]
            self.round_inputs[n] = {"row": dict(row)}
            if (changed or {}).get(n, set()) & {"base", "parent"}:
                state["bases"].pop(str(n), None)
                if state["held"].get(str(n), {}).get("reason") == "PARENT_CLOSED_UNMERGED":
                    state["held"].pop(str(n), None)
            held = state["held"].get(str(n))
            contained = self._contained(base_sha, row)
            self.round_inputs[n].setdefault("contained", contained)
            decision, _ = rules.admission(row, held, self.watch_digest(held, row, base_now),
                                          carried=contained, base=self.base,
                                          hold_label=self.hold_label)
            if decision == "ALREADY_MERGED":
                self.close_merged(row, base_sha, state, summary)
            elif decision == "CANDIDATE":
                candidates.append(row)
            elif decision == "HELD_UNCHANGED":
                summary["held_unchanged"].append(n)
            elif decision == "NOT_AGAINST_BASE":
                self.stacked(row, heads, state, summary)
            else:
                summary["skipped"][str(n)] = decision
        if not candidates:
            self.save(state, summary)
            return summary
        summary["tick"] = "RAN"
        try:
            self.git.fetch([int(r["number"]) for r in candidates])
            base = self.git.main_sha()
        except ReadFailed as failure:
            summary.update(tick="RETRY", reason=failure.reason)
            self.save(state, summary)
            return summary
        admitted: list[int] = []
        watch = None
        if self.admission_watch:
            try:
                watch = self.git.watch_digest(base, self.admission_watch)
            except ReadFailed as failure:
                watch = {"unobserved": failure.reason}
        for row in candidates:
            inputs = self.round_inputs[int(row["number"])]
            inputs["admission_base"] = base
            if self.admission_watch:
                inputs["admission_watch"] = {"paths": list(self.admission_watch), "value": watch}
        executor = ThreadPoolExecutor(max_workers=self.admission_jobs, thread_name_prefix="admission")
        try:
            pending = {executor.submit(self._read_admission, row, base): row for row in candidates}
            while pending:
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                batch: list[int] = []
                # Keep every result already available in the same group. Slow reads
                # do not order other heads; the train still owns folding and landing.
                for future in sorted(done, key=lambda f: int(pending[f]["number"])):
                    row = pending.pop(future)
                    n = int(row["number"])
                    value, failure = future.result()
                    if failure is not None:
                        if isinstance(failure, ReadFailed):
                            self._retry(summary, n, failure.reason)
                        else:  # a check that could not run is a read not made
                            self._retry(summary, n, f"ADMISSION_FAILED:{type(failure).__name__}:{failure}"[:200])
                        continue
                    state["held"].pop(str(n), None)
                    decision, cls = rules.admission_decision(value, self.is_union, reland=self.reland_enabled)
                    residuals = list(value.get("residuals") or [])
                    if decision == "ADMIT":
                        admitted.append(n)
                        batch.append(n)
                    elif summary["stopped"]:
                        self._retry(summary, n, "TRAIN_STOPPED")
                    elif decision == "ALREADY_MERGED":
                        self.close_merged(row, base, state, summary)
                    elif decision == "REPAIR" and self.repair_enabled:
                        self.repair(row, cls, residuals, state, summary)
                    elif decision == "RELAND":
                        self.reland(row, cls, state, summary)
                    elif decision in ("HOLD", "REPAIR"):
                        self.hold(row, "ADMISSION_HELD", state, summary, failing=residuals,
                                  extra=self._watch(base, self._consumed_paths(value)))
                    else:
                        self._retry(summary, n, cls or "ADMISSION_UNREAD")
                summary["admitted"] = sorted(admitted)
                if batch:
                    if summary["stopped"]:
                        for n in batch:
                            self._retry(summary, n, "TRAIN_STOPPED")
                    else:
                        self.land(batch, rows, by_number, state, summary)
                self.save(state, summary)
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        self.save(state, summary)
        return summary

    def land(self, admitted: list[int], rows: list[dict], by_number: dict, state: dict, summary: dict) -> None:
        train = Train(self.git, self.gh, self.gate, receipts=self.receipts, clock=self.clock, sleep=self.sleep,
                      dry_run=self.dry_run, is_union=self.is_union,
                      **{**self.train_options, "hold_label": self.hold_label})
        receipt = train.run(admitted, rows=rows)
        summary["receipt"] = str(train.path)
        summary["receipts"].append(str(train.path))
        summary["stopped"] = summary["stopped"] or receipt.get("stopped", False)
        summary["merged"].extend(m["number"] for m in receipt["merges"] if m.get("identity"))
        summary["api_calls"] = receipt.get("api_calls")
        summary["gates"] = summary.get("gates", 0) + len([g for g in receipt["gates"] if "reused" not in g])
        if "tree_provider" in receipt:
            total = summary.setdefault("tree_provider", {"consulted": 0, "provider": 0, "git": 0, "reasons": {}})
            for key in ("consulted", "provider", "git"):
                total[key] += receipt["tree_provider"][key]
            for reason, count in receipt["tree_provider"]["reasons"].items():
                total["reasons"][reason] = total["reasons"].get(reason, 0) + count
        repaired: set[int] = set()
        for hold in receipt["holds"]:
            n = int(hold["number"])
            row = by_number.get(n)
            if row is None or n in repaired:
                continue
            lines = [f"{rules.MERGE_CONFLICT}{p}" for p in hold.get("paths") or []] if hold["reason"] == "CONFLICT" else []
            cls = rules.repair_class(lines, self.is_union)
            if cls and self.repair_enabled:
                repaired.add(n)
                self.repair(row, cls, lines, state, summary)
            else:
                self.hold(row, hold["reason"], state, summary, failing=hold.get("failing") or [],
                          paths=hold.get("paths") or [])
        for entry in receipt["retry_later"]:
            n = entry.get("number")
            if n is None:
                continue
            n = int(n)
            if entry.get("reason") == "ALREADY_MERGED":
                if n not in summary["merged_elsewhere"]:
                    summary["merged_elsewhere"].append(n)
            elif entry.get("reason") == "GITHUB_NOT_MERGEABLE" and n not in repaired and n in by_number \
                    and self.repair_enabled:
                repaired.add(n)
                self.repair(by_number[n], "STALE_BASE", ["github:NOT_MERGEABLE"], state, summary)
            elif n not in summary["merged"] and n not in repaired:
                self._retry(summary, n, entry.get("reason") or "")
        summary["out"] = sorted(set(summary["out"]) | {int(n) for n in receipt.get("out", {})})

    # ---- what a hold depended on

    def watch_digest(self, held: dict | None, row: dict, base_now: dict) -> str | None:
        """The base branch's content over a held head's watched paths now, or None when it
        cannot be read or the head moved anyway. The base is read once per round and
        fetched only when it moved since the hold."""
        if not held or not held.get("watch") or held.get("head") != row.get("headRefOid"):
            return None
        digest = getattr(self.git, "watch_digest", None)
        if digest is None:
            return None
        try:
            if base_now["sha"] is None:
                base_now["sha"] = self.git.main_sha()
            if base_now["sha"] == held.get("base_sha"):
                return held.get("watch_digest")
            if not base_now["fetched"]:
                self.git.fetch([])
                base_now["fetched"] = True
            value = digest(base_now["sha"], held["watch"])
        except ReadFailed:
            return None
        if value == held.get("watch_digest"):
            held["base_sha"] = base_now["sha"]      # the base moved elsewhere: nothing reopens
        return value

    @staticmethod
    def _consumed_paths(value: dict) -> list[str]:
        """What a hold depends on: the paths the pull request changed and the paths its admission
        checks consumed (PATH, checker CODE, and conservative DIR rows). A failing check's verdict is
        a function of what it read, so the base moving over any of those is what reopens the hold,
        never only a move over the pull request's own paths."""
        paths = set(value.get("changed") or [])
        for row in value.get("consumed") or []:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
                continue
            if row.get("kind") in ("PATH", "CODE"):
                paths.add(row["id"])
            elif row.get("kind") == "DIR" and row["id"].startswith("dir:") and row["id"][4:]:
                paths.add(row["id"][4:])
        return sorted(paths)

    def _watch(self, base: str | None, paths: list[str]) -> dict:
        digest = getattr(self.git, "watch_digest", None)
        paths = sorted(set(paths) | set(self.admission_watch))
        if not paths or base is None or digest is None:
            return {}
        try:
            return {"watch": paths, "base_sha": base, "watch_digest": digest(base, paths)}
        except ReadFailed:
            return {}

    # ---- stacked pull requests

    def stacked(self, row: dict, heads: set, state: dict, summary: dict) -> None:
        n = int(row["number"])
        head, base = row.get("headRefOid"), row.get("baseRefName")
        if base in heads:
            self.round_inputs.setdefault(n, {})["parents"] = {base: [
                {"number": r["number"], "state": "open", "merged_at": None}
                for r in self.round_rows.values() if r.get("headRefName") == base]}
            summary["skipped"][str(n)] = "WAITING_PARENT"
            return
        memory = state["bases"].get(str(n))
        if memory and memory.get("head") == head and memory.get("base") == base:
            summary["skipped"][str(n)] = memory["reason"]
            return
        try:
            parents = self.gh.pulls_with_head(base)
            self.round_inputs.setdefault(n, {})["parents"] = {base: parents}
        except ReadFailed as failure:
            self._retry(summary, n, f"PARENT:{failure.reason}")
            return
        if any(p.get("merged_at") for p in parents):
            if self.dry_run:
                summary["would_retarget"].append(n)
                return
            try:
                self.gh.retarget(n, self.base)
            except ReadFailed as failure:
                self._retry(summary, n, f"RETARGET:{failure.reason}")
                return
            summary["retargeted"].append(n)
        elif any(p.get("state") == "open" for p in parents):
            summary["skipped"][str(n)] = "WAITING_PARENT"
        elif parents:
            relanded = next((state["relanded"][str(p["number"])] for p in parents
                             if str(p.get("number")) in state["relanded"]), None)
            if relanded and self.reland_enabled:
                self.restack(row, relanded, state, summary)
            else:
                self.hold(row, "PARENT_CLOSED_UNMERGED", state, summary, paths=[base])
        else:
            state["bases"][str(n)] = {"head": head, "base": base, "reason": "NOT_AGAINST_BASE"}
            summary["skipped"][str(n)] = "NOT_AGAINST_BASE"

    # ---- heads the base already contains

    def _contained(self, base_sha: str | None, row: dict) -> bool:
        head = row.get("headRefOid")
        if not base_sha or not head:
            return False
        try:
            return self.git.is_ancestor(head, base_sha)
        except ReadFailed as failure:
            self.round_inputs.setdefault(int(row["number"]), {})["contained"] = {"unobserved": failure.reason}
            return False

    def close_merged(self, row: dict, base_sha: str, state: dict, summary: dict) -> None:
        """Fast-forward the branch to the base (never forced: the head is an ancestor) so
        GitHub records the pull request merged; a refused push falls back to one comment
        and closing it."""
        n = int(row["number"])
        ref = row.get("headRefName")
        if self.dry_run:
            summary["would_close_merged"].append(n)
            return
        state["held"].pop(str(n), None)
        try:
            self.git.push_branch(base_sha, ref)
        except ReadFailed:
            try:
                self.gh.comment(n, f"Every commit of this head is already on {self.base}.")
                self.gh.close(n)
            except ReadFailed as failure:
                self._retry(summary, n, f"CLOSE_MERGED:{failure.reason}")
                return
        state["merged_elsewhere"][str(n)] = {"head": row.get("headRefOid"), "head_ref": ref, "at": summary["at"]}
        summary["merged_elsewhere"].append(n)

    # ---- holds, repairs, re-lands

    def hold(self, row: dict, reason: str, state: dict, summary: dict, failing=(), paths=(), cls=None,
             extra: dict | None = None) -> None:
        n = int(row["number"])
        entry = {"head": row.get("headRefOid"), "head_ref": row.get("headRefName"),
                 "title": row.get("title", ""), "reason": reason, "class": cls,
                 "failing": list(failing)[:24], "paths": list(paths), "at": summary["at"], **(extra or {})}
        state["held"][str(n)] = entry
        summary["held_rows"].append({"number": n, **entry})
        if n not in summary["held"]:
            summary["held"].append(n)

    def repair(self, row: dict, cls: str, lines: list[str], state: dict, summary: dict) -> None:
        """Union-merge the current base into the branch and push it by refspec; the new
        head is read next round."""
        n = int(row["number"])
        head, ref = row.get("headRefOid"), row.get("headRefName")
        if self.dry_run:
            summary["would_repair"].append({"number": n, "class": cls, "head": head})
            return
        try:
            base = self.git.main_sha()
            step = self.git.repair_step(base, head, f"Merge {self.base} into {ref} (repair: {cls})")
        except ReadFailed as failure:
            self._retry(summary, n, f"REPAIR:{failure.reason}")
            return
        if step.status == "CONFLICT":
            self.hold(row, "REPAIR_CONFLICT", state, summary, failing=lines, paths=step.conflicts, cls=cls)
            return
        if step.status != "CLEAN" or step.commit == head:
            self._retry(summary, n, f"REPAIR:{step.reason}" if step.status != "CLEAN" else "REPAIR:NOTHING_TO_MERGE")
            return
        try:
            self.git.push_branch(step.commit, ref)
        except ReadFailed as failure:
            self._retry(summary, n, f"REPAIR:{failure.reason}")
            return
        state["held"].pop(str(n), None)
        summary["repaired"].append({"number": n, "class": cls, "head": head, "pushed": step.commit})

    def restack(self, row: dict, parent: dict, state: dict, summary: dict) -> None:
        """A stacked pull request whose parent this train re-landed: replay the child's own
        commits (those after the parent head that was re-landed) onto the ordered branch,
        push them to `<branch>-restacked` and open a pull request on the ordered branch that
        supersedes the child. Restacked only onto an open ordered pull request, and only when
        the rebuilt tree is exactly git's merge of the child's head and the ordered head;
        otherwise held. The record it leaves lets the child's own children follow."""
        n = int(row["number"])
        head, ref, title = row.get("headRefOid"), row.get("headRefName"), row.get("title", "")
        if self.dry_run:
            summary["would_restack"].append(n)
            return
        try:
            onto = self.gh.pull(int(parent["new_number"]))
        except ReadFailed as failure:
            self._retry(summary, n, f"RESTACK:{failure.reason}")
            return
        if onto.get("state") != "open":
            self.hold(row, "PARENT_CLOSED_UNMERGED", state, summary, paths=[row.get("baseRefName")])
            return
        try:
            expected = self.git.union_step(onto["head_sha"], head, f"Merge {parent['ref']} into {ref}")
        except ReadFailed as failure:
            self._retry(summary, n, f"RESTACK:{failure.reason}")
            return
        if expected.status == "CONFLICT":
            self.hold(row, "RESTACK_CONFLICT", state, summary, paths=expected.conflicts)
            return
        if expected.status != "CLEAN":
            self._retry(summary, n, f"RESTACK:{expected.reason}")
            return
        result = self.git.restack(parent["head"], head, onto["head_sha"])
        if result.status == "FAILED":
            self._retry(summary, n, f"RESTACK:{result.reason}")
            return
        if result.status == "CONFLICT":
            self.hold(row, "RESTACK_CONFLICT", state, summary)
            return
        if result.tree != expected.tree:
            self.hold(row, "RESTACK_TREE_MISMATCH", state, summary)
            return
        final = result.commits[-1] if result.commits else onto["head_sha"]
        new_ref = f"{ref}-restacked"
        try:
            self.git.push_branch(final, new_ref)
        except ReadFailed as failure:
            self._retry(summary, n, f"RESTACK:{failure.reason}")
            return
        body = (f"Supersedes #{n}: its own commits replayed onto #{parent['new_number']} "
                f"(`{parent['ref']}`, the ordered re-land of its parent), ending on exactly git's "
                "merge of the two heads.\n\n" + _strip_trailers(row.get("body") or ""))
        try:
            new_number = self.gh.open_pr(new_ref, parent["ref"], title, body)
            self.gh.comment(n, f"Superseded by #{new_number} (restacked onto #{parent['new_number']}).")
            self.gh.close(n)
        except ReadFailed as failure:
            self._retry(summary, n, f"RESTACK:{failure.reason}")
            return
        state["held"].pop(str(n), None)
        state["relanded"][str(n)] = {"head": head, "ref": new_ref, "new_number": new_number,
                                     "at": summary["at"], "onto": parent["new_number"], "from_ref": ref}
        summary["restacked"].append({"number": n, "new_number": new_number, "ref": new_ref,
                                     "onto": parent["new_number"]})

    def reland(self, row: dict, cls: str, state: dict, summary: dict) -> None:
        """Rebuild the head's final tree (after the usual union merge of the base) as an
        ordered history on the base, push it to `<branch>-ordered` (never the original
        branch), and open a pull request that supersedes the original. Held instead if the
        rebuilt tree differs or the original admission still refuses it. The
        constructor's history classifier never substitutes for that reader."""
        n = int(row["number"])
        head, ref, title = row.get("headRefOid"), row.get("headRefName"), row.get("title", "")
        if self.dry_run:
            summary["would_reland"].append(n)
            return
        try:
            base_sha = self.git.main_sha()
            step = self.git.union_step(base_sha, head, f"Merge {self.base} into {ref}")
        except ReadFailed as failure:
            self._retry(summary, n, f"RELAND:{failure.reason}")
            return
        if step.status == "CONFLICT":
            self.hold(row, "RELAND_CONFLICT", state, summary, paths=step.conflicts, cls=cls)
            return
        if step.status != "CLEAN":
            self._retry(summary, n, f"RELAND:{step.reason}")
            return
        result = self.git.reland(base_sha, step.tree, title, self.kind)
        if result.status == "FAILED":
            self._retry(summary, n, f"RELAND:{result.reason}")
            return
        if result.status != "CLEAN":
            self.hold(row, "RELAND_TREE_MISMATCH", state, summary, cls=cls)
            return
        final = result.commits[-1] if result.commits else base_sha
        ordered_ref = f"{ref}-ordered"
        try:
            if self.git.tree(final) != step.tree:
                self.hold(row, "RELAND_TREE_MISMATCH", state, summary, cls=cls)
                return
            verdict = self.history_verdict(base_sha, final) if self.history_verdict else "CLEAN"
        except ReadFailed as failure:
            self._retry(summary, n, f"RELAND:{failure.reason}")
            return
        if verdict != "CLEAN":
            self.hold(row, f"RELAND_{verdict}", state, summary, cls=cls)
            return
        # Read the rebuilt occurrence under the same admission object and base
        # that refused the original. In particular, an external command can use
        # a stricter classifier than history_verdict. Check before any push or
        # PR effect; an identical tree alone says nothing about this read.
        value, error = self._read_admission({**row, "headRefOid": final}, base_sha)
        if error is not None:
            reason = error.reason if isinstance(error, ReadFailed) else f"ADMISSION_FAILED:{type(error).__name__}"
            self._retry(summary, n, f"RELAND:{reason}")
            return
        if not isinstance(value, dict):
            self._retry(summary, n, "RELAND:ADMISSION_UNOBSERVED")
            return
        summary.setdefault("repair_checks", []).append({
            "number": n, "base": base_sha, "head": final, "tree": step.tree,
            "admission": value})
        residuals = list(value.get("residuals") or [])
        if value.get("verdict") == "HELD" and residuals:
            self.hold(row, "RELAND_ADMISSION_HELD", state, summary, failing=residuals, cls=cls,
                      extra={**self._watch(base_sha, self._consumed_paths(value)), "repair_head": final})
            return
        if (value.get("verdict") != "MERGEABLE" or residuals
                or (value.get("admission") or {}).get("complete", True) is not True):
            self._retry(summary, n, "RELAND:ADMISSION_UNOBSERVED")
            return
        try:
            self.git.push_branch(final, ordered_ref)
        except ReadFailed as failure:
            self._retry(summary, n, f"RELAND:{failure.reason}")
            return
        body = (f"Supersedes #{n}: the same final tree, with its history ordered tests, code, docs.\n\n"
                + _strip_trailers(row.get("body") or ""))
        try:
            new_number = self.gh.open_pr(ordered_ref, self.base, title, body)
            self.gh.comment(n, f"Superseded by #{new_number} (the same tree, with an ordered history).")
            self.gh.close(n)
        except ReadFailed as failure:
            self._retry(summary, n, f"RELAND:{failure.reason}")
            return
        state["held"].pop(str(n), None)
        state["relanded"][str(n)] = {"head": head, "ref": ordered_ref, "new_number": new_number, "at": summary["at"],
                                     "from_ref": ref}
        summary["relanded"].append({"number": n, "new_number": new_number, "ref": ordered_ref})
