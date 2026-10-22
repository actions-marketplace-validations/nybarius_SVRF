"""Original gate completion and merge acknowledgement have distinct clocks."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fakes import Admission, Clock, DaemonGitHub, DaemonRepo, FakeGate
from test_daemon import daemon
from svrf.errors import ReadFailed
from svrf.train import Train


class ProducerClocks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = DaemonRepo([1])
        self.gh = DaemonGitHub(self.repo)
        self.gate = FakeGate(self.repo)
        self.clock = Clock()
        self.owner = None
        self.addCleanup(self.close_owner)

    def close_owner(self):
        if self.owner is not None:
            self.owner.close()

    def make_owner(self):
        self.owner = daemon(self.repo, self.gh, self.gate, Admission(), self.root, clock=self.clock,
                            train={"max_rounds": 1, "comment": False,
                                   "pr_comments": False, "status_checks": False})
        return self.owner

    def train(self):
        return Train(self.repo, self.gh, self.gate, receipts=self.root / "receipts",
                     clock=self.clock, sleep=self.clock.sleep, poll_seconds=0, max_rounds=1,
                     comment=False, pr_comments=False, status_checks=False)

    def test_live_pass_is_saved_before_merge_and_ack_precedes_the_landed_tree_read(self):
        gate_run, merge = self.gate.run, self.gh.merge
        snapshots = {}
        def gated(*args):
            result = gate_run(*args)
            self.clock.sleep(4)
            return result
        def acknowledged(*args):
            snapshots["before_merge"] = self.owner.load_state()
            self.clock.sleep(6)
            return merge(*args)
        def read_landed(sha):
            snapshots["before_tree"] = self.owner.load_state()
            for row in snapshots["before_tree"].get("train_gate_history", {}).values():
                snapshots["receipt_digest"] = hashlib.sha256(Path(row["receipt"]).read_bytes()).hexdigest()
            self.clock.sleep(20)
        self.gate.run, self.gh.merge, self.repo.fetch_commit = gated, acknowledged, read_landed
        with patch("svrf.train.time.monotonic", side_effect=lambda: self.clock() + 4000):
            self.make_owner().tick()
        before = snapshots["before_merge"].get("train_gate_history", {})
        self.assertEqual(len(before), 1, "the live PASS wait was absent until Train.run returned")
        first = next(iter(before.values()))
        self.assertTrue(first["green"])
        self.assertEqual(first["prs"], [1])
        self.assertEqual(first["base"], "c0")
        self.assertEqual(first["merges"], [])
        gate = first["completed_event"]
        self.assertEqual((gate["kind"], gate["wall"], gate["monotonic"]), ("gate_completed", 1004, 5004))
        self.assertTrue(gate["owner"]["boot_id"])
        during = next(iter(snapshots["before_tree"]["train_gate_history"].values()))
        self.assertEqual(during["receipt_sha256"], snapshots["receipt_digest"])
        self.assertEqual(during["merges"][0]["observed_tree"], None)
        acknowledgement = during["merges"][0]["acknowledged_event"]
        self.assertEqual((acknowledgement["kind"], acknowledgement["wall"]), ("merge_acknowledged", 1010))
        self.assertEqual(acknowledgement["owner"]["boot_id"], gate["owner"]["boot_id"])
        self.assertEqual(acknowledgement["monotonic"] - gate["monotonic"], 6)
        final = next(iter(self.owner.load_state()["train_gate_history"].values()))
        self.assertTrue(final["merges"][0]["identity"])
        self.assertEqual(final["merges"][0]["at"], 1030)
        self.assertEqual(final["qualification"]["family_status"], "LANDED")
        self.assertEqual(self.owner.load_state()["last_tick"]["observed_at"], 1030)
        self.assertNotIn("merged_at", final["merges"][0])

    def test_an_unobserved_merge_reply_keeps_pass_open_without_inventing_an_ack(self):
        def unavailable(*args):
            raise ReadFailed("MERGE_ACK_UNOBSERVED")
        self.gh.merge = unavailable
        self.make_owner().tick()
        history = self.owner.load_state().get("train_gate_history", {})
        self.assertTrue(history, "gate completion was lost with an unread merge reply")
        self.assertTrue(any(row["green"] for row in history.values()))
        self.assertTrue(all(not row["merges"] for row in history.values()))

    def test_reused_red_gate_preserves_its_original_completion_event(self):
        self.gate.bad = {1}
        train = self.train()
        train.run([1])
        original = train.receipt["gates"][0]
        self.assertIn("completed_event", original, "the original gate has no completion timestamp")
        self.clock.sleep(37)
        family = train.plan([1], "c0")
        reused = train.gate_family(family, "c0")
        self.assertEqual(reused["reused"], 0)
        self.assertEqual(reused["completed_event"], original["completed_event"])
        self.assertEqual(len(self.gate.trees), 1)
        self.assertFalse(reused["green"])

    def test_a_changed_base_is_retained_as_qualification_separate_from_green(self):
        original = self.gate.run
        def moved(*args):
            result = original(*args)
            self.repo.main = self.repo._new({0, 77}, ("c0",))
            return result
        self.gate.run = moved
        self.make_owner().tick()
        rows = list(self.owner.load_state().get("train_gate_history", {}).values())
        self.assertTrue(rows, "the source-qualified gate observation was lost")
        first = rows[0]
        self.assertTrue(first["green"])
        self.assertEqual(first["base"], "c0")
        self.assertEqual(first["qualification"]["family_status"], "REQUEUED")
        self.assertIn("BASE_MOVED_EXTERNALLY", json.dumps(first["qualification"]["alerts"]))
        self.assertEqual(first["merges"], [])

    def test_unavailable_state_projection_preserves_the_original_merge_result(self):
        train = self.train()
        def unavailable(*args):
            raise OSError("original storage unavailable")
        train.on_receipt = unavailable
        receipt = train.run([1])
        self.assertEqual(len(receipt["merges"]), 1)
        self.assertTrue(receipt["merges"][0]["identity"])
        self.assertEqual(receipt.get("state_projection", {}).get("status"), "UNOBSERVED")


if __name__ == "__main__":
    unittest.main()
