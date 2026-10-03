"""An optional tree provider for the family merges: an alternative merge engine whose
proposed tree is cross-checked against git's own merge tree on every merge step the
train builds. Git's tree is always computed; the provider's tree is used only when it
is the identical tree, and anything else (a different tree, a failure, a timeout,
unreadable output) keeps git's tree for that step and records why. Unset, nothing
changes."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from fakes import Admission, Clock, DaemonGitHub, DaemonRepo, FakeGate, FakeGitHub, FakeRepo, git, is_union

from svrf.config import ConfigError, from_dict
from svrf.daemon import Daemon
from svrf.git import RealGit
from svrf.train import Train
from svrf.tree_provider import TreeCommand

MINIMAL = {"repo": "example/project", "gate": {"commands": ["make test"]}}


def train(repo, gh, gate, tmp, **kw):
    clock = Clock()
    kw.setdefault("jobs", 1)
    return Train(repo, gh, gate, receipts=Path(tmp), clock=clock, sleep=clock.sleep, poll_seconds=0,
                 is_union=is_union, **kw)


class FakeProvider:
    """Proposes the fake repository's merge tree (the union of both sides' lanes), or a
    wrong tree, or fails, and records every merge step it was asked about."""

    def __init__(self, repo, mode="same"):
        self.repo, self.mode, self.calls = repo, mode, []

    def propose(self, ours, theirs):
        self.calls.append((ours, theirs))
        if self.mode == "fail":
            raise RuntimeError("engine fell over")
        if self.mode == "refuse":
            return None, "PROVIDER_EXIT:1"
        lanes = self.repo.lanes(ours) | self.repo.lanes(theirs)
        if self.mode == "wrong":
            lanes = lanes | {999}
        return "T" + ",".join(str(n) for n in sorted(lanes)), None


class Configuration(unittest.TestCase):
    def test_unset_by_default(self):
        config = from_dict(MINIMAL)
        self.assertEqual(config.merge.tree_command, "")
        self.assertEqual(config.merge.tree_timeout_seconds, 120)

    def test_the_merge_section_takes_a_command_and_a_timeout(self):
        config = from_dict({**MINIMAL, "merge": {"tree_command": "my-engine", "tree_timeout_seconds": 30}})
        self.assertEqual((config.merge.tree_command, config.merge.tree_timeout_seconds), ("my-engine", 30))

    def test_an_unknown_merge_key_or_a_bad_timeout_is_refused(self):
        with self.assertRaises(ConfigError):
            from_dict({**MINIMAL, "merge": {"command": "my-engine"}})
        for bad in (0, -1, "soon", True):
            with self.assertRaises(ConfigError):
                from_dict({**MINIMAL, "merge": {"tree_command": "x", "tree_timeout_seconds": bad}})

    def test_build_wires_a_provider_only_when_a_command_is_set(self):
        from svrf.app import build

        with tempfile.TemporaryDirectory() as tmp:
            base = {**MINIMAL, "clone": str(Path(tmp) / "clone"), "state_dir": str(Path(tmp) / "state")}
            daemon = build(from_dict(base), github=object())
            self.assertNotIn("tree_provider", daemon.train_options)
            daemon = build(from_dict({**base, "merge": {"tree_command": "my-engine",
                                                        "tree_timeout_seconds": 7}}), github=object())
            provider = daemon.train_options["tree_provider"]
            self.assertIsInstance(provider, TreeCommand)
            self.assertEqual((provider.command, provider.timeout), ("my-engine", 7))
            self.assertEqual(provider.root, (Path(tmp) / "clone").resolve())


class TrainWithoutProvider(unittest.TestCase):
    def test_unset_leaves_the_receipt_exactly_as_before(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = FakeRepo([11, 12])
            gh = FakeGitHub(repo)
            receipt = train(repo, gh, FakeGate(repo), tmp).run([11, 12])
        self.assertEqual(gh.merged, [11, 12])
        self.assertNotIn("tree_provider", receipt)
        for family in receipt["families"]:
            for step in family["steps"]:
                self.assertEqual(set(step), {"number", "commit", "tree"})
        for row in receipt["merges"]:
            self.assertNotIn("tree_source", row)


class TrainWithProvider(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_an_identical_tree_is_used_and_every_family_merge_step_consults_the_provider(self):
        repo = FakeRepo([11, 12])
        gh = FakeGitHub(repo)
        provider = FakeProvider(repo)
        receipt = train(repo, gh, FakeGate(repo), self.tmp, tree_provider=provider).run([11, 12])
        self.assertEqual(gh.merged, [11, 12])
        self.assertTrue(all(m["identity"] for m in receipt["merges"]))
        # two steps folded for the plan, two recomputed while landing; the pair read asks nothing
        self.assertEqual(len(provider.calls), 4)
        self.assertEqual([ours for ours, _ in provider.calls], ["h11", "h12", "h11", "h12"])
        steps = receipt["families"][0]["steps"]
        self.assertEqual([s["tree_source"] for s in steps], ["provider", "provider"])
        self.assertEqual([m["tree_source"] for m in receipt["merges"]], ["provider", "provider"])
        self.assertEqual(receipt["tree_provider"], {"consulted": 4, "provider": 4, "git": 0, "reasons": {}})

    def test_a_different_tree_keeps_gits_tree_records_the_mismatch_and_still_lands(self):
        repo = FakeRepo([11, 12])
        gh = FakeGitHub(repo)
        receipt = train(repo, gh, FakeGate(repo), self.tmp, tree_provider=FakeProvider(repo, "wrong")).run([11, 12])
        self.assertEqual(gh.merged, [11, 12])
        self.assertEqual(repo.tree(repo.main), "T0,11,12")
        self.assertTrue(all(m["identity"] for m in receipt["merges"]))
        self.assertEqual(receipt["alerts"], [])
        step = receipt["families"][0]["steps"][0]
        self.assertEqual((step["tree_source"], step["tree_provider"]), ("git", "PROVIDER_MISMATCH"))
        self.assertEqual(step["tree"], "T0,11")
        self.assertEqual(step["proposed_tree"], "T0,11,999")
        self.assertEqual(receipt["tree_provider"],
                         {"consulted": 4, "provider": 0, "git": 4, "reasons": {"PROVIDER_MISMATCH": 4}})

    def test_a_failing_or_refusing_provider_never_fails_the_landing(self):
        for mode, reason in (("fail", "PROVIDER_FAILED:RuntimeError"), ("refuse", "PROVIDER_EXIT:1")):
            with self.subTest(mode=mode):
                repo = FakeRepo([11, 12])
                gh = FakeGitHub(repo)
                receipt = train(repo, gh, FakeGate(repo), tempfile.mkdtemp(),
                                tree_provider=FakeProvider(repo, mode)).run([11, 12])
                self.assertEqual(gh.merged, [11, 12])
                self.assertEqual([m["tree_source"] for m in receipt["merges"]], ["git", "git"])
                self.assertEqual([m["tree_provider"] for m in receipt["merges"]], [reason, reason])
                self.assertEqual(receipt["tree_provider"]["reasons"], {reason: 4})

    def test_a_step_git_could_not_merge_is_never_offered_to_the_provider(self):
        repo = FakeRepo([11, 12], main_conflicts={11: ["src/x.py"]})
        gh = FakeGitHub(repo)
        provider = FakeProvider(repo)
        receipt = train(repo, gh, FakeGate(repo), self.tmp, tree_provider=provider).run([11, 12])
        self.assertEqual(gh.merged, [12])
        self.assertEqual([h["number"] for h in receipt["holds"]], [11])
        self.assertNotIn("h11", [ours for ours, _ in provider.calls])
        self.assertEqual(receipt["tree_provider"]["consulted"], 2)

    def test_the_round_summary_and_state_carry_the_counts(self):
        repo = DaemonRepo([11, 12])
        gh = DaemonGitHub(repo)
        clock = Clock()
        d = Daemon(repo, gh, FakeGate(repo), Admission(), state_dir=Path(self.tmp), receipts=Path(self.tmp) / "r",
                   clock=clock, sleep=clock.sleep, is_union=is_union,
                   train_options={"poll_seconds": 0, "tree_provider": FakeProvider(repo)})
        out = d.tick()
        self.assertEqual(out["merged"], [11, 12])
        self.assertEqual(out["tree_provider"], {"consulted": 4, "provider": 4, "git": 0, "reasons": {}})
        state = json.loads((Path(self.tmp) / "state.json").read_text())
        self.assertEqual(state["last_tick"]["tree_provider"]["provider"], 4)

    def test_without_a_provider_the_round_summary_is_unchanged(self):
        repo = DaemonRepo([11])
        gh = DaemonGitHub(repo)
        clock = Clock()
        d = Daemon(repo, gh, FakeGate(repo), Admission(), state_dir=Path(self.tmp), receipts=Path(self.tmp) / "r",
                   clock=clock, sleep=clock.sleep, is_union=is_union, train_options={"poll_seconds": 0})
        out = d.tick()
        self.assertEqual(out["merged"], [11])
        self.assertNotIn("tree_provider", out)


class RealCommand(unittest.TestCase):
    """The command against a real clone: what it is given, and how its answer is read."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.work = self.tmp / "work"
        self.work.mkdir()
        git(self.work, "init", "-q", "-b", "main")
        (self.work / "base.txt").write_text("base\n")
        git(self.work, "add", ".")
        git(self.work, "commit", "-qm", "base")
        self.base = git(self.work, "rev-parse", "HEAD")
        for lane in ("a", "b"):
            git(self.work, "checkout", "-q", "-b", lane, self.base)
            (self.work / f"{lane}.txt").write_text(lane)
            git(self.work, "add", ".")
            git(self.work, "commit", "-qm", lane)
        self.a = git(self.work, "rev-parse", "a")
        self.b = git(self.work, "rev-parse", "b")
        self.git_tree = git(self.work, "merge-tree", "--write-tree", self.b, self.a).splitlines()[0]
        self.echo_git = 'git merge-tree --write-tree "$SVRF_MERGE_OURS" "$SVRF_MERGE_THEIRS" | head -1'

    def test_the_command_sees_ours_theirs_and_the_merge_bases_in_the_clone(self):
        seen = self.tmp / "seen.txt"
        command = f'printf "%s\\n" "$SVRF_MERGE_OURS" "$SVRF_MERGE_THEIRS" "$SVRF_MERGE_BASES" "$PWD" > {seen}; ' \
                  + self.echo_git
        tree, reason = TreeCommand(command, self.work, timeout=30).propose(self.b, self.a)
        self.assertEqual((tree, reason), (self.git_tree, None))
        ours, theirs, bases, cwd = seen.read_text().splitlines()
        self.assertEqual((ours, theirs, bases), (self.b, self.a, self.base))
        self.assertEqual(Path(cwd).resolve(), self.work.resolve())

    def test_a_failure_a_timeout_or_unreadable_output_is_a_reason_never_a_tree(self):
        cases = (("exit 3", "PROVIDER_EXIT:3"),
                 ("echo not-a-tree", "PROVIDER_OUTPUT_INVALID"),
                 ("true", "PROVIDER_OUTPUT_INVALID"),
                 ("sleep 20", "PROVIDER_TIMEOUT"))
        for command, expected in cases:
            with self.subTest(command=command):
                started = time.monotonic()
                tree, reason = TreeCommand(command, self.work, timeout=0.5).propose(self.b, self.a)
                self.assertIsNone(tree)
                self.assertTrue(reason.startswith(expected), reason)
                self.assertLess(time.monotonic() - started, 10)

    def test_through_the_train_git_keeps_the_step_when_the_proposal_differs(self):
        mismatch = TreeCommand('git rev-parse "$SVRF_MERGE_OURS^{tree}"', self.work, timeout=30)
        same = TreeCommand(self.echo_git, self.work, timeout=30)
        for provider, source in ((same, "provider"), (mismatch, "git")):
            with self.subTest(source=source):
                t = Train(RealGit(self.work), None, None, receipts=self.tmp / source,
                          pr_comments=False, status_checks=False, tree_provider=provider)
                t.rows[2] = {"number": 2, "head_sha": self.b}
                family = t.plan([2], self.a)
                step = family["steps"][0]
                self.assertEqual(step["tree"], self.git_tree)
                self.assertEqual(step["tree_source"], source)
                self.assertEqual(git(self.work, "rev-parse", f"{step['commit']}^{{tree}}"), self.git_tree)


if __name__ == "__main__":
    unittest.main()
