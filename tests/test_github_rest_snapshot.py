"""A REST-only host must not depend on the unused GraphQL budget."""
import json
import subprocess
from unittest.mock import patch

import unittest

from svrf.errors import ReadFailed
from svrf.github import RealGitHub


def pull(number, *, draft=False, fork=False):
    return {"number": number, "title": f"PR {number}", "body": "original body",
            "draft": draft, "state": "open", "labels": [{"name": "train:hold"}],
            "head": {"ref": f"branch-{number}", "sha": f"{number:040x}",
                     "repo": {"full_name": "other/fork" if fork else "o/r"}},
            "base": {"ref": "main", "repo": {"full_name": "o/r"}}}


def reader(pages):
    calls = []
    def run(argv, **kwargs):
        assert argv[:2] == ["gh", "api"], "GraphQL-capable gh pr command used"
        calls.append(argv)
        assert argv[2].startswith("repos/o/r/pulls?state=open&per_page=100&page=")
        page = int(argv[2].rsplit("=", 1)[1])
        value = pages[page - 1]
        if isinstance(value, Exception):
            return subprocess.CompletedProcess(argv, 1, "", "network unreachable")
        return subprocess.CompletedProcess(argv, 0, json.dumps(value), "")
    return run, calls


class RestSnapshots(unittest.TestCase):
    def test_snapshot_preserves_all_rows_across_pages_and_all_consumed_fields(self):
        first = [pull(i) for i in range(1, 101)]
        last = pull(101, draft=True, fork=True)
        run, calls = reader([first, [last]])
        gh = RealGitHub("o/r")
        with patch("svrf.github.subprocess.run", run):
            rows = gh.snapshot()
        assert len(rows) == 101 and len(calls) == 2
        assert rows[-1] == {"number": 101, "title": "PR 101", "body": "original body",
                            "isDraft": True, "headRefName": "branch-101",
                            "headRefOid": f"{101:040x}", "baseRefName": "main",
                            "labels": [{"name": "train:hold"}], "isCrossRepository": True}
        assert gh.calls == {"graphql": 0, "rest": 2}


    def test_later_page_failure_never_returns_the_successful_prefix(self):
        run, _ = reader([[pull(i) for i in range(1, 101)], OSError()])
        with patch("svrf.github.subprocess.run", run), self.assertRaises(ReadFailed):
            RealGitHub("o/r").snapshot()


    def test_malformed_snapshot_is_unobserved(self):
        for bad in [{}, {"message": "rate limit"}, [dict(pull(1), draft="false")],                                  [dict(pull(1), head={})]]:
            with self.subTest(bad=bad):
                run, _ = reader([bad])
                with patch("svrf.github.subprocess.run", run), self.assertRaises(ReadFailed):
                    RealGitHub("o/r").snapshot()


    def test_duplicate_across_pages_refuses_moving_census(self):
        run, _ = reader([[pull(i) for i in range(1, 101)], [pull(100)]])
        with patch("svrf.github.subprocess.run", run), self.assertRaises(ReadFailed):
            RealGitHub("o/r").snapshot()


    def test_acquisition_limit_refuses_instead_of_silently_truncating(self):
        run, _ = reader([[pull(i) for i in range(1, 101)], [pull(101)]])
        with patch("svrf.github.subprocess.run", run), self.assertRaises(ReadFailed):
            RealGitHub("o/r", limit=100).snapshot()


    def test_only_the_consumed_rest_budget_is_required(self):
        payload = {"resources": {"core": {"remaining": 4500, "reset": 99},
                                 "graphql": {"remaining": 0, "reset": 999999}}}
        with patch("svrf.github.subprocess.run", return_value=
                   subprocess.CompletedProcess([], 0, json.dumps(payload), "")):
            assert RealGitHub("o/r").rate_limit() == {"core": {"remaining": 4500, "reset": 99}}


    def test_missing_rate_read_cannot_become_empty_success(self):
        for payload in [{}, {"resources": {}}, {"resources": {"core": {"remaining": True}}}]:
            with self.subTest(payload=payload):
                with patch("svrf.github.subprocess.run", return_value=
                           subprocess.CompletedProcess([], 0, json.dumps(payload), "")), self.assertRaises(ReadFailed):
                    RealGitHub("o/r").rate_limit()


    def test_draft_transition_reports_missing_rest_capability_without_any_call(self):
        with patch("svrf.github.subprocess.run") as run, self.assertRaises(ReadFailed):
            RealGitHub("o/r").ready(1)
        run.assert_not_called()


    def test_github_calls_have_a_bounded_wait_and_timeout_is_a_read_failure(self):
        def timeout(argv, **kwargs):
            assert kwargs.get("timeout") == 60
            raise subprocess.TimeoutExpired(argv, 60)
        with patch("svrf.github.subprocess.run", timeout), self.assertRaisesRegex(ReadFailed, "TIMEOUT"):
            RealGitHub("o/r").snapshot()


if __name__ == "__main__":
    unittest.main()
