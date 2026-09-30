"""A command's reopening evidence must reach the daemon that retains its hold."""
import json
import shlex
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fakes import DaemonGitHub, DaemonRepo, FakeGate
from test_daemon import daemon
from svrf.admission import Admission
from svrf.daemon import Daemon
from svrf.errors import ReadFailed


class CommandReads(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo = DaemonRepo([31])
        self.repo.root = self.root
        self.repo.merge_preview = lambda *a: {"status": "CLEAN", "conflicts": []}
        self.repo.changed_paths = lambda *a: ["lane:31"]

    def command(self, mutation="", *, exit_code=1):
        code = """
import json, os
from pathlib import Path
p = os.environ.get('SVRF_ADMISSION_READS')
value = {'schema': 'svrf.admission-reads/1', 'head': os.environ['SVRF_HEAD'],
         'base': os.environ['SVRF_BASE'], 'consumed': [{'kind': 'PATH', 'id': 'lane:32'}]}
""" + mutation + """
if p:
    Path(p).write_text(json.dumps(value))
print('original refusal')
""" + f"raise SystemExit({exit_code})\n"
        return shlex.quote(sys.executable) + " -c " + shlex.quote(code)

    def test_original_refusal_carries_source_bound_reads(self):
        answer = Admission(self.repo, command=self.command())('h31', 'c0')
        self.assertEqual(answer['residuals'], ['check:original refusal'])
        self.assertEqual(answer.get('consumed'), [{'kind': 'PATH', 'id': 'lane:32'}])

    def test_hold_is_quiet_until_its_consumed_dependency_changes(self):
        original = Admission(self.repo, command=self.command())
        calls = []
        def read(head, base):
            calls.append((head, base))
            return original(head, base)
        owner = daemon(self.repo, DaemonGitHub(self.repo), FakeGate(self.repo), read, self.root)
        owner.tick()
        owner.tick()
        self.assertEqual(len(calls), 1)
        self.repo.main = self.repo._new({0, 99}, (self.repo.main,))
        owner.tick()
        self.assertEqual(len(calls), 1)
        self.repo.main = self.repo._new({0, 99, 32}, (self.repo.main,))
        owner.tick()
        self.assertEqual(len(calls), 2)

    def test_directory_and_checker_reads_survive_the_watch_projection(self):
        value = {'changed': ['own.py'], 'consumed': [
            {'kind': 'PATH', 'id': 'optional.py'},
            {'kind': 'DIR', 'id': 'dir:fixtures'},
            {'kind': 'CODE', 'id': 'checker.py'},
            {'kind': 'COMMIT', 'id': 'source-sha'}]}
        self.assertEqual(Daemon._consumed_paths(value),
                         ['checker.py', 'fixtures', 'optional.py', 'own.py'])

    def test_malformed_or_moved_evidence_is_an_unavailable_read(self):
        mutations = ["value['head'] = 'other'", "value['base'] = 'other'",
                     "value['schema'] = 'unknown'", "value['consumed'] = None",
                     "value['consumed'] = [{'kind': 'PATH', 'id': '../outside'}]",
                     "value['consumed'] = [{'kind': 'PATH', 'id': ':(glob)**'}]",
                     "value['consumed'] = [{'kind': 'PATH', 'id': '/absolute'}]",
                     "value['consumed'] = [{'kind': 'UNKNOWN', 'id': 'x'}]"]
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.assertRaises(ReadFailed):
                Admission(self.repo, command=self.command(mutation))('h31', 'c0')

    def test_success_still_validates_supplied_read_evidence(self):
        with self.assertRaises(ReadFailed):
            Admission(self.repo, command=self.command("value['head'] = 'other'", exit_code=0))('h31', 'c0')

    def test_legacy_commands_and_unavailable_exits_keep_their_meaning(self):
        self.assertEqual(Admission(self.repo, command='true')('h31', 'c0')['verdict'], 'MERGEABLE')
        with self.assertRaises(ReadFailed):
            Admission(self.repo, command=self.command(exit_code=126))('h31', 'c0')

    def test_concurrent_commands_do_not_share_read_files(self):
        self.repo.is_ancestor = lambda *args: False
        command = self.command("value['consumed'][0]['id'] = os.environ['SVRF_HEAD']")
        owner = Admission(self.repo, command=command)
        with ThreadPoolExecutor(max_workers=2) as pool:
            rows = list(pool.map(lambda head: owner(head, 'c0'), ['left', 'right']))
        self.assertEqual([r.get('consumed') for r in rows],
                         [[{'kind': 'PATH', 'id': 'left'}], [{'kind': 'PATH', 'id': 'right'}]])
