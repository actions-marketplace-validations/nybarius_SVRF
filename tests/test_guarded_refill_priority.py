"""Ready gates get released shared reservations before fresh admission refill."""
from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fakes import DaemonGitHub, DaemonRepo, FakeGate
from test_daemon import daemon
from svrf import daemon as daemon_module
from svrf.gate import MemoryGuard

OBSERVATIONS = {}


class GuardedRefill(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.observed = OBSERVATIONS[self.id()] = {}

    def guard(self, available=18):
        return MemoryGuard(need_gb=10, reserve_gb=8,
                           available_gb=lambda: available, poll=0.01)

    def capture(self, summary):
        self.observed["summary"] = summary
        self.observed["receipts"] = [
            json.loads(Path(path).read_text()) for path in summary.get("receipts", [])
        ]
        return self.observed["receipts"]

    def start(self, owner):
        result, errors = {}, []

        def tick():
            try:
                result.update(owner.tick())
            except BaseException as error:
                errors.append(repr(error))

        thread = threading.Thread(target=tick)
        thread.start()
        return thread, result, errors

    def finish(self, owner, thread, events, errors):
        for event in events:
            event.set()
        thread.join(15)
        self.observed["thread_errors"] = list(errors)
        self.assertFalse(thread.is_alive(), "fixture owner failed to return after release")
        while owner.inflight:
            owner.tick(drain=True)
        owner.close()

    def test_ready_gate_takes_released_reservation_before_deferred_read(self):
        repo = DaemonRepo([1, 2])
        gh = DaemonGitHub(repo)
        memory = self.guard()
        first_started, first_release = threading.Event(), threading.Event()
        next_started, next_release = threading.Event(), threading.Event()
        gate_started, gate_release = threading.Event(), threading.Event()
        calls, submissions, landed = [], [], {}
        merge = gh.merge

        def record_merge(number, sha):
            result = merge(number, sha)
            landed[number] = result
            return result

        gh.merge = record_merge

        def admission(head, base):
            calls.append((head, base))
            started, release = ((first_started, first_release) if head == "h1"
                                else (next_started, next_release))
            started.set()
            if not release.wait(10):
                raise RuntimeError("admission fixture was not released")
            return {"verdict": "MERGEABLE", "residuals": []}

        class HeldGate(FakeGate):
            def run(self, base, commit, label=""):
                if not gate_started.is_set():
                    gate_started.set()
                    if not gate_release.wait(10):
                        raise RuntimeError("gate fixture was not released")
                return super().run(base, commit, label)

        gate = HeldGate(repo)
        owner = daemon(repo, gh, gate, admission, self.path, jobs=1, memory=memory,
                       admission_jobs=1, admission_round_reads=1, tick_seconds=8)
        self.assertIs(owner.train_options["memory"], memory)
        submit = owner._submit

        def submit_and_observe(rows, base):
            numbers = [int(row["number"]) for row in rows]
            submissions.append((numbers, base))
            submit(rows, base)
            # Select a valid executor interleaving: if refill is first, let that
            # actual admission acquire the reservation before the owner continues.
            if 2 in numbers and not next_started.wait(5):
                raise RuntimeError("submitted deferred admission never entered")

        owner._submit = submit_and_observe
        thread, result, errors = self.start(owner)
        try:
            self.assertTrue(first_started.wait(5))
            first_release.set()
            ready = gate_started.wait(1.5)
            with memory.lock:
                reserved = memory.running
            self.observed["before_gate_release"] = {
                "gate_started": ready, "deferred_entered": next_started.is_set(),
                "reservations": reserved, "calls": list(calls),
                "submissions": list(submissions), "merged": list(gh.merged),
            }
            self.assertTrue(
                ready, "a fresh admission took the released reservation before the ready gate"
            )
            self.assertFalse(next_started.is_set())
            self.assertEqual(gh.merged, [])
            self.assertEqual(calls, [("h1", "c0")])
            # Both the unstarted head and the base must be acquired after landing.
            repo.commits["h2-current"] = {"lanes": frozenset({0, 2}), "parents": ("h2",)}
            repo.heads[2] = repo.branches["pr-2"] = "h2-current"
            gate_release.set()
            self.assertTrue(next_started.wait(5))
            self.assertEqual(gh.merged, [1])
            self.assertEqual(calls, [("h1", "c0"), ("h2-current", landed[1])])
            next_release.set()
            thread.join(10)
        finally:
            self.finish(owner, thread,
                        [first_release, next_release, gate_release], errors)
            if result:
                self.capture(result)
        self.assertEqual(errors, [])
        self.assertEqual(gh.merged, [1, 2])
        self.assertEqual(gate.trees, ["T0,1", "T0,1,2"])
        self.assertEqual(len(result["receipts"]), 2)
        self.assertEqual(set(result["admission_seconds"]), {"1", "2"})
        self.assertEqual(memory.running, 0)

    def test_no_qualified_batch_refills_at_the_observed_capacity(self):
        repo, calls, submitted = DaemonRepo([1, 2, 3]), [], []
        gh, memory = DaemonGitHub(repo), self.guard(48)
        gate = FakeGate(repo)

        def admission(head, base):
            calls.append((head, base))
            return {"verdict": "HELD", "residuals": ["check:fixture rejection"]}

        owner = daemon(repo, gh, gate, admission, self.path, jobs=1, memory=memory,
                       admission_jobs=4, admission_round_reads=4, tick_seconds=5,
                       admission_capacity_command="echo 1")
        submit = owner._submit

        def record_submission(rows, base):
            submit(rows, base)
            submitted.append(sorted(owner.inflight))

        owner._submit = record_submission
        try:
            result = owner.tick()
            self.capture(result)
        finally:
            owner.close()
        self.observed["submissions"] = submitted
        self.assertEqual(calls, [("h3", "c0"), ("h2", "c0"), ("h1", "c0")])
        self.assertEqual(submitted, [[3], [2], [1]])
        self.assertEqual(result["admission_capacity"],
                         {"command": "echo 1", "value": 1, "slots": 1})
        self.assertEqual(result["admitted"], [])
        self.assertEqual(result["held"], [3, 2, 1])
        self.assertEqual(set(result["admission_seconds"]), {"1", "2", "3"})
        self.assertEqual(gate.trees, [])
        self.assertEqual(gh.merged, [])
        self.assertEqual(memory.running, 0)

    def test_gate_failure_is_retained_and_does_not_suppress_normal_refill(self):
        repo, calls = DaemonRepo([1, 2]), []
        gh, memory = DaemonGitHub(repo), self.guard()
        gate = FakeGate(repo, bad={1})

        def admission(head, base):
            calls.append((head, base))
            return {"verdict": "MERGEABLE", "residuals": []}

        owner = daemon(repo, gh, gate, admission, self.path, jobs=1, memory=memory,
                       admission_jobs=1, admission_round_reads=1, tick_seconds=5)
        try:
            result = owner.tick()
            receipts = self.capture(result)
            held = owner.load_state()["held"]
        finally:
            owner.close()
        self.assertEqual(calls, [("h1", "c0"), ("h2", "c0")])
        self.assertEqual(gh.merged, [2])
        self.assertIn(1, result["held"])
        self.assertIn("FAILED tests/test_lane_1.py::test_it", held["1"]["failing"])
        self.assertTrue(any(g.get("green") is False for r in receipts for g in r["gates"]))
        self.assertFalse(result["stopped"])
        self.assertEqual(set(result["admission_seconds"]), {"1", "2"})
        self.assertEqual(memory.running, 0)

    def test_landed_tree_mismatch_stops_fresh_refill(self):
        repo, calls = DaemonRepo([1, 2]), []
        gh, memory = DaemonGitHub(repo, mismatch_on=1), self.guard()
        gate = FakeGate(repo)

        def admission(head, base):
            calls.append((head, base))
            return {"verdict": "MERGEABLE", "residuals": []}

        owner = daemon(repo, gh, gate, admission, self.path, jobs=1, memory=memory,
                       admission_jobs=1, admission_round_reads=1, tick_seconds=5)
        try:
            result = owner.tick()
            receipts = self.capture(result)
        finally:
            owner.close()
        self.assertEqual(calls, [("h1", "c0")])
        self.assertEqual(gh.merged, [1])
        self.assertTrue(result["stopped"])
        self.assertEqual(gate.trees, ["T0,1"])
        self.assertEqual(len(receipts), 1)
        self.assertTrue(receipts[0]["stopped"])
        self.assertEqual(result["inflight"], [])
        self.assertEqual(memory.running, 0)

    def test_stop_during_entered_gate_retains_result_and_prevents_fresh_refill(self):
        repo, calls = DaemonRepo([1, 2]), []
        gh, memory = DaemonGitHub(repo), self.guard()

        def admission(head, base):
            calls.append((head, base))
            return {"verdict": "MERGEABLE", "residuals": []}

        class StopGate(FakeGate):
            def run(self, base, commit, label=""):
                result = super().run(base, commit, label)
                owner.request_stop()
                return result

        gate = StopGate(repo)
        owner = daemon(repo, gh, gate, admission, self.path, jobs=1, memory=memory,
                       admission_jobs=1, admission_round_reads=1, tick_seconds=5)
        try:
            result = owner.tick()
            receipts = self.capture(result)
        finally:
            owner.close()
        self.assertEqual(calls, [("h1", "c0")])
        self.assertEqual(gate.trees, ["T0,1"])
        # A watch stop settles already owned work; it must not refill new reads.
        self.assertEqual(gh.merged, [1])
        self.assertTrue(result["stop_requested"])
        self.assertEqual(len(receipts), 1)
        self.assertTrue(receipts[0]["stop_requested"])
        self.assertTrue(receipts[0]["gates"][0]["green"])
        self.assertFalse(receipts[0]["stopped"])
        self.assertEqual(result["inflight"], [])
        self.assertEqual(memory.running, 0)

    def test_deadline_is_checked_again_after_synchronous_landing(self):
        repo, calls = DaemonRepo([1, 2]), []
        gh, memory = DaemonGitHub(repo), self.guard()

        class TickTime:
            now = 0.0

            def monotonic(self):
                return self.now

            def __getattr__(self, name):
                return getattr(time, name)

        tick_time = TickTime()

        def admission(head, base):
            calls.append((head, base))
            return {"verdict": "MERGEABLE", "residuals": []}

        class DeadlineGate(FakeGate):
            def run(self, base, commit, label=""):
                result = super().run(base, commit, label)
                tick_time.now = 2.0
                return result

        gate = DeadlineGate(repo)
        owner = daemon(repo, gh, gate, admission, self.path, jobs=1, memory=memory,
                       admission_jobs=1, admission_round_reads=1, tick_seconds=1)
        try:
            with patch.object(daemon_module, "time", tick_time):
                result = owner.tick()
            self.capture(result)
        finally:
            owner.close()
        self.assertEqual(calls, [("h1", "c0")])
        self.assertEqual(gh.merged, [1])
        self.assertEqual(result["deferred"], [2])
        self.assertEqual(result["wall_seconds"], 2.0)
        self.assertEqual(result["inflight"], [])
        self.assertEqual(memory.running, 0)

    def test_refill_rechecks_current_eligibility_after_landing(self):
        repo, calls = DaemonRepo([1, 2]), []
        gh, memory = DaemonGitHub(repo), self.guard()

        def admission(head, base):
            calls.append((head, base))
            return {"verdict": "MERGEABLE", "residuals": []}

        class HoldDeferredGate(FakeGate):
            def run(self, base, commit, label=""):
                gh.labels[2] = ["train:hold"]
                return super().run(base, commit, label)

        gate = HoldDeferredGate(repo)
        owner = daemon(repo, gh, gate, admission, self.path, jobs=1, memory=memory,
                       admission_jobs=1, admission_round_reads=1, tick_seconds=5)
        try:
            result = owner.tick()
            self.capture(result)
        finally:
            owner.close()
        self.assertEqual(calls, [("h1", "c0")])
        self.assertEqual(gh.merged, [1])
        self.assertEqual(result["skipped"]["2"], "OPT_OUT")
        self.assertEqual(result["inflight"], [])
        self.assertEqual(memory.running, 0)

    def test_entered_and_prequeued_admissions_keep_their_original_futures(self):
        repo = DaemonRepo([1, 2, 3])
        gh, memory = DaemonGitHub(repo), self.guard(38)
        first_started, first_release = threading.Event(), threading.Event()
        second_started, second_release = threading.Event(), threading.Event()
        third_started, third_release = threading.Event(), threading.Event()
        gate_started, gate_release = threading.Event(), threading.Event()
        landed = threading.Event()
        calls = []
        merge = gh.merge

        def record_merge(number, sha):
            result = merge(number, sha)
            if number == 1:
                landed.set()
            return result

        gh.merge = record_merge

        def admission(head, base):
            calls.append((head, base))
            started, release = {
                "h1": (first_started, first_release),
                "h2": (second_started, second_release),
                "h3": (third_started, third_release),
            }[head]
            started.set()
            if not release.wait(10):
                raise RuntimeError("owned admission fixture was not released")
            if head == "h3":
                return {"verdict": "HELD", "residuals": ["check:original queued refusal"]}
            return {"verdict": "MERGEABLE", "residuals": []}

        class HeldGate(FakeGate):
            def run(self, base, commit, label=""):
                if not gate_started.is_set():
                    gate_started.set()
                    if not gate_release.wait(10):
                        raise RuntimeError("owned gate fixture was not released")
                return super().run(base, commit, label)

        gate = HeldGate(repo)
        # One-shot mode deliberately retains its already queued third admission.
        owner = daemon(repo, gh, gate, admission, self.path, jobs=1, memory=memory,
                       admission_jobs=2,
                       admission_priority_command="printf '%s\\n' 1 2 3")
        submitted = threading.Event()
        submit = owner._submit

        def record_submission(rows, base):
            submit(rows, base)
            submitted.set()

        owner._submit = record_submission
        thread, result, errors = self.start(owner)
        try:
            self.assertTrue(submitted.wait(5))
            self.assertTrue(first_started.wait(5))
            self.assertTrue(second_started.wait(5))
            second = owner.inflight[2]["future"]
            third = owner.inflight[3]["future"]
            self.assertTrue(second.running())
            self.assertFalse(third.running())
            self.assertFalse(third.cancelled())
            first_release.set()
            # Three reservations allow either gate/queued-read start order while
            # preserving the entered second read. Existing queues are not cancelled.
            self.assertTrue(third_started.wait(5))
            self.assertIs(owner.inflight[2]["future"], second)
            self.assertIs(owner.inflight[3]["future"], third)
            third_release.set()
            self.assertTrue(gate_started.wait(5))
            self.assertIs(owner.inflight[2]["future"], second)
            self.assertTrue(second.running())
            self.assertFalse(third.cancelled())
            gate_release.set()
            self.assertTrue(landed.wait(5))
            self.assertFalse(second.done())
            self.assertEqual(gh.merged, [1])
            second_release.set()
            thread.join(10)
        finally:
            self.finish(owner, thread,
                        [first_release, second_release, third_release, gate_release], errors)
            if result:
                self.capture(result)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(calls), [("h1", "c0"), ("h2", "c0"), ("h3", "c0")])
        self.assertEqual(gh.merged, [1, 2])
        self.assertIn(3, result["held"])
        self.assertEqual(set(result["admission_seconds"]), {"1", "2", "3"})
        self.assertEqual(memory.running, 0)


if __name__ == "__main__":
    unittest.main()
