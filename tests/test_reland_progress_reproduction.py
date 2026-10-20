"""A pending rebuilt admission must not block an independently ready merge.

This adds one event-controlled read to the original daemon and original fake
repository/gate. No native admission or publication condition is replaced.
"""

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from fakes import DaemonGitHub, DaemonRepo, FakeGate
from test_daemon import daemon


class RebuiltAdmissionProgress(unittest.TestCase):
    def test_ready_head_lands_while_rebuilt_admission_is_still_pending(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = DaemonRepo([1, 2])
            gh = DaemonGitHub(repo)
            rebuilt_entered = threading.Event()
            release_rebuilt = threading.Event()
            ready_returned = threading.Event()
            ready_landed = threading.Event()
            stop = threading.Event()
            calls = []
            ticks = []
            errors = []
            started = time.monotonic()
            original_merge = gh.merge

            def merge(number, sha):
                result = original_merge(number, sha)
                if number == 2:
                    ready_landed.set()
                return result

            gh.merge = merge

            def admission(head, base):
                calls.append({"head": head, "base": base,
                              "seconds": time.monotonic() - started})
                if head == "h1":
                    return {"verdict": "HELD", "residuals": ["reland:REFUSED:MIXED"]}
                if head == "h2":
                    if not rebuilt_entered.wait(5):
                        raise RuntimeError("original rebuilt admission did not start")
                    ready_returned.set()
                    return {"verdict": "MERGEABLE", "residuals": []}
                rebuilt_entered.set()
                if not release_rebuilt.wait(5):
                    raise RuntimeError("test did not release the original rebuilt read")
                return {"verdict": "MERGEABLE", "residuals": []}

            owner = daemon(repo, gh, FakeGate(repo), admission, Path(temporary),
                           admission_jobs=2, jobs=2, admission_round_reads=2,
                           tick_seconds=0.05)

            def run_ticks():
                try:
                    while not stop.is_set():
                        ticks.append(owner.tick())
                except BaseException as error:
                    errors.append(repr(error))

            worker = threading.Thread(target=run_ticks, daemon=True)
            worker.start()
            observed = {}
            try:
                self.assertTrue(rebuilt_entered.wait(2), "rebuild was not exercised")
                self.assertTrue(ready_returned.wait(2), "ready admission was not exercised")
                ready_future = owner.inflight.get(2, {}).get("future")
                if ready_future is not None:
                    self.assertEqual(ready_future.result(timeout=1)[0]["verdict"], "MERGEABLE")
                landed = ready_landed.wait(0.5)
                observed = {
                    "schema": "svrf-reland-progress-observation/1",
                    "upstream_commit": "d5ef9cbd81b5d85763f010b63a86c6704eb47c0f",
                    "daemon_blob": "dde8327df42a56b6b99ef4ed72146d971f6e9fac",
                    "tick_budget_seconds": 0.05,
                    "observation_seconds": time.monotonic() - started,
                    "rebuilt_read_pending": not release_rebuilt.is_set(),
                    "ready_original_future_completed": ready_future.done() if ready_future else True,
                    "ready_merged": landed,
                    "completed_ticks": len(ticks),
                    "calls": list(calls),
                    "published_before_rebuilt_acceptance": bool(
                        [push for push in repo.pushes if push[0] == "pr-1-ordered"]
                        or gh.opened or [number for number in gh.closed if number == 1]
                        or [comment for comment in gh.comments if comment[0] == 1]),
                }
                self.assertEqual(errors, [])
                self.assertFalse(observed["published_before_rebuilt_acceptance"])
                self.assertTrue(
                    landed,
                    "completed independent admission waited for the pending rebuilt admission: "
                    + json.dumps(observed, sort_keys=True),
                )
            finally:
                stop.set()
                release_rebuilt.set()
                worker.join(3)
                while owner.inflight and not worker.is_alive():
                    owner.tick(drain=True)
                owner.close()
                self.assertFalse(worker.is_alive(), "test did not drain the original controller")


if __name__ == "__main__":
    unittest.main()
