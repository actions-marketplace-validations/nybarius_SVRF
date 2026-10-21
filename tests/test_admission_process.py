"""Real process-family custody at the isolated admission supervisor boundary."""
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest


HELPER = Path(__file__).resolve().parents[1] / "src/svrf/_admission_process.py"


def python_command(source):
    return "exec " + shlex.quote(sys.executable) + " -c " + shlex.quote(source)


def until(predicate, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    value = predicate()
    if not value:
        raise AssertionError("bounded process observation did not arrive")
    return value


def read_json(path):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return None


def read_pid(path):
    try:
        return int(path.read_text())
    except (FileNotFoundError, ValueError):
        return None


def parent_of(pid):
    try:
        return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
    except FileNotFoundError:
        return None


def gone(pid):
    return not Path(f"/proc/{pid}").exists()


class AdmissionProcess(unittest.TestCase):
    def start(self, command, **kwargs):
        proc = subprocess.Popen([sys.executable, "-I", str(HELPER), command],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, **kwargs)
        self.addCleanup(self.stop, proc)
        return proc

    @staticmethod
    def stop(proc):
        if proc.poll() is None:
            proc.terminate()
        proc.communicate(timeout=10)

    def test_success_and_nonzero_keep_original_output_environment_and_exit(self):
        with tempfile.TemporaryDirectory() as folder:
            env = {**os.environ, "SVRF_SUPERVISOR_TEST": "original environment"}
            for code in (0, 17):
                with self.subTest(code=code):
                    command = python_command(
                        "import os,sys; print(os.environ['SVRF_SUPERVISOR_TEST']); "
                        "print(os.getcwd()); print('original stderr',file=sys.stderr); "
                        f"raise SystemExit({code})")
                    proc = self.start(command, cwd=folder, env=env)
                    stdout, stderr = proc.communicate(timeout=10)
                    self.assertEqual(proc.returncode, code)
                    self.assertEqual(stdout, "original environment\n" + folder + "\n")
                    self.assertEqual(stderr, "original stderr\n")

    def test_a_direct_command_signal_remains_a_signal_exit(self):
        for signum in (signal.SIGTERM, signal.SIGKILL):
            with self.subTest(signum=signum):
                proc = self.start(f"kill -{int(signum)} $$")
                self.assertEqual(proc.communicate(timeout=10), ("", ""))
                self.assertEqual(proc.returncode, -signum)

    def test_orphan_from_worker_thread_is_reaped_before_original_status_returns(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            marker, release, finished = (root / name for name in ("pids", "release", "finished"))
            child = (
                "import pathlib,time\n"
                f"release=pathlib.Path({str(release)!r})\n"
                "end=time.monotonic()+15\n"
                "while not release.exists() and time.monotonic()<end: time.sleep(0.01)\n"
                f"pathlib.Path({str(finished)!r}).write_text('child finished')\n"
                "raise SystemExit(9)\n")
            source = (
                "import json,os,pathlib,subprocess,sys,threading\n"
                "def worker():\n"
                f" child=subprocess.Popen([sys.executable,'-c',{child!r}],start_new_session=True,"
                "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
                f" pathlib.Path({str(marker)!r}).write_text(json.dumps({{'parent':os.getpid(),'child':child.pid}}))\n"
                "thread=threading.Thread(target=worker); thread.start(); thread.join()\n"
                "print('original parent')\nraise SystemExit(23)\n")
            proc = self.start(python_command(source))
            try:
                pids = until(lambda: read_json(marker))
                until(lambda: parent_of(pids["child"]) == proc.pid and gone(pids["parent"]))
                self.assertEqual(os.getsid(pids["child"]), pids["child"])
                self.assertIsNone(proc.poll(), "pipe EOF and direct-parent exit do not release an adopted child")
                release.touch()
                self.assertEqual(proc.communicate(timeout=10), ("original parent\n", ""))
                self.assertEqual(proc.returncode, 23)
                self.assertTrue(finished.exists())
                self.assertTrue(all(gone(pid) for pid in pids.values()))
            finally:
                release.touch()
                self.stop(proc)

    def test_cancel_reaps_nested_detached_family_and_preserves_unrelated_sibling(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            ready, leaf_ready, release = (root / name for name in ("ready", "leaf", "release"))
            wait = ("end=time.monotonic()+15\n"
                    f"while not pathlib.Path({str(release)!r}).exists() and time.monotonic()<end: time.sleep(0.01)\n")
            imports = "import json,os,pathlib,signal,subprocess,sys,threading,time\n"
            ignore = "signal.signal(signal.SIGINT,signal.SIG_IGN); signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
            leaf = imports + ignore + wait
            middle = (imports + ignore + "def worker():\n"
                      f" leaf=subprocess.Popen([sys.executable,'-c',{leaf!r}],start_new_session=True,"
                      "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
                      f" pathlib.Path({str(leaf_ready)!r}).write_text(str(leaf.pid))\n"
                      "thread=threading.Thread(target=worker); thread.start(); thread.join()\n" + wait)
            source = (imports + ignore + "def worker():\n"
                      f" child=subprocess.Popen([sys.executable,'-c',{middle!r}],start_new_session=True,"
                      "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
                      f" pathlib.Path({str(ready)!r}).write_text(json.dumps({{'parent':os.getpid(),'child':child.pid}}))\n"
                      "thread=threading.Thread(target=worker); thread.start(); thread.join()\n" + wait)
            sibling = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"],
                                       start_new_session=True, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL)
            proc = self.start(python_command(source))
            try:
                pids = until(lambda: read_json(ready))
                pids["leaf"] = until(lambda: read_pid(leaf_ready))
                self.assertEqual(os.getsid(pids["child"]), pids["child"])
                self.assertEqual(os.getsid(pids["leaf"]), pids["leaf"])
                proc.terminate()
                self.assertEqual(proc.communicate(timeout=10), ("", ""))
                self.assertEqual(proc.returncode, -signal.SIGKILL)
                self.assertTrue(all(gone(pid) for pid in pids.values()), pids)
                self.assertIsNone(sibling.poll(), "another admission's sibling is outside this custody")
            finally:
                release.touch()
                self.stop(proc)
                if sibling.poll() is None:
                    sibling.terminate()
                sibling.wait(timeout=5)

    def test_ownership_failure_and_prelaunch_cancellation_never_launch_command(self):
        with tempfile.TemporaryDirectory() as folder:
            marker = Path(folder) / "command-ran"
            command = python_command(f"from pathlib import Path; Path({str(marker)!r}).touch()")
            for failure in ("subreaper", "proc", "cancel"):
                with self.subTest(failure=failure):
                    injection = (
                        "import os,runpy,signal,sys\n"
                        f"module=runpy.run_path({str(HELPER)!r}); namespace=module['main'].__globals__\n"
                        "original=namespace['_establish_custody']\n"
                        "def unavailable(): raise OSError('ownership unavailable')\n"
                        "def cancelled():\n original(); os.kill(os.getpid(),signal.SIGTERM)\n")
                    if failure == "proc":
                        injection += "namespace['_children']=unavailable\n"
                    else:
                        injection += "namespace['_establish_custody']=" + (
                            "cancelled" if failure == "cancel" else "unavailable") + "\n"
                    injection += f"raise SystemExit(module['main']([{command!r}]))\n"
                    run = subprocess.run([sys.executable, "-I", "-c", injection],
                                         capture_output=True, text=True, timeout=10)
                    self.assertEqual(run.returncode, 126, run.stderr)
                    self.assertEqual(run.stdout, "")
                    self.assertIn("INTAKE_OUTPUT_UNOBSERVED:ADMISSION_PROCESS_", run.stderr)
                    self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
