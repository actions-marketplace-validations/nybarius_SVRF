"""Service stop reaches owned admission cancellation without discarding results."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import svrf.admission as admission_module
from svrf.admission import Admission
from svrf.daemon import Daemon
from svrf.errors import ReadFailed
from svrf.gate import MemoryGuard


HEAD, BASE, NEXT_HEAD = "1" * 40, "2" * 40, "3" * 40
ROW = {"number": 1, "headRefOid": HEAD}


def _identity(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return [pid, int(fields[19])]
    except FileNotFoundError:
        return None


def _until(predicate, seconds=5):
    deadline = time.monotonic() + seconds
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert predicate(), "bounded owned-process observation did not complete"


def _command(program):
    return shlex.quote(sys.executable) + " -c " + shlex.quote(program)


class ServiceStopCancellation(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(
            prefix="svrf-service-stop-", dir=os.environ.get("SVRF_TEST_TMPDIR"))
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def capture(self, name, value):
        destination = os.environ.get("SVRF_TEST_EVIDENCE_DIR")
        if destination:
            root = Path(destination) / self._testMethodName
            root.mkdir(parents=True, exist_ok=True)
            partial = self.root / "original-partial.txt"
            if partial.exists():
                body = partial.read_bytes()
                (root / "original-partial.txt").write_bytes(body)
                value = {**value, "original_partial": {
                    "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()}}
            marker = self.root / "family.json"
            if marker.exists():
                identities = json.loads(marker.read_text())
                value = {**value, "family": identities, "family_presence": {
                    key: _identity(identity[0]) == identity
                    for key, identity in identities.items()}}
            (root / (name + ".json")).write_text(
                json.dumps(value, sort_keys=True, indent=2) + "\n")

    def owner(self, command, *, memory=None):
        repo = SimpleNamespace(
            root=self.root, is_ancestor=lambda head, base: False,
            merge_preview=lambda base, head: {"status": "CLEAN", "conflicts": []},
            changed_paths=lambda base, head: ["owned-fixture"],
        )
        admission = Admission(repo, command=command, timeout=20)
        owner = Daemon(
            repo, SimpleNamespace(), SimpleNamespace(), admission,
            state_dir=self.root / "state", receipts=self.root / "receipts",
            memory=memory, jobs=1, admission_jobs=1, tick_seconds=0.01,
        )
        owner._pool = ThreadPoolExecutor(max_workers=1)
        self.addCleanup(self.settle, owner)
        return owner

    def settle(self, owner):
        # Cleanup uses only the original explicit token for these owned fixtures.
        # Never release an executor/slot while its command still has custody.
        for item in owner.inflight.values():
            cancelled = item.get("cancelled")
            if cancelled is not None:
                cancelled.set()
        if owner._pool is not None:
            owner._pool.shutdown(wait=True)
        receipts = []
        while owner.inflight:
            summary = owner._drain()
            if summary.get("receipt"):
                receipts.append(json.loads(Path(summary["receipt"]).read_text()))
        owner.close()
        self.capture("cleanup", {"inflight": sorted(owner.inflight), "receipts": receipts})

    def receive(self, owner, item):
        value, failure, seconds = item["future"].result(timeout=5)
        summary = owner._drain()
        receipt = json.loads(Path(summary["receipt"]).read_text())
        self.assertEqual(owner.inflight, {})
        self.capture("received", {
            "drain_receipt": receipt, "attempt": item["attempt"].snapshot()})
        return value, failure, receipt["drained"][0]

    def family(self):
        marker = self.root / "family.json"
        child_ready = self.root / "child-ready"
        partial = self.root / "original-partial.txt"
        child = f"""import pathlib,signal,time
signal.signal(signal.SIGINT, signal.SIG_IGN)
pathlib.Path({str(child_ready)!r}).touch()
time.sleep(20)
"""
        program = f"""import json,os,pathlib,signal,subprocess,sys,time
signal.signal(signal.SIGINT, signal.SIG_IGN)
child=subprocess.Popen([sys.executable, '-c', {child!r}], start_new_session=True)
ready=pathlib.Path({str(child_ready)!r})
while not ready.exists():
    if child.poll() is not None: raise RuntimeError('owned child failed before ready')
    time.sleep(0.01)
def identity(pid):
    fields=pathlib.Path('/proc/'+str(pid)+'/stat').read_text().rsplit(')',1)[1].split()
    return [pid,int(fields[19])]
pathlib.Path({str(partial)!r}).write_bytes(b'ORIGINAL_PARTIAL_RESULT\\n')
marker=pathlib.Path({str(marker)!r})
pending=marker.with_suffix('.tmp')
pending.write_text(json.dumps({{'leader':identity(os.getpid()),'detached':identity(child.pid)}}))
pending.replace(marker)
time.sleep(20)
"""
        return _command(program), marker, partial

    def test_service_stop_reaps_detached_family_before_slot_and_memory_release(self):
        command, marker, partial = self.family()
        releases = []
        guard = MemoryGuard(need_gb=1, reserve_gb=0,
                            available_gb=lambda: 100, poll=0.01)

        def presence():
            try:
                identities = json.loads(marker.read_text())
            except OSError:
                return {"UNOBSERVED": True}
            return {name: _identity(identity[0]) == identity
                    for name, identity in identities.items()}

        class ObservedMemory:
            @contextmanager
            def admit(inner, **options):
                with guard.admit(**options):
                    try:
                        yield
                    finally:
                        releases.append(("memory", presence(), guard.running))

        class ObservedSlot:
            def acquire(inner):
                return 0

            def release(inner, slot):
                releases.append(("slot", presence(), guard.running))

        owner = self.owner(command, memory=ObservedMemory())
        owner.admission.pool = ObservedSlot()
        owner.admission._slot_root = lambda slot: self.root
        owner._submit([ROW], BASE)
        item = owner.inflight[1]
        _until(marker.exists)
        original_partial = partial.read_bytes()
        self.assertTrue(all(presence().values()))
        self.assertEqual(guard.running, 1)
        owner.request_stop()
        completed = True
        try:
            item["future"].result(timeout=5)
        except TimeoutError:
            completed = False
        observation = {
            "completed_before_cleanup": completed, "presence": presence(),
            "releases": releases, "partial_unchanged": partial.read_bytes() == original_partial,
        }
        self.capture("before-cleanup", observation)
        print("SERVICE_STOP_FAMILY", json.dumps(observation, sort_keys=True), flush=True)
        self.assertTrue(completed, "service stop left the owned admission running")
        value, failure, drained = self.receive(owner, item)
        self.assertIsNone(value)
        self.assertIsInstance(failure, ReadFailed)
        self.assertEqual(failure.reason, "ADMISSION_CANCELLED")
        self.assertEqual([row[0] for row in releases], ["slot", "memory"])
        self.assertTrue(all(not any(row[1].values()) for row in releases))
        self.assertEqual([row[2] for row in releases], [1, 1])
        self.assertEqual(guard.running, 0)
        self.assertFalse(any(presence().values()))  # Zombies still have /proc identities.
        self.assertEqual(partial.read_bytes(), original_partial)
        self.assertEqual(drained["failure"]["reason"], "ADMISSION_CANCELLED")
        self.assertEqual(item["attempt"].snapshot()["read_failure"]["reason"],
                         "ADMISSION_CANCELLED")

    def test_stop_before_submit_starts_no_attempt_or_command(self):
        marker = self.root / "unstarted"
        owner = self.owner(_command(f"from pathlib import Path; Path({str(marker)!r}).touch()"))
        owner.request_stop()
        owner._submit([ROW], BASE)
        self.assertEqual(owner.inflight, {})
        self.assertEqual(owner._pending_attempt_history, {})
        self.assertFalse(marker.exists())

    def test_stop_inside_submit_is_seen_before_inflight_insertion(self):
        marker = self.root / "unstarted"
        owner = self.owner(_command(f"from pathlib import Path; Path({str(marker)!r}).touch()"))
        pool, observations = owner._pool, []

        class StopInsideSubmit:
            def submit(inner, function, *args):
                observations.append(dict(owner.inflight))
                owner.request_stop()
                return pool.submit(function, *args)

            def shutdown(inner, **options):
                return pool.shutdown(**options)

        owner._pool = StopInsideSubmit()
        owner._submit([ROW, {"number": 2, "headRefOid": NEXT_HEAD}], BASE)
        self.assertEqual(observations, [{}])
        self.assertEqual(list(owner.inflight), [1])
        item = owner.inflight[1]
        value, failure, drained = self.receive(owner, item)
        self.assertFalse(marker.exists(), "stop inside submit still launched a command")
        self.assertIsNone(value)
        self.assertIsInstance(failure, ReadFailed)
        self.assertEqual(failure.reason, "ADMISSION_CANCELLED")
        self.assertEqual(drained["failure"]["reason"], "ADMISSION_CANCELLED")

    def test_completed_nonzero_result_survives_racing_service_stop(self):
        owner = self.owner("printf ORIGINAL_DIAGNOSTIC; exit 7")
        original = subprocess.Popen

        def completed(*args, **options):
            process = original(*args, **options)
            process.communicate(timeout=5)
            owner.request_stop()
            return process

        with patch.object(admission_module.subprocess, "Popen", side_effect=completed):
            owner._submit([ROW], BASE)
            item = owner.inflight[1]
            value, failure, drained = self.receive(owner, item)
        self.assertIsNone(failure)
        self.assertEqual(value["verdict"], "HELD")
        self.assertEqual(value["residuals"], ["check:ORIGINAL_DIAGNOSTIC"])
        self.assertEqual(drained["admission"], value)
        self.assertIsNone(drained["failure"])
        attempt = item["attempt"].snapshot()
        self.assertEqual(attempt["result"], value)
        self.assertEqual(attempt["exit"], 7)

    def test_obsolete_head_keeps_explicit_cancellation(self):
        command, marker, partial = self.family()
        owner = self.owner(command)
        owner._submit([ROW], BASE)
        item = owner.inflight[1]
        _until(marker.exists)
        identities = json.loads(marker.read_text())
        original_partial = partial.read_bytes()
        owner._cancel_obsolete_admissions({1: {**ROW, "headRefOid": NEXT_HEAD}})
        self.assertFalse(owner._stop_requested)
        self.assertEqual(item["cancellation"], {
            "number": 1, "head": HEAD, "base": BASE, "expected_pr_head": HEAD,
            "observed_pr_head": NEXT_HEAD, "reason": "HEAD_CHANGED",
        })
        value, failure, drained = self.receive(owner, item)
        self.assertIsNone(value)
        self.assertIsInstance(failure, ReadFailed)
        self.assertEqual(failure.reason, "ADMISSION_CANCELLED")
        self.assertTrue(all(_identity(identity[0]) != identity
                            for identity in identities.values()))
        self.assertEqual(partial.read_bytes(), original_partial)
        self.assertEqual(drained["failure"]["reason"], "ADMISSION_CANCELLED")

    def test_service_stop_exits_memory_wait_without_launching_command(self):
        marker = self.root / "unstarted"
        pressure, observed = threading.Event(), threading.Event()
        pressure.set()

        def available():
            if pressure.is_set():
                observed.set()
                return 0
            return 100

        guard = MemoryGuard(need_gb=10, reserve_gb=8,
                            available_gb=available, poll=0.01)
        owner = self.owner(
            _command(f"from pathlib import Path; Path({str(marker)!r}).touch()"),
            memory=guard)
        owner._submit([ROW], BASE)
        item = owner.inflight[1]
        self.assertTrue(observed.wait(5), "owned worker did not enter memory wait")
        self.assertEqual(guard.running, 0)
        owner.request_stop()
        completed = True
        try:
            item["future"].result(timeout=0.5)
        except TimeoutError:
            completed = False
        finally:
            # Set the original explicit cancellation before releasing pressure,
            # so a failing baseline cannot launch a command during cleanup.
            item["cancelled"].set()
            pressure.clear()
        print("SERVICE_STOP_MEMORY", json.dumps({
            "completed_before_capacity": completed,
            "command_launched": marker.exists(),
        }, sort_keys=True), flush=True)
        value, failure, drained = self.receive(owner, item)
        self.assertTrue(completed, "service stop left the worker waiting for memory")
        self.assertFalse(marker.exists())
        self.assertIsNone(value)
        self.assertIsInstance(failure, ReadFailed)
        self.assertEqual(failure.reason, "ADMISSION_CANCELLED")
        self.assertEqual(guard.running, 0)
        self.assertEqual(drained["failure"]["reason"], "ADMISSION_CANCELLED")


if __name__ == "__main__":
    unittest.main(verbosity=2)
