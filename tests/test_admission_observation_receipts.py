"""Persist the scheduling observations made by the tick that owns the work."""
import json
from pathlib import Path
import tempfile
import unittest

from fakes import Admission, DaemonGitHub, DaemonRepo, FakeGate
from test_daemon import daemon
from svrf.errors import ReadFailed


class AdmissionObservationReceipts(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def run_tick(self, numbers, admission=None, **options):
        repo = DaemonRepo(numbers)
        gh = DaemonGitHub(repo)
        reader = admission or Admission()
        owner = daemon(repo, gh, FakeGate(repo), reader, self.root,
                       jobs=1, **options)
        result = owner.tick()
        owner.close()
        state = json.loads((self.root / 'state.json').read_text())
        paths = list((self.root / 'receipts').glob('tick-*.json'))
        self.assertEqual(len(paths), 1)
        receipt = json.loads(paths[0].read_text())
        self.assertEqual(receipt['schema_version'], 'svrf.tick/1')
        return result, state['last_tick'], receipt, reader, gh

    def assert_retained(self, result, state, receipt, key):
        self.assertIn(key, result)
        for name, saved in (('last_tick', state), ('tick_receipt', receipt)):
            with self.subTest(surface=name, field=key):
                self.assertIn(key, saved)
                self.assertEqual(saved[key], result[key])

    def test_capacity_and_priority_reads_are_retained_from_the_actual_tick(self):
        result, state, receipt, reader, gh = self.run_tick(
            [1, 2, 3], admission_jobs=4, admission_round_reads=2,
            admission_capacity_command='echo 1',
            admission_priority_command="printf '2\\n'")
        self.assertEqual(reader.calls[0][0], 'h2')
        self.assertEqual(sorted(gh.merged), [1, 2, 3])
        self.assertEqual(result['admission_capacity'],
                         {'command': 'echo 1', 'value': 1, 'slots': 1})
        self.assertEqual(result['admission_priority']['read'], [2])
        for key in ('admission_capacity', 'admission_priority'):
            self.assert_retained(result, state, receipt, key)

    def test_zero_capacity_is_retained_without_inventing_a_priority_read(self):
        result, state, receipt, reader, _ = self.run_tick(
            [1, 2], admission_jobs=4, admission_capacity_command='echo 0',
            admission_priority_command="printf '2\\n'")
        self.assertEqual(reader.calls, [])
        self.assertEqual(result['retry_later'], [1, 2])
        self.assertEqual(result['admission_capacity']['value'], 0)
        self.assert_retained(result, state, receipt, 'admission_capacity')
        for saved in (result, state, receipt):
            self.assertNotIn('admission_priority', saved)

    def test_unread_capacity_retains_its_failure_instead_of_a_zero(self):
        result, state, receipt, reader, _ = self.run_tick(
            [1], admission_jobs=2, admission_capacity_command='exit 7')
        self.assertEqual(reader.calls, [])
        self.assertEqual(result['admission_capacity'],
                         {'command': 'exit 7', 'UNOBSERVED': 'EXIT:7'})
        self.assert_retained(result, state, receipt, 'admission_capacity')

    def test_unread_priority_retains_the_actual_failure(self):
        result, state, receipt, _, gh = self.run_tick(
            [1, 2], admission_jobs=1, admission_round_reads=1,
            admission_priority_command='exit 4')
        self.assertEqual(sorted(gh.merged), [1, 2])
        self.assertEqual(result['admission_priority'],
                         {'command': 'exit 4', 'UNOBSERVED': 'EXIT:4'})
        self.assert_retained(result, state, receipt, 'admission_priority')

    def test_the_declared_read_that_stops_refill_is_retained(self):
        calls = []

        def admission(head, base):
            calls.append(head)
            raise ReadFailed('SOURCE_UNAVAILABLE')

        result, state, receipt, _, gh = self.run_tick(
            [1, 2, 3], admission=admission, admission_jobs=1,
            admission_round_reads=1, admission_priority_command="printf '1\\n'")
        self.assertEqual(calls, ['h1'])
        self.assertEqual(gh.merged, [])
        self.assertEqual(result['retry_later'], [1])
        self.assertEqual(result['refill_stopped'], {'declared_unread': [1]})
        self.assert_retained(result, state, receipt, 'refill_stopped')
        self.assert_retained(result, state, receipt, 'admission_priority')

    def test_unconfigured_observations_remain_absent(self):
        result, state, receipt, _, gh = self.run_tick([1])
        self.assertEqual(gh.merged, [1])
        for saved in (result, state, receipt):
            for key in ('admission_capacity', 'admission_priority', 'refill_stopped'):
                self.assertNotIn(key, saved)


if __name__ == '__main__':
    unittest.main()
