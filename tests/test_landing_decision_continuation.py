"""Behavioral controls for fresh receiver decisions on one original checked candidate."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from fakes import Clock, FakeGate, FakeGitHub, FakeRepo
from svrf.errors import ReadFailed
from svrf.train import Train, _LandingRevisionRefused
from unittest.mock import patch


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


class Store:
    def __init__(self, root):
        self.root = Path(root)

    def object_path(self, digest):
        return self.root / "objects" / digest[:2] / (digest + ".json")

    def add(self, value):
        raw = canonical(value)
        digest = hashlib.sha256(raw).hexdigest()
        path = self.object_path(digest)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return path, digest, raw


class Receiver:
    """An opaque callback: Train must consume only its returned contract."""
    def __init__(self, root, outcomes=("refuse", "pass"), mutation=None, after=None):
        self.store = Store(root)
        self.outcomes = outcomes
        self.mutation = mutation
        self.after = after
        self.seen = []
        self.originals = []

    def __call__(self, source):
        self.seen.append(deepcopy(source))
        step = len(self.seen) - 1
        outcome = self.outcomes[min(step, len(self.outcomes) - 1)]
        if outcome in ("pass", "observe", "mismatch", "no_observation"):
            result = {"status": "OBSERVED", "action": "act_land", "source": deepcopy(source),
                      "meton": {"task": "original-method", "observation": "sha256:" + "b" * 64}}
            if outcome == "observe":
                result["action"] = "act_observe"
            elif outcome == "mismatch":
                result["source"]["head"] = "another-head"
            elif outcome == "no_observation":
                del result["meton"]["observation"]
            if self.after:
                self.after(self, step, source)
            return result
        if outcome == "bare":
            raise ValueError("REVISION_CONFLICT")
        if outcome == "read_failed":
            raise ReadFailed("REVISION_CONFLICT")
        if outcome == "internal_exception":
            raise _LandingRevisionRefused(0)
        packet = {"message": "révision changed", "source": deepcopy(source),
                  "secondary": [{"status": "UNOBSERVED", "detail": "notification unavailable"}]}
        path, digest, raw = self.store.add(packet)
        self.originals.append({"path": path, "sha256": digest, "raw": raw, "source": deepcopy(source)})
        result = {"schema": "svrf.landing-refusal/v1", "status": "REFUSED_BEFORE_EFFECTS",
                  "reason": "REVISION_CONFLICT", "source": deepcopy(source),
                  "original": {"path": str(path), "sha256": "sha256:" + digest}}
        if outcome == "missing":
            path.unlink()
        elif outcome == "corrupt":
            path.write_bytes(raw + b" ")
        elif outcome == "oversized":
            raw = b"x" * (1024 * 1024 + 1)
            path.write_bytes(raw)
            result["original"]["sha256"] = "sha256:" + hashlib.sha256(raw).hexdigest()
        elif outcome == "directory":
            path.unlink()
            path.mkdir()
        elif outcome == "fifo":
            path.unlink()
            os.mkfifo(path)
        elif outcome == "symlink":
            target = path.with_suffix(".original")
            path.rename(target)
            path.symlink_to(target)
        if self.mutation:
            self.mutation(self, result)
        if self.after:
            self.after(self, step, source)
        return result



class RevisionContinuation(unittest.TestCase):
    def make(self, outcomes=("refuse", "pass"), mutation=None, after=None, rounds=1):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        repo = FakeRepo([11, 12])
        gh, gate, clock = FakeGitHub(repo), FakeGate(repo), Clock()
        stopped = {"value": False}
        train = Train(repo, gh, gate, receipts=root / "receipts",
                      clock=clock, sleep=clock.sleep, poll_seconds=0,
                      max_rounds=rounds, family_size=1, comment=False,
                      pr_comments=False, status_checks=False,
                      stop_requested=lambda: stopped["value"])
        receiver = Receiver(root / "custody", outcomes, mutation=mutation, after=after)
        train.landing_decision = receiver
        return SimpleNamespace(train=train, receiver=receiver, repo=repo, gh=gh,
                               gate=gate, stopped=stopped, root=root)

    def assert_no_effects(self, case):
        self.assertEqual(case.repo.pushes, [])
        self.assertEqual(case.gh.merged, [])

    def test_fresh_decision_reuses_one_original_gate(self):
        case = self.make(rounds=2)
        receipt = case.train.run([11])
        self.assertEqual(case.gh.merged, [11])
        self.assertEqual(len(case.repo.pushes), 1)
        self.assertEqual(len(case.receiver.seen), 2)
        self.assertEqual(len(case.gate.trees), 1, "a receiver revision refusal reopened an unchanged gate")
        self.assertEqual(len(receipt["gates"]), 1)
        first, second = case.receiver.seen
        for key in ("pr", "head", "base", "prepared", "tree", "family", "family_tree", "gate_index", "gate"):
            self.assertEqual(first[key], second[key], key)
        self.assertNotEqual(first["receipt"], second["receipt"])
        self.assertEqual(len(receipt["decision_refusals"]), 1)
        refusal = receipt["decision_refusals"][0]
        original = case.receiver.originals[0]
        self.assertEqual(refusal["source"], first)
        self.assertEqual(refusal["original"], {"path": str(original["path"]),
                                             "sha256": "sha256:" + original["sha256"]})
        self.assertEqual(original["path"].read_bytes(), original["raw"])
        self.assertEqual(hashlib.sha256(original["raw"]).hexdigest(), original["sha256"])
        self.assertEqual(json.loads(original["raw"])["secondary"][0]["status"], "UNOBSERVED")
        self.assertIn("révision".encode(), original["raw"])

    def test_missing_changed_oversized_or_nonregular_original_never_continues(self):
        for outcome in ("missing", "corrupt", "oversized", "directory", "fifo", "symlink"):
            with self.subTest(outcome=outcome):
                case = self.make((outcome, "pass"))
                receipt = case.train.run([11])
                self.assertEqual(len(case.receiver.seen), 1)
                self.assert_no_effects(case)
                self.assertTrue(receipt["gates"][0]["green"])
                self.assertFalse(receipt.get("decision_refusals"))

    def test_callback_exceptions_never_authorize_continuation(self):
        for outcome in ("bare", "read_failed", "internal_exception"):
            with self.subTest(outcome=outcome):
                case = self.make((outcome, "pass"))
                receipt = case.train.run([11])
                self.assertEqual(len(case.receiver.seen), 1)
                self.assert_no_effects(case)
                self.assertFalse(receipt.get("decision_refusals"))
                expected_reason = {
                    "bare": "METON_DECISION_UNOBSERVED:ValueError",
                    "read_failed": "REVISION_CONFLICT",
                    "internal_exception": "METON_DECISION_UNOBSERVED:_LandingRevisionRefused",
                }[outcome]
                self.assertTrue(any(row["reason"] == expected_reason
                                    for row in receipt["retry_later"]))


    def test_only_exact_source_bound_refusal_contract_continues(self):
        mutations = {
            "schema": lambda r, value: value.update(schema="unknown/1"),
            "status": lambda r, value: value.update(status="OUTCOME_UNKNOWN"),
            "reason": lambda r, value: value.update(reason="OTHER_CONFLICT"),
            "extra_unknown": lambda r, value: value.update(outcome_unknown=True),
            "source_head": lambda r, value: value["source"].update(head="another-head"),
            "source_receipt": lambda r, value: value["source"]["receipt"].update(sha256="0" * 64),
            "missing_original": lambda r, value: value.pop("original"),
            "original_type": lambda r, value: value.update(original=[]),
            "extra_reference": lambda r, value: value["original"].update(retry=True),
            "relative_path": lambda r, value: value["original"].update(path="relative.json"),
            "path_type": lambda r, value: value["original"].update(path=3),
            "digest_type": lambda r, value: value["original"].update(sha256=None),
            "digest_length": lambda r, value: value["original"].update(sha256="sha256:" + "a" * 63),
            "digest_uppercase": lambda r, value: value["original"].update(sha256="sha256:" + "A" * 64),
            "digest_nonhex": lambda r, value: value["original"].update(sha256="sha256:" + "g" * 64),
            "digest_wrong": lambda r, value: value["original"].update(sha256="sha256:" + "0" * 64),
            "oversized_return": lambda r, value: value["original"].update(path="/" + "x" * (1024 * 1024)),
        }
        for name, mutation in mutations.items():
            with self.subTest(boundary=name):
                case = self.make(mutation=mutation)
                receipt = case.train.run([11])
                self.assertEqual(len(case.receiver.seen), 1)
                self.assert_no_effects(case)
                self.assertTrue(receipt["gates"][0]["green"])
                self.assertFalse(receipt.get("decision_refusals"))

    def test_reference_mutation_does_not_replace_verified_identity(self):
        case = self.make()
        source = {"value": "exact"}
        result = case.receiver(source)
        expected = deepcopy(result["original"])
        real_open = os.open
        def mutate_reference(name, flags):
            result["original"].update(path="/changed-original.json", sha256="sha256:" + "f" * 64)
            return real_open(name, flags)
        with patch("svrf.train.os.open", side_effect=mutate_reference):
            self.assertEqual(case.train._retained_landing_revision_refusal(result, source), expected)

    def test_unreadable_original_does_not_authorize_continuation(self):
        case = self.make()
        source = {"value": "exact"}
        result = case.receiver(source)
        with patch("svrf.train.os.open", side_effect=PermissionError("unreadable")):
            self.assertIsNone(case.train._retained_landing_revision_refusal(result, source))




    def test_fresh_base_head_closed_or_held_state_stops_continuation(self):
        for kind in ("base", "head", "closed", "unobserved_state", "held", "stopped"):
            with self.subTest(change=kind):
                case = self.make()
                def changed(receiver, step, source):
                    if step:
                        return
                    if kind == "base":
                        case.repo.main = case.repo._new({0, 77}, ("c0",))
                    elif kind == "head":
                        case.repo.branches["pr-11"] = "changed-head"
                    elif kind == "closed":
                        pull = case.gh.pull
                        case.gh.pull = lambda n: {**pull(n), "state": "closed"}
                    elif kind == "unobserved_state":
                        pull = case.gh.pull
                        case.gh.pull = lambda n: {k: v for k, v in pull(n).items() if k != "state"}
                    elif kind == "held":
                        case.gh.labels = {11: ["train:hold"]}
                    else:
                        case.stopped["value"] = True
                case.receiver.after = changed
                receipt = case.train.run([11])
                self.assertEqual(len(case.receiver.seen), 1)
                self.assert_no_effects(case)
                self.assertEqual(len(receipt["decision_refusals"]), 1)

    def test_closed_during_fresh_decision_prevents_publication(self):
        case = self.make()
        def close_after_decision(receiver, step, source):
            if step != 1:
                return
            pull = case.gh.pull
            case.gh.pull = lambda n: {**pull(n), "state": "closed"}
        case.receiver.after = close_after_decision
        case.train.run([11])
        self.assertEqual(len(case.receiver.seen), 2)
        self.assertEqual(len(case.gate.trees), 1)
        self.assert_no_effects(case)

    def test_stop_during_fresh_pull_prevents_the_second_callback(self):
        case = self.make()
        def stop_on_pull(receiver, step, source):
            if step:
                return
            pull = case.gh.pull
            def stopped_pull(n):
                result = pull(n)
                case.stopped["value"] = True
                return result
            case.gh.pull = stopped_pull
        case.receiver.after = stop_on_pull
        case.train.run([11])
        self.assertEqual(len(case.receiver.seen), 1)
        self.assert_no_effects(case)

    def test_stop_during_post_decision_pull_prevents_publication(self):
        case = self.make()
        def stop_on_pull(receiver, step, source):
            if step != 1:
                return
            pull = case.gh.pull
            def stopped_pull(n):
                result = pull(n)
                case.stopped["value"] = True
                return result
            case.gh.pull = stopped_pull
        case.receiver.after = stop_on_pull
        case.train.run([11])
        self.assertEqual(len(case.receiver.seen), 2)
        self.assert_no_effects(case)

    def test_repeated_original_refusals_are_bounded_and_preserved(self):
        case = self.make(("refuse",))
        receipt = case.train.run([11])
        self.assertEqual(len(case.receiver.seen), 2)
        self.assertEqual(len(case.gate.trees), 1)
        self.assert_no_effects(case)
        self.assertEqual(len(receipt["decision_refusals"]), 2)
        for refusal, original in zip(receipt["decision_refusals"], case.receiver.originals):
            self.assertEqual(refusal["original"]["sha256"], "sha256:" + original["sha256"])
            self.assertEqual(original["path"].read_bytes(), original["raw"])
        self.assertTrue(any(x["reason"] == "METON_DECISION_REVISION_CONFLICT"
                            for x in receipt["retry_later"]))

    def test_fresh_nonlanding_or_invalid_evidence_still_refuses(self):
        for outcome in ("observe", "mismatch", "no_observation"):
            with self.subTest(outcome=outcome):
                case = self.make(("refuse", outcome))
                receipt = case.train.run([11])
                self.assertEqual(len(case.receiver.seen), 2)
                self.assertEqual(len(case.gate.trees), 1)
                self.assert_no_effects(case)
                self.assertEqual(len(receipt["decision_refusals"]), 1)



    def test_multistep_family_retains_its_distinct_prefix_tree(self):
        case = self.make()
        case.train.family_size = 2
        receipt = case.train.run([11, 12])
        self.assertEqual(case.gh.merged, [11, 12])
        self.assertEqual(len(case.gate.trees), 1)
        self.assertEqual(len(case.receiver.seen), 3)
        first, fresh = case.receiver.seen[:2]
        self.assertNotEqual(first["tree"], first["family_tree"])
        self.assertEqual(first["prepared"], fresh["prepared"])
        self.assertEqual(first["gate"], fresh["gate"])
        self.assertEqual(len(receipt["decision_refusals"]), 1)
