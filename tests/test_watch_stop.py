"""A watch stop finishes owned work without starting another qualification."""
import json
import signal
import tempfile
import unittest
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from threading import Event, Thread
from unittest.mock import patch

from fakes import UNION, Admission, DaemonGitHub, DaemonRepo, FakeGate, FakeGitHub, FakeRepo
from test_daemon import daemon
from test_train import train

from svrf import cli


class WatchStop(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "svrf.toml"
        self.path.write_text('repo="a/b"\n[gate]\ncommands=["true"]\n')
        self.config = cli.load(self.path)
        self.handlers, self.events = {}, []

    def stop(self):
        self.handlers[signal.SIGTERM](signal.SIGTERM, None)

    def watch(self, owner):
        def handler(signum, value):
            previous = self.handlers.get(signum, signal.SIG_DFL)
            self.handlers[signum] = value
            return previous

        with patch.object(signal, "signal", handler), \
             patch.object(cli, "_print", self.events.append), \
             patch.object(cli.time, "sleep", lambda seconds: None):
            self.assertEqual(cli._watch(owner, self.config, path=self.path), 0)

    def test_signal_during_gate_retains_green_and_defers_stale_base_to_successor(self):
        repo = DaemonRepo([1])
        gh, gate, admission = DaemonGitHub(repo), FakeGate(repo), Admission()
        original_gate = gate.run

        def stop_on_first_gate(base, commit, label=""):
            result = original_gate(base, commit, label)
            if len(gate.trees) == 1:
                repo.main = repo._new(repo.lanes(repo.main) | {77}, (repo.main,))
                self.stop()
            return result

        gate.run = stop_on_first_gate
        owner = daemon(repo, gh, gate, admission, self.root / "state")
        self.watch(owner)
        self.assertEqual(len(gate.trees), 1)
        self.assertEqual(gh.merged, [])
        original = json.loads(Path(self.events[0]["receipt"]).read_text())
        self.assertTrue(original["gates"][0]["green"])
        self.assertEqual(original["families"][0]["status"], "REQUEUED")
        self.assertEqual(original["pending"], [1])
        self.assertTrue(original["stop_requested"])
        self.assertFalse(original["stopped"])
        self.assertIsNotNone(original["finished"])

        successor_gate = FakeGate(repo)
        successor = daemon(repo, gh, successor_gate, admission, self.root / "state")
        try:
            result = successor.tick()
            self.assertEqual(result["merged"], [1])
            self.assertEqual(successor_gate.trees, ["T0,1,77"])
            receipt = json.loads(Path(result["receipt"]).read_text())
            self.assertTrue(receipt["merges"][0]["identity"])
        finally:
            successor.close()

    def test_signal_during_admission_prevents_refill_and_receives_the_original_result(self):
        repo = DaemonRepo([1, 2])
        gh, gate, calls = DaemonGitHub(repo), FakeGate(repo), []

        def admission(head, base):
            calls.append((head, base))
            self.stop()
            return {"verdict": "MERGEABLE", "residuals": []}

        owner = daemon(repo, gh, gate, admission, self.root / "state",
                       admission_jobs=1, admission_round_reads=1)
        self.watch(owner)
        self.assertEqual(calls, [("h1", "c0")])
        self.assertEqual(gate.trees, [])
        self.assertEqual(gh.merged, [])
        drained = [row for row in self.events if row["tick"] == "DRAINED"]
        self.assertTrue(drained)
        receipt = json.loads(Path(drained[-1]["receipt"]).read_text())
        self.assertEqual(receipt["drained"][0]["head"], "h1")
        self.assertEqual(receipt["drained"][0]["admission"]["verdict"], "MERGEABLE")
        self.assertEqual(owner.inflight, {})

    def test_postponed_repair_remains_discoverable_instead_of_becoming_a_permanent_hold(self):
        repo = DaemonRepo([1], main_conflicts={1: [UNION]})
        gh, admission = DaemonGitHub(repo), Admission()
        owner = daemon(repo, gh, FakeGate(repo), admission, self.root / "state")
        original_step = repo.union_step

        def stop_after_conflict(*args, **kwargs):
            result = original_step(*args, **kwargs)
            owner.request_stop()
            return result

        repo.union_step = stop_after_conflict
        result = owner.tick()
        self.assertEqual(repo.repairs, [])
        self.assertEqual(result["retry_later"], [1])
        self.assertEqual(result["reasons"]["1"], "STOP_REQUESTED")
        self.assertEqual(owner.load_state()["held"], {})
        receipt = json.loads(Path(result["receipt"]).read_text())
        self.assertEqual(receipt["holds"][0]["reason"], "CONFLICT")
        owner.close()
        repo.union_step = original_step
        successor = daemon(repo, gh, FakeGate(repo), admission, self.root / "state")
        try:
            self.assertEqual([row["number"] for row in successor.tick()["repaired"]], [1])
            self.assertEqual(len(repo.repairs), 1)
        finally:
            successor.close()

    def test_a_stopped_owner_keeps_unfinished_future_then_drains_it_without_new_discovery(self):
        repo = DaemonRepo([1, 2])
        gh, admission = DaemonGitHub(repo), Admission()
        owner = daemon(repo, gh, FakeGate(repo), admission, self.root / "state", tick_seconds=0.001)
        future = Future()
        item = {"future": future, "row": {"number": 1, "headRefOid": "h1"}, "base": "c0"}
        owner.inflight[1] = item
        owner.request_stop()
        first = owner.tick()
        self.assertEqual(first["tick"], "DRAINED")
        self.assertIs(owner.inflight[1], item)
        self.assertFalse(future.cancelled())
        self.assertEqual(gh.calls, {"graphql": 0, "rest": 0})
        value = {"verdict": "MERGEABLE", "residuals": []}
        future.set_result((value, None, 0.25))
        second = owner.tick(drain=True)
        self.assertEqual(second["inflight"], [])
        receipt = json.loads(Path(second["receipt"]).read_text())
        self.assertEqual(receipt["drained"][0]["admission"], value)
        self.assertEqual(receipt["drained"][0]["seconds"], 0.25)
        self.assertEqual(admission.calls, [])
        owner.close()


    def test_retirement_settles_running_gate_and_cancels_queued_families_before_close(self):
        repo = DaemonRepo([1, 2, 3])
        gh, gate = DaemonGitHub(repo), FakeGate(repo, bad=[1])
        owner = daemon(repo, gh, gate, Admission(), self.root / "state",
                       admission_jobs=3, train={"jobs": 2, "family_size": 1})
        for row in gh.snapshot():
            future = Future()
            future.set_result(({"verdict": "MERGEABLE", "residuals": []}, None, 0.0))
            owner.inflight[row["number"]] = {"future": future, "row": row, "base": "c0"}
        second_entered, release_second, all_submitted = Event(), Event(), Event()
        settling, closed = Event(), Event()
        starts, waits, failures = [], [], []
        original_gate, original_close = gate.run, owner.close

        class ObservedPool(ThreadPoolExecutor):
            submitted = 0

            def submit(self, *args, **kwargs):
                future = super().submit(*args, **kwargs)
                self.submitted += 1
                if self.submitted == 3:
                    all_submitted.set()
                return future

            def shutdown(self, wait=True, *, cancel_futures=False):
                waits.append(wait)
                settling.set()
                return super().shutdown(wait=wait, cancel_futures=cancel_futures)

        def controlled_gate(base, commit, label=""):
            starts.append(label)
            if label == "F1":
                if not second_entered.wait(5) or not all_submitted.wait(5):
                    raise RuntimeError("second worker or queued family did not enter")
                self.stop()
            elif label == "F2":
                second_entered.set()
                if not release_second.wait(5):
                    raise RuntimeError("second gate was not released")
            return original_gate(base, commit, label)

        def close():
            closed.set()
            return original_close()

        def watch():
            try:
                self.watch(owner)
            except BaseException as error:
                failures.append(error)

        gate.run, owner.close = controlled_gate, close
        with patch("svrf.train.ThreadPoolExecutor", ObservedPool):
            worker = Thread(target=watch)
            worker.start()
            try:
                self.assertTrue(settling.wait(5))
                self.assertTrue(second_entered.is_set())
                self.assertEqual(waits, [True])
                self.assertFalse(closed.is_set())
                paths = list((self.root / "state" / "receipts").glob("train-*.json"))
                self.assertEqual(len(paths), 1)
                self.assertIsNone(json.loads(paths[0].read_text())["finished"])
                self.assertEqual(sorted(starts), ["F1", "F2"])
            finally:
                release_second.set()
                worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertTrue(closed.is_set())
        receipt = json.loads(paths[0].read_text())
        self.assertIsNotNone(receipt["finished"])
        self.assertEqual(sorted(row["family"] for row in receipt["gates"]), ["F1", "F2"])
        self.assertTrue(all(not row["green"] for row in receipt["gates"]))
        self.assertEqual(receipt["pending"], [2, 3])
        self.assertEqual(gh.merged, [])


class TrainStop(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.stopping = False

    def new_train(self, repo, gh, gate):
        return train(repo, gh, gate, self.root, stop_requested=lambda: self.stopping)

    def test_stop_before_run_keeps_requested_heads_pending_without_acquisition(self):
        repo = FakeRepo([1])
        gh, gate = FakeGitHub(repo), FakeGate(repo)
        self.stopping = True
        receipt = self.new_train(repo, gh, gate).run([1])
        self.assertEqual(receipt["pending"], [1])
        self.assertTrue(receipt["stop_requested"])
        self.assertEqual(repo.fetches, 0)
        self.assertEqual(gh.calls, {"graphql": 0, "rest": 0})
        self.assertEqual(gate.trees, [])

    def test_stop_during_resnapshot_does_not_start_requalification(self):
        repo = FakeRepo([1])
        gh, gate = FakeGitHub(repo), FakeGate(repo)
        original_gate, original_snapshot = gate.run, gh.snapshot
        snapshots = 0

        def move_base(base, commit, label=""):
            result = original_gate(base, commit, label)
            if len(gate.trees) == 1:
                repo.main = repo._new(repo.lanes(repo.main) | {77}, (repo.main,))
            return result

        def stop_on_resnapshot():
            nonlocal snapshots
            snapshots += 1
            if snapshots == 2:
                self.stopping = True
            return original_snapshot()

        gate.run, gh.snapshot = move_base, stop_on_resnapshot
        receipt = self.new_train(repo, gh, gate).run([1])
        self.assertEqual(snapshots, 2)
        self.assertEqual(len(gate.trees), 1)
        self.assertEqual(receipt["pending"], [1])
        self.assertEqual(gh.merged, [])

    def test_stop_during_composition_keeps_plan_without_launching_gate(self):
        repo = FakeRepo([1])
        gh, gate = FakeGitHub(repo), FakeGate(repo)
        owner = self.new_train(repo, gh, gate)
        original_plan = owner.plan

        def stop_after_plan(*args, **kwargs):
            result = original_plan(*args, **kwargs)
            self.stopping = True
            return result

        owner.plan = stop_after_plan
        receipt = owner.run([1])
        self.assertEqual(len(receipt["families"]), 1)
        self.assertEqual(receipt["pending"], [1])
        self.assertEqual(gate.trees, [])
        self.assertEqual(gh.merged, [])

    def test_stop_preserves_red_evidence_without_new_bisection_gates(self):
        repo = FakeRepo([1, 2])
        gh, gate = FakeGitHub(repo), FakeGate(repo, bad=[2])
        original_gate = gate.run

        def stop_after_red(base, commit, label=""):
            result = original_gate(base, commit, label)
            self.stopping = True
            return result

        gate.run = stop_after_red
        receipt = self.new_train(repo, gh, gate).run([1, 2])
        self.assertEqual(len(gate.trees), 1)
        self.assertFalse(receipt["gates"][0]["green"])
        self.assertIn("test_lane_2", receipt["gates"][0]["failing"][0])
        self.assertEqual(receipt["pending"], [1, 2])
        self.assertEqual(gh.merged, [])

    def test_stop_during_publication_finishes_identity_checks_and_preserves_mismatch(self):
        for mismatch in (None, 1):
            with self.subTest(mismatch=mismatch):
                self.stopping = False
                repo = FakeRepo([1, 2])
                gh, gate = FakeGitHub(repo, mismatch_on=mismatch), FakeGate(repo)
                original_merge = gh.merge

                def stop_after_merge(number, sha):
                    merged = original_merge(number, sha)
                    self.stopping = True
                    return merged

                gh.merge = stop_after_merge
                receipt = self.new_train(repo, gh, gate).run([1, 2])
                self.assertTrue(receipt["stop_requested"])
                self.assertEqual(len(gate.trees), 1)
                self.assertEqual(gh.merged, [1, 2] if mismatch is None else [1])
                self.assertIs(receipt["merges"][0]["identity"], mismatch is None)
                self.assertEqual(receipt["stopped"], mismatch is not None)
                if mismatch is None:
                    self.assertTrue(all(row["identity"] for row in receipt["merges"]))
                    self.assertEqual(receipt["pending"], [])
                else:
                    self.assertTrue(any(row["alert"] == "TREE_MISMATCH" for row in receipt["alerts"]))
