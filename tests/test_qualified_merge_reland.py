"""An admission owner's qualified merge repair survives transport to the train."""
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from svrf.admission import Admission
from svrf import rules


class QualifiedMergeReland(unittest.TestCase):
    def read(self, lines):
        with tempfile.TemporaryDirectory() as temp:
            git = SimpleNamespace(root=Path(temp), is_ancestor=lambda *a: False,
                merge_preview=lambda *a: {'status': 'CLEAN', 'conflicts': []},
                changed_paths=lambda *a: ['src/runtime.py'])
            command = "printf '%s\\n' " + ' '.join("'"+line+"'" for line in lines) + '; exit 1'
            return Admission(git, command=command)('head', 'base')

    def test_qualified_command_result_relands_the_same_tree(self):
        line = 'reland:REFUSED:MERGE_LOSS'
        value = self.read([line])
        self.assertEqual(value['residuals'], [line])
        self.assertEqual(rules.admission_decision(value), ('RELAND', 'MERGE_LOSS'))
        self.assertEqual(rules.admission_decision(value, reland=False), ('HOLD', None))

    def test_unqualified_loss_and_other_failures_remain_held(self):
        for line in ('history:REFUSED:MERGE_LOSS', 'dispositions:REFUSED:MERGE_LOSS',
                     'check:MERGE_LOSS', 'MERGE_LOSS'):
            with self.subTest(line=line):
                self.assertIsNone(rules.reland_class([line]))
                self.assertEqual(rules.admission_decision(self.read([line])), ('HOLD', None))
        self.assertEqual(rules.admission_decision(self.read([
            'reland:REFUSED:MERGE_LOSS', 'source mismatch'])), ('HOLD', None))


if __name__ == '__main__': unittest.main()
