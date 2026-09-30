"""Ready admissions reach the existing train while other checks are still running."""
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
import json
from pathlib import Path
import threading
from unittest.mock import patch

from fakes import DaemonGitHub, DaemonRepo, FakeGate
from test_daemon import daemon
from svrf.errors import ReadFailed
from svrf.gate import MemoryGuard


class BlockedAdmission:
    def __init__(self, values=None):
        self.release = threading.Event()
        self.started = threading.Event()
        self.finished = threading.Event()
        self.calls = []
        self.values = values or {}

    def __call__(self, head, base):
        self.calls.append((head, base))
        if head == 'h1':
            self.started.set()
            if not self.release.wait(10):
                raise ReadFailed('TEST_RELEASE_TIMEOUT')
            self.finished.set()
        value = self.values.get(head, {'verdict': 'MERGEABLE', 'residuals': []})
        if isinstance(value, Exception):
            raise value
        return value


@contextmanager
def running(owner, admission, *release):
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(owner.tick)
        try:
            assert admission.started.wait(3)
            yield future
        finally:
            admission.release.set()
            for event in release:
                event.set()
            future.result(timeout=10)


def signal_gate(gate, event):
    original = gate.run
    def run(*args, **kwargs):
        event.set()
        return original(*args, **kwargs)
    gate.run = run


def receipts(result):
    return [json.loads(Path(path).read_text()) for path in result['receipts']]


def test_ready_head_lands_before_earlier_admission_finishes_and_keeps_both_receipts(tmp_path):
    repo = DaemonRepo([1, 2])
    gh, gate, admission = DaemonGitHub(repo), FakeGate(repo), BlockedAdmission()
    landed = threading.Event()
    original = gh.merge
    def merge(number, sha):
        value = original(number, sha)
        if number == 2:
            landed.set()
        return value
    gh.merge = merge
    owner = daemon(repo, gh, gate, admission, tmp_path, jobs=2)
    with running(owner, admission) as future:
        assert landed.wait(3), 'ready head waited for an unrelated admission'
        assert not admission.finished.is_set()
        assert gh.merged == [2]
    result = future.result()
    assert result['admitted'] == [1, 2]
    assert result['merged'] == [2, 1]
    assert result['gates'] == 2
    assert result['receipt'] == result['receipts'][-1]
    assert len(set(result['receipts'])) == 2
    records = receipts(result)
    assert [row['requested'] for row in records] == [[2], [1]]
    assert all(m['identity'] for row in records for m in row['merges'])
    assert repo.tree(repo.main) == 'T0,1,2'
    assert gh.calls['graphql'] == 1
    assert sorted(admission.calls) == [('h1', 'c0'), ('h2', 'c0')]
    assert {owner.round_inputs[n]['admission_base'] for n in (1, 2)} == {'c0'}


def test_read_failure_does_not_hold_or_block_another_completed_head(tmp_path):
    repo = DaemonRepo([1, 2, 3])
    gh, gate = DaemonGitHub(repo), FakeGate(repo)
    admission = BlockedAdmission({'h2': ReadFailed('DEPENDENCY_UNAVAILABLE')})
    gated = threading.Event()
    signal_gate(gate, gated)
    owner = daemon(repo, gh, gate, admission, tmp_path, jobs=3)
    with running(owner, admission) as future:
        assert gated.wait(3)
        assert not admission.finished.is_set()
    result = future.result()
    assert result['admitted'] == [1, 3]
    assert result['held'] == []
    assert result['retry_later'] == [2]
    assert result['reasons']['2'] == 'DEPENDENCY_UNAVAILABLE'
    assert result['merged'] == [3, 1]


def test_red_gate_is_retained_when_a_later_group_succeeds(tmp_path):
    repo = DaemonRepo([1, 2])
    gh, gate, admission = DaemonGitHub(repo), FakeGate(repo, bad={2}), BlockedAdmission()
    gated = threading.Event()
    signal_gate(gate, gated)
    owner = daemon(repo, gh, gate, admission, tmp_path, jobs=2)
    with running(owner, admission) as future:
        assert gated.wait(3)
    result = future.result()
    assert result['held'] == [2]
    assert result['merged'] == [1]
    assert [r['requested'] for r in receipts(result)] == [[2], [1]]
    assert owner.load_state()['held']['2']['reason'] == 'GATE_RED'


def test_tree_mismatch_stops_later_groups_in_the_same_tick(tmp_path):
    repo = DaemonRepo([1, 2])
    gh = DaemonGitHub(repo, mismatch_on=2)
    gate, admission, gated = FakeGate(repo), BlockedAdmission(), threading.Event()
    signal_gate(gate, gated)
    owner = daemon(repo, gh, gate, admission, tmp_path, jobs=2)
    with running(owner, admission) as future:
        assert gated.wait(3)
    result = future.result()
    assert gh.merged == [2]
    assert result['merged'] == []
    assert result['stopped'] is True
    assert result['retry_later'] == [1]
    assert result['reasons']['1'] == 'TRAIN_STOPPED'
    assert len(gate.trees) == 1
    assert len(receipts(result)) == 1
    assert receipts(result)[0]['merges'][0]['identity'] is False


def test_admission_and_early_gate_share_the_same_memory_guard(tmp_path):
    repo = DaemonRepo([1, 2])
    gh, gate, admission = DaemonGitHub(repo), FakeGate(repo), BlockedAdmission()
    guard = MemoryGuard(need_gb=10, reserve_gb=0, available_gb=lambda: 20, poll=0.001)
    gated, release_gate = threading.Event(), threading.Event()
    observed = []
    original = gate.run
    def run(*args, **kwargs):
        observed.append(guard.running)
        gated.set()
        assert release_gate.wait(10)
        return original(*args, **kwargs)
    gate.run = run
    owner = daemon(repo, gh, gate, admission, tmp_path, jobs=2, memory=guard)
    with running(owner, admission, release_gate):
        assert gated.wait(3)
        assert not admission.finished.is_set()
        assert observed == [2]
    assert guard.running == 0


def test_late_hold_keeps_its_original_base_and_reopens_after_an_early_merge(tmp_path):
    repo = DaemonRepo([1, 2])
    gh, gate = DaemonGitHub(repo), FakeGate(repo)
    admission = BlockedAdmission({'h1': {'verdict': 'HELD', 'residuals': ['check:dependency'],
                                        'changed': ['lane:2']}})
    gated = threading.Event()
    signal_gate(gate, gated)
    owner = daemon(repo, gh, gate, admission, tmp_path, jobs=2)
    with running(owner, admission) as future:
        assert gated.wait(3)
    assert future.result()['held'] == [1]
    held = owner.load_state()['held']['1']
    assert held['base_sha'] == 'c0'
    assert held['watch_digest'] == repo.watch_digest('c0', ['lane:2'])
    owner.tick()
    assert [h for h, _ in admission.calls].count('h1') == 2


def test_already_completed_heads_remain_one_batched_train(tmp_path):
    class CompletedExecutor:
        def __init__(self, **kwargs):
            pass
        def submit(self, call, *args):
            future = Future()
            future.set_result(call(*args))
            return future
        def shutdown(self, **kwargs):
            pass
    repo = DaemonRepo([1, 2, 3])
    gh, gate = DaemonGitHub(repo), FakeGate(repo)
    owner = daemon(repo, gh, gate, lambda h, b: {'verdict': 'MERGEABLE'}, tmp_path, jobs=4)
    with patch('svrf.daemon.ThreadPoolExecutor', CompletedExecutor):
        result = owner.tick()
    assert result['merged'] == [1, 2, 3]
    assert len(gate.trees) == 1
    assert len(receipts(result)) == 1
    assert receipts(result)[0]['requested'] == [1, 2, 3]
