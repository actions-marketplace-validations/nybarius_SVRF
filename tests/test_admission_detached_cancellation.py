"""Independent cancellation review against intake's new-session child."""
import json
import os
import shlex
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import tempfile
import unittest

from svrf.admission import Admission
from svrf.errors import ReadFailed


def _live(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False


def _until(predicate, seconds=3):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate(), "bounded original process observation failed"


def check_detached_family(tmp_path):
    root = tmp_path / "command"
    root.mkdir()
    release, marker, cleaned = (root / name for name in ("release", "pids.json", "cleaned"))
    child_code = (
        "import pathlib,time\n"
        f"release=pathlib.Path({str(release)!r})\n"
        "deadline=time.monotonic()+20\n"
        "while not release.exists() and time.monotonic()<deadline: time.sleep(0.01)\n"
    )
    command = f"""import json,os,pathlib,signal,subprocess,sys,time
child=subprocess.Popen([sys.executable,'-c',{child_code!r}], start_new_session=True)
pathlib.Path({str(marker)!r}).write_text(json.dumps({{'parent':os.getpid(),'child':child.pid}}))
try:
    while not pathlib.Path({str(release)!r}).exists(): time.sleep(0.01)
finally:
    pathlib.Path({str(cleaned)!r}).write_text('parent cleanup ran')
    try: os.killpg(child.pid,signal.SIGKILL)
    except ProcessLookupError: pass
    child.wait()
"""
    admission = Admission(SimpleNamespace(root=root),
                          command=shlex.quote(sys.executable)+" -c "+shlex.quote(command),
                          timeout=15)
    cancelled = Event()
    processes = {}
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(admission._execute_command, "head", "base", root,
                             root/"reads.json", cancelled=cancelled)
        try:
            _until(marker.exists)
            processes = json.loads(marker.read_text())
            before = time.monotonic()
            cancelled.set()
            with unittest.TestCase().assertRaisesRegex(ReadFailed, "ADMISSION_CANCELLED"):
                future.result(timeout=8)
            observed = {
                "returned_seconds": round(time.monotonic()-before, 6),
                "parent_live": _live(processes["parent"]),
                "detached_child_live": _live(processes["child"]),
                "parent_cleanup_ran": cleaned.exists(),
            }
            print("INDEPENDENT_CANCEL_RESULT", json.dumps(observed, sort_keys=True), flush=True)
            assert not observed["detached_child_live"], (
                "cancelled admission returned while its detached test process remained alive")
        finally:
            release.touch()
            for pid in processes.values():
                _until(lambda pid=pid: not _live(pid), seconds=3)

class DetachedCancellation(unittest.TestCase):
    def test_cancelled_admission_reaps_a_detached_test_process(self):
        with tempfile.TemporaryDirectory(prefix="svrf-detached-review-") as folder:
            check_detached_family(Path(folder))
