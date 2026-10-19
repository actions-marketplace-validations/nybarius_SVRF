"""A changed watch configuration drains original admissions before replacing their owner."""
import signal

import pytest

from fakes import DaemonGitHub, DaemonRepo, FakeGate
from test_daemon import daemon
from test_tick_refill import Slow
from svrf import cli


def test_drain_keeps_the_original_future_and_does_not_admit_a_new_head(tmp_path):
    repo = DaemonRepo([1])
    gh, admission = DaemonGitHub(repo), Slow()
    owner = daemon(repo, gh, FakeGate(repo), admission, tmp_path,
                   admission_jobs=2, tick_seconds=0.05)
    try:
        assert owner.tick()['inflight'] == [1]
        pool = owner._pool
        with pytest.raises(RuntimeError, match='ADMISSIONS_STILL_RUNNING'):
            owner.close()
        repo.commits['h2'] = {'lanes': frozenset({0, 2}), 'parents': ('c0',)}
        repo.heads[2], repo.branches['pr-2'] = 'h2', 'h2'
        assert owner.tick(drain=True)['inflight'] == [1]
        assert admission.calls == ['h1']
        admission.release.set()
        result = owner.tick(drain=True)
        assert result['inflight'] == [] and gh.merged == [1]
        assert admission.calls == ['h1']
        owner.close()
        assert pool._shutdown
        # A fresh owner rediscovers the still-pending head from ordinary state.
        resumed = daemon(repo, gh, FakeGate(repo), admission, tmp_path)
        assert resumed.tick()['merged'] == [2]
        assert gh.merged == [1, 2]
        resumed.close()
    finally:
        admission.release.set()


class EndWatch(Exception):
    pass


def exercise(tmp_path, monkeypatch, changes, *, busy=2, stop=False):
    path = tmp_path / 'svrf.toml'
    initial = 'repo="a/b"\n[gate]\ncommands=["true"]\n[train]\ninterval_seconds=0.01\njobs=2\n'
    path.write_text(initial)
    owners, ticks, sleeps, events, handlers = [], [], [], [], {}

    class Owner:
        def __init__(self, config):
            self.jobs = config.train.jobs
            self.inflight, self.remaining, self.closed = {}, busy, False

        def tick(self, *, drain=False):
            assert not self.closed
            ticks.append((self.jobs, drain))
            if len(ticks) == 1:
                self.inflight = {1: object()}
            elif self.inflight:
                self.remaining -= 1
                if not self.remaining:
                    self.inflight = {}
            return {'tick': 'RAN', 'inflight': list(self.inflight)}

        def close(self):
            assert not self.inflight
            self.closed = True

    def build(config, **kw):
        assert not owners or owners[-1].closed
        owner = Owner(config)
        owners.append(owner)
        return owner

    def sleep(seconds):
        sleeps.append(seconds)
        at = len(sleeps)
        if at in changes:
            path.write_text(changes[at](initial))
        if stop and at == 1:
            handlers[signal.SIGTERM](signal.SIGTERM, None)
        if at >= 5:
            raise EndWatch

    def handler(signum, value):
        previous = handlers.get(signum, signal.SIG_DFL)
        handlers[signum] = value
        return previous

    monkeypatch.setattr(cli, 'build', build)
    monkeypatch.setattr(cli, 'ensure_clone', lambda config: None)
    monkeypatch.setattr(cli.time, 'sleep', sleep)
    monkeypatch.setattr(cli, '_print', events.append)
    monkeypatch.setattr(signal, 'signal', handler)
    try:
        result = cli.main(['--config', str(path), 'watch'])
    except EndWatch:
        result = 'still_watching'
    return result, owners, ticks, sleeps, events


def test_changed_configuration_drains_before_rebuilding(tmp_path, monkeypatch):
    result, owners, ticks, sleeps, events = exercise(tmp_path, monkeypatch,
        {1: lambda text: text.replace('jobs=2', 'jobs=3').replace('0.01', '0.02')})
    assert result == 'still_watching'
    assert ticks[:4] == [(2, False), (2, True), (2, True), (3, False)]
    assert len(owners) == 2 and owners[0].closed
    assert sleeps[-1] == 0.02


def test_unread_configuration_defers_new_work_until_valid(tmp_path, monkeypatch):
    _, owners, ticks, _, events = exercise(tmp_path, monkeypatch,
        {1: lambda text: 'broken = [', 3: lambda text: text.replace('jobs=2', 'jobs=3')}, busy=1)
    assert ticks[:3] == [(2, False), (2, True), (3, False)]
    assert len(owners) == 2
    assert any(row.get('reason', '').startswith('CONFIG_UNOBSERVED:') for row in events)


def test_unread_comments_preserve_the_running_owner(tmp_path, monkeypatch):
    _, owners, ticks, _, _ = exercise(tmp_path, monkeypatch, {1: lambda text: text + '# unrelated\n'})
    assert len(owners) == 1 and all(not drain for _, drain in ticks)


def test_termination_drains_existing_work_then_exits(tmp_path, monkeypatch):
    result, owners, ticks, _, _ = exercise(tmp_path, monkeypatch, {}, stop=True)
    assert result == 0
    assert ticks == [(2, False), (2, True), (2, True)]
    assert len(owners) == 1 and owners[0].closed
