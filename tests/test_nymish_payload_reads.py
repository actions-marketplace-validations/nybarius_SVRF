"""Exact payload-presence reads cross admission and reopen only on presence change."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from svrf.admission import _read_basis
from svrf.daemon import Daemon
from svrf.errors import ReadFailed
from svrf.git import RealGit


class PayloadReads(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.git("init", "-q")
        self.git("config", "user.name", "test")
        self.git("config", "user.email", "test@localhost")
        (self.root / "unread.txt").write_text("initial")
        self.commit()
        self.repo = RealGit(self.root)
        self.blob = self.git("hash-object", "-w", "--stdin", input="retained source\n")
        self.payload = "nymish-base-payload:100644:blob:" + self.blob

    def git(self, *args, input=None):
        return subprocess.check_output(["git", "-C", str(self.root), *args], text=True, input=input).strip()

    def commit(self):
        self.git("add", "-A")
        self.git("commit", "-qm", "occurrence")
        return self.git("rev-parse", "HEAD")

    def test_admission_reads_accept_exact_payload_rows_and_refuse_malformed_ones(self):
        path = self.root / "reads.json"
        row = {"kind": "BASE_NYMISH_PAYLOAD", "id": self.payload}
        body = {"schema": "svrf.admission-reads/1", "head": "head", "base": "base", "consumed": [row]}
        path.write_text(json.dumps(body))
        self.assertEqual(_read_basis(path, "head", "base"), [row])
        for value in [self.payload + ":extra", self.payload.replace("100644", "120000"),
                      self.payload.replace(":blob:", ":tree:"), "../outside"]:
            row["id"] = value
            path.write_text(json.dumps(body))
            with self.subTest(value=value), self.assertRaises(ReadFailed):
                _read_basis(path, "head", "base")

    def test_first_arrival_and_last_removal_reopen_but_unread_edits_and_copy_moves_do_not(self):
        old = self.git("rev-parse", "HEAD")
        absent = self.repo.watch_digest(old, [], payloads=[self.payload])
        (self.root / "unread.txt").write_text("unread move")
        self.assertEqual(self.repo.watch_digest(self.commit(), [], payloads=[self.payload]), absent)
        (self.root / "one.nym").write_text("retained source\n")
        present = self.repo.watch_digest(self.commit(), [], payloads=[self.payload])
        self.assertNotEqual(present, absent)
        (self.root / "two.nym").write_text("retained source\n")
        self.assertEqual(self.repo.watch_digest(self.commit(), [], payloads=[self.payload]), present)
        (self.root / "one.nym").unlink()
        self.assertEqual(self.repo.watch_digest(self.commit(), [], payloads=[self.payload]), present)
        self.git("mv", "two.nym", "outside.txt")
        self.assertEqual(self.repo.watch_digest(self.commit(), [], payloads=[self.payload]), absent)

    def test_mode_is_consumed_and_missing_base_is_unobserved(self):
        (self.root / "one.nym").write_text("retained source\n")
        base = self.commit()
        original = self.repo.watch_digest(base, [], payloads=[self.payload])
        self.git("update-index", "--chmod=+x", "one.nym")
        self.git("commit", "-qm", "mode moved")
        self.assertNotEqual(self.repo.watch_digest("HEAD", [], payloads=[self.payload]), original)
        with self.assertRaises(ReadFailed):
            self.repo.watch_digest("missing-base", [], payloads=[self.payload])

    def test_daemon_carries_the_presence_read_into_its_held_reopening(self):
        owner = object.__new__(Daemon)
        owner.git, owner.admission_watch = self.repo, []
        value = {"consumed": [{"kind": "BASE_NYMISH_PAYLOAD", "id": self.payload}]}
        payloads = owner._consumed_payloads(value)
        self.assertEqual(payloads, [self.payload])
        base = self.git("rev-parse", "HEAD")
        kept = {"head": "head", **owner._watch(base, [], payloads=payloads)}
        old = kept["watch_digest"]
        (self.root / "unread.txt").write_text("unread move")
        newer = self.commit()
        self.assertEqual(owner.watch_digest(kept, {"headRefOid": "head"}, {"sha": newer}), old)
        (self.root / "one.nym").write_text("retained source\n")
        self.assertNotEqual(owner.watch_digest(kept, {"headRefOid": "head"}, {"sha": self.commit()}), old)
