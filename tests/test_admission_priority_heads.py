"""Exact-head priority survives only while the already-listed head still matches.

The host filters declarations by expiry before emitting NUMBER@HEAD. Numeric tokens
remain explicitly PR-scoped. A moved or absent head receives the usual queue order;
an unreadable or malformed command does not remove candidates or skip admission.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fakes import Admission, Clock, DaemonGitHub, DaemonRepo, FakeGate, is_union
from svrf.daemon import Daemon

OLD = "a" * 40
NEW = "b" * 40


class HeadBoundPriority(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = DaemonRepo([41, 42, 43, 44])
        self.repo.commits[OLD] = self.repo.commits["h41"]
        self.repo.heads[41] = self.repo.branches["pr-41"] = OLD
        self.gh = DaemonGitHub(self.repo)
        self.admission = Admission()
        self.clock = Clock()
        self.owner = Daemon(
            self.repo, self.gh, FakeGate(self.repo), self.admission,
            state_dir=Path(self.temp.name), receipts=Path(self.temp.name) / "receipts",
            clock=self.clock, sleep=self.clock.sleep, train_options={"poll_seconds": 0},
            is_union=is_union, jobs=1, admission_jobs=1,
            admission_priority_command=f"printf '41@{OLD}\\n'",
        )
        self.addCleanup(self.owner.close)

    def listed(self):
        rows = self.gh.snapshot()
        self.owner.round_rows = {int(row["number"]): row for row in rows}
        return rows

    def move_head(self):
        self.repo.commits[NEW] = self.repo.commits[OLD]
        self.repo.heads[41] = self.repo.branches["pr-41"] = NEW

    def test_matching_exact_head_is_first_in_an_actual_tick(self):
        before = dict(self.repo.heads)
        out = self.owner.tick()
        numbers = {head: number for number, head in before.items()}
        self.assertEqual([numbers[head] for head, _ in self.admission.calls], [41, 44, 43, 42])
        self.assertEqual(out["admission_priority"]["read"], [41])
        self.assertEqual(out["admission_priority"]["entries"], [{"number": 41, "head": OLD}])
        self.assertEqual(len(self.gh.merged), 4)  # ordinary admission and gate still run

    def test_a_push_after_authorization_receives_normal_order(self):
        self.move_head()
        before = dict(self.repo.heads)
        out = self.owner.tick()
        numbers = {head: number for number, head in before.items()}
        self.assertEqual([numbers[head] for head, _ in self.admission.calls], [44, 43, 42, 41])
        self.assertEqual(out["admission_priority"]["read"], [])
        self.assertEqual(out["admission_priority"]["entries"], [{"number": 41, "head": OLD}])

    def test_missing_snapshot_head_is_not_a_pr_scoped_fallback(self):
        rows = self.listed()
        rows[0].pop("headRefOid")
        summary = {}
        got = self.owner._order_candidates(rows, 1, {}, summary)
        self.assertEqual([row["number"] for row in got], [44, 43, 42, 41])
        self.assertEqual(summary["admission_priority"]["read"], [])

    def test_malformed_tokens_fail_the_priority_read_without_dropping_candidates(self):
        for token in ("41@", "41@null", f"41@{OLD.upper()}", f"41@{OLD}@{OLD}",
                      f"0@{OLD}", f"x@{OLD}", "41@abc", "not-a-number"):
            with self.subTest(token=token), patch.object(self.owner, "_read_lines", return_value=([token], None)):
                summary = {}
                got = self.owner._order_candidates(self.listed(), 1, {}, summary)
                self.assertEqual([row["number"] for row in got], [44, 43, 42, 41])
                self.assertEqual(summary["admission_priority"]["UNOBSERVED"], "NOT_A_PRIORITY")
                self.assertNotIn("read", summary["admission_priority"])

    def test_mixed_bound_and_explicit_pr_scoped_entries_keep_order(self):
        with patch.object(self.owner, "_read_lines", return_value=([f"41@{OLD}", "43", f"42@{NEW}"], None)):
            summary = {}
            got = self.owner._order_candidates(self.listed(), 1, {}, summary)
        self.assertEqual([row["number"] for row in got], [41, 43, 44, 42])
        self.assertEqual(summary["admission_priority"]["read"], [41, 43])

    def test_refill_keeps_matching_previously_seen_head_without_extra_reads(self):
        rows = self.listed()
        summary = {}
        with patch.object(self.owner, "_read_lines", return_value=([f"41@{OLD}"], None)) as command:
            self.owner._order_candidates(rows, 1, {}, summary)
            api_before = dict(self.gh.calls)
            got = self.owner._order_candidates([self.owner.round_rows[42]], 1, {}, summary)
        self.assertEqual([row["number"] for row in got], [42])
        self.assertEqual(summary["admission_priority"]["read"], [41])
        self.assertEqual(command.call_count, 1)
        self.assertEqual(self.gh.calls, api_before)

    def test_smaller_refill_reprojects_a_moved_head_without_extra_command_or_api_read(self):
        rows = self.listed()
        summary = {}
        with patch.object(self.owner, "_read_lines", return_value=([f"41@{OLD}"], None)) as command:
            self.owner._order_candidates(rows, 1, {}, summary)
            self.move_head()
            latest = self.listed()
            api_before = dict(self.gh.calls)
            refill = [latest[1], latest[0]]
            got = self.owner._order_candidates(refill, 2, {}, summary)
        self.assertEqual([row["number"] for row in got], [42, 41])
        self.assertEqual(summary["admission_priority"]["read"], [])
        self.assertEqual(summary["admission_priority"]["entries"], [{"number": 41, "head": OLD}])
        self.assertEqual(command.call_count, 1)
        self.assertEqual(self.gh.calls, api_before)

    def test_refill_preserves_duplicate_positions_and_explicit_legacy_scope(self):
        rows = self.listed()
        summary = {}
        with patch.object(self.owner, "_read_lines", return_value=([f"41@{OLD}", "42", "41"], None)) as command:
            self.owner._order_candidates(rows, 1, {}, summary)
            self.move_head()
            latest = self.listed()
            self.owner._order_candidates([latest[1]], 1, {}, summary)
        self.assertEqual(summary["admission_priority"]["read"], [42, 41])
        self.assertEqual(command.call_count, 1)

    def test_head_binding_preserves_the_bounded_age_ranking(self):
        self.owner.admission_round_reads = 2
        self.move_head()
        summary = {}
        got = self.owner._order_candidates(self.listed(), 1, {"waiting_since": {"42": 3, "43": 1, "44": 2, "41": 4}}, summary)
        self.assertEqual([row["number"] for row in got], [43, 44])
        self.assertEqual(summary["deferred"], [42, 41])
        self.assertEqual(summary["admission_priority"]["read"], [])

    def test_missing_command_input_is_unobserved_and_keeps_normal_order(self):
        self.owner.admission_priority_command = f"cat {self.temp.name}/missing-priority"
        summary = {}
        got = self.owner._order_candidates(self.listed(), 1, {}, summary)
        self.assertEqual([row["number"] for row in got], [44, 43, 42, 41])
        self.assertEqual(summary["admission_priority"]["UNOBSERVED"], "EXIT:1")


if __name__ == "__main__":
    unittest.main()
