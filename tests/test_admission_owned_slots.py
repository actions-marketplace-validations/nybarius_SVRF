"""Queued futures own an admission too: leave excess heads for current discovery."""
import tempfile
import threading
import time
import unittest
from pathlib import Path

from fakes import DaemonGitHub, DaemonRepo, FakeGate
from test_daemon import daemon


class OwnedSlots(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = DaemonRepo([1, 2, 3, 4, 5, 6])
        self.gh = DaemonGitHub(self.repo)
        self.release = threading.Event()
        self.second_release = self.release
        self.calls = []
        self.lock = threading.Lock()

        def admission(head, base):
            with self.lock:
                self.calls.append(head)
            if head in ('h1', 'h2'):
                release = self.second_release if head == 'h2' else self.release
                if not release.wait(10):
                    raise RuntimeError('test admission was not released')
            return {'verdict': 'MERGEABLE', 'residuals': []}

        self.owner = daemon(self.repo, self.gh, FakeGate(self.repo), admission, Path(self.temp.name),
                            jobs=2, admission_jobs=2, admission_round_reads=4, tick_seconds=0.1,
                            admission_priority_command='echo 99')
        self.addCleanup(self.drain)

    def drain(self):
        self.release.set()
        while self.owner.inflight:
            self.owner.tick(drain=True)
        self.owner.close()

    def open_head(self, number, sha):
        self.repo.commits[sha] = {'lanes': frozenset({0, number}), 'parents': ('c0',)}
        self.repo.heads[number] = sha
        self.repo.branches[f'pr-{number}'] = sha

    def receive_until(self, number):
        deadline = time.monotonic() + 10
        while number not in self.gh.merged and time.monotonic() < deadline:
            self.owner.tick()
        self.assertIn(number, self.gh.merged)

    def test_discovery_ticks_do_not_reserve_a_hidden_queue_behind_owned_slots(self):
        first = self.owner.tick()
        self.assertEqual(first['inflight'], [1, 2])
        self.assertEqual(set(first['deferred']), {3, 4, 5, 6})
        second = self.owner.tick()
        self.assertEqual(second['inflight'], [1, 2])
        self.assertEqual(set(second['deferred']), {3, 4, 5, 6})
        self.assertEqual(set(self.calls), {'h1', 'h2'})

    def test_new_priority_is_read_before_an_unstarted_older_head(self):
        # Free exactly one slot: with two free workers, callback start order is
        # chosen by the thread scheduler, even when the priority is submitted first.
        self.second_release = threading.Event()
        self.addCleanup(self.second_release.set)
        self.owner.tick()
        self.open_head(99, 'h99')
        self.release.set()
        self.receive_until(99)
        self.assertIn('h99', self.calls)
        self.assertEqual(self.calls[2], 'h99')
        self.assertIn(99, self.gh.merged)

    def test_a_changed_deferred_head_is_read_only_at_its_current_source(self):
        self.owner.tick()
        self.open_head(3, 'h3-changed')
        self.release.set()
        self.receive_until(3)
        self.assertNotIn('h3', self.calls)
        self.assertIn('h3-changed', self.calls)


    def test_capacity_growth_serves_current_work_while_an_original_read_runs(self):
        self.owner.admission_capacity_command = 'echo 1'
        first = self.owner.tick()

        self.assertEqual(first['inflight'], [1])
        self.assertEqual(self.calls, ['h1'])
        original = self.owner.inflight[1]['future']

        # A deferred head is read at its current source after capacity returns.
        # The original admission stays live throughout that later landing.
        self.open_head(2, 'h2-current')
        self.owner.admission_capacity_command = 'echo 2'

        deadline = time.monotonic() + 3
        while 2 not in self.gh.merged and time.monotonic() < deadline:
            self.owner.tick()

        self.assertIn(
            2, self.gh.merged,
            'increased capacity did not let ready work pass the running read',
        )
        self.assertNotIn(1, self.gh.merged)
        self.assertIn(1, self.owner.inflight)
        self.assertIs(self.owner.inflight[1]['future'], original)
        self.assertEqual(self.calls.count('h1'), 1)
        self.assertNotIn('h2', self.calls)
        self.assertEqual(self.calls.count('h2-current'), 1)



if __name__ == '__main__':
    unittest.main()
