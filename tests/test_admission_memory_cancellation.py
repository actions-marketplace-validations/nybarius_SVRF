"""Behavioral controls for cancellation before the admission callable begins."""
import ast
import contextlib
import importlib
import json
import os
from pathlib import Path
import sys
import threading
import time
import types
import unittest

# Host runs name the checked source; a checkout checks its own.
SOURCE_ROOT = os.environ.get("SVRF_SOURCE_ROOT", str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, SOURCE_ROOT + "/src")
owner = importlib.import_module("svrf.daemon")
gate_owner = importlib.import_module("svrf.gate")
ADMISSION_SOURCE = os.environ.get("ADMISSION_SOURCE", SOURCE_ROOT + "/src/svrf/daemon.py")
source = Path(ADMISSION_SOURCE).read_text()
cls = next(n for n in ast.parse(source).body if isinstance(n, ast.ClassDef) and n.name == "Daemon")
node = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "_read_admission")
namespace = dict(owner.__dict__)
exec(compile(ast.Module(body=[node], type_ignores=[]), ADMISSION_SOURCE, "exec"), namespace)
read_admission = namespace["_read_admission"]
gate_source = os.environ.get("GATE_SOURCE")
if gate_source:
    gate_namespace = dict(gate_owner.__dict__)
    exec(compile(Path(gate_source).read_bytes(), gate_source, "exec"), gate_namespace)
    MemoryGuard = gate_namespace["MemoryGuard"]
else:
    MemoryGuard = gate_owner.MemoryGuard

class PreAdmissionCancellation(unittest.TestCase):
    def invoke(self, admission, memory, cancelled=None):
        return read_admission(types.SimpleNamespace(admission=admission, memory=memory),
                              {"headRefOid":"head-1", "number":1}, "base-1", cancelled)

    def test_cancelled_wait_returns_without_capacity_or_callable(self):
        pressure = threading.Event()
        pressure.set()
        observed_wait = threading.Event()
        cancelled = threading.Event()
        completed = threading.Event()
        calls, outputs = [], []
        def available():
            if pressure.is_set():
                observed_wait.set()
                return 0
            return 100
        guard = MemoryGuard(need_gb=10, reserve_gb=8, available_gb=available, poll=0.01)
        def admission(*args, **kwargs):
            calls.append((args, kwargs))
            return {"verdict":"MERGEABLE"}
        def work():
            outputs.append(self.invoke(admission, guard, cancelled))
            completed.set()
        thread = threading.Thread(target=work)
        thread.start()
        try:
            self.assertTrue(observed_wait.wait(2), "worker never reached memory pressure")
            started = time.monotonic()
            cancelled.set()
            returned_before_capacity = completed.wait(0.4)
            elapsed = time.monotonic() - started
        finally:
            pressure.clear()
            thread.join(2)
        self.assertFalse(thread.is_alive(), "owned worker did not stop")
        self.assertTrue(returned_before_capacity,
                        "cancelled work kept its executor lane while capacity remained unavailable")
        self.assertEqual(calls, [])
        result, error = outputs[0]
        self.assertIsNone(result)
        self.assertIsInstance(error, owner.ReadFailed)
        self.assertEqual(error.reason, "ADMISSION_CANCELLED")
        self.assertEqual(guard.running, 0)
        print(json.dumps({"cancelled_wait_seconds":elapsed,"poll_seconds":guard.poll}))

    def test_cancel_after_reservation_does_not_enter_callable(self):
        cancelled, calls = threading.Event(), []
        class CancelOnEntry:
            def __init__(self): self.running = 0
            @contextlib.contextmanager
            def admit(self, **options):
                self.running += 1
                cancelled.set()
                try: yield
                finally: self.running -= 1
        guard = CancelOnEntry()
        result, error = self.invoke(lambda *a, **k: calls.append(a), guard, cancelled)
        self.assertEqual(calls, [])
        self.assertIsNone(result)
        self.assertIsInstance(error, owner.ReadFailed)
        self.assertEqual(error.reason, "ADMISSION_CANCELLED")
        self.assertEqual(guard.running, 0)

    def test_cancellation_after_callable_entry_preserves_real_failure(self):
        cancelled = threading.Event()
        guard = MemoryGuard(need_gb=10,reserve_gb=8,available_gb=lambda:100,poll=0.01)
        failure = ValueError("original substantive failure")
        calls = []
        def admission(head, base, **options):
            self.assertEqual(guard.running, 1)
            self.assertIs(options["cancelled"], cancelled)
            calls.append((head, base))
            cancelled.set()
            raise failure
        result, error = self.invoke(admission, guard, cancelled)
        self.assertEqual(calls, [("head-1","base-1")])
        self.assertIsNone(result)
        self.assertIs(error, failure)
        self.assertEqual(guard.running, 0)

    def test_no_event_retains_legacy_guard_and_callable_signature(self):
        calls, entered = [], []
        class LegacyGuard:
            @contextlib.contextmanager
            def admit(self):
                entered.append("enter")
                try: yield
                finally: entered.append("release")
        def admission(head, base):
            calls.append((head,base))
            return {"verdict":"MERGEABLE"}
        result, error = self.invoke(admission, LegacyGuard())
        self.assertIsNone(error)
        self.assertEqual(result, {"verdict":"MERGEABLE"})
        self.assertEqual(calls,[("head-1","base-1")])
        self.assertEqual(entered,["enter","release"])

    def test_cancelled_before_wait_does_not_acquire(self):
        cancelled=threading.Event()
        cancelled.set()
        class Untouched:
            def admit(self, **options): raise AssertionError("unexpected reservation")
        result,error=self.invoke(lambda *a,**k: self.fail("unexpected callable"),Untouched(),cancelled)
        self.assertIsNone(result)
        self.assertIsInstance(error,owner.ReadFailed)
        self.assertEqual(error.reason,"ADMISSION_CANCELLED")

if __name__ == "__main__":
    unittest.main(verbosity=2)
