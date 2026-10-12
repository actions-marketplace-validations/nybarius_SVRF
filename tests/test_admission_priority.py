"""An admission priority command names the heads a backlog reads first, every round.

When candidates outnumber the admission slots, a round reads newest first and last tick's unread heads last. A
host can also declare that some pull requests are read before the rest (an urgent repair, a gate other work waits
on). The command prints those numbers, one per line, in the order they are wanted; they lead the backlog and the
usual order follows. An unreadable priority read is recorded and the usual order stands; it never drops a candidate.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fakes import Admission, Clock, DaemonGitHub, DaemonRepo, FakeGate, is_union

from svrf.config import ConfigError, from_dict
from svrf.daemon import STATE_SCHEMA, Daemon


def daemon(repo, gh, admission, tmp, **kw):
    clock = Clock()
    return Daemon(repo, gh, FakeGate(repo), admission, state_dir=Path(tmp), receipts=Path(tmp) / "receipts",
                  clock=clock, sleep=clock.sleep, train_options={"poll_seconds": 0}, is_union=is_union, **kw)


class AdmissionPriority(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        Path(self.tmp, "state.json").write_text(json.dumps({
            "schema_version": STATE_SCHEMA, "held": {}, "merged_elsewhere": {}, "relanded": {}, "bases": {},
            "last_tick": {"retry_later": [41]}}))

    def order(self, command):
        repo = DaemonRepo([41, 42, 43, 44])
        admission = Admission()
        number_of = {sha: n for n, sha in repo.heads.items()}
        out = daemon(repo, DaemonGitHub(repo), admission, self.tmp, jobs=1, admission_jobs=1,
                     admission_priority_command=command).tick()
        return [number_of[head] for head, _ in admission.calls], out

    def test_declared_heads_lead_the_backlog_in_their_order(self):
        calls, out = self.order("printf '41\\n42\\n'")
        self.assertEqual(calls, [41, 42, 44, 43])
        self.assertEqual(out["admission_priority"], {"command": "printf '41\\n42\\n'", "read": [41, 42]})

    def test_a_declared_number_that_is_not_a_candidate_is_ignored(self):
        calls, _ = self.order("printf '99\\n43\\n'")
        self.assertEqual(calls, [43, 44, 42, 41])

    def test_an_unreadable_priority_keeps_the_usual_order(self):
        calls, out = self.order("exit 4")
        self.assertEqual(calls, [44, 43, 42, 41])
        self.assertEqual(out["admission_priority"], {"command": "exit 4", "UNOBSERVED": "EXIT:4"})


class AdmissionPriorityConfig(unittest.TestCase):
    MINIMAL = {"repo": "example/project", "gate": {"commands": ["make test"]}}

    def test_the_priority_command_is_read(self):
        config = from_dict({**self.MINIMAL, "admission": {"command": "true", "priority_command": "echo 1"}})
        self.assertEqual(config.admission_priority_command, "echo 1")
        self.assertIsNone(from_dict(self.MINIMAL).admission_priority_command)

    def test_a_priority_command_must_be_text(self):
        with self.assertRaises(ConfigError):
            from_dict({**self.MINIMAL, "admission": {"command": "true", "priority_command": ["x"]}})


if __name__ == "__main__":
    unittest.main()
