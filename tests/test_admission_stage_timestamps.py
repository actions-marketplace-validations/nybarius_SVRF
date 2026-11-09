"""Explicitly invoked controls for attempt stage timestamps; no discovery-wait claim."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import time
import unittest
from unittest.mock import patch

from svrf.admission_attempt import AdmissionAttempt, command_started, command_finished
from svrf.daemon import Daemon
from svrf.errors import ReadFailed


class AttemptStageTimestamps(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="svrf-attempt-timestamps-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(os.environ.get("STAGE_TIMESTAMP_EVIDENCE", temporary.name)) / self._testMethodName
        self.root.mkdir(parents=True)
        self.wall = 1000.0

    def test_queued_delay_run_and_actual_command_have_distinct_entries(self):
        """Missing or prematurely assigned entry fields hide actual executor delay."""
        release, occupied = Event(), Event()
        observed = {}

        def occupy():
            occupied.set()
            if not release.wait(5):
                raise TimeoutError("owned executor was not released")

        def command():
            began = time.monotonic()
            self.wall = 1007.0
            proc = subprocess.Popen(
                [sys.executable, "-I", "-c", "import os; print(os.getpid())"],
                cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            observed["spawn_returned"] = time.monotonic()
            try:
                command_started(proc, self.root)
                raw = attempt.path.read_bytes()
                (self.root / "command-stage.json").write_bytes(raw)
                observed["command"] = json.loads(raw)
                stdout, stderr = proc.communicate(timeout=5)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.communicate()
                command_finished(proc.returncode)
            observed.update(pid=proc.pid, stdout=stdout, stderr=stderr, exit=proc.returncode)
            self.wall = 1009.0
            return {"verdict": "MERGEABLE", "residuals": []}, None, time.monotonic() - began

        with patch("svrf.admission_attempt.tempfile.NamedTemporaryFile",
                   wraps=tempfile.NamedTemporaryFile) as writes:
            with ThreadPoolExecutor(max_workers=1) as executor:
                blocker = executor.submit(occupy)
                self.assertTrue(occupied.wait(5))
                try:
                    attempt = AdmissionAttempt(self.root, 11, "head-a", "base-a",
                                               clock=lambda: self.wall)
                    queued_raw = attempt.path.read_bytes()
                    (self.root / "queued-stage.json").write_bytes(queued_raw)
                    queued = json.loads(queued_raw)
                    future = executor.submit(attempt.run, command)
                    self.assertFalse(future.running())
                    self.assertEqual(queued["stage"], "queued")
                    for name in ("admission_started_at", "admission_started_monotonic",
                                 "command_started_at", "command_started_monotonic"):
                        self.assertNotIn(name, queued)
                    self.wall = 1005.0
                    released = time.monotonic()
                    release.set()
                    blocker.result(timeout=5)
                    value, failure, _ = future.result(timeout=10)
                finally:
                    release.set()
            record = json.loads(attempt.path.read_text())
            self.assertEqual(writes.call_count, 5, "stage clocks added a persistence write")

        self.assertIsNone(failure)
        self.assertEqual(value, {"verdict": "MERGEABLE", "residuals": []})
        self.assertEqual((observed["exit"], observed["stderr"]), (0, ""))
        self.assertEqual(int(observed["stdout"]), observed["pid"])
        self.assertEqual(record["process"]["pid"], observed["pid"])
        self.assertEqual(record["process"]["returncode"], 0)
        self.assertEqual(record["terminal_disposition"], "ADMITTED")
        self.assertEqual((record["pr"], record["head"], record["base"]), (11, "head-a", "base-a"))
        for name in ("attempt_id", "owner", "started_at", "started_monotonic"):
            self.assertEqual(record[name], queued[name])
        self.assertEqual(record["started_at"], 1000.0)
        for name in ("admission_started_at", "admission_started_monotonic",
                     "command_started_at", "command_started_monotonic"):
            self.assertIn(name, record, "the existing stage write did not retain its entry clock")
            self.assertEqual(record[name], observed["command"][name])
        self.assertEqual(record["admission_started_at"], 1005.0)
        self.assertEqual(record["command_started_at"], 1007.0)
        self.assertLess(record["started_monotonic"], released)
        self.assertLessEqual(released, record["admission_started_monotonic"])
        self.assertLessEqual(record["admission_started_monotonic"], observed["spawn_returned"])
        self.assertLessEqual(observed["spawn_returned"], record["command_started_monotonic"])

    def test_cancelled_before_command_keeps_command_entry_unobserved(self):
        """Entering the executor must not manufacture a command start on cancellation."""
        cancelled = Event()
        cancelled.set()

        def forbidden_admission(*args, **kwargs):
            self.fail("cancelled admission callable entered")

        owner = object.__new__(Daemon)
        owner.admission, owner.memory = forbidden_admission, None
        attempt = AdmissionAttempt(self.root, 12, "head-b", "base-b",
                                   clock=lambda: self.wall)
        queued = json.loads(attempt.path.read_text())
        value, failure, _ = attempt.run(
            owner._timed_read, {"number": 12, "headRefOid": "head-b"}, "base-b", cancelled)
        record = json.loads(attempt.path.read_text())
        self.assertIsNone(value)
        self.assertIsInstance(failure, ReadFailed)
        self.assertEqual(failure.reason, "ADMISSION_CANCELLED")
        self.assertEqual(record["read_failure"],
                         {"type": "ReadFailed", "reason": "ADMISSION_CANCELLED"})
        self.assertEqual(record["terminal_disposition"], "UNOBSERVED")
        self.assertEqual(record["stage"], "complete")
        self.assertIsNone(record["exit"])
        for name in ("process", "command_started_at", "command_started_monotonic"):
            self.assertNotIn(name, record)
        self.assertFalse(attempt.progress_file.exists())
        for name in ("attempt_id", "owner", "started_at", "started_monotonic"):
            self.assertEqual(record[name], queued[name])
        self.assertEqual((record["pr"], record["head"], record["base"]), (12, "head-b", "base-b"))
        self.assertIn("admission_started_at", record)
        self.assertIn("admission_started_monotonic", record)
        self.assertEqual(record["admission_started_at"], 1000.0)
        self.assertLessEqual(record["started_monotonic"], record["admission_started_monotonic"])
