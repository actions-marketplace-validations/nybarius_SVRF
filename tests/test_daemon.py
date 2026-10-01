"""The automatic train: each round discovers its candidates from one pull-request list,
admits a head by the admission check, lands the families, remembers what it held by head
sha, and repairs the mechanical hold classes itself.

Invariants, each enforced where a test names it:
- one owner: a file lock, never a process-name match (SingleOwner);
- every git and gh call names its repository (ExplicitRepository);
- a failed read, a rate limit included, is retried and never enters the held memory (Rounds);
- only a pull request against the base branch is folded onto it (BaseBranch);
- a landed tree is read after its merge commit is fetched, and an unread tree is never a
  mismatch (LandedTreeRead).
"""

from __future__ import annotations

import ast
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from fakes import ROOT, UNION, Admission, Clock, DaemonGitHub, DaemonRepo, FakeGate, is_union

from svrf import rules
from svrf.daemon import Daemon
from svrf.errors import RateLimited, ReadFailed
from svrf.gate import MemoryGuard
from svrf.github import RealGitHub, gh_argv
from svrf.locks import owner_lock
from svrf.train import Train

REPO = "example/project"


def daemon(repo, gh, gate, admission, tmp, **kw):
    clock = kw.pop("clock", None) or Clock()
    return Daemon(repo, gh, gate, admission, state_dir=Path(tmp), receipts=Path(tmp) / "receipts",
                  clock=clock, sleep=clock.sleep, train_options={"poll_seconds": 0, **kw.pop("train", {})},
                  is_union=is_union, **kw)


def held_numbers(tmp):
    state = json.loads((Path(tmp) / "state.json").read_text())
    return sorted(int(n) for n in state["held"])


class PureRules(unittest.TestCase):
    def test_a_held_head_is_retried_iff_its_sha_changed(self):
        self.assertFalse(rules.held_retry({"head": "abc"}, "abc"))
        self.assertTrue(rules.held_retry({"head": "abc"}, "abd"))
        self.assertTrue(rules.held_retry(None, "abc"))

    def test_admission_reads_base_draft_fork_and_the_hold_label(self):
        row = {"number": 5, "headRefOid": "h5", "baseRefName": "main", "isDraft": False,
               "isCrossRepository": False, "labels": []}
        self.assertEqual(rules.admission(row, None)[0], "CANDIDATE")
        self.assertEqual(rules.admission({**row, "labels": [{"name": "train:hold"}]}, None)[0], "OPT_OUT")
        self.assertEqual(rules.admission({**row, "labels": [{"name": "wip"}]}, None, hold_label="wip")[0], "OPT_OUT")
        self.assertEqual(rules.admission({**row, "isDraft": True}, None)[0], "DRAFT")
        self.assertEqual(rules.admission({**row, "baseRefName": "feature/x"}, None)[0], "NOT_AGAINST_BASE")
        self.assertEqual(rules.admission({**row, "baseRefName": "trunk"}, None, base="trunk")[0], "CANDIDATE")
        self.assertEqual(rules.admission({**row, "isCrossRepository": True}, None)[0], "CROSS_REPOSITORY")
        self.assertEqual(rules.admission(row, {"head": "h5"})[0], "HELD_UNCHANGED")
        self.assertEqual(rules.admission(row, {"head": "h4"})[0], "CANDIDATE")

    def test_an_already_merged_head_is_classified_before_any_hold_draft_or_label(self):
        row = {"number": 5, "headRefOid": "h5", "baseRefName": "main", "isDraft": True,
               "isCrossRepository": False, "labels": [{"name": "train:hold"}]}
        self.assertEqual(rules.admission(row, {"head": "h5"}, carried=True)[0], "ALREADY_MERGED")
        self.assertEqual(rules.admission({**row, "isCrossRepository": True}, None, carried=True)[0],
                         "CROSS_REPOSITORY")
        self.assertEqual(rules.admission(row, None, carried=False)[0], "DRAFT")

    def test_the_admission_verdict_decides_admit_hold_repair_or_reland(self):
        d = rules.admission_decision
        self.assertEqual(d({"verdict": "MERGEABLE", "residuals": []}), ("ADMIT", None))
        self.assertEqual(d({"verdict": "LANDED", "residuals": []}), ("ALREADY_MERGED", None))
        self.assertEqual(d({"verdict": "HELD", "residuals": ["check:lint failed"]}), ("HOLD", None))
        self.assertEqual(d({"verdict": "HELD", "residuals": ["history:REFUSED:UNORDERED"]}), ("RELAND", "UNORDERED"))
        self.assertEqual(d({"verdict": "HELD", "residuals": ["history:REFUSED:MIXED"]}), ("RELAND", "MIXED"))
        self.assertEqual(d({"verdict": "HELD", "residuals": ["history:REFUSED:MIXED"]}, reland=False), ("HOLD", None))
        self.assertEqual(d({"verdict": "HELD", "residuals": [f"merge:CONFLICT:{UNION}"]}, is_union),
                         ("REPAIR", "UNION_CONFLICT"))
        self.assertEqual(d({"verdict": "SOMETHING"})[0], "RETRY")

    def test_reland_classes_are_exactly_the_history_order_refusals(self):
        self.assertEqual(rules.reland_class(["history:REFUSED:UNORDERED"]), "UNORDERED")
        self.assertEqual(rules.reland_class(["history:REFUSED:MIXED"]), "MIXED")
        self.assertIsNone(rules.reland_class([]))
        self.assertIsNone(rules.reland_class(["check:signed-off-by missing"]))
        self.assertIsNone(rules.reland_class(["history:REFUSED:UNORDERED", "merge:CONFLICT:tools/x.py"]))
        self.assertIsNone(rules.reland_class(["history:REFUSED:UNORDERED", "history:REFUSED:MIXED"]))

    def test_reland_classes_also_read_an_admission_commands_own_history_verdict(self):
        # An external admission.command cannot emit a bare `history:` residual (that
        # prefix is reserved for the built-in tests-first check), so it reports the same
        # order-only refusal under `reland:REFUSED:<class>` instead. reland_class treats
        # the two prefixes as one class of refusal.
        self.assertEqual(rules.reland_class(["reland:REFUSED:UNORDERED"]), "UNORDERED")
        self.assertEqual(rules.reland_class(["reland:REFUSED:MIXED"]), "MIXED")
        self.assertIsNone(rules.reland_class(["reland:REFUSED:UNORDERED", "history:REFUSED:MIXED"]))

    def test_repair_classes_are_exactly_the_mechanical_ones(self):
        self.assertEqual(rules.repair_class([f"merge:CONFLICT:{UNION}"], is_union), "UNION_CONFLICT")
        self.assertEqual(rules.repair_class(["github:NOT_MERGEABLE"], is_union), "STALE_BASE")
        self.assertIsNone(rules.repair_class([], is_union))
        self.assertIsNone(rules.repair_class(["merge:CONFLICT:tools/x.py"], is_union))
        self.assertIsNone(rules.repair_class([f"merge:CONFLICT:{UNION}", "check:x"], is_union))
        self.assertIsNone(rules.repair_class(["history:REFUSED:MIXED"], is_union))

    def test_an_infrastructure_failure_of_the_gate_is_retried_not_red(self):
        text = "error: cannot lock ref 'refs/remotes/origin/master': is at e8a0 but expected 0770\n"
        self.assertTrue(rules.gate_infra_failure(text).startswith("GATE_INFRA:"))
        self.assertIsNone(rules.gate_infra_failure("error: src/x.c:12:4: unknown identifier 'foo'\n"))
        self.assertTrue(rules.gate_infra_failure("runner lost", ["runner lost"]).startswith("GATE_INFRA:"))


class SingleOwner(unittest.TestCase):
    def test_a_second_owner_does_not_acquire_and_never_waits(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "svrf.lock"
            with owner_lock(path) as first:
                with owner_lock(path) as second:
                    self.assertTrue(first)
                    self.assertFalse(second)
            with owner_lock(path) as again:
                self.assertTrue(again)

    def test_an_inherited_lock_is_owned(self):
        import fcntl

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "svrf.lock"
            handle = open(path, "a+")
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                with owner_lock(path) as owned:
                    self.assertTrue(owned)
            finally:
                handle.close()

    def test_a_tick_while_another_owner_runs_reads_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = DaemonRepo([1])
            gh = DaemonGitHub(repo)
            d = daemon(repo, gh, FakeGate(repo), Admission(), tmp)
            with owner_lock(Path(tmp) / "svrf.lock"):
                out = d.tick()
            self.assertEqual(out["tick"], "LOCKED")
            self.assertEqual(gh.calls, {"graphql": 0, "rest": 0})
            self.assertEqual(gh.rate_reads, 0)
            self.assertEqual(gh.merged, [])

    def test_no_owner_is_found_by_matching_process_command_lines(self):
        for path in (ROOT / "src" / "svrf").glob("*.py"):
            self.assertNotIn("pgrep", path.read_text(encoding="utf-8"), path.name)


class Rounds(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_a_round_discovers_admits_and_lands_with_one_list(self):
        repo = DaemonRepo([11, 12, 13])
        gh = DaemonGitHub(repo)
        admission = Admission()
        out = daemon(repo, gh, FakeGate(repo), admission, self.tmp).tick()
        self.assertEqual(out["tick"], "RAN")
        self.assertEqual(out["admitted"], [11, 12, 13])
        self.assertEqual(gh.merged, [11, 12, 13])
        self.assertEqual(gh.calls["graphql"], 1)
        self.assertEqual(len(admission.calls), 3)
        self.assertTrue(Path(out["receipt"]).is_file())

    def test_admission_reads_overlap_up_to_jobs_a_slow_pr_never_blocks_the_rest(self):
        repo = DaemonRepo([14, 15, 16, 17])
        gh = DaemonGitHub(repo)
        admission = Admission(delay=0.2)
        out = daemon(repo, gh, FakeGate(repo), admission, self.tmp, jobs=4).tick()
        self.assertEqual(out["admitted"], [14, 15, 16, 17])
        self.assertGreaterEqual(admission.peak, 2)
        self.assertLessEqual(admission.peak, 4)

    def test_admission_jobs_of_one_reads_serially(self):
        repo = DaemonRepo([18, 19])
        gh = DaemonGitHub(repo)
        admission = Admission(delay=0.1)
        daemon(repo, gh, FakeGate(repo), admission, self.tmp, jobs=1).tick()
        self.assertEqual(admission.peak, 1)

    def test_the_memory_guard_shared_with_the_gate_serializes_admission_too(self):
        import time as _time

        repo = DaemonRepo([181, 182, 183])
        gh = DaemonGitHub(repo)
        admission = Admission(delay=0.1)
        guard = MemoryGuard(need_gb=10, reserve_gb=8, available_gb=lambda: 20, sleep=lambda s: _time.sleep(0.01))
        daemon(repo, gh, FakeGate(repo), admission, self.tmp, jobs=3, memory=guard).tick()
        self.assertEqual(admission.peak, 1)

    def test_skips_drafts_forks_other_bases_and_the_hold_label(self):
        repo = DaemonRepo([21, 22, 23, 24, 25])
        gh = DaemonGitHub(repo, drafts={21}, forks={22}, bases={23: "feature/other"}, labels={24: ["train:hold"]})
        admission = Admission()
        out = daemon(repo, gh, FakeGate(repo), admission, self.tmp).tick()
        self.assertEqual(gh.merged, [25])
        self.assertEqual(out["skipped"], {"21": "DRAFT", "22": "CROSS_REPOSITORY", "23": "NOT_AGAINST_BASE",
                                          "24": "OPT_OUT"})
        self.assertEqual([h for h, _ in admission.calls], ["h25"])
        self.assertEqual(held_numbers(self.tmp), [])

    def test_a_held_head_is_held_by_sha_and_retried_only_when_its_head_moves(self):
        repo = DaemonRepo([31, 32])
        gh = DaemonGitHub(repo)
        admission = Admission({"h31": {"verdict": "HELD", "residuals": ["check:lint failed"]}})
        d = daemon(repo, gh, FakeGate(repo), admission, self.tmp)
        first = d.tick()
        self.assertEqual(first["held"], [31])
        self.assertEqual(gh.merged, [32])
        report = json.loads((Path(self.tmp) / "held.json").read_text())
        self.assertEqual([(r["number"], r["head"], r["failing"]) for r in report["held"]],
                         [(31, "h31", ["check:lint failed"])])
        calls = len(admission.calls)
        second = d.tick()
        self.assertEqual(second["tick"], "IDLE")
        self.assertEqual(len(admission.calls), calls)
        new = repo.move_head(31)
        third = d.tick()
        self.assertEqual(third["admitted"], [31])
        self.assertEqual(admission.calls[-1][0], new)
        self.assertEqual(gh.merged, [32, 31])
        self.assertEqual(held_numbers(self.tmp), [])

    def test_an_idle_round_costs_the_rate_read_and_one_list(self):
        repo = DaemonRepo([41, 42])
        gh = DaemonGitHub(repo, drafts={42})
        admission = Admission({"h41": {"verdict": "HELD", "residuals": ["check:x"]}})
        d = daemon(repo, gh, FakeGate(repo), admission, self.tmp)
        d.tick()
        before = (dict(gh.calls), gh.rate_reads, repo.fetches, len(admission.calls))
        out = d.tick()
        self.assertEqual(out["tick"], "IDLE")
        self.assertEqual(gh.calls["graphql"] - before[0]["graphql"], 1)
        self.assertEqual(gh.calls["rest"] - before[0]["rest"], 0)
        self.assertEqual(gh.rate_reads - before[1], 1)
        self.assertEqual(repo.fetches, before[2])
        self.assertEqual(len(admission.calls), before[3])

    def test_a_rate_limited_list_is_retried_and_the_held_memory_is_unchanged(self):
        repo = DaemonRepo([51])
        gh = DaemonGitHub(repo)
        admission = Admission({"h51": {"verdict": "HELD", "residuals": ["check:x"]}})
        d = daemon(repo, gh, FakeGate(repo), admission, self.tmp)
        d.tick()
        state = (Path(self.tmp) / "state.json").read_text()
        gh.rate_limited_list = 1
        out = d.tick()
        self.assertEqual(out["tick"], "RETRY")
        self.assertTrue(out["reason"].startswith("RATE_LIMITED:"))
        self.assertEqual(json.loads((Path(self.tmp) / "state.json").read_text())["held"], json.loads(state)["held"])

    def test_a_failed_admission_read_is_retried_never_held(self):
        repo = DaemonRepo([61])
        gh = DaemonGitHub(repo)
        admission = Admission(failures={"h61": RateLimited("API rate limit already exceeded")})
        d = daemon(repo, gh, FakeGate(repo), admission, self.tmp)
        out = d.tick()
        self.assertEqual(out["retry_later"], [61])
        self.assertEqual(out["held"], [])
        self.assertEqual(held_numbers(self.tmp), [])
        d.tick()
        self.assertEqual(gh.merged, [61])

    def test_content_residuals_stay_held_order_residuals_reland_neither_reaches_the_gate(self):
        repo = DaemonRepo([71, 72, 73])
        gh = DaemonGitHub(repo)
        gate = FakeGate(repo)
        admission = Admission({"h71": {"verdict": "HELD", "residuals": ["check:license header missing"]},
                               "h72": {"verdict": "HELD", "residuals": ["history:REFUSED:UNORDERED"]}})
        out = daemon(repo, gh, gate, admission, self.tmp).tick()
        self.assertEqual(out["admitted"], [73])
        self.assertEqual(out["held"], [71])
        self.assertEqual([r["number"] for r in out["relanded"]], [72])
        self.assertEqual(gh.merged, [73])
        for tree in gate.trees:
            self.assertNotIn("71", tree.split(","))
            self.assertNotIn("72", tree.split(","))

    def test_a_train_hold_is_remembered_by_head_with_its_failing_lines(self):
        repo = DaemonRepo([81, 82])
        gh = DaemonGitHub(repo)
        gate = FakeGate(repo, bad={81})
        d = daemon(repo, gh, gate, Admission(), self.tmp)
        out = d.tick()
        self.assertEqual(gh.merged, [82])
        self.assertEqual(out["held"], [81])
        row = json.loads((Path(self.tmp) / "held.json").read_text())["held"][0]
        self.assertEqual((row["number"], row["head"], row["reason"]), (81, "h81", "GATE_RED"))
        self.assertIn("FAILED tests/test_lane_81.py::test_it", row["failing"])
        gates = len(gate.trees)
        self.assertEqual(d.tick()["tick"], "IDLE")
        self.assertEqual(len(gate.trees), gates)

    def test_a_held_pr_that_closed_leaves_the_memory(self):
        repo = DaemonRepo([91])
        gh = DaemonGitHub(repo)
        admission = Admission({"h91": {"verdict": "HELD", "residuals": ["check:x"]}})
        d = daemon(repo, gh, FakeGate(repo), admission, self.tmp)
        d.tick()
        self.assertEqual(held_numbers(self.tmp), [91])
        repo.heads.pop(91)
        d.tick()
        self.assertEqual(held_numbers(self.tmp), [])

    def test_dry_run_neither_merges_repairs_nor_remembers(self):
        repo = DaemonRepo([95, 96])
        gh = DaemonGitHub(repo)
        admission = Admission({"h96": {"verdict": "HELD", "residuals": [f"merge:CONFLICT:{UNION}"]}})
        out = daemon(repo, gh, FakeGate(repo), admission, self.tmp, dry_run=True).tick()
        self.assertEqual(gh.merged, [])
        self.assertEqual(repo.pushes, [])
        self.assertEqual(out["repaired"], [])
        self.assertEqual([r["number"] for r in out["would_repair"]], [96])
        self.assertFalse((Path(self.tmp) / "state.json").exists())

    def test_a_round_keeps_only_what_a_reader_uses(self):
        repo = DaemonRepo([113])
        gh = DaemonGitHub(repo)
        daemon(repo, gh, FakeGate(repo), Admission(), self.tmp).tick()
        kept = sorted(p.name for p in Path(self.tmp).iterdir() if p.is_file())
        self.assertEqual(kept, ["held.json", "state.json", "svrf.lock"])


class Repairs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_a_union_conflict_is_union_merged_pushed_by_refspec_and_requeued(self):
        repo = DaemonRepo([101])
        gh = DaemonGitHub(repo)
        admission = Admission({"h101": {"verdict": "HELD", "residuals": [f"merge:CONFLICT:{UNION}"]}})
        d = daemon(repo, gh, FakeGate(repo), admission, self.tmp)
        out = d.tick()
        self.assertEqual(out["repaired"], [{"number": 101, "class": "UNION_CONFLICT",
                                            "head": "h101", "pushed": repo.branches["pr-101"]}])
        self.assertEqual(repo.pushes[-1][0], "pr-101")
        self.assertEqual(out["held"], [])
        repo.heads[101] = repo.branches["pr-101"]
        d.tick()
        self.assertEqual(gh.merged, [101])

    def test_repair_can_be_turned_off(self):
        repo = DaemonRepo([102])
        gh = DaemonGitHub(repo)
        admission = Admission({"h102": {"verdict": "HELD", "residuals": [f"merge:CONFLICT:{UNION}"]}})
        out = daemon(repo, gh, FakeGate(repo), admission, self.tmp, repair=False).tick()
        self.assertEqual(out["repaired"], [])
        self.assertEqual(out["held"], [102])

    def test_a_repair_that_still_conflicts_is_held_with_its_paths(self):
        repo = DaemonRepo([103], repair_conflicts={103: ["tools/x.py"]})
        gh = DaemonGitHub(repo)
        admission = Admission({"h103": {"verdict": "HELD", "residuals": [f"merge:CONFLICT:{UNION}"]}})
        out = daemon(repo, gh, FakeGate(repo), admission, self.tmp).tick()
        self.assertEqual(out["repaired"], [])
        self.assertEqual(out["held"], [103])
        row = json.loads((Path(self.tmp) / "held.json").read_text())["held"][0]
        self.assertEqual((row["reason"], row["paths"]), ("REPAIR_CONFLICT", ["tools/x.py"]))

    def test_a_train_conflict_on_a_union_path_is_repaired_not_held(self):
        repo = DaemonRepo([104], main_conflicts={104: [UNION]})
        gh = DaemonGitHub(repo)
        out = daemon(repo, gh, FakeGate(repo), Admission(), self.tmp).tick()
        self.assertEqual([r["number"] for r in out["repaired"]], [104])
        self.assertEqual(out["held"], [])

    def test_github_not_mergeable_is_a_stale_base_repair(self):
        repo = DaemonRepo([105])
        gh = DaemonGitHub(repo)

        def pull(number, _pull=gh.pull):
            row = _pull(number)
            row["mergeable"] = False
            return row
        gh.pull = pull
        out = daemon(repo, gh, FakeGate(repo), Admission(), self.tmp).tick()
        self.assertEqual([(r["number"], r["class"]) for r in out["repaired"]], [(105, "STALE_BASE")])
        self.assertEqual(out["held"], [])


class OrderedReland(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_an_unordered_head_is_relanded_not_held(self):
        repo = DaemonRepo([5])
        expected_tree = repo.tree(repo.heads[5])
        gh = DaemonGitHub(repo, bodies={5: "the original body\nCo-Authored-By: X <x@x>\n"})
        admission = Admission({"h5": {"verdict": "HELD", "residuals": ["history:REFUSED:UNORDERED"]}})
        out = daemon(repo, gh, FakeGate(repo), admission, self.tmp).tick()
        self.assertEqual(out["held"], [])
        self.assertEqual(len(gh.opened), 1)
        opened = gh.opened[0]
        self.assertEqual((opened["head"], opened["base"]), ("pr-5-ordered", "main"))
        self.assertTrue(opened["body"].startswith("Supersedes #5"))
        self.assertIn("the original body", opened["body"])
        self.assertNotIn("Co-Authored-By", opened["body"])
        self.assertEqual(out["relanded"], [{"number": 5, "new_number": opened["number"], "ref": "pr-5-ordered"}])
        self.assertEqual(repo.tree(repo.branches["pr-5-ordered"]), expected_tree)
        self.assertEqual(gh.closed, [5])
        self.assertIn(f"Superseded by #{opened['number']}", gh.comments[0][1])

    def test_a_tree_mismatch_after_the_rebuild_is_held_not_opened(self):
        repo = DaemonRepo([6], reland_mismatch=True)
        gh = DaemonGitHub(repo)
        admission = Admission({"h6": {"verdict": "HELD", "residuals": ["history:REFUSED:MIXED"]}})
        out = daemon(repo, gh, FakeGate(repo), admission, self.tmp).tick()
        self.assertEqual(out["held"], [6])
        self.assertEqual((gh.opened, gh.closed), ([], []))
        row = json.loads((Path(self.tmp) / "held.json").read_text())["held"][0]
        self.assertEqual(row["reason"], "RELAND_TREE_MISMATCH")

    def test_a_rebuilt_history_that_still_fails_the_check_is_held(self):
        repo = DaemonRepo([9])
        gh = DaemonGitHub(repo)
        admission = Admission({"h9": {"verdict": "HELD", "residuals": ["history:REFUSED:UNORDERED"]}})
        out = daemon(repo, gh, FakeGate(repo), admission, self.tmp,
                     history_verdict=lambda b, h: "REFUSED:UNORDERED").tick()
        self.assertEqual(out["held"], [9])
        self.assertEqual(gh.opened, [])

    def test_a_dry_run_reports_would_reland_and_touches_nothing(self):
        repo = DaemonRepo([7])
        gh = DaemonGitHub(repo)
        admission = Admission({"h7": {"verdict": "HELD", "residuals": ["history:REFUSED:UNORDERED"]}})
        out = daemon(repo, gh, FakeGate(repo), admission, self.tmp, dry_run=True).tick()
        self.assertEqual(out["would_reland"], [7])
        self.assertEqual((repo.pushes, gh.opened, gh.closed), ([], [], []))

    def test_the_original_branch_is_never_pushed_to(self):
        repo = DaemonRepo([8])
        gh = DaemonGitHub(repo)
        admission = Admission({"h8": {"verdict": "HELD", "residuals": ["history:REFUSED:UNORDERED"]}})
        daemon(repo, gh, FakeGate(repo), admission, self.tmp).tick()
        pushed = {branch for branch, _ in repo.pushes}
        self.assertNotIn("pr-8", pushed)
        self.assertIn("pr-8-ordered", pushed)


class AlreadyMerged(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_a_head_the_base_contains_is_closed_as_merged_never_read_or_gated(self):
        repo = DaemonRepo([121])
        repo.main = repo._new({0, 121}, ("c0",))
        gh = DaemonGitHub(repo)
        admission = Admission()
        out = daemon(repo, gh, FakeGate(repo), admission, self.tmp).tick()
        self.assertEqual(out["merged_elsewhere"], [121])
        self.assertEqual(out["retry_later"], [])
        self.assertEqual(admission.calls, [])
        self.assertEqual(gh.merged, [])
        self.assertIn(("pr-121", repo.main), repo.pushes)
        self.assertEqual(held_numbers(self.tmp), [])

    def test_a_held_head_becomes_merged_once_the_base_absorbs_it_elsewhere(self):
        repo = DaemonRepo([131, 132])
        gh = DaemonGitHub(repo)
        admission = Admission()
        d = daemon(repo, gh, FakeGate(repo, bad={131}), admission, self.tmp)
        self.assertEqual(d.tick()["held"], [131])
        calls = len(admission.calls)
        repo.main = repo._new(repo.lanes(repo.main) | {131}, (repo.main,))
        second = d.tick()
        self.assertEqual(second["merged_elsewhere"], [131])
        self.assertEqual(len(admission.calls), calls)
        self.assertEqual(held_numbers(self.tmp), [])

    def test_a_refused_forward_push_falls_back_to_one_comment_and_close(self):
        repo = DaemonRepo([141])
        repo.main = repo._new({0, 141}, ("c0",))
        repo.push_refusals = {"pr-141"}
        gh = DaemonGitHub(repo)
        out = daemon(repo, gh, FakeGate(repo), Admission(), self.tmp).tick()
        self.assertEqual(out["merged_elsewhere"], [141])
        self.assertEqual(repo.pushes, [])
        self.assertEqual(gh.comments, [(141, "Every commit of this head is already on main.")])
        self.assertEqual(gh.closed, [141])

    def test_a_dry_run_only_reports_it(self):
        repo = DaemonRepo([151])
        repo.main = repo._new({0, 151}, ("c0",))
        gh = DaemonGitHub(repo)
        out = daemon(repo, gh, FakeGate(repo), Admission(), self.tmp, dry_run=True).tick()
        self.assertEqual(out["would_close_merged"], [151])
        self.assertEqual((repo.pushes, gh.comments, gh.closed), ([], [], []))


class BaseBranch(unittest.TestCase):
    """Only a pull request against the base branch is folded onto it. A stacked one waits
    while its parent is open, is retargeted once its parent merged, and is held when its
    parent closed unmerged; the parent is read once per (head, base)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_a_stacked_pr_waits_for_its_open_parent_without_a_read(self):
        repo = DaemonRepo([201, 202])
        gh = DaemonGitHub(repo, bases={202: "pr-201"})
        admission = Admission({"h201": {"verdict": "HELD", "residuals": ["check:x"]}})
        out = daemon(repo, gh, FakeGate(repo), admission, self.tmp).tick()
        self.assertEqual(out["skipped"], {"202": "WAITING_PARENT"})
        self.assertEqual(gh.calls["rest"], 0)
        self.assertEqual([h for h, _ in admission.calls], ["h201"])

    def test_a_stacked_pr_whose_parent_merged_is_retargeted_then_landed(self):
        repo = DaemonRepo([203])
        gh = DaemonGitHub(repo, bases={203: "feature/parent"})
        gh.parents = {"feature/parent": [{"number": 190, "state": "closed", "merged_at": "2026-09-25T17:00:00Z"}]}
        d = daemon(repo, gh, FakeGate(repo), Admission(), self.tmp)
        out = d.tick()
        self.assertEqual(out["retargeted"], [203])
        self.assertEqual(gh.retargeted, [(203, "main")])
        self.assertEqual(gh.merged, [])
        d.tick()
        self.assertEqual(gh.merged, [203])

    def test_a_stacked_pr_whose_parent_closed_unmerged_is_held(self):
        repo = DaemonRepo([204])
        gh = DaemonGitHub(repo, bases={204: "feature/abandoned"})
        gh.parents = {"feature/abandoned": [{"number": 191, "state": "closed", "merged_at": None}]}
        d = daemon(repo, gh, FakeGate(repo), Admission(), self.tmp)
        self.assertEqual(d.tick()["held"], [204])
        row = json.loads((Path(self.tmp) / "held.json").read_text())["held"][0]
        self.assertEqual((row["reason"], row["paths"]), ("PARENT_CLOSED_UNMERGED", ["feature/abandoned"]))
        rest = gh.calls["rest"]
        self.assertEqual(d.tick()["tick"], "IDLE")
        self.assertEqual(gh.calls["rest"], rest)

    def test_a_pr_on_a_branch_no_pr_heads_is_read_once_per_head_and_base(self):
        repo = DaemonRepo([205])
        gh = DaemonGitHub(repo, bases={205: "feature/sibling"})
        d = daemon(repo, gh, FakeGate(repo), Admission(), self.tmp)
        self.assertEqual(d.tick()["skipped"], {"205": "NOT_AGAINST_BASE"})
        rest = gh.calls["rest"]
        d.tick()
        self.assertEqual(gh.calls["rest"], rest)


class UnreadTree(DaemonRepo):
    """A merge commit GitHub made is not in the clone until it is fetched by sha."""

    def __init__(self, prs, *, fetchable=True, **kw):
        super().__init__(prs, **kw)
        self.fetchable = fetchable
        self.unfetched = set()

    def fetch_commit(self, sha):
        if self.fetchable:
            self.unfetched.discard(sha)

    def tree(self, commit):
        if commit in self.unfetched:
            raise ReadFailed(f"GIT_FAILED:rev-parse:128:{commit}")
        return super().tree(commit)


class UnfetchedMerges(DaemonGitHub):
    def merge(self, number, sha):
        merged = super().merge(number, sha)
        self.repo.unfetched.add(merged)
        return merged


class LandedTreeRead(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _run(self, repo, numbers, **kw):
        gh = UnfetchedMerges(repo)
        receipt = Train(repo, gh, FakeGate(repo), receipts=Path(self.tmp), clock=Clock(), sleep=lambda s: None,
                        poll_seconds=0, **kw).run(numbers)
        return gh, receipt

    def test_the_merge_commit_is_fetched_before_its_tree_is_read(self):
        repo = UnreadTree([301, 302])
        gh, receipt = self._run(repo, [301, 302])
        self.assertEqual(gh.merged, [301, 302])
        self.assertEqual(receipt["alerts"], [])
        self.assertTrue(all(m["identity"] for m in receipt["merges"]))

    def test_an_unreadable_landed_tree_is_retried_never_a_mismatch(self):
        repo = UnreadTree([303, 304], fetchable=False)
        gh, receipt = self._run(repo, [303, 304], max_rounds=1)
        self.assertNotIn("TREE_MISMATCH", [a["alert"] for a in receipt["alerts"]])
        self.assertFalse(receipt["stopped"])
        self.assertIn("LANDED_TREE_UNREAD", [u["reason"].split(":")[0] for u in receipt["retry_later"]])
        self.assertIsNone(receipt["merges"][0]["identity"])
        self.assertEqual(gh.merged, [303])


class WatchedPathRetry(unittest.TestCase):
    """The held memory is (head, base content over the watched paths): a held head is read
    again when its head moved or the base changed over what its hold depended on, and not
    when the base moved elsewhere."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_held_retry_reads_head_and_the_watched_digest(self):
        held = {"head": "a", "watch_digest": "t1"}
        self.assertFalse(rules.held_retry(held, "a", "t1"))
        self.assertTrue(rules.held_retry(held, "a", "t2"))
        self.assertTrue(rules.held_retry(held, "b", "t1"))
        self.assertFalse(rules.held_retry(held, "a"))

    def test_a_held_head_is_read_again_when_the_base_changes_what_it_watched(self):
        repo = DaemonRepo([31, 32, 33])
        gh = DaemonGitHub(repo)
        admission = Admission({"h31": {"verdict": "HELD", "residuals": ["check:x"], "changed": ["lane:32"]},
                               "h32": {"verdict": "HELD", "residuals": ["check:y"]}})
        d = daemon(repo, gh, FakeGate(repo), admission, self.tmp)
        d.tick()                                   # 31 and 32 held, 33 lands (outside 31's watch)
        self.assertEqual(gh.merged, [33])
        self.assertEqual(held_numbers(self.tmp), [31, 32])
        d.tick()
        self.assertEqual([h for h, _ in admission.calls].count("h31"), 1)
        repo.move_head(32)                         # 32 is fixed and lands; the base now carries lane 32
        d.tick()
        self.assertEqual(gh.merged, [33, 32])
        self.assertEqual([h for h, _ in admission.calls].count("h31"), 1)
        d.tick()                                   # the base changed over 31's watch: read again
        self.assertEqual([h for h, _ in admission.calls].count("h31"), 2)

    def test_a_held_head_is_read_again_when_the_base_changes_an_always_watched_path(self):
        """The environment a verdict ran in (a requirements file, a toolchain pin) is part of
        what every hold depended on, even when the pull request never touched it."""
        repo = DaemonRepo([41, 42])
        gh = DaemonGitHub(repo)
        admission = Admission({"h41": {"verdict": "HELD", "residuals": ["check:x"], "changed": ["lane:41"]}})
        d = daemon(repo, gh, FakeGate(repo), admission, self.tmp, admission_watch=["lane:42"])
        d.tick()                                   # 41 held, 42 lands
        self.assertEqual(held_numbers(self.tmp), [41])
        d.tick()                                   # the base changed over the always-watched path
        self.assertEqual([h for h, _ in admission.calls].count("h41"), 2)

    def test_admission_watch_is_read_from_the_config(self):
        from svrf import config as cfg
        path = Path(self.tmp) / "svrf.toml"
        path.write_text('repo = "o/r"\n[gate]\ncommands = ["true"]\n[admission]\ncommand = "true"\nwatch = ["req.txt"]\n', encoding="utf-8")
        self.assertEqual(cfg.load(path).admission_watch, ["req.txt"])


    def test_a_held_head_is_read_again_when_the_base_changes_what_its_checks_consumed(self):
        """The hold's watch is what its failing checks consumed (the admission's `consumed` PATH
        rows), not only the paths the pull request changed: a failing suite that reads lane 32
        is read again when lane 32 lands, although the held head changed nothing there."""
        repo = DaemonRepo([31, 32, 33])
        gh = DaemonGitHub(repo)
        admission = Admission({"h31": {"verdict": "HELD", "residuals": ["check:suites:tests/test_x.py:exit=1"], "changed": [],
                                       "consumed": [{"id": "lane:32", "kind": "PATH"}, {"id": "dir:tests", "kind": "DIR"},
                                                    {"id": "h31", "kind": "COMMIT"}]},
                               "h32": {"verdict": "HELD", "residuals": ["check:y"]}})
        d = daemon(repo, gh, FakeGate(repo), admission, self.tmp)
        d.tick()
        self.assertEqual(held_numbers(self.tmp), [31, 32])
        state = json.loads((Path(self.tmp) / "state.json").read_text())
        self.assertEqual(state["held"]["31"]["watch"], ["lane:32", "tests"])
        d.tick()
        self.assertEqual([h for h, _ in admission.calls].count("h31"), 1)
        repo.move_head(32)
        d.tick()
        self.assertEqual(gh.merged, [33, 32])
        d.tick()                                   # the base changed over what 31's check consumed: read again
        self.assertEqual([h for h, _ in admission.calls].count("h31"), 2)

    def test_the_watch_is_the_union_of_changed_and_consumed_paths(self):
        repo = DaemonRepo([31])
        gh = DaemonGitHub(repo)
        admission = Admission({"h31": {"verdict": "HELD", "residuals": ["check:x"], "changed": ["lane:5"],
                                       "consumed": [{"id": "lane:7", "kind": "PATH"}, {"id": "lane:5", "kind": "PATH"}]}})
        d = daemon(repo, gh, FakeGate(repo), admission, self.tmp)
        d.tick()
        state = json.loads((Path(self.tmp) / "state.json").read_text())
        self.assertEqual(state["held"]["31"]["watch"], ["lane:5", "lane:7"])


def _string_lists(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
            head = node.elts[0]
            if isinstance(head, ast.Constant) and head.value in ("git", "gh"):
                yield node


class ExplicitRepository(unittest.TestCase):
    def test_every_git_invocation_names_its_repository(self):
        for path in (ROOT / "src" / "svrf").glob("*.py"):
            for node in _string_lists(path):
                if node.elts[0].value != "git":
                    continue
                second = node.elts[1] if len(node.elts) > 1 else None
                self.assertTrue(isinstance(second, ast.Constant) and second.value == "-C",
                                f"{path.name}:{node.lineno} git without -C")

    def test_every_gh_invocation_names_its_repository(self):
        seen = []

        def run(argv, **kw):
            seen.append(list(argv))
            text = "{}"
            if argv[1:3] == ["api", "rate_limit"]:
                text = json.dumps({"resources": {"graphql": {"remaining": 1, "reset": 2},
                                                 "core": {"remaining": 1, "reset": 2}}})
            elif "pulls?state=open" in " ".join(argv):
                text = "[]"
            elif "pulls/7/merge" in " ".join(argv):
                text = json.dumps({"merged": True, "sha": "m"})
            elif argv[1] == "api" and "pulls/7" in " ".join(argv):
                text = json.dumps({"head": {"sha": "h", "ref": "r"}, "mergeable": True, "state": "open"})
            elif "pulls?state=all" in " ".join(argv):
                text = "[]"
            elif argv[1] == "api" and argv[-1].startswith("body="):
                text = json.dumps({"number": 8})
            return subprocess.CompletedProcess(argv, 0, stdout=text, stderr="")

        import svrf.github as module

        gh = RealGitHub(REPO)
        original = module.subprocess.run
        module.subprocess.run = run
        try:
            gh.rate_limit()
            gh.snapshot()
            gh.pull(7)
            with self.assertRaises(ReadFailed):
                gh.ready(7)
            gh.merge(7, "h")
            gh.comment(7, "x")
            gh.close(7)
            gh.retarget(7, "main")
            gh.pulls_with_head("b")
        finally:
            module.subprocess.run = original
        self.assertEqual(len(seen), 8)
        for argv in seen:
            self.assertEqual(argv[0], "gh")
            bound = ("--repo" in argv and argv[argv.index("--repo") + 1] == REPO) or \
                any(a.startswith(f"repos/{REPO}/") for a in argv) or argv[1:] == ["api", "rate_limit"]
            self.assertTrue(bound, argv)
        with self.assertRaises(ValueError):
            gh_argv(["pr", "list"], REPO)

    def test_the_snapshot_reads_labels_base_fork_and_body(self):
        fields = RealGitHub.SNAPSHOT_FIELDS.split(",")
        for name in ("number", "headRefOid", "headRefName", "baseRefName", "isDraft", "labels",
                     "isCrossRepository", "title", "body"):
            self.assertIn(name, fields)


if __name__ == "__main__":
    unittest.main()
