"""Only the chosen family is demanded by the train."""
import unittest
from unittest.mock import patch

from fakes import is_union
from svrf import rules


class FactoredFamily(unittest.TestCase):
    def test_selection_does_not_enumerate_unused_maximal_families(self):
        conflicts = [{'a': n, 'b': n + 1, 'paths': ['code.py']} for n in range(0, 48, 2)]
        with patch.object(rules, 'families', side_effect=AssertionError('UNUSED_FAMILY_CONSTRUCTION')):
            chosen, out = rules.choose_families(list(range(48)), conflicts, [], is_union)
        self.assertEqual(chosen, list(range(0, 48, 2)))
        self.assertEqual(out[1], {'with': [0], 'paths': ['code.py']})
