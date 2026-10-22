"""Gate timings separate admission/preparation from commands without changing outcomes."""
from contextlib import contextmanager
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from fakes import FakeGitHub, FakeRepo, git
import svrf.gate as gate_module
from svrf.gate import CommandGate, MemoryGuard
from svrf.train import Train


class Counter:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class GatePhaseTimings(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.clone = self.root / "repository"
        self.clone.mkdir()
        git(self.clone, "init", "-q", "-b", "main")
        (self.clone / "source.txt").write_text("source\n")
        git(self.clone, "add", ".")
        git(self.clone, "commit", "-qm", "source")
        self.head = git(self.clone, "rev-parse", "HEAD").strip()
        self.tree = git(self.clone, "rev-parse", "HEAD^{tree}").strip()
        self.gate = CommandGate(self.clone, self.root / "slots", 1, ["check"],
                                setup=["prepare"], logs=self.root / "logs")
        self.clock = Counter()
        self.repo = FakeRepo([])
        self.github = FakeGitHub(self.repo)
        self.family = {"id": "F1", "prs": [], "base": self.head,
                       "commit": self.head, "tree": self.tree, "steps": [], "gate": None}

    def owner(self, memory=None):
        return Train(self.repo, self.github, self.gate, receipts=self.root / "receipts",
                     clock=self.clock, memory=memory, comment=False,
                     pr_comments=False, status_checks=False)

    @contextmanager
    def measured(self, *, checkout_failure=None, command_exit=0):
        original_git, original_run = self.gate._git, subprocess.run
        original_lock = gate_module.flocked
        original_acquire, original_release = self.gate.slots.acquire, self.gate.slots.release

        def git_read(*args, **kwargs):
            if args[0] == "checkout" and checkout_failure is not None:
                self.clock.advance(41)
                raise checkout_failure
            answer = original_git(*args, **kwargs)
            self.clock.advance({"worktree": 2, "checkout": 3, "clean": 5,
                                "rev-parse": 7, "diff": 11}.get(args[0], 0))
            return answer

        def command(argv, *args, **kwargs):
            result = original_run(argv, *args, **kwargs)
            if len(argv) > 3 and argv[3] == "ls-files":
                self.clock.advance(13)
            return result

        def acquire(*args, **kwargs):
            slot = original_acquire(*args, **kwargs)
            self.clock.advance(17)
            return slot

        def release(*args, **kwargs):
            result = original_release(*args, **kwargs)
            self.clock.advance(19)
            return result

        @contextmanager
        def locked(*args, **kwargs):
            with original_lock(*args, **kwargs):
                self.clock.advance(23)
                yield

        def step(command, *args, **kwargs):
            elapsed = 29 if command == "prepare" else 31
            self.clock.advance(elapsed)
            return command_exit, "FAIL fixture" if command_exit else "", elapsed

        with patch("svrf.gate.time.monotonic", self.clock), \
                patch("svrf.gate.subprocess.run", side_effect=command), \
                patch("svrf.gate.flocked", locked), \
                patch.object(self.gate, "_git", side_effect=git_read), \
                patch.object(self.gate, "_step", side_effect=step), \
                patch.object(self.gate.slots, "acquire", side_effect=acquire), \
                patch.object(self.gate.slots, "release", side_effect=release):
            yield

    def warm_slot(self):
        self.gate._prepare(self.gate.pool / "slot-0", self.head)

    def assert_slot_released(self):
        slot = self.gate.slots.acquire(block=False)
        self.assertIsNotNone(slot, "a failed/timed gate retained its exclusive slot")
        self.gate.slots.release(slot)

    def test_actual_memory_guard_wait_is_distinct_from_preparation_and_commands(self):
        availability = iter([0] * 24 + [100])
        memory = MemoryGuard(need_gb=10, reserve_gb=8,
                             available_gb=lambda: next(availability),
                             sleep=self.clock.advance, poll=15)
        owner = self.owner(memory)
        with self.measured():
            row = owner.gate_family(self.family, self.head)
        self.assertTrue(row["green"], row)
        self.assertEqual(row["seconds"], 504)
        self.assertEqual(row["phase_seconds"], {
            "memory_wait": 360, "slot_wait": 17, "worktree_prepare": 7,
            "metadata": 18, "setup_lock_wait": 23, "setup_commands": 29,
            "gate_commands": 31, "slot_release": 19,
        })
        self.assertEqual(sum(row["phase_seconds"].values()), row["seconds"])
        self.assertEqual(row["prepare_phase_seconds"], {"worktree_create": 2, "clean": 5})
        self.assertEqual(row["prepare_phase_status"]["checkout"], "skipped")
        self.assertEqual([step["seconds"] for step in row["steps"]], [29, 31])
        self.assertTrue(all(value == "complete" for value in row["phase_status"].values()))
        self.assertEqual(memory.running, 0)
        self.assert_slot_released()

    def test_reused_worktree_separates_index_checkout_and_clean(self):
        self.warm_slot()
        owner = self.owner()
        with self.measured():
            row = owner.gate_family(self.family, self.head)
        self.assertTrue(row["green"], row)
        self.assertEqual(row["seconds"], 158)
        self.assertEqual(row["phase_seconds"]["worktree_prepare"], 21)
        self.assertEqual(row["prepare_phase_seconds"], {"index_flags": 13, "checkout": 3, "clean": 5})
        self.assertEqual(row["prepare_phase_status"]["worktree_create"], "skipped")
        self.assertEqual(row["phase_status"]["memory_wait"], "skipped")
        self.assertEqual(row["phase_seconds"]["memory_wait"], 0)

    def test_checkout_exception_retains_partial_phase_and_releases_slot(self):
        self.warm_slot()
        owner = self.owner()
        failure = OSError("checkout interrupted")
        with self.measured(checkout_failure=failure):
            row = owner.gate_family(self.family, self.head)
        self.assertFalse(row["green"])
        self.assertEqual(row["read_failure"], "GATE_FAILED:OSError:checkout interrupted")
        self.assertEqual(row["seconds"], 90)
        self.assertEqual(row["phase_seconds"], {
            "memory_wait": 0, "slot_wait": 17, "worktree_prepare": 54, "slot_release": 19,
        })
        self.assertEqual(row["prepare_phase_seconds"], {"index_flags": 13, "checkout": 41})
        self.assertEqual(row["prepare_phase_status"]["checkout"], "failed")
        self.assertEqual(row["prepare_phase_status"]["clean"], "not_started")
        self.assertEqual(row["phase_status"]["worktree_prepare"], "failed")
        self.assertEqual(row["phase_status"]["metadata"], "not_started")
        self.assertEqual(row["phase_status"]["slot_release"], "complete")
        self.assertEqual(self.family["status"], "GATE_RETRY")
        self.assertEqual(owner.receipt["holds"], [])
        self.assert_slot_released()

    def test_failed_slot_acquisition_keeps_original_exception_and_never_releases(self):
        failure = OSError("slot read failed")

        def unavailable():
            self.clock.advance(47)
            raise failure

        with self.measured(), \
                patch.object(self.gate.slots, "acquire", side_effect=unavailable), \
                patch.object(self.gate.slots, "release") as release:
            with self.assertRaises(OSError) as caught:
                self.gate.run(self.head, self.head, "F1")
        self.assertIs(caught.exception, failure)
        self.assertEqual(failure.gate_phase_timing["phase_seconds"], {"slot_wait": 47})
        self.assertEqual(failure.gate_phase_timing["phase_status"]["slot_wait"], "failed")
        self.assertEqual(failure.gate_phase_timing["phase_status"]["slot_release"], "not_started")
        release.assert_not_called()

    def test_failed_memory_admission_names_unstarted_gate(self):
        def unreadable():
            self.clock.advance(53)
            raise OSError("memory unreadable")

        memory = MemoryGuard(available_gb=unreadable)
        owner = self.owner(memory)
        with patch("svrf.gate.time.monotonic", self.clock), \
                patch.object(self.gate, "run") as run:
            row = owner.gate_family(self.family, self.head)
        self.assertEqual(row["read_failure"], "GATE_FAILED:OSError:memory unreadable")
        self.assertEqual(row["phase_seconds"], {"memory_wait": 53})
        self.assertEqual(row["phase_status"], {"memory_wait": "failed", "gate_details": "not_started"})
        self.assertEqual(memory.running, 0)
        run.assert_not_called()

    def test_reused_red_receipt_does_not_count_original_phase_cost_again(self):
        owner = self.owner()
        with self.measured(command_exit=1), patch.object(self.gate, "run", wraps=self.gate.run) as run:
            original = owner.gate_family(self.family, self.head)
            self.clock.advance(37)
            reused = owner.gate_family({**self.family, "id": "F2"}, self.head)
        self.assertFalse(original["green"])
        self.assertTrue(original["phase_seconds"])
        self.assertEqual(run.call_count, 1)
        self.assertEqual(reused["reused"], 0)
        self.assertEqual(reused["seconds"], 0)
        self.assertEqual(reused["phase_seconds"], {})
        self.assertEqual(reused["prepare_phase_seconds"], {})
        self.assertEqual(reused["phase_status"], {"gate": "reused"})
        self.assertEqual(reused["completed_event"], original["completed_event"])


if __name__ == "__main__":
    unittest.main()
