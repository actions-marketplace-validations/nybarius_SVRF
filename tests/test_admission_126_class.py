"""An admission command that exits 126 names why on its output; SVRF records that class line as the reason."""
import shlex
import tempfile
import unittest
from pathlib import Path

from fakes import DaemonGitHub, DaemonRepo, FakeGate
from test_daemon import daemon
from svrf.admission import Admission
from svrf.errors import ReadFailed

CLASSES = ["IO_CHARGE_UNOBSERVED:worker-3", "IO_POOL_CAPACITY", "INTAKE_BUDGET_YIELD:3/8",
           "INTAKE_OUTPUT_UNOBSERVED:suite-x"]


def printing(*lines, exit_code=126):
    return "".join(f"echo {shlex.quote(line)}; " for line in lines) + f"exit {exit_code}"


class Exit126(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.repo = DaemonRepo([31])
        self.repo.root = self.tmp
        self.repo.merge_preview = lambda *a: {"status": "CLEAN", "conflicts": []}
        self.repo.changed_paths = lambda *a: ["lane:31"]

    def reason(self, command):
        with self.assertRaises(ReadFailed) as caught:
            Admission(self.repo, command=command)("h31", "c0")
        return caught.exception.reason

    def test_each_class_line_is_the_recorded_reason(self):
        for line in CLASSES:
            with self.subTest(line=line):
                self.assertEqual(self.reason(printing("noise before", line)), line)

    def test_the_class_line_is_read_from_error_output_too_and_the_last_one_wins(self):
        self.assertEqual(self.reason("echo IO_POOL_CAPACITY >&2; exit 126"), "IO_POOL_CAPACITY")
        self.assertEqual(self.reason(printing("IO_POOL_CAPACITY", "INTAKE_BUDGET_YIELD:1/2")),
                         "INTAKE_BUDGET_YIELD:1/2")

    def test_no_class_line_keeps_the_bare_code(self):
        self.assertEqual(self.reason(printing("some diagnostic")), "ADMISSION_COMMAND_UNAVAILABLE:126")
        self.assertEqual(self.reason("exit 126"), "ADMISSION_COMMAND_UNAVAILABLE:126")

    def test_a_class_line_on_another_exit_code_is_not_read(self):
        answer = Admission(self.repo, command=printing("IO_POOL_CAPACITY", exit_code=1))("h31", "c0")
        self.assertEqual(answer["residuals"], ["check:IO_POOL_CAPACITY"])

    def test_every_class_is_retry_later_with_that_reason_and_never_a_hold(self):
        for line in CLASSES:
            with self.subTest(line=line), tempfile.TemporaryDirectory() as tmp:
                repo = DaemonRepo([31])
                repo.root = Path(tmp)
                repo.merge_preview = lambda *a: {"status": "CLEAN", "conflicts": []}
                repo.changed_paths = lambda *a: ["lane:31"]
                gh = DaemonGitHub(repo)
                owner = daemon(repo, gh, FakeGate(repo), Admission(repo, command=printing(line)), tmp, jobs=1)
                result = owner.tick()
                self.assertEqual(result["retry_later"], [31])
                self.assertEqual(result["reasons"]["31"], line)
                self.assertEqual(result["held"], [])
                self.assertEqual(owner.load_state()["held"], {})
