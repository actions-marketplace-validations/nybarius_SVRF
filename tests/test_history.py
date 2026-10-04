"""The history-order check, the ordered re-land, the repair and the admission check, on
real git repositories with a bare origin. Everything is plumbing: nothing is checked out,
and a branch checked out in another worktree is still updated by refspec."""

from __future__ import annotations

import contextlib
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from fakes import git

from svrf import history
from svrf.admission import Admission
from svrf.errors import ReadFailed
from svrf.git import RealGit
from svrf.globs import PathSet

KIND = history.path_kind(PathSet(["tests/**"]), PathSet(["docs/**", "*.md"]))


@contextlib.contextmanager
def elsewhere():
    """Run with the process working directory outside every repository."""
    before = os.getcwd()
    with tempfile.TemporaryDirectory() as away:
        os.chdir(away)
        try:
            yield
        finally:
            os.chdir(before)


class Repo(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.origin = self.tmp / "origin.git"
        self.work = self.tmp / "work"
        self.work.mkdir()
        git(self.tmp, "init", "-q", "--bare", "-b", "main", str(self.origin))
        git(self.work, "init", "-q", "-b", "main")
        git(self.work, "remote", "add", "origin", str(self.origin))
        (self.work / "src").mkdir()
        (self.work / "src" / "app.py").write_text("value = 1\n")
        (self.work / "requirements.txt").write_text("a\n")
        self.commit("base")
        git(self.work, "push", "-q", "origin", "HEAD:refs/heads/main")
        self.base = git(self.work, "rev-parse", "HEAD")

    def commit(self, message):
        git(self.work, "add", "-A")
        git(self.work, "commit", "-qm", message)
        return git(self.work, "rev-parse", "HEAD")

    def write(self, path, text):
        target = self.work / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def rg(self, **kw):
        return RealGit(self.work, **kw)


class Dispositions(Repo):
    def test_tests_then_code_then_docs_is_clean(self):
        git(self.work, "checkout", "-q", "-b", "feature", self.base)
        self.write("tests/test_app.py", "def test(): pass\n")
        self.commit("test: app")
        self.write("src/app.py", "value = 2\n")
        self.commit("feat: app")
        self.write("docs/app.md", "notes\n")
        head = self.commit("docs: app")
        rows = history.dispositions(self.rg(), self.base, head, KIND)
        self.assertEqual([r["disposition"] for r in rows], ["TESTS", "CODE", "DOCS"])
        self.assertEqual(history.verdict(self.rg(), self.base, head, KIND), "CLEAN")

    def test_code_first_is_unordered_and_tests_with_code_is_mixed(self):
        git(self.work, "checkout", "-q", "-b", "feature", self.base)
        self.write("src/app.py", "value = 2\n")
        head = self.commit("feat: app")
        self.assertEqual(history.verdict(self.rg(), self.base, head, KIND), "REFUSED:UNORDERED")
        self.write("src/app.py", "value = 3\n")
        self.write("tests/test_app.py", "def test(): pass\n")
        head = self.commit("feat: both")
        rows = history.dispositions(self.rg(), self.base, head, KIND)
        self.assertEqual([r["disposition"] for r in rows], ["UNORDERED", "MIXED"])

    def test_a_refactor_needs_no_tests_and_an_automatic_merge_authors_nothing(self):
        git(self.work, "checkout", "-q", "-b", "feature", self.base)
        self.write("src/app.py", "value = 1  # tidy\n")
        self.commit("refactor: tidy")
        git(self.work, "checkout", "-q", "main")
        self.write("src/other.py", "x = 1\n")
        self.commit("main moves")
        main = git(self.work, "rev-parse", "HEAD")
        git(self.work, "checkout", "-q", "feature")
        git(self.work, "merge", "-q", "--no-edit", "main")
        head = git(self.work, "rev-parse", "HEAD")
        rows = history.dispositions(self.rg(), main, head, KIND)
        self.assertEqual([r["disposition"] for r in rows], ["REFACTOR", "MERGE"])
        self.assertEqual(history.verdict(self.rg(), main, head, KIND), "CLEAN")


class Relands(Repo):
    def test_the_reland_builds_tests_code_docs_on_the_base_ending_on_the_same_tree(self):
        git(self.work, "checkout", "-q", "-b", "mixed", self.base)
        self.write("tests/test_app.py", "def test(): pass\n")
        self.write("src/app.py", "value = 2\n")
        self.write("docs/note.md", "a note\n")
        head = self.commit("feat: everything at once")
        g = self.rg()
        tree = g.tree(head)
        result = g.reland(self.base, tree, "everything at once", KIND)
        self.assertEqual(result.status, "CLEAN")
        self.assertEqual(result.tree, tree)
        self.assertEqual(len(result.commits), 3)
        rows = history.dispositions(g, self.base, result.commits[-1], KIND)
        self.assertEqual([r["disposition"] for r in rows], ["TESTS", "CODE", "DOCS"])
        subjects = [git(self.work, "log", "-1", "--format=%s", sha) for sha in result.commits]
        self.assertEqual(subjects, ["test: everything at once", "everything at once", "docs: everything at once"])
        self.assertEqual(g.tree(result.commits[-1]), tree)

    def test_a_tests_only_head_omits_the_empty_commits(self):
        git(self.work, "checkout", "-q", "-b", "tests-only", self.base)
        self.write("tests/test_only.py", "def test(): pass\n")
        head = self.commit("feat: a lone test")
        g = self.rg()
        result = g.reland(self.base, g.tree(head), "a lone test", KIND)
        self.assertEqual((result.status, len(result.commits)), ("CLEAN", 1))
        self.assertEqual(git(self.work, "log", "-1", "--format=%s", result.commits[0]), "test: a lone test")

    def test_deleted_files_are_relanded_too(self):
        git(self.work, "checkout", "-q", "-b", "delete", self.base)
        (self.work / "src" / "app.py").unlink()
        self.write("tests/test_gone.py", "def test(): pass\n")
        head = self.commit("feat: delete app")
        g = self.rg()
        result = g.reland(self.base, g.tree(head), "delete app", KIND)
        self.assertEqual(result.status, "CLEAN")
        self.assertEqual(result.tree, g.tree(head))


class RepairPlumbing(Repo):
    def setUp(self):
        super().setUp()
        git(self.work, "checkout", "-q", "-b", "lane", self.base)
        self.write("tests/test_lane.py", "def test(): pass\n")
        self.commit("test: lane")
        self.write("requirements.txt", "a\nlane\n")
        self.commit("feat: lane")
        git(self.work, "push", "-q", "origin", "HEAD:refs/heads/lane")
        git(self.work, "checkout", "-q", "--detach", self.base)
        self.write("requirements.txt", "a\nmain\n")
        self.commit("main")
        git(self.work, "push", "-q", "origin", "HEAD:refs/heads/main")
        git(self.work, "fetch", "-q", "origin")
        git(self.work, "branch", "-q", "-f", "lane", "origin/lane")
        git(self.work, "worktree", "add", "-q", str(self.tmp / "other"), "lane")

    def test_the_repair_pushes_a_union_merge_while_the_branch_is_checked_out_elsewhere(self):
        g = self.rg(union=PathSet(["requirements*.txt"]))
        with elsewhere():
            main = g.main_sha()
            head = git(self.work, "rev-parse", "origin/lane")
            self.assertEqual(RealGit(self.work).union_step(main, head, "plain").status, "CONFLICT")
            step = g.repair_step(main, head, "Merge main into lane")
            self.assertEqual(step.status, "CLEAN")
            g.push_branch(step.commit, "lane")
        pushed = git(self.origin, "rev-parse", "refs/heads/lane")
        self.assertEqual(pushed, step.commit)
        self.assertEqual(g.parents(step.commit), [head, main])
        text = git(self.work, "cat-file", "-p", f"{step.commit}:requirements.txt")
        self.assertEqual(text.splitlines(), ["a", "lane", "main"])
        rows = history.dispositions(g, main, step.commit, KIND)
        self.assertEqual([r["disposition"] for r in rows], ["TESTS", "CODE", "MERGE"])

    def test_the_admission_check_names_the_conflict_and_the_history(self):
        g = self.rg(union=PathSet(["requirements*.txt"]))
        main = g.main_sha()
        head = git(self.work, "rev-parse", "origin/lane")
        value = Admission(g, order="tests-first", kind=KIND)(head, main)
        self.assertEqual(value["verdict"], "HELD")
        self.assertEqual(value["residuals"], ["merge:CONFLICT:requirements.txt"])
        self.assertEqual(sorted(value["changed"]), ["requirements.txt", "tests/test_lane.py"])
        self.assertEqual(Admission(g)(main, main)["verdict"], "LANDED")

    def test_the_admission_command_holds_and_a_missing_command_is_a_read_failure(self):
        g = self.rg()
        main = g.main_sha()
        git(self.work, "checkout", "-q", "--detach", main)
        self.write("x.py", "x = 1\n")
        head = self.commit("x")
        ok = Admission(g, command="test -n \"$SVRF_HEAD\"")(head, main)
        self.assertEqual(ok["verdict"], "MERGEABLE")
        held = Admission(g, command="echo 'license header missing'; exit 1")(head, main)
        self.assertEqual(held["residuals"], ["check:license header missing"])
        with self.assertRaises(ReadFailed):
            Admission(g, command="definitely-not-a-command-svrf")(head, main)

    def test_a_timed_out_admission_command_leaves_no_orphan_behind(self):
        """The command runs `bash -c`, and bash starts the real work (uv, python, pytest) as
        children. A timeout that kills bash alone leaves them running: they keep the host's cores,
        every later admission read slows under them and times out in turn, and the train reads
        ADMISSION_COMMAND_TIMEOUT round after round for a head whose intake would pass. The
        command runs in its own session and the timeout kills that whole process group."""
        import os
        import signal
        import time
        g = self.rg()
        main = g.main_sha()
        git(self.work, "checkout", "-q", "--detach", main)
        self.write("x.py", "x = 1\n")
        head = self.commit("x")
        pid_file = Path(self.work) / "child.pid"
        command = f"sleep 30 & echo $! > {pid_file}; wait"
        with self.assertRaises(ReadFailed) as raised:
            Admission(g, command=command, timeout=0.5)(head, main)
        self.assertEqual(str(raised.exception), "ADMISSION_COMMAND_TIMEOUT")
        child = int(pid_file.read_text().strip())
        for _ in range(50):
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            os.kill(child, signal.SIGKILL)
            self.fail(f"the command's child {child} outlived the timeout")

    def test_the_admission_command_can_report_its_own_order_only_refusal_for_reland(self):
        # A command that classifies commit order itself (rather than relying on the
        # built-in tests-first check) reports it under `reland:REFUSED:<class>`, one of
        # the residual prefixes reland_class reads (`history:` is reserved for the
        # built-in check). Every other line from the same command is still wrapped
        # under `check:` so it is never misread as a structured residual.
        g = self.rg()
        main = g.main_sha()
        git(self.work, "checkout", "-q", "--detach", main)
        self.write("x.py", "x = 1\n")
        head = self.commit("x")
        held = Admission(g, command="echo 'reland:REFUSED:UNORDERED'; echo 'also this line'; exit 1")(head, main)
        self.assertEqual(held["residuals"], ["reland:REFUSED:UNORDERED", "check:also this line"])


class AdmissionConcurrency(Repo):
    """Two admission reads for two different heads, run at once. Without a worktree pool
    they would have to share the one clone's working tree and could not run at once
    without one `git checkout` stepping on the other; with `pool=`/`slots=` each read
    gets its own worktree, so two heavy admission commands genuinely overlap and the
    shared clone's own working tree is never touched."""

    def setUp(self):
        super().setUp()
        git(self.work, "checkout", "-q", "-b", "lane-a", self.base)
        self.write("a.py", "a = 1\n")
        self.head_a = self.commit("feat: a")
        git(self.work, "checkout", "-q", "-b", "lane-b", self.base)
        self.write("b.py", "b = 1\n")
        self.head_b = self.commit("feat: b")
        git(self.work, "checkout", "-q", "--detach", self.base)

    def test_two_slow_admission_commands_overlap_each_in_its_own_worktree(self):
        g = self.rg()
        pool = self.tmp / "admission-pool"
        command = 'git checkout -q --detach -f "$SVRF_HEAD" && sleep 0.2 && git rev-parse HEAD'
        a = Admission(g, command=command, pool=pool, slots=2)
        b = Admission(g, command=command, pool=pool, slots=2)
        results: dict = {}
        errors: list = []

        def run(name, admission, head):
            try:
                results[name] = admission(head, self.base)
            except Exception as error:  # noqa: BLE001 - surfaced via `errors` below
                errors.append(error)

        started = time.monotonic()
        threads = [threading.Thread(target=run, args=("a", a, self.head_a)),
                  threading.Thread(target=run, args=("b", b, self.head_b))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        elapsed = time.monotonic() - started
        self.assertFalse(errors, errors)
        self.assertLess(elapsed, 0.35)
        self.assertEqual(results["a"]["verdict"], "MERGEABLE")
        self.assertEqual(results["b"]["verdict"], "MERGEABLE")
        # the shared clone's own working tree was never checked out by either read
        self.assertEqual(git(self.work, "rev-parse", "HEAD"), self.base)
        self.assertEqual(len([p for p in pool.iterdir() if p.is_dir()]), 2)


class GlobSemantics(unittest.TestCase):
    def test_globs(self):
        s = PathSet(["tests/**", "*.md", "src/**/gen_*.py", "requirements*.txt"])
        for path in ("tests/a.py", "tests/x/y.py", "README.md", "docs/a/b.md", "src/gen_a.py",
                     "src/x/y/gen_b.py", "requirements-dev.txt", "pkg/requirements.txt"):
            self.assertTrue(s(path), path)
        for path in ("src/a.py", "testsx/a.py", "a.mdx", "src/x/gen.py"):
            self.assertFalse(s(path), path)
        self.assertFalse(PathSet([]))
        self.assertFalse(PathSet([])("anything"))


if __name__ == "__main__":
    unittest.main()
