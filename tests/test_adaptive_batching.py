"""Adaptive family size: the train reads its own retained receipts, estimates the chance an
admitted pull request is red at the gate, and cuts families at the size that minimises the
expected gate runs per pull request under halving bisection (capped). Admission reads get
their own slot count instead of borrowing the gate's memory-sized `jobs`."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from svrf import batching
from svrf.config import ConfigError, from_dict

MINIMAL = {"repo": "example/project", "gate": {"commands": ["make test"]}}


def _receipt(folder: Path, name: str, first_size: int, landed: bool, heads: dict | None = None) -> None:
    prs = list(heads) if heads else list(range(1, first_size + 1))
    family = {"id": "F1", "prs": prs, "status": "LANDED" if landed else "BISECTED"}
    receipt = {"families": [family]}
    if heads:
        receipt["prs"] = {str(n): {"number": n, "head_sha": sha} for n, sha in heads.items()}
    (folder / f"train-{name}.json").write_text(json.dumps(receipt))


class ExpectedCost(unittest.TestCase):
    def test_a_single_pull_request_costs_one_gate(self):
        for p in (0.0, 0.1, 0.5, 0.9):
            self.assertEqual(batching.expected_gates_per_pr(1, p), 1.0)

    def test_without_red_arrivals_a_family_costs_one_gate_for_all(self):
        self.assertEqual(batching.expected_gates_per_pr(8, 0.0), 1 / 8)

    def test_cost_is_exact_halving_recursion(self):
        p = 0.25
        q2 = (1 - p) ** 2
        self.assertAlmostEqual(batching.expected_gates_per_pr(2, p), (1 + (1 - q2) * 2) / 2)


class OptimalSize(unittest.TestCase):
    def test_no_red_arrivals_take_the_cap(self):
        self.assertEqual(batching.optimal_family_size(0.0, cap=8), 8)

    def test_frequent_red_arrivals_gate_one_at_a_time(self):
        self.assertEqual(batching.optimal_family_size(0.36, cap=8), 1)

    def test_rare_red_arrivals_batch(self):
        k = batching.optimal_family_size(0.02, cap=16)
        self.assertGreater(k, 4)
        self.assertLessEqual(k, 16)

    def test_unknown_rate_keeps_the_cap(self):
        self.assertEqual(batching.optimal_family_size(None, cap=8), 8)


class RedProbabilityFromReceipts(unittest.TestCase):
    def test_estimate_reads_first_family_outcomes(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            for i in range(30):
                _receipt(folder, f"a{i}", 1, landed=(i % 3 != 0))  # 1 red in 3 singletons
            p, n = batching.red_probability(folder)
            self.assertEqual(n, 30)
            self.assertAlmostEqual(p, 1 / 3, places=2)

    def test_a_red_head_regated_across_rounds_is_one_observation(self):
        # The same pull request at the same head re-gated red in ten rounds is one red arrival,
        # not ten: the verdict on that exact head was already observed.
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            for i in range(10):
                _receipt(folder, f"r{i}", 1, landed=False, heads={7: "a" * 40})
            for i in range(9):
                _receipt(folder, f"g{i}", 1, landed=True, heads={100 + i: f"{i:040d}"})
            p, n = batching.red_probability(folder)
            self.assertEqual(n, 10)
            self.assertAlmostEqual(p, 1 / 10, places=2)

    def test_a_moved_head_is_a_new_observation(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            _receipt(folder, "r0", 1, landed=False, heads={7: "a" * 40})
            _receipt(folder, "r1", 1, landed=True, heads={7: "b" * 40})
            self.assertEqual(batching.red_probability(folder)[1], 2)

    def test_no_receipts_is_unknown_not_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(batching.red_probability(Path(tmp)), (None, 0))

    def test_unreadable_receipt_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "train-bad.json").write_text("{")
            _receipt(folder, "ok", 2, landed=True)
            p, n = batching.red_probability(folder)
            self.assertEqual(n, 1)

    def test_choose_reads_receipts_and_returns_its_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            for i in range(40):
                _receipt(folder, f"g{i}", 4, landed=True)
            choice = batching.choose(folder, cap=8)
            self.assertEqual(choice["family_size"], 8)
            self.assertEqual(choice["observations"], 40)
            self.assertEqual(choice["red_probability"], 0.0)


class Config(unittest.TestCase):
    def test_family_size_may_be_adaptive_with_a_cap(self):
        config = from_dict({**MINIMAL, "train": {"family_size": "adaptive", "family_cap": 12}})
        self.assertEqual(config.train.family_size, "adaptive")
        self.assertEqual(config.train.family_cap, 12)

    def test_other_family_size_words_are_refused(self):
        with self.assertRaises(ConfigError):
            from_dict({**MINIMAL, "train": {"family_size": "big"}})

    def test_admission_slots_default_to_jobs_and_may_be_set(self):
        self.assertIsNone(from_dict(MINIMAL).admission_slots)
        config = from_dict({**MINIMAL, "admission": {"command": "true", "slots": 4}})
        self.assertEqual(config.admission_slots, 4)
        with self.assertRaises(ConfigError):
            from_dict({**MINIMAL, "admission": {"slots": 0}})


if __name__ == "__main__":
    unittest.main()


class TrainUsesTheChosenSize(unittest.TestCase):
    def test_adaptive_train_records_its_choice_and_cuts_families_by_it(self):
        from fakes import FakeGate, FakeGitHub, FakeRepo
        from test_train import train
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            for i in range(30):
                _receipt(folder, f"r{i}", 1, landed=(i % 2 == 0))   # p = 1/2: gate one at a time
            repo = FakeRepo([1, 2, 3])
            gh, gate = FakeGitHub(repo), FakeGate(repo)
            receipt = train(repo, gh, gate, tmp, family_size="adaptive", family_cap=8).run([1, 2, 3])
            self.assertEqual(receipt["batching"][0]["family_size"], 1)
            self.assertEqual(gh.merged, [1, 2, 3])
            self.assertTrue(all(len(f["prs"]) == 1 for f in receipt["families"]))
