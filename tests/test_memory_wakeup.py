"""Receive the checked wakeup into the original gate and reservation owner."""
from types import SimpleNamespace
import threading

from svrf import gate


def test_release_wakes_default_sleep_waiter_without_consuming_poll(monkeypatch):
    """Control the elapsed-poll boundary; run the real MemoryGuard and errors owner."""
    denied = threading.Event()
    allow_poll = threading.Event()
    entered = threading.Event()
    errors = []
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        assert allow_poll.wait(5), "controlled poll was not released"

    # This is the default-sleeper identity boundary, with time held before a poll
    # elapses. No reservation, condition, or production check_stop is mocked.
    monkeypatch.setattr(gate, "time", SimpleNamespace(sleep=sleep))

    def available():
        if memory.running:
            denied.set()
        return 18

    memory = gate.MemoryGuard(need_gb=10, reserve_gb=8,
                              available_gb=available, sleep=sleep, poll=15)
    held = memory.admit()
    held.__enter__()
    holder_active = True

    def waiter():
        try:
            with memory.admit():
                assert memory.running == 1
                entered.set()
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=waiter, name="memory-wakeup-control", daemon=True)
    worker.start()
    try:
        assert denied.wait(2), "waiter did not reach the actual capacity predicate"
        held.__exit__(None, None, None)
        holder_active = False
        assert entered.wait(2), "released reservation did not wake the waiting gate"
        assert not slept, "a poll was charged after the reservation became available"
    finally:
        if holder_active:
            held.__exit__(None, None, None)
        allow_poll.set()
        worker.join(2)
    assert not worker.is_alive()
    assert not errors
    assert memory.running == 0
