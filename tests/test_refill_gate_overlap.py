"""Admission slots stay productive while the single landing owner runs a gate."""
import tempfile
import threading
import unittest
from pathlib import Path

from fakes import DaemonGitHub, DaemonRepo, FakeGate
from test_daemon import daemon


class GateOverlap(unittest.TestCase):
    def exercise(self, *, mismatch=False):
        with tempfile.TemporaryDirectory() as temp:
            repo = DaemonRepo([1, 2])
            gh = DaemonGitHub(repo, mismatch_on=1 if mismatch else None)
            first_started = threading.Event()
            first_release = threading.Event()
            next_started = threading.Event()
            next_release = threading.Event()
            gate_started = threading.Event()
            gate_release = threading.Event()
            calls = []

            def admission(head, base):
                calls.append((head, base))
                if head == "h1":
                    first_started.set()
                    if not first_release.wait(10):
                        raise RuntimeError("first admission was not released")
                else:
                    next_started.set()
                    if not next_release.wait(10):
                        raise RuntimeError("refilled admission was not released")
                return {"verdict": "MERGEABLE", "residuals": []}

            class HeldGate(FakeGate):
                def run(self, base, commit, label=""):
                    if not gate_started.is_set():
                        gate_started.set()
                        if not gate_release.wait(10):
                            raise RuntimeError("first gate was not released")
                    return super().run(base, commit, label)

            gate = HeldGate(repo)
            owner = daemon(repo, gh, gate, admission, Path(temp), jobs=1,
                           admission_jobs=1, admission_round_reads=1, tick_seconds=10)
            result, errors = {}, []

            def tick():
                try:
                    result.update(owner.tick())
                except BaseException as error:
                    errors.append(error)

            thread = threading.Thread(target=tick)
            thread.start()
            try:
                self.assertTrue(first_started.wait(5))
                # The first snapshot saw h2. Refill must reacquire the unstarted head.
                repo.commits["h2-current"] = {"lanes": frozenset({0, 2}), "parents": ("h2",)}
                repo.heads[2] = repo.branches["pr-2"] = "h2-current"
                first_release.set()
                self.assertTrue(gate_started.wait(5))
                self.assertTrue(
                    next_started.wait(5),
                    "a free admission slot waited behind the running final gate",
                )
                self.assertEqual(gh.merged, [], "publication crossed the unfinished gate")
                self.assertEqual(calls, [("h1", "c0"), ("h2-current", "c0")])
            finally:
                first_release.set()
                next_release.set()
                gate_release.set()
                thread.join(15)
                while owner.inflight:
                    owner.tick(drain=True)
                owner.close()
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(len(calls), 2, "an already running admission was duplicated")
            if mismatch:
                self.assertTrue(result["stopped"])
                self.assertEqual(gh.merged, [1])
                self.assertIn(2, result["retry_later"])
                self.assertEqual(gate.trees, ["T0,1"])
            else:
                self.assertEqual(gh.merged, [1, 2])
                # Admission #2 began before #1 landed, but its final gate includes #1.
                self.assertEqual(gate.trees, ["T0,1", "T0,1,2"])
                self.assertEqual(result["inflight"], [])
                self.assertFalse(result["stopped"])

    def test_current_deferred_head_starts_before_the_previous_gate_returns(self):
        self.exercise()

    def test_refilled_result_cannot_publish_after_a_landed_tree_mismatch(self):
        self.exercise(mismatch=True)


if __name__ == "__main__":
    unittest.main()
