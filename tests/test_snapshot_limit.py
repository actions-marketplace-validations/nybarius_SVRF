"""[train] snapshot_limit is the most open pull requests one snapshot lists; every GitHub reader is built with it."""
import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from svrf import cli, doctor
from svrf.app import build
from svrf.config import ConfigError, from_dict
from svrf.github import RealGitHub

MINIMAL = {"repo": "o/r", "gate": {"commands": ["true"]}}


def pull(number):
    return {"number": number, "title": f"PR {number}", "body": "", "draft": False, "state": "open",
            "labels": [], "head": {"ref": f"b{number}", "sha": f"{number:040x}", "repo": {"full_name": "o/r"}},
            "base": {"ref": "main", "repo": {"full_name": "o/r"}}}


def pages(total):
    rows = [pull(i) for i in range(1, total + 1)]
    return [rows[i:i + 100] for i in range(0, total, 100)]


def run_pages(chunks):
    def run(argv, **kwargs):
        page = int(argv[2].rsplit("=", 1)[1])
        value = chunks[page - 1] if page <= len(chunks) else []
        return subprocess.CompletedProcess(argv, 0, json.dumps(value), "")
    return run


class Recorder:
    limits = []

    def __init__(self, repo, limit=300):
        Recorder.limits.append(limit)
        self.limit = limit

    def merge_commit(self, number):
        return None


class SnapshotLimit(unittest.TestCase):
    def setUp(self):
        Recorder.limits = []

    def test_the_default_limit_lists_a_thousand_open_pull_requests(self):
        self.assertEqual(from_dict(MINIMAL).train.snapshot_limit, 1000)

    def test_a_configured_limit_is_a_positive_whole_count(self):
        self.assertEqual(from_dict({**MINIMAL, "train": {"snapshot_limit": 50}}).train.snapshot_limit, 50)
        for bad in (0, -1, "5", True, 2.5):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                from_dict({**MINIMAL, "train": {"snapshot_limit": bad}})

    def test_a_snapshot_of_440_open_pull_requests_succeeds_with_the_configured_limit(self):
        config = from_dict(MINIMAL)
        with tempfile.TemporaryDirectory() as tmp:
            config.state_dir = Path(tmp)
            gh = build(config).gh
        self.assertEqual(gh.limit, 1000)
        with patch("svrf.github.subprocess.run", run_pages(pages(440))):
            self.assertEqual(len(gh.snapshot()), 440)

    def test_the_old_default_of_300_refuses_440(self):
        from svrf.errors import ReadFailed
        with patch("svrf.github.subprocess.run", run_pages(pages(440))), self.assertRaises(ReadFailed) as caught:
            RealGitHub("o/r").snapshot()
        self.assertEqual(caught.exception.reason, "SNAPSHOT_LIMIT_EXCEEDED")

    def test_the_daemon_reader_gets_the_configured_limit(self):
        config = from_dict({**MINIMAL, "train": {"snapshot_limit": 640}})
        with tempfile.TemporaryDirectory() as tmp:
            config.state_dir = Path(tmp)
            self.assertEqual(build(config).gh.limit, 640)

    def test_the_doctor_reader_gets_the_configured_limit(self):
        config = from_dict({**MINIMAL, "train": {"snapshot_limit": 640}})
        with patch("svrf.github.RealGitHub", Recorder):
            try:
                doctor.run(config, subprocess_run=lambda *a, **k: subprocess.CompletedProcess(a, 1, "", ""))
            except Exception:
                pass
        self.assertEqual(Recorder.limits, [640])

    def test_the_replay_reader_gets_the_configured_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "svrf.toml"
            path.write_text(f'repo = "a/b"\nclone = "{tmp}"\nstate_dir = "{tmp}/state"\n'
                            '[gate]\ncommands = ["true"]\n[train]\nsnapshot_limit = 640\n')
            with patch("svrf.cli.RealGitHub", Recorder), patch("svrf.cli.ensure_clone"), \
                    patch("svrf.cli.RealGit"), patch("svrf.cli.replayer.reconstruct", return_value=([], [])), \
                    contextlib.redirect_stdout(io.StringIO()):
                cli.main(["--config", str(path), "replay", "--merged", "55"])
        self.assertEqual(Recorder.limits, [640])


if __name__ == "__main__":
    unittest.main()
