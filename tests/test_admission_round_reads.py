"""A bounded admission round starts at most round_reads candidates, longest-waiting first, and refills freed slots.

A round's candidates are fixed when it starts. With a long backlog and few admission lanes one round can run for
hours, and a pull request opened (or declared urgent) meanwhile waits for the next. With [admission] round_reads a
round starts at most that many candidates and, as each read finishes, lists again and starts the next from the
current queue, so a slow read never idles the other slots and a new head is not left to the next round. The order is the
declared priority first, then whoever has waited longest. A candidate starts waiting when it is first seen and again
each time it is read, and a tie goes to the older pull request, so a steady stream of new heads never starves an old
one. What a round leaves unread when it ends is recorded as deferred.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fakes import Admission, Clock, DaemonGitHub, DaemonRepo, FakeGate, is_union

from svrf.config import ConfigError, from_dict
from svrf.daemon import Daemon


def _open(repo, n):
    """A pull request opened between rounds, built as FakeRepo builds its own."""
    sha = f"h{n}"
    repo.commits[sha] = {"lanes": frozenset({0, n}), "parents": ("c0",)}
    repo.heads[n] = sha
    repo.branches[f"pr-{n}"] = sha


class Rounds:
    def __init__(self, numbers, **kw):
        self.tmp = tempfile.mkdtemp()
        self.repo = DaemonRepo(numbers)
        self.gh = DaemonGitHub(self.repo)
        self.clock = Clock()
        self.kw = kw

    def tick(self):
        admission = Admission()
        number_of = {sha: n for n, sha in self.repo.heads.items()}
        out = Daemon(self.repo, self.gh, FakeGate(self.repo), admission, state_dir=Path(self.tmp),
                     receipts=Path(self.tmp) / "receipts", clock=self.clock, sleep=self.clock.sleep,
                     train_options={"poll_seconds": 0}, is_union=is_union, jobs=1, admission_jobs=1,
                     **self.kw).tick()
        self.clock.now += 60
        return [number_of[head] for head, _ in admission.calls], out


class RoundReads(unittest.TestCase):
    def test_a_bounded_round_reads_the_oldest_first_and_refills_the_rest(self):
        rounds = Rounds([51, 52, 53, 54], admission_round_reads=2)
        read, out = rounds.tick()
        self.assertEqual(read, [51, 52, 54, 53])
        self.assertNotIn("deferred", out)

    def test_the_next_round_reads_only_the_newer_arrival(self):
        rounds = Rounds([51, 52, 53, 54], admission_round_reads=2)
        rounds.tick()
        _open(rounds.repo, 55)
        read, out = rounds.tick()
        self.assertEqual(read, [55])
        self.assertNotIn("deferred", out)

    def test_declared_priority_leads_a_bounded_round(self):
        rounds = Rounds([51, 52, 53, 54], admission_round_reads=2, admission_priority_command="echo 54")
        read, _ = rounds.tick()
        self.assertEqual(read, [54, 51, 53, 52])

    def test_without_round_reads_every_candidate_is_read(self):
        rounds = Rounds([51, 52, 53, 54])
        read, out = rounds.tick()
        self.assertEqual(sorted(read), [51, 52, 53, 54])
        self.assertNotIn("deferred", out)


class RoundReadsConfig(unittest.TestCase):
    MINIMAL = {"repo": "example/project", "gate": {"commands": ["make test"]}}

    def test_round_reads_is_a_whole_number_of_at_least_one(self):
        self.assertEqual(from_dict({**self.MINIMAL, "admission": {"command": "true", "round_reads": 8}}).admission_round_reads, 8)
        self.assertIsNone(from_dict(self.MINIMAL).admission_round_reads)
        for bad in (0, -1, "8", True):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                from_dict({**self.MINIMAL, "admission": {"command": "true", "round_reads": bad}})


if __name__ == "__main__":
    unittest.main()
