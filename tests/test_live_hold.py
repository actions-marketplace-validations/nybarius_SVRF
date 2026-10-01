"""A hold arriving after discovery must still prevent publication."""
import json
import tempfile
import unittest
from unittest.mock import patch

from fakes import Admission, DaemonGitHub, DaemonRepo, FakeGate, FakeGitHub, FakeRepo
from test_daemon import daemon
from test_train import train
from svrf.github import RealGitHub


class LiveHold(unittest.TestCase):
    def test_late_hold_prevents_push_and_merge_but_other_lane_can_land(self):
        repo = FakeRepo([1, 2])

        class Hub(FakeGitHub):
            def pull(self, number):
                return {**super().pull(number),
                        "labels": [{"name": "train:hold"}] if number == 1 else []}

        hub = Hub(repo)
        with tempfile.TemporaryDirectory() as directory:
            receipt = train(repo, hub, FakeGate(repo), directory).run([1, 2])
        self.assertEqual(hub.merged, [2])
        self.assertEqual([branch for branch, _ in repo.pushes], ["pr-2"])
        self.assertTrue(all(row["identity"] for row in receipt["merges"]))
        self.assertIn({"number": 1, "reason": "HOLD_LABEL:train:hold"}, receipt["retry_later"])

    def test_hold_after_preparation_still_prevents_merge(self):
        repo = FakeRepo([1])

        class Hub(FakeGitHub):
            def pull(self, number):
                return {**super().pull(number),
                        "labels": [{"name": "train:hold"}] if repo.pushes else []}

        hub = Hub(repo)
        with tempfile.TemporaryDirectory() as directory:
            receipt = train(repo, hub, FakeGate(repo), directory).run([1])
        self.assertEqual(len(repo.pushes), 1)
        self.assertEqual(hub.merged, [])
        self.assertEqual(receipt["merges"], [])

    def test_missing_or_malformed_labels_are_an_unobserved_read(self):
        for labels in (None, "train:hold", [None], [{"unread": "name"}]):
            with self.subTest(labels=labels):
                repo = FakeRepo([1])

                class Hub(FakeGitHub):
                    def pull(self, number):
                        return {**super().pull(number), "labels": labels}

                hub = Hub(repo)
                with tempfile.TemporaryDirectory() as directory:
                    receipt = train(repo, hub, FakeGate(repo), directory).run([1])
                self.assertEqual(repo.pushes, [])
                self.assertEqual(hub.merged, [])
                self.assertEqual(receipt["holds"], [])
                self.assertIn({"number": 1, "reason": "PULL_LABELS_UNOBSERVED"},
                              receipt["retry_later"])

    def test_daemon_carries_custom_hold_label_to_landing(self):
        repo = DaemonRepo([1])

        class Hub(DaemonGitHub):
            def pull(self, number):
                return {**super().pull(number), "labels": [{"name": "operator:wait"}]}

        hub = Hub(repo)
        with tempfile.TemporaryDirectory() as directory:
            daemon(repo, hub, FakeGate(repo), Admission(), directory,
                   hold_label="operator:wait").tick()
        self.assertEqual(hub.merged, [])
        self.assertEqual(repo.pushes, [])

    def test_real_pull_preserves_label_occurrence(self):
        value = {"head": {"sha": "abc", "ref": "branch"}, "state": "open",
                 "draft": False, "labels": [{"name": "train:hold", "id": 7}]}
        with patch.object(RealGitHub, "_gh", return_value=json.dumps(value)):
            result = RealGitHub("o/r").pull(1)
        self.assertEqual(result.get("labels"), value["labels"])
