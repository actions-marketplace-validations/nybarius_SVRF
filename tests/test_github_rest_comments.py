"""Complete issue-comment reads must precede surface deduplication."""

import json
import subprocess
import unittest
from unittest.mock import patch

from svrf import pr_surface
from svrf.github import RealGitHub
from svrf.train import post_surface


def comment_reader(pages):
    calls = []

    def run(argv, **kwargs):
        assert argv[:2] == ["gh", "api"]
        endpoint = "repos/o/r/issues/1/comments?per_page=100"
        assert argv[2] == endpoint or argv[2].startswith(endpoint + "&page=")
        calls.append(argv)
        page = int(argv[2].rsplit("&page=", 1)[1]) if "&page=" in argv[2] else 1
        value = pages[page - 1]
        if isinstance(value, Exception):
            raise value
        return subprocess.CompletedProcess(argv, 0, json.dumps(value), "")

    return run, calls


class RestComments(unittest.TestCase):
    event = {"number": 1, "phase": "QUEUED", "position": 1, "batch": "F1", "members": [1]}

    def _post(self, pages, event=None):
        gh, counts = RealGitHub("o/r"), {}
        run, calls = comment_reader(pages)
        with patch("svrf.github.subprocess.run", run), \
                patch.object(gh, "comment") as create, patch.object(gh, "update_comment") as edit:
            posted = post_surface(gh, lambda fn, *args: fn(*args), "a" * 40,
                                  self.event if event is None else event,
                                  status_checks=False, pr_comments=True, counts=counts)
        return posted, create, edit, counts, calls

    def test_all_comments_are_returned_even_after_a_surface_marker(self):
        first = [{"id": i, "body": "ordinary comment"} for i in range(1, 101)]
        second = [{"id": i, "body": "later comment"} for i in range(101, 201)]
        second[0]["body"] = pr_surface.render_comment(self.event)
        run, calls = comment_reader([first, second, [{"id": 201, "body": None}]])
        gh = RealGitHub("o/r", limit=1)
        with patch("svrf.github.subprocess.run", run):
            rows = gh.list_comments(1)
        self.assertEqual(rows, first + second + [{"id": 201, "body": ""}])
        self.assertEqual(len(calls), 3)
        self.assertEqual(gh.calls, {"graphql": 0, "rest": 3})

    def test_full_last_page_requires_an_empty_following_page(self):
        first = [{"id": i, "body": ""} for i in range(1, 101)]
        run, calls = comment_reader([first, []])
        with patch("svrf.github.subprocess.run", run):
            self.assertEqual(RealGitHub("o/r").list_comments(1), first)
        self.assertEqual(len(calls), 2)

    def test_surface_after_first_hundred_is_updated_without_a_new_comment(self):
        first = [{"id": i, "body": "ordinary comment"} for i in range(1, 101)]
        existing = {"id": 101, "body": pr_surface.render_comment(self.event)}
        changed = dict(self.event, phase="GATING")
        posted, create, edit, counts, calls = self._post([first, [existing]], changed)
        self.assertTrue(posted)
        create.assert_not_called()
        edit.assert_called_once_with(101, pr_surface.render_comment(changed))
        self.assertEqual(counts, {"comment_write": 1})
        self.assertEqual(len(calls), 2)

    def test_same_hash_after_first_hundred_skips_both_writes(self):
        first = [{"id": i, "body": "ordinary comment"} for i in range(1, 101)]
        existing = {"id": 101, "body": pr_surface.render_comment(self.event)}
        posted, create, edit, counts, calls = self._post([first, [existing]])
        self.assertTrue(posted)
        create.assert_not_called()
        edit.assert_not_called()
        self.assertEqual(counts, {"comment_skip": 1})
        self.assertEqual(len(calls), 2)

    def test_absent_surface_is_created_only_after_the_complete_read(self):
        first = [{"id": i, "body": "ordinary comment"} for i in range(1, 101)]
        posted, create, edit, counts, calls = self._post([first, [{"id": 101, "body": None}]])
        self.assertTrue(posted)
        create.assert_called_once_with(1, pr_surface.render_comment(self.event))
        edit.assert_not_called()
        self.assertEqual(counts, {"comment_write": 1})
        self.assertEqual(len(calls), 2)

    def test_incomplete_later_page_never_authorizes_a_write(self):
        ordinary = [{"id": i, "body": "ordinary comment"} for i in range(1, 101)]
        invalid_pages = [OSError("network unreachable"), {},
                         [{"id": 100, "body": "repeated source row"}],
                         [{"id": 101, "body": 42}]]
        for has_surface in (False, True):
            first = [dict(row) for row in ordinary]
            if has_surface:
                first[0]["body"] = pr_surface.render_comment(dict(self.event, phase="GATING"))
            for bad in invalid_pages:
                with self.subTest(has_surface=has_surface, bad=repr(bad)):
                    posted, create, edit, counts, calls = self._post([first, bad])
                    self.assertFalse(posted)
                    create.assert_not_called()
                    edit.assert_not_called()
                    self.assertEqual(counts, {})
                    self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
