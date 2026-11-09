"""The same original Git read must survive a large redundant consumed family."""
import errno
import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from svrf.daemon import Daemon
from svrf.git import RealGit


class WatchPathFactor(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.git("init", "-q")
        self.git("config", "user.name", "test")
        self.git("config", "user.email", "test@localhost")
        for name in ["read/one", "read/deep/two", "read.extra", "unread/three",
                     "quote\nname", "unicode-\u00e9"]:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(name + "\n")
        self.base = self.commit()
        self.repo = RealGit(self.root)

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.root), *args], text=True).strip()

    def commit(self):
        self.git("add", "-A")
        self.git("commit", "-qm", "source occurrence")
        return self.git("rev-parse", "HEAD")

    def original(self, commit, paths):
        listing = self.git("ls-tree", "-r", "--full-tree", commit, "--", *sorted(set(paths)))
        return "sha256:" + hashlib.sha256(listing.encode()).hexdigest()

    def test_large_consumed_family_preserves_original_digest_and_selective_reopening(self):
        # These absent child reads are already consumed by the retained directory read.
        paths = ["read"] + ["read/absent-%05d/%s" % (i, "x" * 128) for i in range(20000)]
        self.assertGreater(sum(len(p) + 1 for p in paths), 2 * 1024 * 1024)
        expected = self.original(self.base, ["read"])
        self.assertEqual(self.repo.watch_digest(self.base, paths), expected)
        (self.root / "unread/three").write_text("unread changed")
        self.assertEqual(self.repo.watch_digest(self.commit(), paths), expected)
        (self.root / "read/one").write_text("consumed changed")
        changed = self.commit()
        self.assertNotEqual(self.repo.watch_digest(changed, paths), expected)
        self.assertEqual(self.repo.watch_digest(changed, paths), self.original(changed, ["read"]))

    def test_literal_prefix_boundaries_quotes_and_absence_keep_git_semantics(self):
        paths = ["read", "read/one", "read/", "read/deep/two", "read.extra",
                 "absent", "absent/later", "quote\nname", "unicode-\u00e9"]
        expected = self.original(self.base, paths)
        self.assertEqual(self.repo.watch_digest(self.base, paths), expected)
        (self.root / "read.extra").write_text("similar prefix is still consumed")
        changed = self.commit()
        self.assertNotEqual(self.repo.watch_digest(changed, paths), expected)
        self.assertEqual(self.repo.watch_digest(changed, paths), self.original(changed, paths))
        (self.root / "absent").mkdir()
        (self.root / "absent/later").write_text("new demanded source")
        arrived = self.commit()
        self.assertNotEqual(self.repo.watch_digest(arrived, paths), self.repo.watch_digest(changed, paths))

    def test_unavailable_transport_retains_hold_basis_and_exact_missing_read(self):
        owner = object.__new__(Daemon)
        owner.git, owner.admission_watch = self.repo, []
        with patch.object(self.repo, "_run", side_effect=OSError(errno.E2BIG, "argument bound")):
            held = owner._watch(self.base, ["read"])
        self.assertEqual(held["watch"], ["read"])
        self.assertEqual(held["base_sha"], self.base)
        self.assertNotIn("watch_digest", held)
        self.assertEqual(held["watch_read_failure"], "WATCH_PATHS_UNREADABLE:7")
        held["head"] = "candidate"
        recovered = owner.watch_digest(held, {"headRefOid": "candidate"}, {"sha": self.base})
        self.assertEqual(recovered, self.original(self.base, ["read"]))
        self.assertNotIn("watch_read_failure", held)



if __name__ == "__main__":
    unittest.main()
