"""Focused controls for native conflict deferral and base-bound hold reception."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import threading
import tempfile
import unittest
from unittest import mock

from fakes import Clock, DaemonGitHub, DaemonRepo, FakeGate
from svrf.admission import Admission
from svrf.daemon import Daemon
from svrf.errors import ReadFailed
from svrf.git import RealGit
from svrf import rules


class NativeConflictControls(unittest.TestCase):
    def setUp(self):
        evidence = os.environ.get("NATIVE_CONFLICT_EVIDENCE")
        if evidence is None:
            temporary = tempfile.TemporaryDirectory(prefix="svrf-native-conflict-")
            self.addCleanup(temporary.cleanup)
            evidence = temporary.name
        self.case = Path(evidence) / self._testMethodName
        self.case.mkdir(parents=True)
        self.observed = {}
        self.environment = mock.patch.dict(os.environ, {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_ALLOW_PROTOCOL": "",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_AUTHOR_NAME": "fixture",
            "GIT_AUTHOR_EMAIL": "fixture@localhost",
            "GIT_COMMITTER_NAME": "fixture",
            "GIT_COMMITTER_EMAIL": "fixture@localhost",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def tearDown(self):
        self.observed["physical_commands"] = {
            p.relative_to(self.case).as_posix(): json.loads(p.read_text())
            for p in self.case.glob("command-*.json")
        }
        (self.case / "RESULT.json").write_text(
            json.dumps(self.observed, indent=2, sort_keys=True) + "\n")

    def command(self, label, *, code=0, bad_head=False, consumed="proof.py"):
        marker = self.case / ("command-" + label + ".json")
        program = (
            "import json,os\nfrom pathlib import Path\n"
            "head=os.environ['SVRF_HEAD']; base=os.environ['SVRF_BASE']\n"
            f"Path({str(marker)!r}).write_text(json.dumps({{'pid':os.getpid(),'head':head,'base':base,'exit':{code}}}))\n"
            "value={'schema':'svrf.admission-reads/1','head':head,'base':base,"
            f"'consumed':[{{'kind':'PATH','id':{consumed!r}}}]}}\n"
            + ("value['head']='different-source-head'\n" if bad_head else "")
            + "Path(os.environ['SVRF_ADMISSION_READS']).write_text(json.dumps(value))\n"
            + ("print('real-check-failed')\n" if code else "")
            + f"raise SystemExit({code})\n"
        )
        return shlex.quote(sys.executable) + " -c " + shlex.quote(program), marker

    def real_repository(self, label="repo", *, union=None):
        root = self.case / label
        root.mkdir()
        records = []
        def git(*args, data=None):
            done = subprocess.run(["git", "-C", str(root), *args],
                                  input=data, capture_output=True, timeout=15)
            records.append({"argv": list(args), "exit": done.returncode,
                            "stdout": done.stdout.decode(errors="replace"),
                            "stderr": done.stderr.decode(errors="replace")})
            if done.returncode:
                raise RuntimeError(records[-1])
            return done.stdout.decode().strip()
        git("init", "-q")
        def commit(text, parents, message):
            blob = git("hash-object", "-w", "--stdin", data=text.encode())
            tree = git("mktree", data=(
                f"100644 blob {blob}\tleft.txt\n100644 blob {blob}\tright.txt\n").encode())
            argv = ["commit-tree", tree]
            for parent in parents:
                argv += ["-p", parent]
            return git(*argv, data=(message + "\n").encode())
        common = commit("common\n", [], "common")
        head = commit("head\n", [common], "head")
        base = commit("base\n", [common], "base")
        git("update-ref", "refs/heads/main", base)
        git("symbolic-ref", "HEAD", "refs/heads/main")
        repo = RealGit(root, union=(lambda path: False) if union is None else union)
        native = repo.merge_preview
        def observe_merge(a, b):
            value = native(a, b)
            self.observed.setdefault("native_reads", []).append(
                {"base": a, "head": b, "result": value})
            return value
        repo.merge_preview = observe_merge
        self.observed.setdefault("repositories", []).append({
            "root": str(root), "common": common, "head": head, "base": base,
            "commands": records})
        return repo, common, head, base, commit

    def fake_owner(self, *, command_exit=1, consumed="lane:32"):
        repo = DaemonRepo([31])
        repo.root = self.case / "working"
        repo.root.mkdir()
        repo.union = lambda path: False
        repo.changed_paths = lambda *args: ["lane:31"]
        native_state = {"blocked": True, "failure": None}
        native_calls = []
        def native(base, head):
            native_calls.append([base, head])
            if native_state["failure"] is not None:
                raise native_state["failure"]
            return {"status": "CONFLICT" if native_state["blocked"] else "CLEAN",
                    "conflicts": ["lane:31"] if native_state["blocked"] else []}
        repo.merge_preview = native
        command, marker = self.command("receiver", code=command_exit, consumed=consumed)
        admission = Admission(repo, command=command)
        clock = Clock()
        owner = Daemon(repo, DaemonGitHub(repo), FakeGate(repo), admission,
                       state_dir=self.case / "state", receipts=self.case / "receipts",
                       clock=clock, sleep=clock.sleep, is_union=repo.union,
                       train_options={"poll_seconds": 0})
        return owner, repo, native_state, native_calls, marker

    def test_real_non_union_conflict_skips_command_and_slot(self):
        repo, _, head, base, _ = self.real_repository()
        command, marker = self.command("blocked")
        admission = Admission(repo, command=command, pool=self.case / "pool", slots=1)
        with mock.patch.object(admission.pool, "acquire", wraps=admission.pool.acquire) as lease:
            value = admission(head, base, 31)
            self.observed.update(value=value, slot_acquisitions=lease.call_count,
                                 command_executed=marker.exists())
        self.assertEqual(value["verdict"], "HELD")
        self.assertEqual(value["residuals"],
                         ["merge:CONFLICT:left.txt", "merge:CONFLICT:right.txt"])
        self.assertEqual(lease.call_count, 0)
        self.assertFalse(marker.exists())
        self.assertEqual(value["native_conflict_base"], base)
        self.assertEqual(value["checks_not_run"], ["admission_command"])
        self.assertNotIn("consumed", value)

    def test_resolved_real_merge_runs_original_command_and_receives_evidence(self):
        repo, _, head, base, commit = self.real_repository()
        command, marker = self.command("resolved")
        admission = Admission(repo, command=command)
        blocked = admission(head, base, 31)
        self.assertFalse(marker.exists())
        resolved = commit("head\n", [base], "resolved content")
        value = admission(head, resolved, 31)
        self.observed.update(blocked=blocked, resolved_base=resolved, value=value)
        self.assertTrue(marker.exists())
        self.assertEqual(json.loads(marker.read_text())["base"], resolved)
        self.assertEqual(value, {"verdict": "MERGEABLE", "residuals": [],
                                 "changed": ["left.txt", "right.txt"],
                                 "consumed": [{"kind": "PATH", "id": "proof.py"}]})
        self.assertNotIn("native_conflict_base", value)

    def test_native_failure_and_moved_command_evidence_remain_unobserved(self):
        repo, common, head, base, _ = self.real_repository()
        command, marker = self.command("bad-evidence", bad_head=True)
        admission = Admission(repo, command=command)
        native = repo.merge_preview
        repo.merge_preview = lambda *args: {
            "status": "FAILED", "conflicts": [], "reason": "MERGE_TREE_FAILED:128"}
        with self.assertRaises(ReadFailed) as failed:
            admission(head, base, 31)
        self.assertEqual(failed.exception.reason, "MERGE_TREE_FAILED:128")
        self.assertFalse(marker.exists())
        repo.merge_preview = native
        with self.assertRaises(ReadFailed) as moved:
            admission(head, common, 31)
        self.assertTrue(marker.exists())
        self.assertIn("ADMISSION_READS", moved.exception.reason)
        self.observed.update(native_failure=failed.exception.reason,
                             moved_evidence_failure=moved.exception.reason)

    def test_union_and_unknown_policy_preserve_original_command_flow(self):
        for label, policy in (("union", lambda path: True), ("unknown", None),
                              ("indeterminate", lambda path: None)):
            with self.subTest(policy=label):
                repo, _, head, base, _ = self.real_repository(label, union=lambda path: True)
                repo.union = policy
                command, marker = self.command(label, code=1)
                value = Admission(repo, command=command)(head, base, 31)
                self.observed[label] = value
                self.assertTrue(marker.exists())
                self.assertEqual(value["verdict"], "HELD")
                self.assertIn("check:real-check-failed", value["residuals"])
                self.assertNotIn("native_conflict_base", value)
                self.assertNotIn("checks_not_run", value)
                self.assertEqual(rules.admission_decision(value, lambda path: True),
                                 ("HOLD", None))

    def test_receiver_reopens_new_base_even_with_equal_tree_and_watched_bytes(self):
        owner, repo, _, calls, marker = self.fake_owner()
        first = owner.tick()
        held = owner.load_state()["held"]["31"]
        self.assertEqual(held["native_conflict_base"], repo.main)
        self.assertEqual(len(calls), 1)
        same = owner.tick()
        self.assertEqual(len(calls), 1)
        self.assertIn(31, same["held_unchanged"])
        previous_tree = repo.tree(repo.main)
        repo.main = repo._new(repo.lanes(repo.main), (repo.main,))
        self.assertEqual(repo.tree(repo.main), previous_tree)
        owner.tick()
        self.assertEqual(len(calls), 2)
        self.assertEqual(owner.load_state()["held"]["31"]["native_conflict_base"], repo.main)
        repo.main = repo._new({0, 99}, (repo.main,))
        owner.tick()
        self.assertEqual(len(calls), 3)
        moved_head = repo.move_head(31)
        owner.tick()
        self.assertEqual(len(calls), 4)
        self.assertEqual(calls[-1], [repo.main, moved_head])
        self.assertFalse(marker.exists())
        self.observed.update(first=first, same=same, calls=calls,
                             held=owner.load_state()["held"]["31"])

    def test_resolved_receiver_preserves_real_failure_and_ordinary_source_watch(self):
        owner, repo, native, calls, marker = self.fake_owner()
        owner.tick()
        self.assertFalse(marker.exists())
        native["blocked"] = False
        repo.main = repo._new({0, 99}, (repo.main,))
        resolved = owner.tick()
        state = owner.load_state()
        held = state["held"]["31"]
        self.assertTrue(marker.exists())
        self.assertEqual(held["failing"], ["check:real-check-failed"])
        self.assertNotIn("native_conflict_base", held)
        self.assertEqual(len(calls), 2)
        repo.main = repo._new({0, 99, 100}, (repo.main,))
        unrelated = owner.tick()
        self.assertEqual(len(calls), 2)
        self.assertIn(31, unrelated["held_unchanged"])
        repo.main = repo._new({0, 99, 100, 32}, (repo.main,))
        owner.tick()
        self.assertEqual(len(calls), 3)
        self.assertEqual(owner.load_state()["held"]["31"]["failing"],
                         ["check:real-check-failed"])
        self.observed.update(resolved=resolved, unrelated=unrelated, calls=calls,
                             held=owner.load_state()["held"]["31"])

    def test_receiver_keeps_original_hold_when_fresh_native_read_fails(self):
        owner, repo, native, calls, marker = self.fake_owner()
        owner.tick()
        original_hold = owner.load_state()["held"]["31"]
        repo.main = repo._new(repo.lanes(repo.main), (repo.main,))
        native["failure"] = ReadFailed("NATIVE_SOURCE_UNAVAILABLE")
        summary = owner.tick()
        self.assertEqual(owner.load_state()["held"]["31"], original_hold)
        self.assertIn(31, summary["retry_later"])
        self.assertEqual(len(calls), 2)
        self.assertFalse(marker.exists())
        self.observed.update(summary=summary, original_hold=original_hold, calls=calls,
                             state=owner.load_state())

    def test_cancellation_after_native_read_is_not_changed_to_a_hold(self):
        repo, _, head, base, _ = self.real_repository()
        command, marker = self.command("cancelled")
        cancelled = threading.Event()
        native = repo.merge_preview
        def cancel_after_native(a, b):
            value = native(a, b)
            cancelled.set()
            return value
        repo.merge_preview = cancel_after_native
        with self.assertRaises(ReadFailed) as caught:
            Admission(repo, command=command)(head, base, 31, cancelled=cancelled)
        self.assertEqual(caught.exception.reason, "ADMISSION_CANCELLED")
        self.assertFalse(marker.exists())
        self.observed["failure"] = caught.exception.reason
