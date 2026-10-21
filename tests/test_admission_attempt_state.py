"""Every saved round names the admissions still owned by this process."""
import hashlib
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import threading
import time
import unittest

from fakes import DaemonGitHub, DaemonRepo, FakeGate
from test_daemon import daemon
from svrf.admission import Admission


class AttemptState(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = DaemonRepo([1, 2, 3, 4])
        self.gh = DaemonGitHub(self.repo)
        self.release = threading.Event()
        self.calls = []
        self.call_lock = threading.Lock()

        def admission(head, base):
            with self.call_lock:
                self.calls.append((head, base))
            if not self.release.wait(8):
                raise RuntimeError("test admission was not released")
            return {"verdict": "MERGEABLE", "residuals": []}

        self.owner = daemon(self.repo, self.gh, FakeGate(self.repo), admission, self.root,
                            jobs=4, admission_jobs=4, tick_seconds=0.05)
        self.addCleanup(self.drain)

    def drain(self):
        self.release.set()
        while self.owner.inflight:
            self.owner.tick(drain=True)
        self.owner.close()

    def test_rate_floor_retains_all_owned_sources_and_elapsed_time(self):
        first = self.owner.tick()
        self.assertEqual(first["inflight"], [1, 2, 3, 4])
        self.gh.remaining = 0
        second = self.owner.tick()
        self.assertEqual(second["tick"], "RATE_FLOOR")
        self.assertEqual(second["inflight"], [1, 2, 3, 4],
                         "a rate-limited round hid still-owned admissions")
        kept = self.owner.load_state()["last_tick"]
        self.assertEqual(kept["inflight"], [1, 2, 3, 4])
        attempts = kept.get("admission_attempts", {})
        self.assertEqual(set(attempts), {"1", "2", "3", "4"})
        for number, row in attempts.items():
            previous = first["admission_attempts"][number]
            self.assertEqual((row["head"], row["base"]), ("h" + number, "c0"))
            self.assertEqual(row["attempt_id"], previous["attempt_id"])
            self.assertEqual(row["started_at"], previous["started_at"])
            self.assertEqual(row["started_monotonic"], previous["started_monotonic"])
            self.assertGreaterEqual(row["elapsed_seconds"], previous["elapsed_seconds"])
            self.assertEqual(row["owner"]["pid"], os.getpid())
            self.assertIsInstance(row["owner"]["start_ticks"], int)
            self.assertTrue(row["owner"]["boot_id"])
            self.assertEqual(kept["admission_sources"][number],
                             {"head": "h" + number, "base": "c0"})
        self.assertEqual(len(self.calls), 4)

    def test_each_saved_round_binds_the_observed_progress_bytes_and_last_event(self):
        from test_admission_progress_projection import beginning, encode_events
        first = self.owner.tick()
        attempts = first.get("admission_attempts", {})
        self.assertIn("1", attempts)
        self.assertEqual(attempts["1"]["progress"]["status"], "UNOBSERVED")
        path = Path(attempts["1"]["progress_file"])
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [beginning(), {"kind": "start", "span": "scan", "stage": "scan"}]
        raw = encode_events(rows)
        one = json.loads(raw.splitlines()[-1])
        path.write_bytes(raw)
        self.gh.remaining = 0
        second = self.owner.tick()["admission_attempts"]["1"]
        progress = second["progress"]
        self.assertEqual(progress["status"], "OBSERVED")
        self.assertEqual(progress["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(progress["last_event"], one)
        previous_state = self.owner.state_path.read_bytes()
        rows.append({"kind": "progress", "work_id": "receipt-a", "obligation": "tests/test_a.py",
                     "outcome": 0, "provenance": "COMPUTED"})
        after = encode_events(rows)
        two = json.loads(after.splitlines()[-1])
        with path.open("ab") as stream:
            stream.write(after[len(raw):])
        third = self.owner.tick()["admission_attempts"]["1"]
        self.assertNotEqual(third["progress"]["sha256"], progress["sha256"])
        self.assertEqual(third["progress"]["last_event"], two)
        self.assertNotEqual(self.owner.state_path.read_bytes(), previous_state)
        self.assertEqual(third["attempt_id"], second["attempt_id"])

    def test_failed_snapshot_preserves_live_attempts_without_restarting_them(self):
        self.owner.tick()
        original = {n: item["future"] for n, item in self.owner.inflight.items()}
        self.gh.rate_limited_list = 1
        second = self.owner.tick()
        self.assertEqual(second["tick"], "RETRY")
        self.assertEqual(second["inflight"], [1, 2, 3, 4],
                         "a failed source read hid still-owned admissions")
        self.assertEqual(set(second.get("admission_attempts", {})), {"1", "2", "3", "4"})
        self.assertTrue(all(self.owner.inflight[n]["future"] is f for n, f in original.items()))
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(self.owner.load_state()["held"], {})

    def test_unexpected_tick_failure_persists_owned_attempts_and_preserves_the_error(self):
        self.owner.tick()

        def fail():
            raise RuntimeError("original discovery failure")

        self.gh.snapshot = fail
        with self.assertRaisesRegex(RuntimeError, "original discovery failure"):
            self.owner.tick()
        kept = self.owner.load_state()["last_tick"]
        self.assertEqual(kept["tick"], "ERROR")
        self.assertEqual(kept["inflight"], [1, 2, 3, 4])
        self.assertEqual(set(kept.get("admission_attempts", {})), {"1", "2", "3", "4"})
        self.assertEqual(len(self.calls), 4)

    def test_attempt_record_precedes_the_callable_and_does_not_create_its_event_file(self):
        observed = []

        def inspect_record(head, base):
            rows = [json.loads(p.read_text()) for p in
                    (self.root / "admission-attempts").glob("*.json")]
            observed.append([r for r in rows if r["head"] == head and r["base"] == base])
            return {"verdict": "HELD", "residuals": ["check:expected refusal"]}

        self.owner.admission = inspect_record
        self.owner.tick()
        self.assertEqual(len(observed), 4)
        for rows in observed:
            self.assertEqual(len(rows), 1, "admission began without its durable source identity")
            row = rows[0]
            self.assertEqual(row["owner"]["pid"], os.getpid())
            event = Path(row["progress_file"])
            self.assertTrue(event.is_relative_to(self.root / "admission-progress"))
            self.assertFalse(event.exists(), "only the command may begin its own event stream")

    def test_record_storage_failure_is_visible_without_changing_the_admission(self):
        (self.root / "admission-attempts").write_text("not a directory")
        first = self.owner.tick()
        self.assertEqual(first["inflight"], [1, 2, 3, 4])
        attempts = first.get("admission_attempts", {})
        self.assertEqual(set(attempts), {"1", "2", "3", "4"})
        self.assertTrue(all(row.get("telemetry_failure") for row in attempts.values()))
        self.release.set()
        received = self.owner.tick()
        self.assertEqual(received["retry_later"], [])
        self.assertEqual(received["held"], [])
        self.assertEqual(sorted(received["admitted"]), [1, 2, 3, 4])


class CommandAttemptState(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = DaemonRepo([1])
        self.repo.root = self.root
        self.repo.merge_preview = lambda *args: {"status": "CLEAN", "conflicts": []}
        self.repo.changed_paths = lambda *args: ["lane:1"]
        self.gh = DaemonGitHub(self.repo)
        self.release = self.root / "release"
        self.owner = None
        self.addCleanup(self.drain)

    def drain(self):
        self.release.touch()
        if self.owner is not None:
            while self.owner.inflight:
                self.owner.tick(drain=True)
            self.owner.close()

    def make_owner(self, command):
        self.owner = daemon(self.repo, self.gh, FakeGate(self.repo),
                            Admission(self.repo, command=command, timeout=5), self.root,
                            tick_seconds=0.1)
        return self.owner

    def test_live_command_identity_and_progress_path_stay_bound_to_its_original_source(self):
        marker = self.root / "command.json"
        script = (
            "import json,os,pathlib,sys,time; "
            "pathlib.Path(sys.argv[1]).write_text(json.dumps({'pid':os.getpid(),"
            "'progress_file':os.environ.get('SVRF_ADMISSION_PROGRESS_FILE')})); "
            "\nwhile not pathlib.Path(sys.argv[2]).exists(): time.sleep(0.01)\n"
        )
        command = "exec " + shlex.join([sys.executable, "-c", script, str(marker), str(self.release)])
        owner = self.make_owner(command)
        owner.tick()
        deadline = time.monotonic() + 2
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(marker.exists(), "the real command did not start")
        command_row = json.loads(marker.read_text())
        self.gh.remaining = 0
        kept = owner.tick()
        attempts = kept.get("admission_attempts", {})
        self.assertIn("1", attempts, "the current command has no retained attempt")
        row = attempts["1"]
        self.assertEqual((row["head"], row["base"]), ("h1", "c0"))
        self.assertEqual(row["stage"], "command")
        self.assertEqual(row["process"]["pid"], command_row["pid"])
        self.assertIsInstance(row["process"]["start_ticks"], int)
        self.assertTrue(row["process"]["boot_id"])
        self.assertEqual(row["slot"], str(self.root))
        self.assertEqual(row["progress_file"], command_row["progress_file"])
        self.assertFalse(row["future_done"])
        self.assertFalse(Path(row["progress_file"]).exists())
        self.release.touch()
        owner.tick(drain=True)
        completed = json.loads(Path(row["record_file"]).read_text())
        self.assertEqual(completed["stage"], "complete")
        self.assertEqual(completed["process"]["returncode"], 0)
        self.assertEqual(completed["result"]["verdict"], "MERGEABLE")
        self.assertIsNone(completed["read_failure"])

    def test_simultaneous_commands_receive_distinct_source_bound_progress_paths(self):
        self.repo = DaemonRepo([1, 2])
        self.repo.root = self.root
        self.repo.merge_preview = lambda *args: {"status": "CLEAN", "conflicts": []}
        self.repo.changed_paths = lambda *args: ["lane:1"]
        self.gh = DaemonGitHub(self.repo)
        script = (
            "import json,os,pathlib,sys,time; "
            "p=pathlib.Path(sys.argv[1])/(os.environ['SVRF_PR']+'.json'); "
            "p.write_text(json.dumps({'pid':os.getpid(),'head':os.environ['SVRF_HEAD'],"
            "'progress_file':os.environ.get('SVRF_ADMISSION_PROGRESS_FILE')})); "
            "\nwhile not pathlib.Path(sys.argv[2]).exists(): time.sleep(0.01)\n"
        )
        command = "exec " + shlex.join([sys.executable, "-c", script, str(self.root), str(self.release)])
        self.owner = daemon(self.repo, self.gh, FakeGate(self.repo),
                            Admission(self.repo, command=command, timeout=5), self.root,
                            jobs=2, admission_jobs=2, tick_seconds=0.1)
        self.owner.tick()
        deadline = time.monotonic() + 2
        while not all((self.root / (str(n) + ".json")).exists() for n in (1, 2)) and time.monotonic() < deadline:
            time.sleep(0.01)
        self.gh.remaining = 0
        attempts = self.owner.tick().get("admission_attempts", {})
        self.assertEqual(set(attempts), {"1", "2"})
        paths = set()
        for number, row in attempts.items():
            actual = json.loads((self.root / (number + ".json")).read_text())
            self.assertEqual(row["head"], actual["head"])
            self.assertEqual(row["process"]["pid"], actual["pid"])
            self.assertEqual(row["progress_file"], actual["progress_file"])
            self.assertIn(row["attempt_id"], actual["progress_file"])
            paths.add(actual["progress_file"])
        self.assertEqual(len(paths), 2)

    def test_exit_126_keeps_its_actual_reason_and_terminal_record_without_becoming_a_hold(self):
        owner = self.make_owner("echo IO_POOL_CAPACITY; exit 126")
        result = owner.tick()
        self.assertEqual(result["retry_later"], [1])
        self.assertEqual(result["reasons"]["1"], "IO_POOL_CAPACITY")
        self.assertEqual(result["held"], [])
        records = list((self.root / "admission-attempts").glob("*.json"))
        self.assertEqual(len(records), 1, "the completed read lost its original attempt")
        row = json.loads(records[0].read_text())
        self.assertEqual(row["stage"], "complete")
        self.assertEqual(row["process"]["returncode"], 126)
        self.assertEqual(row["read_failure"], {"type": "ReadFailed", "reason": "IO_POOL_CAPACITY"})
        self.assertIsNone(row["result"])
        self.assertGreaterEqual(row["elapsed_seconds"], 0)
        self.assertEqual(owner.load_state()["last_tick"]["inflight"], [])


if __name__ == "__main__":
    unittest.main()
