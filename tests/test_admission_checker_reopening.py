"""A command-bound failed admission reopens only when its consumed checker changes."""
import json
import tempfile
import unittest
from pathlib import Path

from fakes import Admission, DaemonGitHub, DaemonRepo, FakeGate
from test_daemon import daemon
from svrf.errors import ReadFailed


class CommandAdmission(Admission):
    def __init__(self, command, verdicts=None):
        super().__init__(verdicts)
        self.command = command


class CheckerCommandReopening(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = DaemonRepo([31])
        self.gh = DaemonGitHub(self.repo)
        self.gate = FakeGate(self.repo)

    def owner(self, admission):
        return daemon(self.repo, self.gh, self.gate, admission, self.root)

    def first_failure(self):
        first = CommandAdmission("CHECKER=old original-admission", {
            "h31": {"verdict": "HELD", "residuals": ["check:original-failure"],
                    "consumed": [{"kind": "CODE", "id": "lane:31"}]}})
        self.owner(first).tick()
        held = json.loads((self.root / "state.json").read_text())["held"]["31"]
        self.assertEqual(held["failing"], ["check:original-failure"])
        self.assertEqual(first.calls, [("h31", self.repo.main)])
        return held

    def test_changed_command_reopens_same_head_and_base_after_restart(self):
        original = self.first_failure()
        successor = CommandAdmission("CHECKER=new original-admission")
        self.owner(successor).tick()
        self.assertEqual(len(successor.calls), 1,
                         "a changed consumed checker cannot inherit the old hold")
        self.assertEqual(self.gh.merged, [31])
        self.assertEqual(original["failing"], ["check:original-failure"])

    def test_unchanged_command_keeps_the_same_failed_read(self):
        original = self.first_failure()
        unchanged = CommandAdmission("CHECKER=old original-admission")
        self.owner(unchanged).tick()
        self.assertEqual(unchanged.calls, [])
        self.assertEqual(self.gh.merged, [])
        self.assertEqual(json.loads((self.root / "state.json").read_text())["held"]["31"], original)

    def test_changed_admission_command_does_not_reopen_an_original_gate_failure(self):
        self.gate = FakeGate(self.repo, bad={31})
        self.owner(CommandAdmission("CHECKER=old original-admission")).tick()
        original = json.loads((self.root / "state.json").read_text())["held"]["31"]
        self.assertEqual(original["reason"], "GATE_RED")
        successor = CommandAdmission("CHECKER=new original-admission")
        before = len(self.gate.trees)
        self.owner(successor).tick()
        self.assertEqual(successor.calls, [])
        self.assertEqual(len(self.gate.trees), before)
        self.assertEqual(self.gh.merged, [])


    def failed_watch_owner(self, command):
        def unreadable_watch(*args):
            raise ReadFailed("ORIGINAL_WATCH_UNAVAILABLE")
        self.repo.watch_digest = unreadable_watch
        admission = CommandAdmission(command, {
            "h31": {"verdict": "HELD", "residuals": ["check:original-failure"],
                    "consumed": [{"kind": "PATH", "id": "lane:31"}]}})
        owner = daemon(self.repo, self.gh, self.gate, admission, self.root,
                       admission_watch=["lane:31"])
        return owner, admission

    def test_unchanged_command_and_failed_watch_stay_quiet_after_restart(self):
        owner, original = self.failed_watch_owner("CHECKER=old original-admission")
        owner.tick()
        held = json.loads((self.root / "state.json").read_text())["held"]["31"]
        self.assertEqual(len(original.calls), 1)
        successor, unchanged = self.failed_watch_owner("CHECKER=old original-admission")
        successor.tick()
        successor.tick()
        self.assertEqual(unchanged.calls, [],
                         "omitting the failed watch is not an observed source change")
        self.assertEqual(json.loads((self.root / "state.json").read_text())["held"]["31"], held)
        self.assertEqual(self.gh.merged, [])

    def test_changed_command_reopens_even_when_the_original_watch_stays_unreadable(self):
        owner, original = self.failed_watch_owner("CHECKER=old original-admission")
        owner.tick()
        successor, changed = self.failed_watch_owner("CHECKER=new original-admission")
        successor.tick()
        successor.tick()
        self.assertEqual(len(original.calls), 1)
        self.assertEqual(len(changed.calls), 1,
                         "one changed checker warrants one ordinary admission, not repeated unchanged retries")
        self.assertEqual(self.gh.merged, [])

if __name__ == "__main__":
    unittest.main()
