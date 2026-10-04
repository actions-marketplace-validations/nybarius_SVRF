"""The union merge of a configured path is git's own union merge, byte for byte.

Each case builds a base, a pull request side ("ours") and a base-branch side ("theirs")
of one file in a real repository and runs the union step on it. The result is compared
with git itself in two ways: `git merge-file --union` on the three versions, and a git
merge of the same three commits in a repository whose `.gitattributes` declares
`merge=union` for the file (what `git merge` does with that attribute). Neither
reference repository is the one the union step runs in.
"""

from __future__ import annotations

import os
import random
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from fakes import IDENT, git

from svrf import rules
from svrf.git import RealGit
from svrf.globs import PathSet


def _raw(cwd: Path, *args: str, input: bytes | None = None, ok=(0,)) -> bytes:
    done = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, input=input,
                          env={**os.environ, **IDENT})
    if done.returncode not in ok:
        raise AssertionError(f"git {' '.join(args)}: {done.returncode}: {done.stderr!r}")
    return done.stdout


def _histogram() -> list[str]:
    """`git merge` runs its content merges with the histogram diff. `git merge-file` takes
    that option where this git offers it; the fixed cases below merge the same either way."""
    usage = subprocess.run(["git", "merge-file", "-h"], capture_output=True, text=True)
    return ["--diff-algorithm=histogram"] if "diff-algorithm" in usage.stdout + usage.stderr else []


class Repository:
    """A scratch repository whose commits are built by plumbing from {path: bytes}."""

    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True)
        git(root, "init", "-q", "-b", "main")

    def commit(self, files: dict[str, bytes], parent: str | None = None) -> str:
        index = self.root / ".git" / "scratch-index"
        if index.exists():
            index.unlink()
        env = {"GIT_INDEX_FILE": str(index)}
        for path, body in files.items():
            blob = _raw(self.root, "hash-object", "-w", "--stdin", input=body).decode().strip()
            git(self.root, "update-index", "--add", "--cacheinfo", f"100644,{blob},{path}", env=env)
        tree = git(self.root, "write-tree", env=env)
        index.unlink(missing_ok=True)
        return git(self.root, "commit-tree", tree, "-m", "c", *(["-p", parent] if parent else []))

    def blob(self, commit_or_tree: str, path: str) -> bytes:
        return _raw(self.root, "cat-file", "blob", f"{commit_or_tree}:{path}")


class UnionIsGitsUnion(unittest.TestCase):
    PATH = "index.txt"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.count = 0

    # ---- the three readings of one case

    def _sides(self, repo: Repository, base, ours, theirs, path, extra=None):
        common = dict(extra or {})
        if base is not None:
            common[path] = base
        root = repo.commit(common)
        return (repo.commit({**common, path: ours}, root), repo.commit({**common, path: theirs}, root))

    def svrf_union(self, base, ours, theirs, path=None, extra=None) -> bytes:
        path = path or self.PATH
        self.count += 1
        repo = Repository(self.tmp / f"svrf{self.count}")
        head, acc = self._sides(repo, base, ours, theirs, path, extra)
        g = RealGit(repo.root, union=PathSet([path]))
        step = g.union_step(acc, head, "Merge main into lane")
        self.assertEqual(step.status, "CLEAN", step)
        self.assertEqual(g.parents(step.commit), [head, acc])
        return repo.blob(step.tree, path)

    def merge_file_union(self, base, ours, theirs) -> bytes:
        scratch = Path(tempfile.mkdtemp(dir=self.tmp))
        names = []
        for role, body in (("ours", ours), ("base", base or b""), ("theirs", theirs)):
            (scratch / role).write_bytes(body)
            names.append(str(scratch / role))
        done = subprocess.run(["git", "merge-file", "-p", "--union", *_histogram(), *names], capture_output=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout

    def attribute_union(self, base, ours, theirs, path=None) -> bytes:
        """git's merge of the same three versions with `merge=union` declared for the path."""
        path = path or self.PATH
        self.count += 1
        repo = Repository(self.tmp / f"attr{self.count}")
        attrs = {".gitattributes": f"/{path} merge=union\n".encode()}
        head, acc = self._sides(repo, base, ours, theirs, path, attrs)
        done = subprocess.run(["git", "-C", str(repo.root), "-c", f"attr.tree={acc}", "merge-tree", "--write-tree",
                               head, acc], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        return repo.blob(done.stdout.split("\n", 1)[0].strip(), path)

    def assertGitUnion(self, base, ours, theirs, *, expected: bytes | None = None):
        got = self.svrf_union(base, ours, theirs)
        self.assertEqual(got, self.merge_file_union(base, ours, theirs))
        self.assertEqual(got, self.attribute_union(base, ours, theirs))
        if expected is not None:
            self.assertEqual(got, expected)

    # ---- fixed cases

    def test_insertions_at_the_same_spot_keep_ours_then_theirs_in_place(self):
        self.assertGitUnion(b"a\nb\nc\n", b"a\nb\nO1\nO2\nc\n", b"a\nb\nT1\nc\n",
                            expected=b"a\nb\nO1\nO2\nT1\nc\n")

    def test_the_pull_request_side_comes_first_within_a_hunk_not_after_the_whole_base(self):
        self.assertGitUnion(b"a\nm\nz\n", b"a\nm\nO\nz\n", b"a\nm\nT\nz\nT2\n",
                            expected=b"a\nm\nO\nT\nz\nT2\n")

    def test_both_sides_append_at_the_end(self):
        self.assertGitUnion(b"a\n", b"a\nO\n", b"a\nT\n", expected=b"a\nO\nT\n")

    def test_interleaved_hunks(self):
        base = b"".join(b"%d\n" % i for i in range(1, 13))
        ours = base.replace(b"2\n", b"2o\n").replace(b"5\n", b"5o\n").replace(b"9\n", b"9o\n9p\n")
        theirs = base.replace(b"2\n", b"2t\n").replace(b"6\n", b"6t\n").replace(b"9\n", b"9t\n")
        self.assertGitUnion(base, ours, theirs)

    def test_clean_hunks_and_a_union_hunk_in_one_file(self):
        base = b"head\none\ntwo\nthree\nfour\nfive\ntail\n"
        ours = b"head\none\nTWO\nthree\nfour\nfive-o\ntail\n"
        theirs = b"head\nthree\nfour\nfive-t\ntail\nmore\n"
        self.assertGitUnion(base, ours, theirs)

    def test_a_deletion_on_one_side_is_kept(self):
        self.assertGitUnion(b"a\ngone\nb\nc\n", b"a\nb\nc\nO\n", b"a\ngone\nb\nc\nT\n",
                            expected=b"a\nb\nc\nO\nT\n")

    def test_repeated_lines_are_not_collapsed(self):
        self.assertGitUnion(b"x\n", b"x\nO\nO\n", b"x\nT\nO\n")

    def test_a_line_both_sides_add_appears_as_git_puts_it(self):
        self.assertGitUnion(b"a\nc\n", b"a\nb\nO\nc\n", b"a\nb\nT\nc\n")

    def test_one_side_empties_the_file(self):
        self.assertGitUnion(b"a\nb\n", b"", b"a\nb\nT\n")

    def test_no_trailing_newline_on_ours(self):
        self.assertGitUnion(b"a\nb\n", b"a\nb\nO", b"a\nb\nT\n")

    def test_no_trailing_newline_on_theirs(self):
        self.assertGitUnion(b"a\nb\n", b"a\nb\nO\n", b"a\nb\nT")

    def test_no_trailing_newline_anywhere(self):
        self.assertGitUnion(b"a\nb", b"a\nb\nO", b"a\nb\nT")

    def test_a_trailing_newline_added_on_one_side_only(self):
        self.assertGitUnion(b"a\nb", b"a\nb\n", b"a\nb\nT")

    def test_crlf_lines_stay_crlf(self):
        self.assertGitUnion(b"a\r\nb\r\n", b"a\r\nO\r\nb\r\n", b"a\r\nT\r\nb\r\n",
                            expected=b"a\r\nO\r\nT\r\nb\r\n")

    def test_mixed_line_endings(self):
        self.assertGitUnion(b"a\r\nb\nc\r\n", b"a\r\nb\nO\nc\r\n", b"a\r\nb\nT\r\nc\r\n")

    def test_bytes_that_are_not_utf8(self):
        self.assertGitUnion(b"a\n\xff\xfe\n", b"a\n\xff\xfe\nO \xe9\n", b"a\n\xff\xfe\nT \x80\n")

    def test_a_file_added_on_both_sides(self):
        self.assertGitUnion(None, b"x\ny\n", b"x\nz\n", expected=b"x\ny\nz\n")

    def test_a_path_with_characters_special_to_attribute_patterns(self):
        path = "sub dir/a b*[1]?.txt"
        got = self.svrf_union(b"a\n", b"a\nO\n", b"a\nT\n", path=path)
        self.assertEqual(got, b"a\nO\nT\n")

    def test_a_directory_attributes_file_that_declares_another_merge(self):
        extra = {"sub/.gitattributes": b"*.txt merge=binary\n"}
        got = self.svrf_union(b"a\n", b"a\nO\n", b"a\nT\n", path="sub/list.txt", extra=extra)
        self.assertEqual(got, b"a\nO\nT\n")

    def test_seeded_random_cases_match_gits_merge_with_the_union_attribute(self):
        rng = random.Random(20261004)
        words = ["a", "b", "c", "d", "{", "}", "x", "y", ""]
        for _ in range(40):
            base = [rng.choice(words) for _ in range(rng.randint(0, 10))]

            def edit(lines):
                lines = list(lines)
                for _ in range(rng.randint(1, 4)):
                    at = rng.randint(0, len(lines))
                    roll = rng.random()
                    if roll < 0.5 or not lines:
                        lines.insert(at, rng.choice(words + [f"n{rng.randint(0, 3)}"]))
                    elif roll < 0.8:
                        del lines[min(at, len(lines) - 1)]
                    else:
                        lines[min(at, len(lines) - 1)] = rng.choice(words)
                return lines

            def body(lines):
                end = rng.choice(["\n", "\n", "\n", "", "\r\n"])
                return (end if end else "\n").join(lines).encode() + (end.encode() if lines else b"")

            b, o, t = body(base), body(edit(base)), body(edit(base))
            with self.subTest(base=b, ours=o, theirs=t):
                self.assertEqual(self.svrf_union(b, o, t), self.attribute_union(b, o, t))

    # ---- what still conflicts

    def test_a_union_path_deleted_on_one_side_still_conflicts(self):
        repo = Repository(self.tmp / "delete")
        root = repo.commit({self.PATH: b"a\n", "keep.txt": b"k\n"})
        head = repo.commit({self.PATH: b"a\nO\n", "keep.txt": b"k\n"}, root)
        acc = repo.commit({"keep.txt": b"k\n"}, root)
        step = RealGit(repo.root, union=PathSet([self.PATH])).union_step(acc, head, "m")
        self.assertEqual((step.status, step.conflicts), ("CONFLICT", [self.PATH]))

    def test_a_conflict_on_another_path_still_conflicts(self):
        repo = Repository(self.tmp / "other")
        root = repo.commit({self.PATH: b"a\n", "code.py": b"x = 0\n"})
        head = repo.commit({self.PATH: b"a\nO\n", "code.py": b"x = 1\n"}, root)
        acc = repo.commit({self.PATH: b"a\nT\n", "code.py": b"x = 2\n"}, root)
        step = RealGit(repo.root, union=PathSet([self.PATH])).union_step(acc, head, "m")
        self.assertEqual((step.status, step.conflicts), ("CONFLICT", ["code.py"]))


class UnionAttributes(unittest.TestCase):
    """The pure part: declaring git's union merge for given files in one directory's
    `.gitattributes`, after everything that file already declares."""

    def test_appends_after_existing_lines(self):
        self.assertEqual(rules.union_attributes("*.md text\n", ["list.txt"]),
                         "*.md text\n/list.txt merge=union\n")

    def test_terminates_an_unterminated_last_line(self):
        self.assertEqual(rules.union_attributes("*.md text", ["list.txt"]),
                         "*.md text\n/list.txt merge=union\n")

    def test_escapes_pattern_characters_and_quotes_whitespace(self):
        self.assertEqual(rules.union_attributes("", ["a*b?[c]\\d.txt"]),
                         "/a\\*b\\?\\[c]\\\\d.txt merge=union\n")
        self.assertEqual(rules.union_attributes("", ['a b"c.txt']),
                         '"/a b\\"c.txt" merge=union\n')

    def test_each_name_once_in_order(self):
        self.assertEqual(rules.union_attributes("", ["b", "a", "b"]),
                         "/a merge=union\n/b merge=union\n")


if __name__ == "__main__":
    unittest.main()
