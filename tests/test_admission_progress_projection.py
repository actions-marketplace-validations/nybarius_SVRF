"""Event transport preserves active spans and unique completed work."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest

from svrf.admission_attempt import AdmissionAttempt, process_identity, read_progress


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def encode_events(rows):
    previous, out = None, []
    for index, fields in enumerate(rows):
        event = {"schema": "example.progress/1", "kind": "stage", "sequence": index,
                 "previous": previous, "wall": 100.0 + index, "monotonic": 10.0 + index,
                 "pid": os.getpid(), "lane": "command", **fields}
        event["digest"] = "sha256:" + hashlib.sha256(canonical(event)).hexdigest()
        previous = event["digest"]
        out.append(canonical(event) + b"\n")
    return b"".join(out)


def beginning(**fields):
    return {"kind": "begin", "pr": 1, "head": "h1", "base": "c0",
            "owner": process_identity(os.getpid()), "observer_sha256": "sha256:producer", **fields}


class ProgressProjection(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "events.jsonl"

    def read(self, rows):
        self.path.write_bytes(encode_events(rows))
        return read_progress(self.path)

    def summary(self, result):
        self.assertEqual(result["status"], "OBSERVED", result)
        self.assertIn("summary", result, "valid events have no retained stage/coverage projection")
        return result["summary"]

    def test_nested_end_restores_the_outer_stage_and_keeps_wrapper_lane(self):
        rows = [beginning(), {"kind": "stage", "lane": "wrapper", "stage": "admission"},
                {"kind": "start", "span": "outer", "stage": "suite", "blocking_resource": "cpu",
                 "obligation": "tests/test_a.py"},
                {"kind": "start", "span": "inner", "stage": "read"},
                {"kind": "end", "span": "inner"}]
        value = self.summary(self.read(rows))
        self.assertEqual(value["stage"], "suite")
        self.assertEqual([r["stage"] for r in value["active_stages"]], ["suite", "admission"])
        self.assertEqual(value["stage_started_at"], 102.0)
        self.assertEqual(value["blocking_resource"], "cpu")
        self.assertGreaterEqual(value["stage_elapsed_seconds"], 0)
        self.assertGreaterEqual(value["seconds_without_progress"], 0)

    def test_duplicate_work_is_not_a_new_completion_or_a_progress_clock_reset(self):
        rows = [beginning(),
                {"kind": "progress", "work_id": "receipt-a", "obligation": "tests/test_a.py",
                 "outcome": 0, "provenance": "COMPUTED"},
                {"kind": "stage", "stage": "waiting"},
                {"kind": "progress", "work_id": "receipt-a", "obligation": "tests/test_a.py",
                 "outcome": 0, "provenance": "REUSED"}]
        value = self.summary(self.read(rows))
        self.assertEqual(value["completed_work_ids"], ["receipt-a"])
        self.assertEqual(value["completed_obligations"], 1)
        self.assertEqual(value["last_progress"]["sequence"], 1)
        self.assertEqual(value["last_progress"]["provenance"], "COMPUTED")
        self.assertEqual(value["coverage_digest"],
                         "sha256:" + hashlib.sha256(canonical(["receipt-a"])).hexdigest())

    def test_terminal_closes_stages_and_retains_the_original_completion_time_and_exit(self):
        rows = [beginning(), {"kind": "start", "span": "s", "stage": "suite"},
                {"kind": "terminal", "disposition": "UNOBSERVED", "exit": 126,
                 "wall": 112.0, "monotonic": 22.0}]
        value = self.summary(self.read(rows))
        self.assertEqual(value["stage"], "TERMINAL")
        self.assertEqual(value["active_stages"], [])
        self.assertEqual(value["terminal_disposition"], "UNOBSERVED")
        self.assertEqual(value["terminal_at"], 112.0)
        self.assertEqual(value["exit"], 126)
        self.assertEqual(value["elapsed_seconds"], 12.0)
        self.assertIsNone(value["stage_elapsed_seconds"])

    def test_digest_mismatch_truncation_and_events_after_terminal_are_unobserved(self):
        base = [beginning(), {"kind": "stage", "stage": "scan"}]
        raw = encode_events(base)
        damaged = raw.replace(b'"stage":"scan"', b'"stage":"fake"')
        after = encode_events([beginning(), {"kind": "terminal", "disposition": "HELD", "exit": 1},
                               {"kind": "stage", "stage": "late"}])
        for value in (damaged, raw[:-1], after):
            with self.subTest(value=value[-30:]):
                self.path.write_bytes(value)
                result = read_progress(self.path)
                self.assertEqual(result["status"], "UNOBSERVED")
                self.assertNotIn("summary", result)

    def test_a_malformed_progress_event_is_unknown_work_not_empty_coverage(self):
        result = self.read([beginning(), {"kind": "progress", "obligation": "tests/test_a.py"}])
        self.assertEqual(result["status"], "UNOBSERVED")
        self.assertNotIn("summary", result)

    def test_an_attempt_cannot_borrow_a_different_heads_event_stream(self):
        attempt = AdmissionAttempt(Path(self.temp.name), 1, "h1", "c0")
        attempt.progress_file.parent.mkdir(parents=True)
        attempt.progress_file.write_bytes(encode_events([beginning(head="other")]))
        progress = attempt.snapshot()["progress"]
        self.assertEqual(progress["status"], "UNOBSERVED")
        self.assertNotIn("summary", progress)


if __name__ == "__main__":
    unittest.main()
