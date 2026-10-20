"""A tick refills freed admission slots from the current snapshot, retains its durations, and
carries a still-running admission across its boundary without duplicating or dropping it."""
import json
from pathlib import Path
import threading
import time

from fakes import DaemonGitHub, DaemonRepo, FakeGate
from test_daemon import daemon


class Slow:
    """Head h1 blocks until released; every other head answers at once."""

    def __init__(self, slow='h1'):
        self.slow, self.release, self.started = slow, threading.Event(), threading.Event()
        self.calls, self.lock = [], threading.Lock()

    def __call__(self, head, base):
        with self.lock:
            self.calls.append(head)
        if head == self.slow:
            self.started.set()
            assert self.release.wait(10), 'slow admission never released'
        return {'verdict': 'MERGEABLE', 'residuals': []}


def receipts(result):
    return [json.loads(Path(p).read_text()) for p in result['receipts']]


def test_tick_retains_wall_time_and_per_admission_durations(tmp_path):
    repo = DaemonRepo([1, 2])
    gh, gate = DaemonGitHub(repo), FakeGate(repo)

    def admission(head, base):
        time.sleep(0.05 if head == 'h1' else 0.01)
        return {'verdict': 'MERGEABLE', 'residuals': []}

    owner = daemon(repo, gh, gate, admission, tmp_path, jobs=2)
    result = owner.tick()
    assert result['wall_seconds'] > 0.04
    assert set(result['admission_seconds']) == {'1', '2'}
    assert result['admission_seconds']['1'] >= 0.045
    assert 0 < result['admission_seconds']['2'] < result['admission_seconds']['1']
    kept = sorted((tmp_path / 'receipts').glob('tick-*.json'))
    assert len(kept) == 1
    row = json.loads(kept[0].read_text())
    assert row['wall_seconds'] == result['wall_seconds']
    assert row['admission_seconds'] == result['admission_seconds']
    assert row['merged'] == result['merged']


def test_freed_slot_is_refilled_from_the_current_snapshot_while_a_slow_read_runs(tmp_path):
    # one slow head and seven quick ones; two slots and a round of two reads: today the
    # round reads the slow head and one quick head, then waits for the slow one
    repo = DaemonRepo([1, 2, 3, 4, 5, 6, 7, 8])
    gh, gate, admission = DaemonGitHub(repo), FakeGate(repo), Slow()
    owner = daemon(repo, gh, gate, admission, tmp_path, jobs=2, admission_jobs=2, admission_round_reads=2)
    landed = threading.Event()
    original = gh.merge

    def merge(number, sha):
        value = original(number, sha)
        if len([n for n in gh.merged if n != 1]) + 1 >= 7 and number != 1:
            landed.set()
        return value

    gh.merge = merge
    out = {}
    thread = threading.Thread(target=lambda: out.update(owner.tick()))
    thread.start()
    try:
        assert admission.started.wait(5)
        assert landed.wait(5), 'the quick heads waited for the slow read'
        assert 1 not in gh.merged
    finally:
        admission.release.set()
        thread.join(15)
    assert sorted(gh.merged) == [1, 2, 3, 4, 5, 6, 7, 8]
    assert admission.calls.count('h1') == 1


def test_a_running_admission_crosses_the_tick_boundary_once_with_its_receipts(tmp_path):
    repo = DaemonRepo([1, 2])
    gh, gate, admission = DaemonGitHub(repo), FakeGate(repo), Slow()
    owner = daemon(repo, gh, gate, admission, tmp_path, jobs=2, admission_jobs=2, tick_seconds=0.3)
    first = owner.tick()
    assert admission.started.is_set()
    assert gh.merged == [2]
    assert first['inflight'] == [1]
    assert first['merged'] == [2]
    # a new head arrives; the next snapshot excludes head 1 (live) and reads the new one
    repo.commits['h3'] = {'lanes': frozenset({0, 3}), 'parents': ('c0',)}
    repo.heads[3] = 'h3'
    repo.branches['pr-3'] = 'h3'
    second = owner.tick()
    assert gh.merged == [2, 3]
    assert second['inflight'] == [1]
    assert admission.calls.count('h1') == 1, 'a live admission was started again'
    admission.release.set()
    third = owner.tick()
    assert third['inflight'] == []
    assert gh.merged == [2, 3, 1]
    assert third['merged'] == [1]
    assert admission.calls.count('h1') == 1
    assert [r['requested'] for r in receipts(first) + receipts(second) + receipts(third)] == [[2], [3], [1]]
    assert all(m['identity'] for r in receipts(third) for m in r['merges'])


def test_tick_seconds_is_a_positive_number_or_unset():
    import pytest
    from svrf.config import ConfigError, from_dict
    base = {'repo': 'example/project', 'gate': {'commands': ['make test']}}
    assert from_dict(base).train.tick_seconds is None
    assert from_dict({**base, 'train': {'tick_seconds': 240}}).train.tick_seconds == 240
    for bad in (0, -1, '5', True):
        with pytest.raises(ConfigError):
            from_dict({**base, 'train': {'tick_seconds': bad}})


class Declared:
    """Declared head `fail` raises a read failure; head `after` waits until it has failed; the rest answer at once."""

    def __init__(self, fail='h1', after='h2'):
        self.fail, self.after, self.failed = fail, after, threading.Event()
        self.calls, self.lock = [], threading.Lock()

    def __call__(self, head, base):
        from svrf.errors import ReadFailed
        with self.lock:
            self.calls.append(head)
        if head == self.fail:
            self.failed.set()
            raise ReadFailed('GH_UNAVAILABLE')
        if head == self.after:
            assert self.failed.wait(10)
            time.sleep(0.05)
        return {'verdict': 'MERGEABLE', 'residuals': []}


def test_refill_does_not_take_undeclared_heads_while_a_declared_read_failed(tmp_path):
    repo = DaemonRepo([1, 2, 3, 4, 5, 6, 7, 8])
    gh, gate, admission = DaemonGitHub(repo), FakeGate(repo), Declared()
    owner = daemon(repo, gh, gate, admission, tmp_path, jobs=2, admission_jobs=2, admission_round_reads=2,
                   admission_priority_command="printf '1\\n2\\n'")
    result = owner.tick()
    assert sorted(admission.calls) == ['h1', 'h2']
    assert result['retry_later'] == [1]
    assert result['refill_stopped'] == {'declared_unread': [1]}
    assert result['inflight'] == []
    assert gh.merged == [2]


def test_refill_takes_a_declared_unread_head_ahead_of_undeclared_ones(tmp_path):
    repo = DaemonRepo([1, 2, 3, 4, 5, 6, 7, 8])
    gh, gate, admission = DaemonGitHub(repo), FakeGate(repo), Declared()
    owner = daemon(repo, gh, gate, admission, tmp_path, jobs=2, admission_jobs=2, admission_round_reads=2,
                   admission_priority_command="printf '1\\n2\\n3\\n'")
    result = owner.tick()
    assert sorted(admission.calls) == ['h1', 'h2', 'h3']
    assert result['refill_stopped'] == {'declared_unread': [1]}
    assert sorted(gh.merged) == [2, 3]


def test_an_unreadable_priority_command_leaves_refill_unchanged(tmp_path):
    repo = DaemonRepo([1, 2, 3, 4, 5, 6, 7, 8])
    gh, gate, admission = DaemonGitHub(repo), FakeGate(repo), Declared()
    owner = daemon(repo, gh, gate, admission, tmp_path, jobs=2, admission_jobs=2, admission_round_reads=2,
                   admission_priority_command='exit 4')
    result = owner.tick()
    assert 'refill_stopped' not in result
    assert len(admission.calls) == 8
    assert sorted(gh.merged) == [2, 3, 4, 5, 6, 7, 8]
