"""Declared work leads when current admission capacity leaves only one free slot."""

import tempfile
import threading
import unittest
from pathlib import Path

from fakes import DaemonGitHub, DaemonRepo, FakeGate
from test_daemon import daemon


class EffectivePriority(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.release = threading.Event()
        self.calls = []
        self.changed = threading.Condition()
        self.owner = None
        self.addCleanup(self.drain)

    def drain(self):
        self.release.set()
        if self.owner is not None:
            while self.owner.inflight:
                self.owner.tick(drain=True)
            self.owner.close()

    def start(self, numbers, **options):
        self.repo = DaemonRepo(numbers)
        self.gh = DaemonGitHub(self.repo)

        def admission(head, base):
            with self.changed:
                self.calls.append(head)
                self.changed.notify_all()
            if not self.release.wait(10):
                raise RuntimeError('test admission was not released')
            return {'verdict': 'MERGEABLE', 'residuals': []}

        self.owner = daemon(
            self.repo, self.gh, FakeGate(self.repo), admission, Path(self.temp.name),
            jobs=2, admission_jobs=4, admission_round_reads=8, tick_seconds=0.05,
            admission_priority_command='echo 42', **options,
        )

    def await_calls(self, count):
        with self.changed:
            self.assertTrue(self.changed.wait_for(lambda: len(self.calls) >= count, 2))

    def open_head(self, number):
        sha = f'h{number}'
        self.repo.commits[sha] = {'lanes': frozenset({0, number}), 'parents': ('c0',)}
        self.repo.heads[number] = sha
        self.repo.branches[f'pr-{number}'] = sha

    def test_declared_head_gets_the_slot_left_by_running_admissions(self):
        self.start([1, 2, 3])
        first = self.owner.tick()
        self.await_calls(3)
        self.assertEqual(first['inflight'], [1, 2, 3])
        original = {number: item['future'] for number, item in self.owner.inflight.items()}
        self.open_head(41)
        self.open_head(42)

        second = self.owner.tick()
        self.await_calls(4)

        self.assertEqual(self.calls[3], 'h42')
        self.assertEqual(second['inflight'], [1, 2, 3, 42])
        self.assertEqual(second['deferred'], [41])
        self.assertEqual(second['admission_priority']['read'], [42])
        for number, future in original.items():
            self.assertIs(self.owner.inflight[number]['future'], future)
        self.assertEqual(len(self.calls), 4)

    def test_declared_head_leads_at_a_lower_observed_capacity(self):
        self.start([41, 42], admission_capacity_command='echo 1')

        out = self.owner.tick()
        self.await_calls(1)

        self.assertEqual(self.calls, ['h42'])
        self.assertEqual(out['inflight'], [42])
        self.assertEqual(out['deferred'], [41])
        self.assertEqual(out['admission_capacity']['slots'], 1)
        self.assertEqual(out['admission_priority']['read'], [42])

    def test_a_queue_that_fits_starts_without_a_priority_read(self):
        self.start([41, 42])

        out = self.owner.tick()
        self.await_calls(2)

        self.assertCountEqual(self.calls, ['h41', 'h42'])
        self.assertEqual(out['inflight'], [41, 42])
        self.assertNotIn('admission_priority', out)
        self.assertNotIn('deferred', out)


if __name__ == '__main__':
    unittest.main()
