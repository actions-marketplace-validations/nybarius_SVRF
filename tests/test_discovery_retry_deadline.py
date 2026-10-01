"""A refused discovery retains its observed wake time without admitting a head."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fakes import Admission, DaemonGitHub, DaemonRepo, FakeGate
from test_daemon import daemon
from svrf.errors import RateLimited, ReadFailed


class DiscoveryRetryDeadline(unittest.TestCase):
    def test_failed_snapshot_keeps_the_header_deadline_in_result_and_saved_state(self):
        repo = DaemonRepo([17])
        gh = DaemonGitHub(repo)
        admission = Admission()
        with tempfile.TemporaryDirectory() as tmp:
            owner = daemon(repo, gh, FakeGate(repo), admission, tmp)
            failure = RateLimited('request budget exhausted', reset=1800000000)
            with patch.object(gh, 'snapshot', side_effect=failure):
                result = owner.tick()
            self.assertEqual(result['tick'], 'RETRY')
            self.assertEqual(result['retry_at'], 1800000000)
            state = json.loads((Path(tmp) / 'state.json').read_text())
            self.assertEqual(state['last_tick']['retry_at'], result['retry_at'])
            self.assertEqual(state['last_tick']['reason'], failure.reason)
            self.assertEqual(state['held'], {})
            self.assertEqual(admission.calls, [])

    def test_budget_floor_retains_the_observed_reset_without_listing(self):
        repo = DaemonRepo([17])
        gh = DaemonGitHub(repo)
        with tempfile.TemporaryDirectory() as tmp:
            owner = daemon(repo, gh, FakeGate(repo), Admission(), tmp)
            with patch.object(gh, 'rate_limit', return_value={'core': {'remaining': 0, 'reset': 1800000010}}), \
                    patch.object(gh, 'snapshot') as listing:
                result = owner.tick()
            self.assertEqual(result['tick'], 'RATE_FLOOR')
            self.assertEqual(result['retry_at'], 1800000010)
            listing.assert_not_called()
            state = json.loads((Path(tmp) / 'state.json').read_text())
            self.assertEqual(state['last_tick']['retry_at'], 1800000010)

    def test_missing_or_malformed_deadlines_are_not_invented_or_carried_forward(self):
        repo = DaemonRepo([17])
        gh = DaemonGitHub(repo)
        with tempfile.TemporaryDirectory() as tmp:
            owner = daemon(repo, gh, FakeGate(repo), Admission(), tmp)
            for reset in (None, 'tomorrow', float('nan'), float('inf'), -1, True):
                with self.subTest(reset=reset), patch.object(gh, 'snapshot', side_effect=RateLimited('limited', reset=reset)):
                    result = owner.tick()
                self.assertEqual(result['tick'], 'RETRY')
                self.assertNotIn('retry_at', result)
            with patch.object(gh, 'snapshot', side_effect=RateLimited('limited', reset=1800000000)):
                owner.tick()
            with patch.object(gh, 'snapshot', side_effect=ReadFailed('network unavailable')):
                result = owner.tick()
            self.assertNotIn('retry_at', result)
            state = json.loads((Path(tmp) / 'state.json').read_text())
            self.assertNotIn('retry_at', state['last_tick'])
