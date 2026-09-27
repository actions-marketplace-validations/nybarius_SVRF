"""Unavailable input reads and admission bases stay distinct from observed values."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fakes import DaemonRepo, DaemonGitHub, FakeGate, Admission
from test_daemon import daemon
from svrf.errors import ReadFailed


class Inputs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = DaemonRepo([1])
        self.gh = DaemonGitHub(self.repo)
        self.admission = Admission()
        self.owner = daemon(self.repo, self.gh, FakeGate(self.repo), self.admission, Path(self.tmp.name),
                            admission_watch=['lane:2'])

    def test_failed_ancestry_read_is_not_recorded_as_observed_false(self):
        with patch.object(self.repo, 'is_ancestor', side_effect=ReadFailed('missing object')):
            self.owner._tick(requested={1})
        self.assertEqual(self.owner.round_inputs[1]['contained'], {'unobserved': 'missing object'})

    def test_admission_failure_retains_exact_base_and_environment_watch_before_the_call(self):
        original = self.repo.main
        def fail(head, base):
            self.repo.main = self.repo._new({0, 2}, (original,))
            raise ReadFailed('dependency missing')
        self.owner.admission = fail
        self.owner._tick(requested={1})
        inputs = self.owner.round_inputs[1]
        self.assertEqual(inputs['admission_base'], original)
        self.assertEqual(inputs['admission_watch'], {'paths': ['lane:2'], 'value': self.repo.watch_digest(original, ['lane:2'])})
        self.assertNotEqual(inputs['admission_watch']['value'], self.repo.watch_digest(self.repo.main, ['lane:2']))

    def test_explicit_forget_notifies_the_bound_driver_under_the_same_lock(self):
        class Driver:
            def released(inner, owner, numbers):
                inner.seen = (owner, numbers)
        driver = Driver()
        self.owner.demand = driver
        self.owner.forget([1])
        self.assertEqual(driver.seen, (self.owner, [1]))


if __name__ == '__main__':
    unittest.main()
