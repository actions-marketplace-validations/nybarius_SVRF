"""A reconstructed history must satisfy the original reader before publication."""
import json

import tempfile
import unittest
from pathlib import Path

from fakes import Admission, DaemonGitHub, DaemonRepo, FakeGate
from test_daemon import daemon
from svrf.errors import ReadFailed


def world(tmp_path, answer, **kwargs):
    repo = DaemonRepo([5])
    gh = DaemonGitHub(repo)
    calls = []

    def admission(head, base):
        calls.append((head, base))
        assert repo.pushes == [] and gh.opened == [] and gh.closed == []
        if head == "h5":
            return {"verdict": "HELD", "residuals": ["reland:REFUSED:MIXED"]}
        if isinstance(answer, Exception):
            raise answer
        return answer

    return repo, gh, calls, daemon(repo, gh, FakeGate(repo), admission, tmp_path, **kwargs)


def check_rebuilt_refusal_is_retained_and_the_next_round_is_quiet(tmp_path, residual):
    repo, gh, calls, owner = world(tmp_path, {
        "verdict": "HELD", "residuals": [residual], "changed": ["lane:5"],
        "consumed": [{"kind": "PATH", "id": "lane:9"}]})
    first = owner.tick()
    assert len(calls) == 2
    assert first["held"] == [5]
    assert (repo.pushes, gh.opened, gh.closed, gh.comments) == ([], [], [], [])
    held = json.loads((tmp_path / "state.json").read_text())["held"]["5"]
    assert held["reason"] == "RELAND_ADMISSION_HELD"
    assert held["failing"] == [residual]
    assert held["watch"] == ["lane:5", "lane:9"]
    assert held["repair_head"] == calls[-1][0]
    second = owner.tick()
    assert second["held_unchanged"] == [5]
    assert len(calls) == 2
    repo.main = repo._new({0, 9}, (repo.main,))
    owner.tick()
    assert len(calls) == 4  # changing a watched dependency reopens precisely this hold


def check_incomplete_rebuilt_read_neither_publishes_nor_enters_held_memory(tmp_path, answer):
    repo, gh, calls, owner = world(tmp_path, answer)
    out = owner.tick()
    assert len(calls) == 2
    assert out["retry_later"]
    assert out["held"] == []
    assert (repo.pushes, gh.opened, gh.closed, gh.comments) == ([], [], [], [])


def check_accepted_rebuilt_read_precedes_all_publication_effects(tmp_path):
    repo, gh, calls, owner = world(tmp_path, {"verdict": "MERGEABLE", "residuals": []})
    out = owner.tick()
    assert len(calls) == 2
    assert calls[0] == ("h5", "c0")
    assert calls[1][0] == repo.pushes[0][1]
    assert calls[1][1] == "c0"
    assert out["relanded"] and gh.closed == [5]


def check_history_failure_precedes_even_the_branch_push(tmp_path):
    repo, gh, calls, owner = world(tmp_path, {"verdict": "MERGEABLE"},
                                  history_verdict=lambda *_: "REFUSED:UNORDERED")
    out = owner.tick()
    assert out["held"] == [5]
    assert len(calls) == 1
    assert (repo.pushes, gh.opened, gh.closed) == ([], [], [])


def check_reported_clean_tree_is_checked_against_the_actual_candidate(tmp_path):
    repo, gh, calls, owner = world(tmp_path, {"verdict": "MERGEABLE"})
    original = repo.reland

    def corrupt(*args):
        result = original(*args)
        repo.commits[result.commits[-1]]["lanes"] = frozenset({0, 99})
        return result

    repo.reland = corrupt
    out = owner.tick()
    assert out["held"] == [5]
    assert (repo.pushes, gh.opened, gh.closed) == ([], [], [])


class OriginalAdmissionBeforePublication(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.tmp_path = Path(directory.name)

    def test_rebuilt_history_refusal_is_quiet(self):
        check_rebuilt_refusal_is_retained_and_the_next_round_is_quiet(
            self.tmp_path, "reland:REFUSED:MIXED")

    def test_rebuilt_content_refusal_is_quiet(self):
        check_rebuilt_refusal_is_retained_and_the_next_round_is_quiet(
            self.tmp_path, "check:tests failed")

    def test_checker_failure_does_not_publish(self):
        check_incomplete_rebuilt_read_neither_publishes_nor_enters_held_memory(
            self.tmp_path, ReadFailed("CHECKER_UNAVAILABLE"))

    def test_unobserved_read_does_not_publish(self):
        check_incomplete_rebuilt_read_neither_publishes_nor_enters_held_memory(
            self.tmp_path, {"verdict": "UNOBSERVED"})

    def test_unknown_read_does_not_publish(self):
        check_incomplete_rebuilt_read_neither_publishes_nor_enters_held_memory(
            self.tmp_path, {"verdict": "UNKNOWN"})

    def test_contradictory_read_does_not_publish(self):
        check_incomplete_rebuilt_read_neither_publishes_nor_enters_held_memory(
            self.tmp_path, {"verdict": "MERGEABLE", "residuals": ["check:contradiction"]})

    def test_partial_read_does_not_publish(self):
        check_incomplete_rebuilt_read_neither_publishes_nor_enters_held_memory(
            self.tmp_path, {"verdict": "MERGEABLE", "admission": {"complete": False}})

    def test_acceptance_precedes_effects(self):
        check_accepted_rebuilt_read_precedes_all_publication_effects(self.tmp_path)

    def test_history_failure_precedes_push(self):
        check_history_failure_precedes_even_the_branch_push(self.tmp_path)

    def test_actual_candidate_tree_is_checked(self):
        check_reported_clean_tree_is_checked_against_the_actual_candidate(self.tmp_path)
