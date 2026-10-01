"""The refused operation's response binds its retry time."""
import subprocess
import unittest
from unittest.mock import patch

from svrf.errors import RateLimited
from svrf.github import RealGitHub


class ResponseRateBounds(unittest.TestCase):
    def refused(self, headers):
        result = subprocess.CompletedProcess([], 1,
            "HTTP/2.0 403 Forbidden\r\n" + headers + "\r\n\r\n"
            '{"message":"API rate limit exceeded"}',
            "gh: API rate limit exceeded (HTTP 403)")
        with patch("svrf.github.subprocess.run", return_value=result) as run:
            with self.assertRaises(RateLimited) as raised:
                RealGitHub("o/r").pull(1)
        self.assertIn("--include", run.call_args.args[0])
        return raised.exception

    def test_exhausted_response_carries_its_own_reset(self):
        failure = self.refused("X-RateLimit-Remaining: 0\r\nX-RateLimit-Reset: 12345")
        self.assertEqual(failure.reset, 12345)

    def test_secondary_retry_after_binds_even_with_positive_core_budget(self):
        with patch("svrf.github.time.time", return_value=100):
            failure = self.refused("Retry-After: 42\r\nX-RateLimit-Remaining: 4999\r\n"
                                   "X-RateLimit-Reset: 9999")
        self.assertEqual(failure.reset, 142)

    def test_positive_core_budget_does_not_supply_a_refusal_reset(self):
        self.assertIsNone(self.refused("X-RateLimit-Remaining: 4999\r\n"
                                      "X-RateLimit-Reset: 9999").reset)

    def test_malformed_reset_remains_missing(self):
        self.assertIsNone(self.refused("X-RateLimit-Remaining: 0\r\n"
                                      "X-RateLimit-Reset: unknown").reset)

    def test_success_strips_transport_headers_before_json_reader(self):
        result = subprocess.CompletedProcess([], 0,
            'HTTP/2.0 200 OK\r\nContent-Type: application/json\r\n\r\n'
            '{"head":{"sha":"abc","ref":"b"},"state":"open","draft":false}', "")
        with patch("svrf.github.subprocess.run", return_value=result):
            self.assertEqual(RealGitHub("o/r").pull(1)["head_sha"], "abc")
