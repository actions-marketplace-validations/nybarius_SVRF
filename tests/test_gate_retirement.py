"""Retirement leaves resource waits without interrupting an owned checker."""
from contextlib import contextmanager
from pathlib import Path
from threading import Event
import tempfile
import unittest
from unittest.mock import patch

from fakes import FakeGate, FakeGitHub, FakeRepo, git
from test_train import train
from svrf.gate import CommandGate, MemoryGuard
from svrf.locks import SlotPool, flocked
from svrf.train import Train


class MemoryRetirement(unittest.TestCase):
    def test_stop_during_memory_wait_defers_checker_and_keeps_candidate_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            stopping = Event()
            availability, waits = [0], []

            def wait(seconds):
                waits.append(seconds)
                stopping.set()
                availability[0] = 100

            memory = MemoryGuard(available_gb=lambda: availability[0], sleep=wait, poll=15)
            repo = FakeRepo([1])
            github, gate = FakeGitHub(repo), FakeGate(repo)
            owner = train(repo, github, gate, tmp, memory=memory,
                          stop_requested=stopping.is_set)
            receipt = owner.run([1])
            self.assertEqual(gate.trees, [])
            self.assertEqual(github.merged, [])
            self.assertEqual(receipt["pending"], [1])
            self.assertEqual(receipt["holds"], [])
            self.assertEqual(receipt["gates"][0]["read_failure"], "STOP_REQUESTED")
            self.assertEqual(receipt["gates"][0]["phase_status"]["memory_wait"], "postponed")
            self.assertEqual(memory.running, 0)
            self.assertEqual(waits, [15])

    def test_legacy_guard_contract_is_preserved_and_its_reservation_is_released(self):
        with tempfile.TemporaryDirectory() as tmp:
            stopping, released = Event(), Event()

            class LegacyGuard:
                need_gb, reserve_gb = 10, 8

                @contextmanager
                def admit(self):
                    try:
                        stopping.set()
                        yield
                    finally:
                        released.set()

            repo = FakeRepo([1])
            github, gate = FakeGitHub(repo), FakeGate(repo)
            receipt = train(repo, github, gate, tmp, memory=LegacyGuard(),
                            stop_requested=stopping.is_set).run([1])
            self.assertEqual(gate.trees, [])
            self.assertEqual(receipt["gates"][0]["read_failure"], "STOP_REQUESTED")
            self.assertEqual(receipt["pending"], [1])
            self.assertTrue(released.is_set())

    def test_stop_during_memory_reservation_releases_the_increment(self):
        with tempfile.TemporaryDirectory() as tmp:
            stopping = Event()

            def available():
                stopping.set()
                return 100

            memory = MemoryGuard(available_gb=available)
            repo = FakeRepo([1])
            github, gate = FakeGitHub(repo), FakeGate(repo)
            receipt = train(repo, github, gate, tmp, memory=memory,
                            stop_requested=stopping.is_set).run([1])
            self.assertEqual(memory.running, 0)
            self.assertEqual(gate.trees, [])
            self.assertEqual(receipt["pending"], [1])
            self.assertEqual(receipt["gates"][0]["read_failure"], "STOP_REQUESTED")


class GateRetirement(unittest.TestCase):
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
        self.head = git(self.clone, "rev-parse", "HEAD")
        self.tree = git(self.clone, "rev-parse", "HEAD^{tree}")
        self.stopping = Event()
        self.marker = self.root / "executed"
        self.gate = CommandGate(self.clone, self.root / "slots", 1,
                                [f"echo gate >> {self.marker}"],
                                setup=[f"echo setup >> {self.marker}"],
                                logs=self.root / "logs")
        self.memory = MemoryGuard(available_gb=lambda: 100)
        repo = FakeRepo([])
        self.owner = Train(repo, FakeGitHub(repo), self.gate,
                           receipts=self.root / "receipts", memory=self.memory,
                           stop_requested=self.stopping.is_set, comment=False,
                           pr_comments=False, status_checks=False)
        self.family = {"id": "F1", "prs": [], "base": self.head, "commit": self.head,
                       "tree": self.tree, "steps": [], "gate": None}

    def read(self):
        return self.owner.gate_family(self.family, self.head)

    def assert_postponed(self, row):
        self.assertFalse(row["green"])
        self.assertEqual(row["read_failure"], "STOP_REQUESTED")
        self.assertEqual(self.family["status"], "GATE_RETRY")
        self.assertEqual(self.owner.receipt["holds"], [])
        self.assertEqual(self.memory.running, 0)
        self.assertFalse(self.marker.exists())
        self.assertEqual(self.gate.slots.held, {})

    def assert_slot_free(self):
        other = SlotPool(self.gate.pool, 1)
        slot = other.acquire(block=False)
        self.assertIsNotNone(slot)
        other.release(slot)

    def test_stop_in_real_slot_wait_preserves_foreign_lease_and_never_prepares(self):
        other = SlotPool(self.gate.pool, 1)
        slot = other.acquire()
        waits = []

        def wait(seconds):
            waits.append(seconds)
            self.stopping.set()

        try:
            self.gate.slots.sleep = wait
            row = self.read()
            self.assert_postponed(row)
            self.assertEqual(row["phase_status"]["slot_wait"], "postponed")
            self.assertEqual(row["phase_status"]["worktree_prepare"], "not_started")
            self.assertEqual(row["phase_status"]["slot_release"], "not_started")
            self.assertFalse((slot / ".git").exists())
            self.assertEqual(waits, [5])
            self.assertIn(slot, other.held)
            self.assertIsNone(SlotPool(self.gate.pool, 1).acquire(block=False))
        finally:
            other.release(slot)
        self.assert_slot_free()

    def test_stop_in_real_setup_lock_wait_releases_owned_memory_and_slot(self):
        waits = []

        def wait(seconds):
            waits.append(seconds)
            self.stopping.set()

        with flocked(self.gate.setup_lock) as owned:
            self.assertTrue(owned)
            with patch("svrf.locks.time.sleep", side_effect=wait):
                row = self.read()
            self.assert_postponed(row)
            self.assertEqual(row["phase_status"]["setup_lock_wait"], "postponed")
            self.assertEqual(row["phase_status"]["setup_commands"], "not_started")
            self.assertEqual(row["phase_status"]["gate_commands"], "not_started")
            self.assertEqual(row["phase_status"]["slot_release"], "complete")
            self.assertTrue(waits)
            self.assertLessEqual(max(waits), 0.25)
            with flocked(self.gate.setup_lock) as second:
                self.assertFalse(second)
        self.assert_slot_free()

    def test_stop_after_actual_checkout_releases_slot_without_starting_command(self):
        prepare = self.gate._prepare

        def stop_after_prepare(*args, **kwargs):
            prepare(*args, **kwargs)
            self.stopping.set()

        with patch.object(self.gate, "_prepare", side_effect=stop_after_prepare):
            row = self.read()
        self.assert_postponed(row)
        self.assertEqual(row["phase_status"]["worktree_prepare"], "complete")
        self.assertEqual(row["phase_status"]["slot_release"], "complete")
        self.assert_slot_free()

    def test_stop_after_first_command_preserves_remaining_commands_and_real_failure(self):
        step = self.gate._step

        def stop_after_step(*args, **kwargs):
            result = step(*args, **kwargs)
            self.stopping.set()
            return result

        self.gate.commands.append("echo 'FAIL real second command'; exit 1")
        with patch.object(self.gate, "_step", side_effect=stop_after_step):
            row = self.read()
        self.assertFalse(row["green"])
        self.assertIsNone(row["read_failure"])
        self.assertEqual(row["failing"], ["FAIL real second command"])
        self.assertEqual([s["rc"] for s in row["steps"]], [0, 0, 1])
        self.assertEqual(self.marker.read_text().splitlines(), ["setup", "gate"])
        self.assertEqual(self.family["status"], "RED")
        self.assertEqual(self.memory.running, 0)
        self.assert_slot_free()


if __name__ == "__main__":
    unittest.main()
