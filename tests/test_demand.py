"""An optional demand driver uses the existing owner and exposes exact round inputs."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fakes import DaemonRepo, DaemonGitHub, FakeGate, Admission
from test_daemon import daemon
from svrf.app import build
from svrf.config import from_dict, ConfigError
from svrf.daemon import Daemon
from svrf.locks import owner_lock


class Driver:
    def __init__(self, callback=lambda owner: {'tick': 'QUIESCENT', 'rounds': []}):
        self.callback = callback
        self.calls = 0

    def drain(self, owner):
        self.calls += 1
        return self.callback(owner)


class Demand(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = DaemonRepo([1, 2])
        self.gh = DaemonGitHub(self.repo)
        self.admission = Admission({'h1': {'verdict': 'HELD', 'changed': ['lane:1']},
                                    'h2': {'verdict': 'HELD', 'changed': ['lane:2']}})
        self.driver = Driver()
        self.owner = daemon(self.repo, self.gh, FakeGate(self.repo), self.admission, self.root,
                            demand=self.driver, jobs=2)

    def test_empty_driver_makes_no_discovery_read(self):
        before = dict(self.gh.calls)
        self.assertEqual(self.owner.tick()['tick'], 'QUIESCENT')
        self.assertEqual(self.driver.calls, 1)
        self.assertEqual(dict(self.gh.calls), before)
        self.assertEqual(self.admission.calls, [])

    def test_release_notification_is_optional_and_runs_after_retained_state_is_saved(self):
        self.assertEqual(self.owner.forget([1])['tick'], 'FORGOT')
        observed = []
        self.driver.released = lambda owner, numbers: observed.append((numbers, owner.load_state()['held']))
        self.owner.forget([1, 2])
        self.assertEqual(observed, [([1, 2], {})])

    def test_driver_runs_inside_the_existing_single_owner_lock(self):
        def check(owner):
            with patch('svrf.locks._inherited', return_value=False):
                with owner_lock(owner.lock_path) as acquired:
                    self.assertFalse(acquired)
            return {'tick': 'QUIESCENT'}
        self.driver.callback = check
        self.owner.tick()

    def test_selected_round_keeps_parallel_admission_and_records_its_inputs(self):
        self.driver.callback = lambda owner: owner._tick(requested={1})
        result = self.owner.tick()
        self.assertEqual(result['held'], [1])
        self.assertEqual([head for head, _ in self.admission.calls], ['h1'])
        reads = self.owner.round_inputs[1]
        self.assertEqual(reads['row']['headRefOid'], 'h1')
        self.assertFalse(reads['contained'])
        self.assertEqual(self.owner.jobs, 2)
        self.assertEqual(set(self.owner.round_rows), {1, 2})

    def test_changed_parent_releases_only_ancestry_memory(self):
        self.gh.bases[1] = 'parent'
        self.gh.parents = {'parent': [{'number': 9, 'state': 'closed', 'merged_at': None}]}
        self.driver.callback = lambda owner: owner._tick(requested={1})
        self.owner.tick()
        self.assertEqual(self.owner.load_state()['held']['1']['reason'], 'PARENT_CLOSED_UNMERGED')
        self.gh.parents['parent'][0]['merged_at'] = 'merged'
        self.driver.callback = lambda owner: owner._tick(requested={1}, changed={1: {'parent'}})
        result = self.owner.tick()
        self.assertEqual(result['retargeted'], [1])
        self.assertEqual(self.owner.round_inputs[1]['parents']['parent'], self.gh.parents['parent'])

    def test_ordinary_gate_hold_survives_control_movement(self):
        self.driver.callback = lambda owner: owner._tick(requested={1})
        self.owner.tick()
        self.driver.callback = lambda owner: owner._tick(requested={1}, changed={1: {'base'}})
        self.owner.tick()
        self.assertEqual(len(self.admission.calls), 1)


class Configuration(unittest.TestCase):
    def test_driver_is_explicit_configuration_and_import_failures_are_not_polling_fallback(self):
        config = from_dict({'repo': 'example/project', 'gate': {'commands': ['true']},
                            'demand_driver': 'test_demand:factory'})
        self.assertEqual(config.demand_driver, 'test_demand:factory')
        self.assertIsInstance(build(config).demand, Driver)
        config.demand_driver = 'missing_module_for_test:factory'
        with self.assertRaises(ConfigError):
            build(config)


def factory(config):
    return Driver()


if __name__ == '__main__':
    unittest.main()
