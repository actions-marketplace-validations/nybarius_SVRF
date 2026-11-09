"""An admission hold is shown on the pull request through the train's own surface (the
commit status on the held head and the one living comment), so a worker that reads only
the pull request learns which head was held and every failing line. Each distinct hold is
posted once: re-reading the same head to the same residual posts nothing, a changed
residual edits the same comment, and a failed post is retried at the next hold without
touching the held memory."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fakes import Admission, Clock, DaemonGitHub, DaemonRepo, FakeGate, is_union

from svrf import pr_surface
from svrf.daemon import Daemon
from svrf.errors import ReadFailed


def daemon(repo, gh, tmp, admission=None, train=None, **kw):
    clock = Clock()
    return Daemon(repo, gh, FakeGate(repo), admission or Admission(), state_dir=Path(tmp),
                  receipts=Path(tmp) / "receipts", clock=clock, sleep=clock.sleep,
                  train_options={"poll_seconds": 0, **(train or {})}, is_union=is_union, **kw)


def surface_comments(gh, number):
    return [row for row in gh.comment_rows if row["number"] == number and pr_surface.is_surface_comment(row["body"])]


def hold(d, state, number, head, failing):
    summary = {"at": "20261029T220000Z", "held_rows": [], "held": []}
    d.hold({"number": number, "headRefOid": head, "headRefName": f"b{number}", "title": "t"},
           "ADMISSION_HELD", state, summary, failing=failing)
    return state["held"][str(number)]


class AdmissionHoldSurface(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_an_admission_hold_shows_its_head_and_every_failing_line_on_the_pull_request(self):
        repo = DaemonRepo([31, 32])
        gh = DaemonGitHub(repo)
        admission = Admission({"h31": {"verdict": "HELD",
                                       "residuals": ["check:lint failed", "suite:tests/test_x.py FAILED"]}})
        out = daemon(repo, gh, self.tmp, admission).tick()
        self.assertEqual(out["held"], [31])
        self.assertEqual([(s["state"], s["context"]) for s in gh.statuses["h31"]], [("failure", "svrf")])
        self.assertIn("check:lint failed", gh.statuses["h31"][0]["description"])
        [comment] = surface_comments(gh, 31)
        self.assertIn("h31", comment["body"])
        self.assertIn("check:lint failed", comment["body"])
        self.assertIn("suite:tests/test_x.py FAILED", comment["body"])

    def test_the_same_hold_read_again_posts_nothing(self):
        gh = DaemonGitHub(DaemonRepo([41]))
        d = daemon(gh.repo, gh, self.tmp)
        state = {"held": {}}
        first = hold(d, state, 41, "h41", ["check:x"])
        rest = gh.calls["rest"]
        second = hold(d, state, 41, "h41", ["check:x"])
        self.assertEqual(gh.calls["rest"], rest)
        self.assertEqual(second["surfaced"], first["surfaced"])
        self.assertEqual(len(surface_comments(gh, 41)), 1)

    def test_a_changed_residual_edits_the_one_comment(self):
        gh = DaemonGitHub(DaemonRepo([42]))
        d = daemon(gh.repo, gh, self.tmp)
        state = {"held": {}}
        hold(d, state, 42, "h42", ["check:x"])
        hold(d, state, 42, "h42", ["check:y"])
        [comment] = surface_comments(gh, 42)
        self.assertIn("check:y", comment["body"])
        self.assertNotIn("check:x", comment["body"])
        self.assertEqual([s["description"] for s in gh.statuses["h42"]], ["held: check:x", "held: check:y"])

    def test_a_new_head_held_to_the_same_residual_is_shown_at_that_head(self):
        gh = DaemonGitHub(DaemonRepo([43]))
        d = daemon(gh.repo, gh, self.tmp)
        state = {"held": {}}
        hold(d, state, 43, "h43", ["check:x"])
        hold(d, state, 43, "h43b", ["check:x"])
        self.assertEqual(len(gh.statuses["h43b"]), 1)
        [comment] = surface_comments(gh, 43)
        self.assertIn("h43b", comment["body"])

    def test_a_failed_post_is_retried_at_the_next_hold_and_never_changes_the_held_memory(self):
        gh = DaemonGitHub(DaemonRepo([44]))
        d = daemon(gh.repo, gh, self.tmp)
        state = {"held": {}}
        original = gh.set_status

        def refused(*args, **kw):
            raise ReadFailed("SECONDARY_RATE_LIMIT")

        gh.set_status = refused
        entry = hold(d, state, 44, "h44", ["check:x"])
        self.assertNotIn("surfaced", entry)
        self.assertEqual(entry["failing"], ["check:x"])
        gh.set_status = original
        again = hold(d, state, 44, "h44", ["check:x"])
        self.assertIn("surfaced", again)
        self.assertEqual([s["description"] for s in gh.statuses["h44"]], ["held: check:x"])

    def test_without_pr_comments_only_the_status_is_posted(self):
        gh = DaemonGitHub(DaemonRepo([45]))
        d = daemon(gh.repo, gh, self.tmp, train={"pr_comments": False, "status_checks": True})
        hold(d, {"held": {}}, 45, "h45", ["check:x"])
        self.assertEqual(len(gh.statuses["h45"]), 1)
        self.assertEqual(surface_comments(gh, 45), [])

    def test_with_both_switches_off_nothing_is_posted(self):
        gh = DaemonGitHub(DaemonRepo([46]))
        d = daemon(gh.repo, gh, self.tmp, train={"pr_comments": False, "status_checks": False})
        rest = gh.calls["rest"]
        entry = hold(d, {"held": {}}, 46, "h46", ["check:x"])
        self.assertEqual(gh.calls["rest"], rest)
        self.assertNotIn("surfaced", entry)

    def test_a_dry_run_posts_nothing(self):
        gh = DaemonGitHub(DaemonRepo([47]))
        d = daemon(gh.repo, gh, self.tmp, dry_run=True)
        rest = gh.calls["rest"]
        hold(d, {"held": {}}, 47, "h47", ["check:x"])
        self.assertEqual(gh.calls["rest"], rest)

    def test_the_held_report_carries_what_was_shown(self):
        repo = DaemonRepo([48])
        gh = DaemonGitHub(repo)
        admission = Admission({"h48": {"verdict": "HELD", "residuals": ["check:x"]}})
        daemon(repo, gh, self.tmp, admission).tick()
        [row] = json.loads((Path(self.tmp) / "held.json").read_text())["held"]
        [comment] = surface_comments(gh, 48)
        self.assertEqual(row["surfaced"], pr_surface.comment_hash(comment["body"]))


if __name__ == "__main__":
    unittest.main()
