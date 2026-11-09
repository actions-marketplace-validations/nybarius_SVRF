"""Receive a finite set of completed original admissions before family planning."""
from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from fakes import DaemonGitHub, DaemonRepo, FakeGate
from test_daemon import daemon
from svrf import daemon as daemon_module
from svrf.errors import ReadFailed
from svrf.gate import MemoryGuard

OBSERVATIONS = {}


class ReadyCompletion(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name)
        self.observed = OBSERVATIONS[self.id()] = {}

    def exercise(self, *, guarded=True, release_at="classify", second_value=None,
                 second_failure=None, queued=False, finish_refill=False,
                 before_land_stop=False, gate_hook=None, conflicts=(), gate_bad=()):
        repo = DaemonRepo([1, 2, 3] if queued else [1, 2], conflicts=conflicts)
        gh = DaemonGitHub(repo)
        memory = (MemoryGuard(need_gb=10, reserve_gb=8,
                              available_gb=lambda: 48, poll=0.01) if guarded else None)
        release_second = threading.Event()
        calls, timeline, attempts, submissions, publications = [], [], {}, [], []
        futures, holder = {}, {}
        self.observed.update(calls=calls, timeline=timeline, submissions=submissions,
                             publications=publications)
        merge = gh.merge

        def observed_merge(number, sha):
            result = merge(number, sha)
            publications.append((number, result))
            return result

        gh.merge = observed_merge

        def admission(head, base):
            calls.append((head, base))
            if head == "h2":
                if not release_second.wait(5):
                    raise RuntimeError("second original admission was not released")
                if second_failure is not None:
                    raise second_failure
                if second_value is not None:
                    return dict(second_value)
            return {"verdict": "MERGEABLE", "residuals": [], "fixture_head": head}

        class ObservedGate(FakeGate):
            def run(self, base, commit, label=""):
                if not holder.get("gate_seen"):
                    holder["gate_seen"] = True
                    timeline.append({"event": "first_gate",
                                     "second_done": futures[2].done(),
                                     "refill_started": any(h.startswith("h3") for h, _ in calls)})
                    if release_at == "gate":
                        release_second.set()
                    if gate_hook is not None:
                        gate_hook(holder["owner"], repo, gh)
                return super().run(base, commit, label)

        gate = ObservedGate(repo, bad=gate_bad)
        options = ({"admission_round_reads": 2, "tick_seconds": 5,
                    "admission_priority_command": "printf '%s\\n' 1 2 3"} if queued else {})
        owner = daemon(repo, gh, gate, admission, self.path, jobs=1,
                       memory=memory, admission_jobs=2,
                       train={"family_size": 8, "max_rounds": 1}, **options)
        holder["owner"] = owner
        original_wait = daemon_module.wait
        original_decision = daemon_module.rules.admission_decision
        original_submit = owner._submit

        def observed_submit(rows, base):
            submissions.append({"numbers": [int(r["number"]) for r in rows],
                                "heads": [r["headRefOid"] for r in rows], "base": base})
            original_submit(rows, base)
            if finish_refill and any(int(r["number"]) == 3 for r in rows):
                # A valid unguarded refill interleaving: the new worker finishes
                # before planning, but it is not part of the original wait cohort.
                futures[3] = owner.inflight[3]["future"]
                attempts[3] = owner.inflight[3]["attempt"].path
                futures[3].result(timeout=5)
                timeline.append({"event": "fresh_refill_finished",
                                 "future_done": futures[3].done()})

        owner._submit = observed_submit

        def observed_wait(pending, *args, **kwargs):
            done, waiting = original_wait(pending, *args, **kwargs)
            first = not futures
            for n, item in owner.inflight.items():
                if n in futures:
                    self.assertIs(item["future"], futures[n])
                else:
                    futures[n] = item["future"]
                    attempts[n] = item["attempt"].path
            if first:
                timeline.append({"event": "first_completed_returned",
                                 "ready": sorted(n for n, f in futures.items() if f in done),
                                 "second_done": futures[2].done()})
            return done, waiting

        def observed_decision(value, *args, **kwargs):
            answer = original_decision(value, *args, **kwargs)
            if value.get("fixture_head") == "h1" and release_at == "classify":
                self.assertFalse(futures[2].done())
                release_second.set()
                futures[2].result(timeout=5)
                timeline.append({"event": "second_original_finished",
                                 "future_done": futures[2].done(),
                                 "first_decision": list(answer)})
                if before_land_stop:
                    owner.request_stop()
            return answer

        try:
            with patch.object(daemon_module, "wait", observed_wait), \
                    patch.object(daemon_module.rules, "admission_decision", observed_decision):
                summary = owner.tick()
            receipts = [json.loads(Path(p).read_bytes()) for p in summary["receipts"]]
            self.observed.update(
                requested=[r["requested"] for r in receipts],
                gates=[{"prs": g["prs"], "green": g["green"], "tree": g["tree"]}
                       for r in receipts for g in r["gates"]],
                merged=list(gh.merged), gate_trees=list(gate.trees),
                admitted=summary["admitted"], retry_later=summary["retry_later"],
                reasons=summary["reasons"], held=summary["held"],
                stop_requested=summary.get("stop_requested", False),
                inflight_before_close=sorted(owner.inflight),
                remaining_originals=[
                    {"pr": n, "same_future": item["future"] is futures[n],
                     "done": item["future"].done()}
                    for n, item in owner.inflight.items()],
                original_futures=[
                    {"pr": n, "done": f.done(), "cancelled": f.cancelled()}
                    for n, f in futures.items()],
                attempts=[
                    {"pr": n, "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
                     "original": json.loads(p.read_bytes())}
                    for n, p in sorted(attempts.items())],
            )
        finally:
            release_second.set()
            if before_land_stop:
                # Stop retains the original Future. Receive it through the
                # existing effects-free drain before retiring the owner.
                with self.assertRaisesRegex(RuntimeError, "^ADMISSIONS_STILL_RUNNING$"):
                    owner.close()
                drained = owner.tick(drain=True)
                self.observed["drain"] = {
                    "summary": drained,
                    "receipts": [json.loads(Path(p).read_bytes()) for p in drained["receipts"]],
                    "attempts_after": [
                        {"pr": n, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
                        for n, p in sorted(attempts.items())],
                }
            owner.close()
        return summary, receipts, gate, gh, memory, calls, timeline

    def test_collects_original_completed_during_receiving_before_planning(self):
        summary, receipts, gate, gh, memory, calls, timeline = self.exercise()
        self.assertEqual(timeline[0],
                         {"event": "first_completed_returned", "ready": [1], "second_done": False})
        self.assertTrue(timeline[1]["future_done"])
        self.assertEqual(timeline[1]["first_decision"], ["ADMIT", None])
        self.assertEqual(sorted(calls), [("h1", "c0"), ("h2", "c0")])
        self.assertEqual(summary["admitted"], [1, 2])
        self.assertEqual(gh.merged, [1, 2])
        self.assertEqual(memory.running, 0)
        self.assertEqual([r["requested"] for r in receipts], [[1, 2]],
                         "an already-completed original admission was split into a second Train run")
        self.assertEqual(gate.trees, ["T0,1,2"])

    def test_unfinished_original_does_not_delay_ready_gate(self):
        summary, receipts, gate, gh, memory, calls, timeline = self.exercise(release_at="gate")
        first_gate = next(x for x in timeline if x["event"] == "first_gate")
        self.assertFalse(first_gate["second_done"])
        self.assertEqual([r["requested"] for r in receipts], [[1], [2]])
        self.assertEqual(gate.trees, ["T0,1", "T0,1,2"])
        self.assertEqual(sorted(calls), [("h1", "c0"), ("h2", "c0")])
        self.assertEqual(gh.merged, [1, 2])
        self.assertEqual(memory.running, 0)

    def test_completed_negative_and_unobserved_results_keep_original_refusals(self):
        observed_variants = []
        variants = [
            ("held", {"second_value": {"verdict": "HELD", "residuals": ["check:original failure"]}},
             "HELD"),
            ("unobserved", {"second_failure": ReadFailed("ORIGINAL_ADMISSION_UNOBSERVED")},
             "UNOBSERVED"),
        ]
        for label, arguments, disposition in variants:
            with self.subTest(label=label):
                self.path = Path(self.temporary.name) / label
                summary, receipts, gate, gh, memory, calls, _ = self.exercise(**arguments)
                observed_variants.append({"variant": label, **json.loads(json.dumps(self.observed))})
                attempts = {r["pr"]: r["original"] for r in self.observed["attempts"]}
                self.assertEqual(attempts[2]["terminal_disposition"], disposition)
                self.assertEqual(attempts[2]["head"], "h2")
                self.assertEqual(attempts[2]["base"], "c0")
                self.assertEqual(sorted(calls), [("h1", "c0"), ("h2", "c0")])
                self.assertEqual(summary["admitted"], [1])
                self.assertEqual([r["requested"] for r in receipts], [[1]])
                self.assertEqual(gate.trees, ["T0,1"])
                self.assertEqual(gh.merged, [1])
                self.assertEqual(memory.running, 0)
                if label == "held":
                    self.assertIn(2, summary["held"])
                    self.assertEqual(attempts[2]["result"]["residuals"], ["check:original failure"])
                else:
                    self.assertIn(2, summary["retry_later"])
                    self.assertNotIn(2, summary["held"])
                    self.assertEqual(attempts[2]["read_failure"]["reason"], "ORIGINAL_ADMISSION_UNOBSERVED")
        self.observed.clear()
        self.observed["variants"] = observed_variants

    def test_completed_head_is_still_rechecked_before_publication(self):
        def move_second(owner, repo, gh):
            repo.move_head(2)

        summary, receipts, gate, gh, memory, calls, _ = self.exercise(gate_hook=move_second)
        self.assertEqual(summary["admitted"], [1, 2])
        self.assertEqual([r["requested"] for r in receipts], [[1, 2]])
        self.assertEqual(gate.trees, ["T0,1,2"])
        self.assertEqual(gh.merged, [1])
        self.assertEqual(receipts[0]["pending"], [2])
        self.assertEqual(sorted(calls), [("h1", "c0"), ("h2", "c0")])
        self.assertEqual(memory.running, 0)

    def test_stop_before_drain_keeps_original_completed_future_owned(self):
        summary, receipts, gate, gh, memory, calls, _ = self.exercise(before_land_stop=True)
        self.assertTrue(summary["stop_requested"])
        self.assertEqual(gh.merged, [])
        self.assertEqual(gate.trees, [])
        self.assertEqual(sorted(calls), [("h1", "c0"), ("h2", "c0")])
        self.assertEqual(self.observed["inflight_before_close"], [2])
        self.assertEqual(self.observed["remaining_originals"],
                         [{"pr": 2, "same_future": True, "done": True}])
        drained = self.observed["drain"]
        self.assertEqual(drained["summary"]["inflight"], [])
        self.assertEqual(drained["summary"]["merged"], [])
        self.assertEqual(len(drained["receipts"]), 1)
        received = drained["receipts"][0]["drained"]
        self.assertEqual([r["number"] for r in received], [2])
        self.assertEqual(received[0]["head"], "h2")
        self.assertEqual(received[0]["base"], "c0")
        original = next(r["original"] for r in self.observed["attempts"] if r["pr"] == 2)
        self.assertEqual(received[0]["admission"], original["result"])
        self.assertIsNone(received[0]["failure"])
        self.assertEqual(drained["attempts_after"],
                         [{"pr": r["pr"], "sha256": r["sha256"]} for r in self.observed["attempts"]])
        self.assertEqual(memory.running, 0)

    def test_guarded_refill_follows_coalesced_landing_and_reads_current_source(self):
        def change_unstarted_head(owner, repo, gh):
            repo.commits["h3-current"] = {"lanes": frozenset({0, 3}), "parents": ("h3",)}
            repo.heads[3] = repo.branches["pr-3"] = "h3-current"

        summary, receipts, gate, gh, memory, calls, timeline = self.exercise(
            queued=True, gate_hook=change_unstarted_head)
        first_gate = next(x for x in timeline if x["event"] == "first_gate")
        self.assertFalse(first_gate["refill_started"])
        self.assertEqual([r["requested"] for r in receipts], [[1, 2], [3]])
        self.assertEqual(gate.trees, ["T0,1,2", "T0,1,2,3"])
        self.assertEqual(gh.merged, [1, 2, 3])
        published = dict(self.observed["publications"])
        self.assertEqual(sorted(calls[:2]), [("h1", "c0"), ("h2", "c0")])
        self.assertEqual(calls[2:], [("h3-current", published[2])])
        self.assertEqual(memory.running, 0)

    def test_unguarded_fresh_refill_is_not_added_to_original_drain_snapshot(self):
        summary, receipts, gate, gh, memory, calls, timeline = self.exercise(
            guarded=False, queued=True, finish_refill=True)
        first_gate = next(x for x in timeline if x["event"] == "first_gate")
        self.assertTrue(first_gate["refill_started"])
        self.assertTrue(next(x for x in timeline if x["event"] == "fresh_refill_finished")["future_done"])
        self.assertEqual([r["requested"] for r in receipts], [[1, 2], [3]])
        self.assertEqual(gate.trees, ["T0,1,2", "T0,1,2,3"])
        self.assertEqual(sorted(calls), [("h1", "c0"), ("h2", "c0"), ("h3", "c0")])
        self.assertEqual(gh.merged, [1, 2, 3])

    def test_stop_during_owned_coalesced_gate_settles_family_without_refill(self):
        def stop_owner(owner, repo, gh):
            owner.request_stop()

        summary, receipts, gate, gh, memory, calls, _ = self.exercise(
            queued=True, gate_hook=stop_owner)
        self.assertTrue(summary["stop_requested"])
        self.assertEqual([r["requested"] for r in receipts], [[1, 2]])
        self.assertEqual(gate.trees, ["T0,1,2"])
        self.assertEqual(gh.merged, [1, 2])
        self.assertEqual(sorted(calls), [("h1", "c0"), ("h2", "c0")])
        self.assertEqual(summary["deferred"], [3])
        self.assertEqual(memory.running, 0)

    def test_coalesced_candidates_still_use_pairwise_conflict_selection(self):
        summary, receipts, gate, gh, memory, calls, _ = self.exercise(
            conflicts=[((1, 2), ["source.py"])])
        self.assertEqual(summary["admitted"], [1, 2])
        self.assertEqual([r["requested"] for r in receipts], [[1, 2]])
        self.assertEqual(gate.trees, ["T0,1"])
        self.assertEqual(gh.merged, [1])
        self.assertEqual(receipts[0]["out"]["2"]["with"], [1])
        self.assertEqual(receipts[0]["out"]["2"]["paths"], ["source.py"])
        self.assertEqual(sorted(calls), [("h1", "c0"), ("h2", "c0")])
        self.assertEqual(memory.running, 0)

    def test_coalesced_gate_failure_is_retained_and_bisected(self):
        summary, receipts, gate, gh, memory, calls, _ = self.exercise(gate_bad={2})
        self.assertEqual([r["requested"] for r in receipts], [[1, 2]])
        self.assertEqual(gate.trees, ["T0,1,2", "T0,1"])
        self.assertEqual([g["green"] for g in receipts[0]["gates"]], [False, True, False])
        original_red, _, reused_red = receipts[0]["gates"]
        self.assertEqual(reused_red["reused"], 0)
        self.assertEqual(reused_red["tree"], original_red["tree"])
        self.assertEqual(reused_red["failing"], original_red["failing"])
        self.assertEqual(reused_red["seconds"], 0.0)
        self.assertEqual(gh.merged, [1])
        self.assertIn(2, summary["held"])
        self.assertEqual(sorted(calls), [("h1", "c0"), ("h2", "c0")])
        self.assertEqual(memory.running, 0)


if __name__ == "__main__":
    unittest.main()
