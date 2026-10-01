"""A failed admission return remains readable while the next head is pending."""
import json
import tempfile
import unittest
from pathlib import Path

from fakes import Admission, DaemonGitHub, DaemonRepo, FakeGate
from test_daemon import daemon
from svrf.errors import ReadFailed


class RoundReturn(unittest.TestCase):
    def test_failed_admission_retains_reason_and_source_in_latest_round(self):
        repo = DaemonRepo([7])
        head, base = repo.heads[7], repo.main
        reason = "ADMISSION_COMMAND_UNAVAILABLE:126"
        admission = Admission(failures={head: ReadFailed(reason)})
        with tempfile.TemporaryDirectory() as directory:
            owner = daemon(repo, DaemonGitHub(repo), FakeGate(repo), admission, directory)
            result = owner.tick()
            state = json.loads((Path(directory) / "state.json").read_text())
        self.assertEqual(result["retry_later"], [7])
        self.assertEqual(state["last_tick"].get("retry_later"), [7])
        self.assertEqual(state["last_tick"].get("reasons"), {"7": reason})
        self.assertEqual(state["last_tick"].get("admission_sources"),
                         {"7": {"head": head, "base": base}})
        self.assertEqual(state["held"], {})
