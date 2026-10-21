"""Terminal attempts remain visible after their executor slots are released."""
import json
from pathlib import Path
import tempfile
import unittest

from fakes import DaemonGitHub, DaemonRepo, FakeGate
from test_daemon import daemon
from svrf.admission import Admission
from svrf.errors import ReadFailed


class AttemptHistory(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = DaemonRepo([1])
        self.repo.root = self.root
        self.repo.merge_preview = lambda *args: {"status": "CLEAN", "conflicts": []}
        self.repo.changed_paths = lambda *args: ["lane:1"]
        self.gh = DaemonGitHub(self.repo)
        self.owner = None
        self.addCleanup(self.close_owner)

    def close_owner(self):
        if self.owner is not None:
            while self.owner.inflight:
                self.owner.tick(drain=True)
            self.owner.close()

    def make_owner(self, admission):
        self.owner = daemon(self.repo, self.gh, FakeGate(self.repo), admission, self.root)
        return self.owner

    def history(self):
        state = self.owner.load_state()
        self.assertIn("admission_history", state, "finished attempts vanished with their slots")
        return state["admission_history"]

    def test_read_failure_retains_its_exact_terminal_without_repeating_it_at_rate_floor(self):
        owner = self.make_owner(Admission(self.repo, command="echo IO_POOL_CAPACITY; exit 126", timeout=2))
        first = owner.tick()
        self.assertEqual(first["retry_later"], [1])
        self.assertEqual(first["held"], [])
        self.assertEqual(first["inflight"], [])
        history = self.history()
        self.assertEqual(len(history), 1)
        key, row = next(iter(history.items()))
        self.assertEqual(key, row["attempt_id"])
        self.assertEqual((row["pr"], row["head"], row["base"]), (1, "h1", "c0"))
        self.assertEqual(row["terminal_disposition"], "UNOBSERVED")
        self.assertEqual(row["exit"], 126)
        self.assertEqual(row["terminal_at"], row["finished_at"])
        self.assertEqual(row["stage"], "complete")
        self.assertEqual(row["read_failure"]["reason"], "IO_POOL_CAPACITY")
        self.assertTrue(Path(row["record_file"]).is_file())
        self.gh.remaining = 0
        owner.tick()
        self.assertEqual(self.history(), history)
        self.assertEqual(owner.load_state()["last_tick"]["admission_attempts"], {})

    def test_genuine_check_failure_is_a_hold_with_its_original_exit(self):
        owner = self.make_owner(Admission(self.repo, command="echo original_check_failure; exit 7", timeout=2))
        tick = owner.tick()
        self.assertEqual(tick["held"], [1])
        self.assertEqual(tick["retry_later"], [])
        row = next(iter(self.history().values()))
        self.assertEqual(row["terminal_disposition"], "HELD")
        self.assertEqual(row["exit"], 7)
        self.assertEqual(row["result"]["residuals"], ["check:original_check_failure"])
        self.assertEqual(owner.load_state()["held"]["1"]["reason"], "ADMISSION_HELD")

    def test_same_source_can_have_two_distinct_original_outcomes(self):
        def unavailable(head, base):
            raise ReadFailed("SOURCE_UNAVAILABLE")
        owner = self.make_owner(unavailable)
        owner.tick()
        one = set(self.history())
        owner.tick()
        two = set(self.history())
        self.assertEqual(len(one), 1)
        self.assertEqual(len(two), 2)
        self.assertTrue(one < two)
        self.assertTrue(all(row["exit"] is None for row in self.history().values()))
        self.assertEqual(owner.load_state()["held"], {})

    def test_recent_history_is_bounded_and_older_durable_records_remain(self):
        def unavailable(head, base):
            raise ReadFailed("SOURCE_UNAVAILABLE")
        owner = self.make_owner(unavailable)
        owner.tick()
        first = next(iter(self.history().values()))
        for _ in range(34):
            owner.tick()
        history = self.history()
        self.assertEqual(len(history), 32)
        self.assertNotIn(first["attempt_id"], history)
        self.assertTrue(Path(first["record_file"]).is_file())
        self.assertEqual(len(list((self.root / "admission-attempts").glob("*.json"))), 35)

    def test_loaded_history_survives_a_new_owner_without_creating_new_work(self):
        def unavailable(head, base):
            raise ReadFailed("SOURCE_UNAVAILABLE")
        owner = self.make_owner(unavailable)
        owner.tick()
        history = self.history()
        owner.close()
        self.gh.remaining = 0
        self.make_owner(unavailable).tick()
        self.assertEqual(self.history(), history)


if __name__ == "__main__":
    unittest.main()
