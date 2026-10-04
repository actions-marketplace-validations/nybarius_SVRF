"""The train's own clone: fetches, merge previews, re-lands and pushes.

Every git call names its repository (`git -C <clone>`), so nothing depends on the
process working directory. No step checks a branch out: commits are made with plumbing
(`merge-tree`, `commit-tree`, a temporary index) and pushed by refspec, so a branch that
is checked out in someone's worktree is never touched there.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .errors import ReadFailed
from .redact import redact
from .rules import union_attributes


@dataclass
class Step:
    """One union step: CLEAN with the commit and tree it made, CONFLICT with the paths,
    or FAILED with the read failure."""

    status: str
    commit: str | None = None
    tree: str | None = None
    conflicts: list[str] = field(default_factory=list)
    reason: str | None = None


@dataclass
class Reland:
    """An ordered re-land: CLEAN with its (up to three) linear commits and final tree,
    MISMATCH when the rebuild did not reproduce the demanded tree, or FAILED."""

    status: str
    commits: list[str] = field(default_factory=list)
    tree: str | None = None
    reason: str | None = None


def _never(path: str) -> bool:
    return False


class RealGit:
    def __init__(self, root: Path | str, *, identity=("svrf", "svrf@localhost"), remote: str = "origin",
                 base: str = "main", union: Callable[[str], bool] = _never):
        self.root = Path(root).expanduser().resolve()
        self.remote, self.base, self.union = remote, base, union
        name, email = identity
        self.env = {**os.environ, "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
                    "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email}

    # ---- plumbing

    def _run(self, *args, input: str | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(self.root), *args], capture_output=True, text=True, input=input,
                              env=env or self.env)

    def _raw(self, *args, input: bytes | None = None) -> subprocess.CompletedProcess:
        """`_run` with bytes in and out: file contents and paths exactly as git holds them."""
        return subprocess.run(["git", "-C", str(self.root), *args], capture_output=True, input=input, env=self.env)

    def _out(self, *args, input: str | None = None, env: dict | None = None) -> str:
        done = self._run(*args, input=input, env=env)
        if done.returncode != 0:
            stderr = redact(done.stderr.strip(), env or self.env)
            raise ReadFailed(f"GIT_FAILED:{args[0]}:{done.returncode}:{stderr[:160]}")
        return done.stdout.strip()

    # ---- reads

    def fetch(self, numbers: list[int]) -> None:
        refspecs = [f"+refs/heads/{self.base}:refs/remotes/{self.remote}/{self.base}"]
        refspecs += [f"+refs/pull/{n}/head:refs/remotes/{self.remote}/pr/{n}" for n in numbers]
        self._out("fetch", "-q", self.remote, *refspecs)

    def fetch_commit(self, sha: str) -> None:
        """Fetch the base branch and one commit by sha (GitHub serves any reachable commit)."""
        self._out("fetch", "-q", self.remote, f"+refs/heads/{self.base}:refs/remotes/{self.remote}/{self.base}", sha)

    def main_sha(self) -> str:
        """The base branch's commit on the remote, read live (`ls-remote`)."""
        line = self._out("ls-remote", self.remote, f"refs/heads/{self.base}")
        if not line:
            raise ReadFailed("BASE_UNREADABLE")
        sha = line.split()[0]
        if self._run("cat-file", "-e", f"{sha}^{{commit}}").returncode != 0:
            self.fetch([])
        return sha

    def tree(self, commit: str) -> str:
        if self._run("rev-parse", f"{commit}^{{tree}}").returncode != 0:
            self.fetch([])
        return self._out("rev-parse", f"{commit}^{{tree}}")

    def parents(self, commit: str) -> list[str]:
        return self._out("rev-list", "--parents", "-n", "1", commit).split()[1:]

    def is_ancestor(self, a: str, b: str) -> bool:
        return self._run("merge-base", "--is-ancestor", a, b).returncode == 0

    def changed(self, base: str, commit: str) -> list[tuple[str, str]]:
        text = self._out("diff", "--name-status", "--no-renames", base, commit)
        return [tuple(line.split("\t", 1)) for line in text.splitlines() if "\t" in line]

    def changed_paths(self, base: str, head: str) -> list[str]:
        """Paths the pull request changes relative to its merge base with `base`."""
        return [p for p in self._out("diff", "--name-only", "--no-renames", f"{base}...{head}").splitlines() if p]

    def watch_digest(self, commit: str, paths: list[str]) -> str:
        """A digest of `commit`'s content over `paths`: equal digests, equal content there."""
        listing = self._out("ls-tree", "-r", "--full-tree", commit, "--", *sorted(set(paths))) if paths else ""
        return "sha256:" + hashlib.sha256(listing.encode()).hexdigest()

    def merge_preview(self, a: str, b: str) -> dict:
        """git's own merge of two commits, without union resolution: CLEAN with its tree,
        CONFLICT with the conflicted paths, or FAILED.

        The attributes are read from `a` (the base side of every real call), never from
        whatever this clone happens to have checked out: `git merge-tree` otherwise reads
        .gitattributes from the checkout, which a train clone never keeps aligned with
        either side of the merge, so a merge=nym path is missed and reported as an
        ordinary text conflict."""
        done = self._run("-c", f"attr.tree={a}", "merge-tree", "--write-tree", "--name-only", "--no-messages", a, b)
        if done.returncode == 0:
            return {"status": "CLEAN", "tree": done.stdout.split("\n", 1)[0].strip(), "conflicts": []}
        if done.returncode == 1:
            lines = [line for line in done.stdout.split("\n")[1:] if line.strip()]
            return {"status": "CONFLICT", "tree": None, "conflicts": sorted(set(lines))}
        return {"status": "FAILED", "tree": None, "conflicts": [], "reason": f"MERGE_TREE_FAILED:{done.returncode}"}

    def pairs(self, rows: list[dict], base: str) -> dict:
        """Which two heads conflict, read off git's merge of every unordered pair of heads
        not already in `base`. No gate runs."""
        live = [r for r in rows if not self.is_ancestor(r["head_sha"], base)]
        live.sort(key=lambda r: r["number"])
        read, conflicts, unreadable = 0, [], []
        for i, left in enumerate(live):
            for right in live[i + 1:]:
                read += 1
                merge = self.merge_preview(left["head_sha"], right["head_sha"])
                if merge["status"] == "CONFLICT":
                    conflicts.append({"a": left["number"], "b": right["number"], "paths": merge["conflicts"]})
                elif merge["status"] != "CLEAN":
                    unreadable.append({"a": left["number"], "b": right["number"], "reason": merge["reason"]})
        return {"read": read, "conflicts": conflicts, "unreadable": unreadable}

    # ---- the union step

    def union_step(self, acc: str, head: str, message: str, union: Callable[[str], bool] | None = None) -> Step:
        """Merge `acc` (the base branch, or the fold so far) into the pull request `head`:
        git's merge with the pull request as ours and the base as theirs, committed with
        parents (head, acc) exactly as `git merge <base>` on the branch would. When every
        path that merge leaves conflicted is a configured union-merge path, the merge is
        made again by git with `merge=union` declared for those paths, so each is git's own
        union merge, byte for byte what `.gitattributes` declaring it would give."""
        union = union or self.union
        try:
            if self.is_ancestor(acc, head):
                return Step("CLEAN", commit=head, tree=self.tree(head))
            if self.is_ancestor(head, acc):
                return Step("CLEAN", commit=acc, tree=self.tree(acc))
            # Same attribute-source fix as `merge_preview`: read .gitattributes from `acc`
            # (the base/theirs side), never from this clone's checkout.
            code, tree, stages = self._merge_tree(acc, head, acc)
            if code == 1:
                other = sorted(p for p in stages if not union(p))
                if other:
                    return Step("CONFLICT", conflicts=other)
                for path, by_stage in sorted(stages.items()):
                    if 2 not in by_stage or 3 not in by_stage:
                        return Step("CONFLICT", conflicts=[path])
                code, tree, stages = self._merge_tree(self._union_attribute_tree(acc, sorted(stages)), head, acc)
                if code == 1:
                    return Step("CONFLICT", conflicts=sorted(stages))
            commit = self._out("commit-tree", tree, "-p", head, "-p", acc, "-m", message)
            return Step("CLEAN", commit=commit, tree=tree)
        except ReadFailed as failure:
            return Step("FAILED", reason=failure.reason)

    def _merge_tree(self, attributes: str, ours: str, theirs: str) -> tuple[int, str, dict[str, dict[int, str]]]:
        """git's merge of `ours` and `theirs` with .gitattributes read from `attributes`:
        (0 clean or 1 conflicted, the tree, each conflicted path's stages)."""
        done = self._raw("-c", f"attr.tree={attributes}", "merge-tree", "--write-tree", "-z", "--no-messages",
                         ours, theirs)
        if done.returncode not in (0, 1):
            raise ReadFailed(f"MERGE_TREE_FAILED:{done.returncode}")
        fields = done.stdout.split(b"\0")
        tree = fields[0].decode().strip()
        stages: dict[str, dict[int, str]] = {}
        if done.returncode == 1:
            for field in fields[1:]:
                if not field:
                    break
                meta, _, path = field.partition(b"\t")
                _mode, sha, stage = meta.decode().split()
                stages.setdefault(path.decode("utf-8", "surrogateescape"), {})[int(stage)] = sha
        return done.returncode, tree, stages

    def _union_attribute_tree(self, commit: str, paths: list[str]) -> str:
        """`commit`'s tree with `merge=union` declared for `paths`, each in the
        `.gitattributes` of its own directory after whatever that file already declares:
        an attribute source for git's merge, never committed."""
        by_dir: dict[str, list[str]] = {}
        for path in paths:
            folder, _, name = path.rpartition("/")
            by_dir.setdefault(folder, []).append(name)
        sets: dict[str, tuple[str, str] | None] = {}
        for folder, names in by_dir.items():
            attributes = f"{folder}/.gitattributes" if folder else ".gitattributes"
            read = self._raw("cat-file", "blob", f"{commit}:{attributes}")
            text = read.stdout.decode("utf-8", "surrogateescape") if read.returncode == 0 else ""
            body = union_attributes(text, names).encode("utf-8", "surrogateescape")
            written = self._raw("hash-object", "-w", "--stdin", input=body)
            if written.returncode != 0:
                raise ReadFailed(f"GIT_FAILED:hash-object:{written.returncode}")
            sets[attributes] = ("100644", written.stdout.decode().strip())
        return self._overlay_tree(self.tree(commit), sets)

    repair_step = union_step

    def _overlay_tree(self, base_tree: str, sets: dict[str, tuple[str, str] | None]) -> str:
        """`base_tree` with each path set to its (mode, blob) or removed (None), by plumbing
        alone: a temporary index, never a checkout."""
        with tempfile.TemporaryDirectory() as tmp:
            env = {**self.env, "GIT_INDEX_FILE": str(Path(tmp) / "index")}
            self._out("read-tree", base_tree, env=env)
            for path, value in sets.items():
                if value is None:
                    self._run("update-index", "--force-remove", path, env=env)
                else:
                    mode, blob = value
                    self._out("update-index", "--add", "--cacheinfo", f"{mode},{blob},{path}", env=env)
            return self._out("write-tree", env=env)

    def _tree_map(self, tree: str) -> dict[str, tuple[str, str]]:
        text = self._out("ls-tree", "-r", "--full-tree", tree)
        out: dict[str, tuple[str, str]] = {}
        for line in text.splitlines():
            meta, _, path = line.partition("\t")
            mode, kind, blob = meta.split()
            if kind == "blob":
                out[path] = (mode, blob)
        return out

    # ---- ordered re-land

    def reland(self, base: str, target_tree: str, title: str, kind: Callable[[str], str]) -> Reland:
        """Rebuild `target_tree` as up to three linear commits on `base`: the changed test
        files, then the code (everything but docs), then the docs, each built with
        `commit-tree` (no checkout), empty ones omitted. The result is trusted only if the
        last commit's tree is exactly `target_tree`."""
        try:
            base_tree = self.tree(base)
            base_map = self._tree_map(base_tree)
            head_map = self._tree_map(target_tree)
        except ReadFailed as failure:
            return Reland("FAILED", reason=failure.reason)
        changed = sorted(p for p in (set(base_map) | set(head_map)) if base_map.get(p) != head_map.get(p))
        tests = [p for p in changed if kind(p) == "test"]
        docs = [p for p in changed if kind(p) == "doc"]
        try:
            commits: list[str] = []
            parent, parent_tree = base, base_tree
            tree1 = self._overlay_tree(base_tree, {p: head_map.get(p) for p in tests}) if tests else base_tree
            if tree1 != parent_tree:
                parent = self._out("commit-tree", tree1, "-p", parent, "-m", f"test: {title}")
                commits.append(parent)
                parent_tree = tree1
            tree2 = self._overlay_tree(target_tree, {p: base_map.get(p) for p in docs}) if docs else target_tree
            if docs and self._overlay_tree(tree2, {p: head_map.get(p) for p in docs}) != target_tree:
                return Reland("MISMATCH", reason="RELAND_TREE_MISMATCH")
            if tree2 != parent_tree:
                parent = self._out("commit-tree", tree2, "-p", parent, "-m", title)
                commits.append(parent)
                parent_tree = tree2
            if target_tree != parent_tree:
                parent = self._out("commit-tree", target_tree, "-p", parent, "-m", f"docs: {title}")
                commits.append(parent)
                parent_tree = target_tree
        except ReadFailed as failure:
            return Reland("FAILED", reason=failure.reason)
        if parent_tree != target_tree:
            return Reland("MISMATCH", tree=parent_tree, reason="RELAND_TREE_MISMATCH")
        return Reland("CLEAN", commits=commits, tree=parent_tree)

    # ---- writes

    def push_branch(self, commit: str, branch: str) -> None:
        """Advance a branch on the remote to `commit` by refspec (never forced)."""
        done = self._run("push", "-q", self.remote, f"{commit}:refs/heads/{branch}")
        if done.returncode != 0:
            raise ReadFailed(f"PUSH_REFUSED:{branch}:{done.stderr.strip()[:160]}")

    # ---- raw access for the history check

    def out(self, *args: str) -> str:
        return self._out(*args)
