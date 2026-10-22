"""An original failed gate remains addressable after its durable hold is written."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from fakes import Admission, DaemonGitHub, DaemonRepo, FakeGate, FakeGitHub, FakeRepo
from test_daemon import daemon
from test_train import train


class RecordedGate(FakeGate):
    def run(self, base, commit, label=""):
        result = super().run(base, commit, label)
        result["steps"] = [{"phase": "gate", "command": "check", "rc": 0 if result["green"] else 1,
                            "seconds": 0.25, "tail": "original check receipt"}]
        return result


class GateHoldBinding(unittest.TestCase):
    def original_row(self, hold):
        binding = hold["gate"]
        receipt = json.loads(Path(binding["receipt"]).read_text())
        row = receipt["gates"][binding["index"]]
        digest = hashlib.sha256(json.dumps(
            row, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
        self.assertEqual(binding["row_sha256"], digest)
        self.assertEqual({k: binding[k] for k in ("base", "commit", "tree")},
                         {k: row[k] for k in ("base", "commit", "tree")})
        self.assertFalse(row["green"])
        self.assertFalse(row.get("read_failure"))
        self.assertEqual(row["steps"][0]["rc"], 1)
        self.assertEqual(row["steps"][0]["tail"], "original check receipt")
        return row

    def test_singleton_hold_keeps_the_original_gate_row_and_source_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = FakeRepo([7])
            gh, gate = FakeGitHub(repo), RecordedGate(repo, bad={7})
            owner = train(repo, gh, gate, tmp)
            receipt = owner.run([7])
            hold, = receipt["holds"]
            self.assertEqual(hold["reason"], "GATE_RED")
            self.assertEqual(hold["gate"]["receipt"], str(owner.path.resolve()))
            self.assertEqual(hold["gate"]["index"], 0)
            self.assertEqual(self.original_row(hold)["prs"], [7])
            self.assertEqual(gh.merged, [])
            self.assertEqual(len(gate.trees), 1)

    def test_bisection_reference_names_the_singleton_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = FakeRepo([1, 2, 3, 4])
            gh, gate = FakeGitHub(repo), RecordedGate(repo, bad={2})
            owner = train(repo, gh, gate, tmp)
            receipt = owner.run([1, 2, 3, 4])
            hold, = receipt["holds"]
            self.assertEqual(hold["number"], 2)
            row = self.original_row(hold)
            self.assertEqual(row["prs"], [2])
            self.assertGreater(hold["gate"]["index"], 0)
            self.assertEqual(sorted(gh.merged), [1, 3, 4])
            self.assertIn("FAILED tests/test_lane_2.py::test_it", hold["failing"])

    def test_daemon_preserves_the_binding_without_rechecking_the_held_head(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = DaemonRepo([8])
            gh, gate, admission = DaemonGitHub(repo), RecordedGate(repo, bad={8}), Admission()
            owner = daemon(repo, gh, gate, admission, tmp)
            first = owner.tick()
            hold = owner.load_state()["held"]["8"]
            self.assertEqual(hold["reason"], "GATE_RED")
            self.assertEqual(self.original_row(hold)["prs"], [8])
            self.assertEqual(first["held"], [8])
            self.assertNotIn("watch", hold)
            self.assertNotIn("base_sha", hold)
            original_calls = list(admission.calls), list(gate.trees)
            owner.tick()
            self.assertEqual(owner.load_state()["held"]["8"]["gate"], hold["gate"])
            self.assertEqual((admission.calls, gate.trees), original_calls)
            self.assertEqual(gh.merged, [])

    def test_a_failed_read_is_still_retried_without_a_content_hold(self):
        class UnreadGate(RecordedGate):
            def run(self, base, commit, label=""):
                result = super().run(base, commit, label)
                result["read_failure"] = "GATE_INFRA:READ_UNAVAILABLE"
                return result

        with tempfile.TemporaryDirectory() as tmp:
            repo = FakeRepo([9])
            gh, gate = FakeGitHub(repo), UnreadGate(repo, bad={9})
            receipt = train(repo, gh, gate, tmp, max_rounds=1).run([9])
            self.assertEqual(receipt["holds"], [])
            self.assertEqual(gh.merged, [])
            self.assertTrue(any(row["reason"] == "GATE_INFRA:READ_UNAVAILABLE"
                                for row in receipt["retry_later"]))

    def test_an_unrecorded_result_cannot_invent_an_original_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = FakeRepo([10])
            owner = train(repo, FakeGitHub(repo), RecordedGate(repo, bad={10}), tmp)
            owner.settle_red([10], {"failing": ["FAIL unrecorded"]}, "unrecorded")
            hold, = owner.receipt["holds"]
            self.assertEqual(hold["reason"], "GATE_RED")
            self.assertEqual(hold["failing"], ["FAIL unrecorded"])
            self.assertNotIn("gate", hold)


if __name__ == "__main__":
    unittest.main()
