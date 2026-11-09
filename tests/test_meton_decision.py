"""The original effect owner consumes a retained decision before publication."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from fakes import Clock, FakeGate, FakeGitHub, FakeRepo
from svrf.errors import ReadFailed
from svrf.train import Train


class MetonDecision(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = FakeRepo([11, 12])
        self.gh = FakeGitHub(self.repo)
        self.gate = FakeGate(self.repo)
        clock = Clock()
        self.train = Train(self.repo, self.gh, self.gate, receipts=self.root,
                           clock=clock, sleep=clock.sleep, poll_seconds=0,
                           max_rounds=1, family_size=1, comment=False,
                           pr_comments=False, status_checks=False)
        self.seen = []
        self.pushes = []
        push = self.repo.push_branch
        def pushed(*args):
            self.pushes.append(args)
            return push(*args)
        self.repo.push_branch = pushed

    def received(self, source):
        self.seen.append(deepcopy(source))
        return {"status": "OBSERVED", "action": "act_land", "source": deepcopy(source),
                "meton": {"task": "task-original", "observation": "sha256:" + "a" * 64}}

    def run_with(self, decision, numbers=(11,)):
        # Existing Train silently ignores this supplied read: the original RED.
        self.train.landing_decision = decision
        return self.train.run(list(numbers))

    def test_stale_observation_refuses_before_push_or_merge(self):
        def stale(source):
            raise ReadFailed("METON_OBSERVATION_STALE")
        receipt = self.run_with(stale)
        self.assertEqual(self.gh.merged, [], "a stale Meton observation still permitted a merge")
        self.assertEqual(self.pushes, [])
        self.assertTrue(any(x['reason'] == 'METON_OBSERVATION_STALE' for x in receipt['retry_later']))

    def test_received_action_precedes_effect_and_retains_original_gate(self):
        def receive(source):
            self.assertEqual(self.gh.merged, [])
            self.assertEqual(self.pushes, [])
            path = Path(source['receipt']['path'])
            raw = path.read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), source['receipt']['sha256'])
            original = json.loads(raw)
            self.assertEqual(original['gates'][source['gate_index']], source['gate'])
            self.assertTrue(source['gate']['green'])
            self.assertEqual(source['head'], original['prs']['11']['head_sha'])
            return self.received(source)
        receipt = self.run_with(receive)
        self.assertEqual(len(self.seen), 1, "landing never consumed Meton's retained decision")
        self.assertEqual(self.gh.merged, [11])
        decision = receipt['decisions'][receipt['merges'][0]['decision']]
        self.assertEqual(decision['meton']['observation'], 'sha256:' + 'a' * 64)
        self.assertEqual(decision['source'], self.seen[0])
        self.assertEqual(len(self.gate.trees), 1)
        self.train._save()
        raw = Path(self.seen[0]['receipt']['path']).read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), self.seen[0]['receipt']['sha256'])

    def test_moved_input_cannot_carry_the_old_action(self):
        def stale(source):
            result = self.received(source)
            result['source']['head'] = 'other-head'
            return result
        self.run_with(stale)
        self.assertEqual(self.gh.merged, [])
        self.assertEqual(self.pushes, [])

    def test_unobserved_or_nonlanding_decision_keeps_the_original_gate(self):
        def wait(source):
            result = self.received(source)
            result['action'] = 'act_observe'
            return result
        receipt = self.run_with(wait)
        self.assertEqual(self.gh.merged, [])
        self.assertEqual(self.pushes, [])
        self.assertTrue(receipt['gates'][0]['green'])
        self.assertEqual(receipt['decisions'][0]['action'], 'act_observe')

    def test_missing_observation_is_not_permission(self):
        def incomplete(source):
            result = self.received(source)
            del result['meton']['observation']
            return result
        self.run_with(incomplete)
        self.assertEqual(self.gh.merged, [])
        self.assertEqual(self.pushes, [])

    def test_base_changed_during_receiving_reopens_before_publication(self):
        def moved(source):
            result = self.received(source)
            self.repo.main = self.repo._new({0, 77}, ('c0',))
            return result
        self.run_with(moved)
        self.assertEqual(self.gh.merged, [])
        self.assertEqual(self.pushes, [])

    def test_two_rounds_keep_distinct_sources_under_one_retained_method_task(self):
        first = self.run_with(self.received, (11,))
        second = Train(self.repo, self.gh, self.gate, receipts=self.root,
                       poll_seconds=0, max_rounds=1, comment=False,
                       pr_comments=False, status_checks=False)
        second.landing_decision = self.received
        later = second.run([12])
        self.assertEqual(len(self.seen), 2)
        self.assertNotEqual(self.seen[0], self.seen[1])
        self.assertEqual(self.gh.merged, [11, 12])
        self.assertEqual(first['decisions'][0]['meton']['task'], later['decisions'][0]['meton']['task'])


if __name__ == '__main__':
    unittest.main()
