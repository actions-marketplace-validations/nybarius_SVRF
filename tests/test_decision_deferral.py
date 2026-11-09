"""Focused controls for deferring an unread landing decision within one run."""
from collections import Counter
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest

from fakes import Clock, FakeGate, FakeGitHub, FakeRepo
from svrf.errors import ReadFailed
from svrf.train import Train

OBSERVATIONS = []


def file_identity(path):
    raw = Path(path).read_bytes()
    return {"path": str(path), "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


class Receiver:
    def __init__(self, root, outcomes=None, after=None):
        self.root = Path(root)
        self.root.mkdir(parents=True)
        self.outcomes = outcomes or {}
        self.after = after
        self.seen = []
        self.originals = []
        self.counts = Counter()

    def __call__(self, source):
        self.seen.append(deepcopy(source))
        number = source["pr"]
        outcomes = self.outcomes.get(number, ("pass",))
        offset = self.counts[number]
        self.counts[number] += 1
        outcome = outcomes[min(offset, len(outcomes) - 1)]
        packet = {"schema": "fixture.decision-original/v1", "outcome": outcome,
                  "source": deepcopy(source)}
        raw = (json.dumps(packet, sort_keys=True, separators=(",", ":")) + "\n").encode()
        path = self.root / ("%03d.json" % len(self.seen))
        path.write_bytes(raw)
        digest = hashlib.sha256(raw).hexdigest()
        self.originals.append({"path": path, "raw": raw, "sha256": digest})
        if self.after is not None:
            self.after(self, source, outcome)
        if outcome == "value_error":
            raise ValueError("decision observation unavailable")
        if outcome == "unknown_read":
            raise ReadFailed("OUTCOME_UNKNOWN")
        if outcome == "unknown_return":
            return {"status": "OUTCOME_UNKNOWN", "source": deepcopy(source)}
        if outcome == "refuse":
            return {"schema": "svrf.landing-refusal/v1",
                    "status": "REFUSED_BEFORE_EFFECTS", "reason": "REVISION_CONFLICT",
                    "source": deepcopy(source),
                    "original": {"path": str(path), "sha256": "sha256:" + digest}}
        return {"status": "OBSERVED", "action": "act_land", "source": deepcopy(source),
                "meton": {"task": "fixture-method", "observation": "sha256:" + "b" * 64}}


class DecisionDeferral(unittest.TestCase):
    def make(self, numbers=(11,), family_size=1, outcomes=None, jobs=2, after=None):
        self._case_count = getattr(self, "_case_count", 0) + 1
        retained = os.environ.get("DECISION_DEFERRAL_ARTIFACTS")
        if retained:
            root = Path(retained) / self._testMethodName / str(self._case_count)
            root.mkdir(parents=True)
        else:
            temporary = tempfile.TemporaryDirectory()
            self.addCleanup(temporary.cleanup)
            root = Path(temporary.name)
        repo = FakeRepo(numbers)
        gh, gate, clock = FakeGitHub(repo), FakeGate(repo), Clock()
        stopped = {"value": False}
        receiver = Receiver(root / "originals", outcomes, after)
        train = Train(repo, gh, gate, receipts=root / "receipts",
                      clock=clock, sleep=clock.sleep, poll_seconds=0,
                      max_rounds=3, family_size=family_size, jobs=jobs, comment=False,
                      pr_comments=False, status_checks=False,
                      stop_requested=lambda: stopped["value"], landing_decision=receiver)
        return SimpleNamespace(root=root, repo=repo, gh=gh, gate=gate, clock=clock,
                               receiver=receiver, train=train, stopped=stopped)

    def observe(self, case, receipt, note=None):
        result = {"test": self._testMethodName, "root": str(case.root),
                  "gate_count": len(receipt["gates"]),
                  "gate_trees": [row["tree"] for row in receipt["gates"]],
                  "callback_prs": [source["pr"] for source in case.receiver.seen],
                  "pending": receipt["pending"], "merged": list(case.gh.merged),
                  "retry_later": deepcopy(receipt["retry_later"]),
                  "holds": deepcopy(receipt["holds"]),
                  "receipt": file_identity(case.train.path),
                  "originals": [file_identity(item["path"]) for item in case.receiver.originals]}
        if note is not None:
            result["note"] = note
        OBSERVATIONS.append(result)

    def assert_originals(self, case):
        for item in case.receiver.originals:
            self.assertEqual(item["path"].read_bytes(), item["raw"])
            self.assertEqual(hashlib.sha256(item["raw"]).hexdigest(), item["sha256"])

    def assert_pending(self, case, receipt, reason, expected=(11,)):
        self.assertEqual(receipt["pending"], list(expected))
        self.assertEqual(receipt["holds"], [])
        self.assertEqual(case.repo.pushes, [])
        self.assertEqual(case.gh.merged, [])
        self.assertTrue(all(row["green"] and not row.get("read_failure")
                            for row in receipt["gates"]))
        self.assertEqual([row["reason"] for row in receipt["retry_later"]], [reason])
        self.assert_originals(case)

    def test_unobserved_valueerror_is_pending_after_one_gate(self):
        case = self.make(outcomes={11: ("value_error",)})
        receipt = case.train.run([11])
        self.observe(case, receipt)
        self.assertEqual(len(receipt["gates"]), 1,
                         "an unread decision repeated an already-green gate in the same run")
        self.assertEqual(case.receiver.counts[11], 1)
        self.assert_pending(case, receipt, "METON_DECISION_UNOBSERVED:ValueError")

    def test_explicit_unknown_effect_is_never_retried_in_the_same_run(self):
        for outcome, reason in (("unknown_read", "OUTCOME_UNKNOWN"),
                                ("unknown_return", "METON_DECISION_SOURCE_UNOBSERVED")):
            with self.subTest(outcome=outcome):
                case = self.make(outcomes={11: (outcome,)})
                receipt = case.train.run([11])
                self.observe(case, receipt, outcome)
                self.assertEqual(len(receipt["gates"]), 1)
                self.assertEqual(case.receiver.counts[11], 1)
                self.assert_pending(case, receipt, reason)

    def test_structured_refusal_keeps_one_fresh_acquisition_and_then_defers(self):
        for outcomes, merged in ((("refuse", "pass"), [11]), (("refuse",), [])):
            with self.subTest(outcomes=outcomes):
                case = self.make(outcomes={11: outcomes})
                receipt = case.train.run([11])
                self.observe(case, receipt, list(outcomes))
                self.assertEqual(len(receipt["gates"]), 1)
                self.assertEqual(case.receiver.counts[11], 2)
                self.assertEqual(case.gh.merged, merged)
                self.assertEqual(receipt["pending"], [] if merged else [11])
                first, second = case.receiver.seen
                for key in ("pr", "head", "base", "prepared", "tree", "family",
                            "family_tree", "gate_index", "gate"):
                    self.assertEqual(first[key], second[key], key)
                self.assertNotEqual(first["receipt"], second["receipt"])
                refusals = receipt["decision_refusals"]
                self.assertEqual(len(refusals), 1 if merged else 2)
                for refusal, original in zip(refusals, case.receiver.originals):
                    self.assertEqual(refusal["original"],
                                     {"path": str(original["path"]),
                                      "sha256": "sha256:" + original["sha256"]})
                if not merged:
                    self.assert_pending(case, receipt, "METON_DECISION_REVISION_CONFLICT")
                self.assert_originals(case)

    def test_landed_prefix_and_entered_later_family_survive_suffix_deferral(self):
        case = self.make(numbers=(11, 12, 13, 14), family_size=2,
                         outcomes={12: ("unknown_read",)})
        second_entered, release_second, second_completed = (threading.Event() for _ in range(3))
        self.addCleanup(release_second.set)
        original_run = case.gate.run
        def entered_gate(base, commit, label=""):
            lanes = case.repo.lanes(commit)
            if 12 in lanes and 13 not in lanes:
                if not second_entered.wait(3):
                    raise AssertionError("the second original gate did not enter")
            elif 12 in lanes and 13 in lanes:
                second_entered.set()
                if not release_second.wait(3):
                    raise AssertionError("the entered original gate was not released")
            result = original_run(base, commit, label)
            if 12 in lanes and 13 in lanes:
                second_completed.set()
            return result
        case.gate.run = entered_gate
        def release_after_unread(receiver, source, outcome):
            if source["pr"] == 12:
                release_second.set()
        case.receiver.after = release_after_unread
        receipt = case.train.run([11, 12, 13, 14])
        self.observe(case, receipt, {"second_entered": second_entered.is_set(),
                                    "second_completed": second_completed.is_set()})
        self.assertTrue(second_entered.is_set())
        self.assertTrue(second_completed.is_set())
        self.assertEqual(case.gh.merged, [11, 13, 14])
        self.assertEqual(receipt["pending"], [12])
        self.assertEqual(receipt["holds"], [])
        self.assertEqual(case.receiver.counts, {11: 1, 12: 1, 13: 1, 14: 1})
        self.assertEqual(len(receipt["gates"]), 3)
        self.assertEqual(set(case.gate.trees),
                         {"T0,11,12", "T0,11,12,13,14", "T0,11,13,14"})
        self.assertEqual(case.repo.lanes(case.repo.main), frozenset({0, 11, 13, 14}))
        self.assertEqual(receipt["retry_later"], [{"number": 12, "reason": "OUTCOME_UNKNOWN"}])
        prefix = receipt["merges"][0]
        later_source = next(source for source in case.receiver.seen if source["pr"] == 13)
        self.assertEqual(later_source["base"], prefix["merge"])
        self.assertEqual(later_source["gate"]["tree"], "T0,11,13,14")
        self.assertEqual(later_source["tree"], "T0,11,13")
        self.assertNotIn(12, case.repo.lanes(later_source["prepared"]))
        self.assert_originals(case)

    def test_postdecision_base_or_head_movement_still_replans(self):
        for kind in ("base", "head"):
            with self.subTest(kind=kind):
                case = self.make()
                def move_once(receiver, source, outcome):
                    if len(receiver.seen) != 1:
                        return
                    if kind == "base":
                        case.repo.main = case.repo._new({0, 77}, ("c0",))
                    else:
                        new_head = case.repo._new({0, 11}, ("h11",))
                        case.repo.heads[11] = new_head
                        case.repo.branches["pr-11"] = new_head
                case.receiver.after = move_once
                receipt = case.train.run([11])
                self.observe(case, receipt, kind)
                self.assertEqual(case.gh.merged, [11])
                self.assertEqual(receipt["pending"], [])
                self.assertEqual(len(receipt["gates"]), 2)
                self.assertEqual(case.receiver.counts[11], 2)
                key = "base" if kind == "base" else "head"
                self.assertNotEqual(case.receiver.seen[0][key], case.receiver.seen[1][key])
                self.assertIn({"number": 11, "reason": "METON_DECISION_" + key.upper() + "_MOVED"},
                              receipt["retry_later"])
                self.assert_originals(case)

    def test_publication_readfailure_keeps_its_existing_replan_branch(self):
        case = self.make()
        original_push, calls = case.repo.push_branch, []
        def fail_first_push(commit, branch):
            calls.append((commit, branch))
            if len(calls) == 1:
                raise ReadFailed("PUSH_REFUSED_BEFORE_EFFECTS")
            return original_push(commit, branch)
        case.repo.push_branch = fail_first_push
        receipt = case.train.run([11])
        self.observe(case, receipt)
        self.assertEqual(len(calls), 2)
        self.assertEqual(case.gh.merged, [11])
        self.assertEqual(receipt["pending"], [])
        self.assertEqual(len(receipt["gates"]), 2)
        self.assertEqual(case.receiver.counts[11], 2)
        self.assertEqual(receipt["retry_later"],
                         [{"number": 11, "reason": "PUSH_REFUSED_BEFORE_EFFECTS"}])
        self.assert_originals(case)

    def test_stop_after_observed_decision_preserves_pending_and_no_effects(self):
        case = self.make()
        case.receiver.after = lambda *unused: case.stopped.update(value=True)
        receipt = case.train.run([11])
        self.observe(case, receipt)
        self.assertEqual(len(receipt["gates"]), 1)
        self.assertEqual(case.receiver.counts[11], 1)
        self.assert_pending(case, receipt, "STOP_REQUESTED")

    def test_a_later_fresh_run_can_receive_the_pending_candidate(self):
        case = self.make(outcomes={11: ("unknown_read",)})
        first = case.train.run([11])
        self.observe(case, first, "first unread invocation")
        self.assertEqual(len(first["gates"]), 1)
        self.assert_pending(case, first, "OUTCOME_UNKNOWN")
        old_original = file_identity(case.receiver.originals[0]["path"])
        receiver = Receiver(case.root / "fresh-originals")
        fresh = Train(case.repo, case.gh, case.gate, receipts=case.root / "fresh-receipts",
                      clock=case.clock, sleep=case.clock.sleep, poll_seconds=0,
                      max_rounds=3, family_size=1, comment=False,
                      pr_comments=False, status_checks=False, landing_decision=receiver)
        second = fresh.run([11])
        self.assertEqual(case.gh.merged, [11])
        self.assertEqual(second["pending"], [])
        self.assertEqual(len(second["gates"]), 1)
        self.assertEqual(len(case.gate.trees), 2)
        self.assertEqual(len(receiver.seen), 1)
        self.assertNotEqual(case.receiver.seen[0]["receipt"], receiver.seen[0]["receipt"])
        self.assertEqual(file_identity(case.receiver.originals[0]["path"]), old_original)
        self.assert_originals(case)
