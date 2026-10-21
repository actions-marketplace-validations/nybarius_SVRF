"""Gate preparation materializes a new slot once and restores reused source."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fakes import git
from svrf.gate import CommandGate


class SameSourceGate(unittest.TestCase):
    def test_initial_preparation_does_not_repeat_worktree_materialization(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repository"
            root.mkdir()
            git(root, "init", "-q", "-b", "main")
            (root / "source.txt").write_text("source\n")
            git(root, "add", ".")
            git(root, "commit", "-qm", "source")
            head = git(root, "rev-parse", "HEAD").strip()
            gate = CommandGate(root, Path(tmp) / "slots", 1, ["true"], logs=Path(tmp) / "logs")
            slot = Path(tmp) / "slots" / "slot-0"
            original = gate._git
            calls = []
            def traced(*args, **kwargs):
                calls.append(args)
                return original(*args, **kwargs)
            with patch.object(gate, "_git", side_effect=traced):
                gate._prepare(slot, head)
            self.assertEqual(sum(args[:2] == ("worktree", "add") for args in calls), 1)
            self.assertFalse(any(args and args[0] == "checkout" for args in calls), calls)
            calls.clear()
            with patch.object(gate, "_git", side_effect=traced):
                gate._prepare(slot, head)
            self.assertEqual(sum(args and args[0] == "checkout" for args in calls), 1)
            self.assertEqual((slot / "source.txt").read_text(), "source\n")

    def test_dirty_tracked_source_is_restored_before_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repository"
            root.mkdir()
            git(root, "init", "-q", "-b", "main")
            (root / "source.txt").write_text("source\n")
            git(root, "add", ".")
            git(root, "commit", "-qm", "source")
            head = git(root, "rev-parse", "HEAD").strip()
            gate = CommandGate(root, Path(tmp) / "slots", 1, ["true"], logs=Path(tmp) / "logs")
            slot = Path(tmp) / "slots" / "slot-0"
            gate._prepare(slot, head)
            (slot / "source.txt").write_text("tamper\n")
            gate._prepare(slot, head)
            self.assertEqual((slot / "source.txt").read_text(), "source\n")

    def test_index_flags_cannot_hide_changed_source_from_gate(self):
        for flag in ("--assume-unchanged", "--skip-worktree"):
            with self.subTest(flag=flag), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / "repository"
                root.mkdir()
                git(root, "init", "-q", "-b", "main")
                (root / "source.txt").write_text("source\n")
                git(root, "add", ".")
                git(root, "commit", "-qm", "source")
                head = git(root, "rev-parse", "HEAD").strip()
                gate = CommandGate(root, Path(tmp) / "slots", 1,
                                   ["test \"$(cat source.txt)\" = source"],
                                   logs=Path(tmp) / "logs")
                slot = Path(tmp) / "slots" / "slot-0"
                gate._prepare(slot, head)
                git(slot, "update-index", flag, "source.txt")
                (slot / "source.txt").write_text("tamper\n")
                self.assertEqual(git(slot, "status", "--porcelain"), "")
                result = gate.run(head, head, flag)
                self.assertTrue(result["green"], result)
                self.assertEqual((slot / "source.txt").read_text(), "source\n")
