"""An admission capacity command caps how many admission reads run at once, every round.

A host whose admission check leases one shared, IO-admitted resource can run only that many reads at once; the rest
wait out their lease and come back unobserved. The configured admission slots are an upper bound; the command's
integer, read at the start of each round, is the round's actual concurrency. A failed or unreadable capacity read is
retained as unobserved: no admission runs that round and every candidate is retried, never read at an invented width.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fakes import Admission, Clock, DaemonGitHub, DaemonRepo, FakeGate, is_union

from svrf.config import ConfigError, from_dict
from svrf.daemon import Daemon


def daemon(repo, gh, admission, tmp, **kw):
    clock = Clock()
    return Daemon(repo, gh, FakeGate(repo), admission, state_dir=Path(tmp), receipts=Path(tmp) / "receipts",
                  clock=clock, sleep=clock.sleep, train_options={"poll_seconds": 0}, is_union=is_union, **kw)


class AdmissionCapacity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_the_capacity_read_caps_the_configured_slots(self):
        repo = DaemonRepo([61, 62, 63, 64])
        admission = Admission(delay=0.1)
        out = daemon(repo, DaemonGitHub(repo), admission, self.tmp, jobs=1, admission_jobs=4,
                     admission_capacity_command="echo 1").tick()
        self.assertEqual(sorted(out["admitted"]), [61, 62, 63, 64])
        self.assertEqual(admission.peak, 1)
        self.assertEqual(out["admission_capacity"], {"command": "echo 1", "value": 1, "slots": 1})

    def test_a_capacity_above_the_slots_never_widens_them(self):
        repo = DaemonRepo([65, 66, 67])
        admission = Admission(delay=0.1)
        out = daemon(repo, DaemonGitHub(repo), admission, self.tmp, jobs=1, admission_jobs=2,
                     admission_capacity_command="echo 9").tick()
        self.assertLessEqual(admission.peak, 2)
        self.assertEqual(out["admission_capacity"]["slots"], 2)

    def test_zero_capacity_reads_nothing_and_retries_every_candidate(self):
        repo = DaemonRepo([71, 72])
        admission = Admission()
        out = daemon(repo, DaemonGitHub(repo), admission, self.tmp, jobs=1, admission_jobs=4,
                     admission_capacity_command="echo 0").tick()
        self.assertEqual(admission.calls, [])
        self.assertEqual(sorted(out["retry_later"]), [71, 72])
        self.assertEqual(out["reasons"]["71"], "ADMISSION_CAPACITY:0")

    def test_an_unreadable_capacity_is_unobserved_and_reads_nothing(self):
        for command, word in (("exit 3", "EXIT:3"), ("echo two", "NOT_A_COUNT")):
            with self.subTest(command=command):
                tmp = tempfile.mkdtemp()
                repo = DaemonRepo([81])
                admission = Admission()
                out = daemon(repo, DaemonGitHub(repo), admission, tmp, jobs=1, admission_jobs=4,
                             admission_capacity_command=command).tick()
                self.assertEqual(admission.calls, [])
                self.assertEqual(out["retry_later"], [81])
                self.assertEqual(out["reasons"]["81"], f"ADMISSION_CAPACITY_UNOBSERVED:{word}")
                self.assertEqual(out["admission_capacity"], {"command": command, "UNOBSERVED": word})


class AdmissionCapacityConfig(unittest.TestCase):
    MINIMAL = {"repo": "example/project", "gate": {"commands": ["make test"]}}

    def test_the_admission_capacity_command_is_read(self):
        config = from_dict({**self.MINIMAL, "admission": {"command": "true", "capacity_command": "echo 1"}})
        self.assertEqual(config.admission_capacity_command, "echo 1")
        self.assertIsNone(from_dict(self.MINIMAL).admission_capacity_command)

    def test_a_capacity_command_must_be_text(self):
        with self.assertRaises(ConfigError):
            from_dict({**self.MINIMAL, "admission": {"command": "true", "capacity_command": 1}})


if __name__ == "__main__":
    unittest.main()
