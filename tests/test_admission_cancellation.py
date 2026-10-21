"""Retire obsolete admission commands without admitting or holding their old source."""
import json
import os
from pathlib import Path
import shlex
import sys
import time
from types import SimpleNamespace

import pytest

from fakes import DaemonGitHub, DaemonRepo, FakeGate
from test_daemon import daemon
from svrf.admission import Admission
from svrf.errors import ReadFailed


def until(predicate, seconds=3):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate(), "bounded test observation did not complete"


def live(pid):
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        return state != "Z"
    except FileNotFoundError:
        return False


@pytest.fixture
def running(tmp_path):
    root = tmp_path / "commands"
    root.mkdir()
    marker, release, starts = [root / name for name in ("started.json", "release", "starts")]
    repo = DaemonRepo([1])
    repo.root = root
    repo.merge_preview = lambda base, head: {"status": "CLEAN", "conflicts": []}
    repo.changed_paths = lambda base, head: ["lane:1"]
    gh = DaemonGitHub(repo)
    child = ("import pathlib,time; p=pathlib.Path(" + repr(str(release)) + ")\n"
             "while not p.exists(): time.sleep(0.01)\n")
    command = f"""import json,os,pathlib,subprocess,sys,time
root=pathlib.Path({str(root)!r})
with (root/'starts').open('a') as stream: stream.write(os.environ['SVRF_HEAD']+'\\n')
if os.environ['SVRF_HEAD']=='h1':
    child=subprocess.Popen([sys.executable,'-c',{child!r}])
    (root/'started.json').write_text(json.dumps({{'parent':os.getpid(),'child':child.pid}}))
    while not (root/'release').exists(): time.sleep(0.01)
    child.wait()
"""
    admission = Admission(repo, command=shlex.quote(sys.executable) + " -c " + shlex.quote(command),
                          timeout=10)
    owner = daemon(repo, gh, FakeGate(repo), admission, tmp_path / "state",
                   jobs=1, admission_jobs=1, admission_round_reads=1, tick_seconds=0.05)
    first = owner.tick()
    assert first["inflight"] == [1]
    until(marker.exists)
    processes = json.loads(marker.read_text())
    fixture = SimpleNamespace(owner=owner, repo=repo, gh=gh, starts=starts, processes=processes)
    try:
        yield fixture
    finally:
        release.touch()
        deadline = time.monotonic() + 5
        while owner.inflight and time.monotonic() < deadline:
            owner.tick(drain=True)
            time.sleep(0.01)
        owner.close()


def ticks(fixture, finished, seconds=2):
    rows = []
    deadline = time.monotonic() + seconds
    while not finished() and time.monotonic() < deadline:
        rows.append(fixture.owner.tick())
    assert finished(), "obsolete admission kept its occupied slot"
    return rows


def cancellations(rows):
    return [item for row in rows for item in row.get("admission_cancelled", [])]


def test_closed_pr_stops_parent_and_child_and_releases_the_slot(running):
    running.repo.heads.pop(1)
    running.gh.pull = lambda number: {"number": number, "state": "closed", "head_sha": "h1"}
    rows = ticks(running, lambda: not running.owner.inflight)
    assert any(item["reason"] == "PR_CLOSED" for item in cancellations(rows))
    assert all(row["admitted"] == [] and row["held"] == [] for row in rows)
    assert running.gh.merged == []
    until(lambda: not any(live(pid) for pid in running.processes.values()))
    assert running.owner.load_state()["held"] == {}
    records = [json.loads(path.read_text()) for path in running.owner.receipts.glob("tick-*.json")]
    assert any(record.get("admission_cancelled") for record in records)


def test_a_changed_head_releases_the_old_slot_and_checks_its_replacement(running):
    replacement = running.repo.move_head(1)
    rows = ticks(running, lambda: 1 in running.gh.merged)
    assert any(item["reason"] == "HEAD_CHANGED" for item in cancellations(rows))
    assert running.starts.read_text().splitlines() == ["h1", replacement]
    assert running.gh.merged == [1]
    assert all(row["held"] == [] for row in rows)
    until(lambda: not any(live(pid) for pid in running.processes.values()))


def test_a_missing_snapshot_row_does_not_prove_the_pr_closed(running):
    running.repo.heads.pop(1)
    original = running.owner.inflight[1]["future"]
    row = running.owner.tick()
    assert running.owner.inflight[1]["future"] is original
    assert not row.get("admission_cancelled")
    assert all(live(pid) for pid in running.processes.values())


def test_an_unreadable_closed_pr_lookup_preserves_the_original_read(running):
    running.repo.heads.pop(1)

    def unread(number):
        raise ReadFailed("TEMPORARY_GITHUB_FAILURE")

    running.gh.pull = unread
    row = running.owner.tick()
    assert 1 in running.owner.inflight
    assert not row.get("admission_cancelled")
    assert all(live(pid) for pid in running.processes.values())


def test_irrelevant_main_movement_preserves_the_current_head_and_command(running):
    original = running.owner.inflight[1]["future"]
    running.repo.main = running.repo._new({0, 77}, (running.repo.main,))
    row = running.owner.tick()
    assert running.owner.inflight[1]["future"] is original
    assert not row.get("admission_cancelled")
    assert running.starts.read_text().splitlines() == ["h1"]


def test_a_reland_is_watched_at_the_original_pr_head(running):
    item = running.owner.inflight[1]
    original = dict(item["row"])
    item["row"] = {**original, "headRefOid": "synthetic-reland"}
    item["reland"] = {"row": original}
    row = running.owner.tick()
    assert 1 in running.owner.inflight
    assert not row.get("admission_cancelled")
    assert all(live(pid) for pid in running.processes.values())
