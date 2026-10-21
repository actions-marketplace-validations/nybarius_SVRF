"""An unavailable dependency read must not erase a failed verdict's reopening basis."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fakes import Admission, Clock, DaemonGitHub, DaemonRepo, FakeGate
from svrf import rules
from svrf.daemon import Daemon
from svrf.errors import ReadFailed
from svrf.git import RealGit


class UnavailableWatchRepo(DaemonRepo):
    def __init__(self):
        super().__init__([31, 32, 33])
        self.blocked = True

    def watch_digest(self, commit, paths):
        if self.blocked is True or self.blocked and commit in self.blocked:
            raise ReadFailed("WATCH_TEMPORARILY_UNREADABLE")
        return super().watch_digest(commit, paths)


class HoldWatchRecovery(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = UnavailableWatchRepo()
        self.gh = DaemonGitHub(self.repo)
        self.admission = Admission({
            "h31": {"verdict": "HELD", "residuals": ["check:actual-failure"],
                    "consumed": [{"kind": "CODE", "id": "lane:32"}]},
            "h32": {"verdict": "HELD", "residuals": ["check:dependency"]}})
        self.owner = self.owner_at_saved_state()

    def owner_at_saved_state(self):
        clock = Clock()
        return Daemon(self.repo, self.gh, FakeGate(self.repo), self.admission,
                      state_dir=self.root, receipts=self.root / "receipts",
                      clock=clock, sleep=clock.sleep, train_options={"poll_seconds": 0})

    def held(self):
        return json.loads((self.root / "state.json").read_text())["held"]["31"]

    def reads(self):
        return sum(head == "h31" for head, _ in self.admission.calls)

    def test_recovered_basis_does_not_recheck_unrelated_main_movement(self):
        original_base = self.repo.main
        self.owner.tick()  # The failed read is saved; unrelated lane 33 lands.
        self.assertEqual(self.gh.merged, [33])
        self.assertEqual(self.held().get("watch"), ["lane:32"])
        self.assertEqual(self.held().get("base_sha"), original_base)
        self.assertIsNone(self.held().get("watch_digest"))
        self.repo.blocked = False
        self.owner.tick()
        self.assertEqual(self.reads(), 1)
        self.assertEqual(self.held()["watch_digest"], "")  # Empty is a valid observed digest.
        self.assertEqual(self.held()["base_sha"], self.repo.main)
        self.assertEqual(self.held()["failing"], ["check:actual-failure"])

    def test_restart_recovers_original_basis_before_comparing_a_fixed_dependency(self):
        self.owner.tick()
        self.repo.move_head(32)
        self.owner.tick()  # Lane 32 lands while the dependency reads are unavailable.
        self.assertEqual(self.gh.merged, [33, 32])
        self.assertEqual(self.reads(), 1)
        self.repo.blocked = {self.repo.main}  # Old basis readable; current source still unknown.
        restarted = self.owner_at_saved_state()
        restarted.tick()
        self.assertEqual(self.reads(), 1)
        self.assertEqual(self.held()["failing"], ["check:actual-failure"])
        self.repo.blocked = False
        restarted.tick()
        self.assertEqual(self.reads(), 2)
        self.assertNotIn(31, self.gh.merged)  # The repeated check still observes a real failure.
        self.assertEqual(self.held()["failing"], ["check:actual-failure"])

    def test_payload_only_basis_survives_the_same_read_failure(self):
        root = self.root / "payload-repository"
        root.mkdir()

        def git(*args, input=None):
            return subprocess.check_output(["git", "-C", str(root), *args],
                                           input=input, text=True).strip()

        git("init", "-q")
        git("config", "user.name", "test")
        git("config", "user.email", "test@localhost")
        (root / "unread.txt").write_text("initial")
        git("add", "-A")
        git("commit", "-qm", "original base")
        base = git("rev-parse", "HEAD")
        blob = git("hash-object", "-w", "--stdin", input="retained source\n")
        payload = "nymish-base-payload:100644:blob:" + blob
        repo = RealGit(root)
        owner = object.__new__(Daemon)
        owner.git, owner.admission_watch = repo, []
        with patch.object(repo, "watch_digest", side_effect=ReadFailed("TEMPORARILY_UNREADABLE")):
            held = {"head": "candidate", "failing": ["check:actual-failure"],
                    **owner._watch(base, [], payloads=[payload])}
        self.assertEqual(held.get("payload_watch"), [payload])
        (root / "arrival.nym").write_text("retained source\n")
        git("add", "-A")
        git("commit", "-qm", "payload arrival")
        current = owner.watch_digest(held, {"headRefOid": "candidate"}, {"sha": git("rev-parse", "HEAD")})
        self.assertTrue(rules.held_retry(held, "candidate", current))
        self.assertEqual(held["failing"], ["check:actual-failure"])


if __name__ == "__main__":
    unittest.main()
