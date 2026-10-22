"""Pending reconstructed reads keep their source, slot and publication conditions."""

from contextlib import contextmanager
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from fakes import DaemonGitHub, DaemonRepo, FakeGate
from test_daemon import daemon
from svrf.github import RealGitHub


@contextmanager
def pending(test, *, answer=None, two_heads=False, stop_train=False):
    with tempfile.TemporaryDirectory() as temporary:
        repo = DaemonRepo([1, 2] if two_heads or stop_train else [1])
        gh = DaemonGitHub(repo, mismatch_on=2 if stop_train else None)
        entered, release, finished = (threading.Event() for _ in range(3))
        merged = threading.Event()
        calls, ticks, errors = [], [], []
        current, missing = {}, set()
        original_pull, original_merge = gh.pull, gh.merge

        def pull(number):
            value = {**original_pull(number), "head_ref": f"pr-{number}", "base_ref": "main"}
            if number == 1:
                value.update(current)
                for key in missing:
                    value.pop(key, None)
            return value

        def merge(number, sha):
            result = original_merge(number, sha)
            if number == 2:
                merged.set()
                if stop_train:
                    release.set()
            return result

        gh.pull, gh.merge = pull, merge

        def admission(head, base):
            calls.append((head, base, threading.current_thread().name))
            if head == "h1":
                return {"verdict": "HELD", "residuals": ["reland:REFUSED:MIXED"]}
            if head == "h2":
                if stop_train and not entered.wait(3):
                    raise RuntimeError("rebuilt read did not start before the independent admission")
                return {"verdict": "MERGEABLE", "residuals": []}
            entered.set()
            if not release.wait(3):
                raise RuntimeError("rebuilt read was not released")
            return answer if answer is not None else {"verdict": "MERGEABLE", "residuals": []}

        owner = daemon(repo, gh, FakeGate(repo), admission, Path(temporary),
                       admission_jobs=2 if stop_train else 1,
                       admission_round_reads=2 if stop_train else 1,
                       tick_seconds=1 if stop_train else 0.03)

        def tick():
            try:
                deadline = time.monotonic() + 2
                while True:
                    ticks.append(owner.tick())
                    if entered.is_set() or time.monotonic() >= deadline:
                        break
            except BaseException as error:
                errors.append(repr(error))
            finally:
                finished.set()

        worker = threading.Thread(target=tick, name="controller", daemon=True)
        worker.start()
        try:
            test.assertTrue(entered.wait(2), "reconstructed admission was not exercised")
            yield {"repo": repo, "gh": gh, "owner": owner, "entered": entered,
                   "release": release, "finished": finished, "merged": merged,
                   "worker": worker, "calls": calls, "ticks": ticks, "errors": errors,
                   "current": current, "missing": missing, "path": Path(temporary)}
        finally:
            release.set()
            worker.join(3)
            test.assertFalse(worker.is_alive(), "controller did not return during cleanup")
            while owner.inflight:
                owner.tick(drain=True)
            owner.close()
            test.assertEqual(errors, [])


def receive(test, case):
    case["release"].set()
    case["worker"].join(2)
    test.assertFalse(case["worker"].is_alive())
    owner = case["owner"]
    if 1 in owner.inflight:
        owner.inflight[1]["future"].result(timeout=1)
        # The short first tick has already established a pending continuation.
        # Receive both original outcomes in one bounded round: a new discovery
        # round would have different once-per-round read obligations.
        previous = owner.tick_seconds
        try:
            owner.tick_seconds = 1
            case["ticks"].append(owner.tick())
        finally:
            owner.tick_seconds = previous
    return case["ticks"][-1]


class RebuiltContinuation(unittest.TestCase):
    def test_pending_candidate_keeps_the_owned_slot_and_drain_retains_both_sources(self):
        with pending(self) as case:
            self.assertTrue(case["finished"].wait(0.3), "rebuilt read blocked the controller")
            owner = case["owner"]
            item = owner.inflight[1]
            candidate = case["calls"][-1][0]
            self.assertNotEqual(candidate, "h1")
            self.assertEqual(item["row"]["headRefOid"], candidate)
            self.assertEqual(item["base"], "c0")
            self.assertEqual(item["inputs"]["row"]["headRefOid"], candidate)
            original = item["inputs"]["reland"]
            self.assertEqual(original["row"]["headRefOid"], "h1")
            self.assertEqual(original["row"]["headRefName"], "pr-1")
            self.assertEqual(original["row"]["baseRefName"], "main")
            self.assertEqual(original["class"], "MIXED")
            self.assertEqual(original["tree"], case["repo"].tree(candidate))
            self.assertEqual(original["ref"], "pr-1-ordered")
            self.assertEqual(original["inputs"]["admission_base"], "c0")
            self.assertTrue(all(row[2].startswith("admission") for row in case["calls"]))
            executor = owner._pool
            second = owner.tick()
            self.assertIs(owner.inflight[1], item)
            self.assertIs(owner._pool, executor)
            self.assertEqual(second["inflight"], [1])
            self.assertEqual(len(case["calls"]), 2)
            with self.assertRaisesRegex(RuntimeError, "ADMISSIONS_STILL_RUNNING"):
                owner.close()
            case["release"].set()
            item["future"].result(timeout=1)
            out = owner.tick(drain=True)
            result = json.loads(Path(out["receipt"]).read_text())["drained"][0]
            self.assertEqual(result["head"], candidate)
            self.assertEqual(result["base"], "c0")
            self.assertEqual(result["inputs"]["reland"], original)
            self.assertEqual(result["admission"]["verdict"], "MERGEABLE")
            self.assertEqual(owner.inflight, {})
            self.assertEqual((case["repo"].pushes, case["gh"].opened,
                              case["gh"].closed, case["gh"].comments), ([], [], [], []))

    def test_main_only_advance_does_not_discard_an_accepted_successor(self):
        with pending(self) as case:
            case["finished"].wait(0.1)
            repo = case["repo"]
            checked = case["calls"][-1][0]
            repo.main = repo._new({0, 7}, (repo.main,))
            out = receive(self, case)
            self.assertEqual(repo.pushes, [("pr-1-ordered", checked)])
            self.assertEqual(case["gh"].closed, [1])
            self.assertEqual(len(out["relanded"]), 1)
            self.assertEqual(len(case["calls"]), 2)
            self.assertEqual(case["calls"][-1][1], "c0")
            self.assertEqual(out["merged"], [])

    def test_rebuilt_hold_retains_original_source_and_consumed_watch(self):
        with pending(self, answer={"verdict": "HELD", "residuals": ["check:failed"],
                                   "changed": ["lane:1"],
                                   "consumed": [{"kind": "PATH", "id": "lane:9"}]}) as case:
            case["finished"].wait(0.1)
            out = receive(self, case)
            self.assertEqual(out["held"], [1])
            held = json.loads((case["path"] / "state.json").read_text())["held"]["1"]
            self.assertEqual(held["head"], "h1")
            self.assertEqual(held["head_ref"], "pr-1")
            self.assertEqual(held["repair_head"], case["calls"][-1][0])
            self.assertEqual(held["base_sha"], "c0")
            self.assertEqual(held["reason"], "RELAND_ADMISSION_HELD")
            self.assertEqual(held["watch"], ["lane:1", "lane:9"])
            self.assertEqual(case["owner"].tick()["held_unchanged"], [1])
            self.assertEqual(len(case["calls"]), 2)
            self.assertEqual((case["repo"].pushes, case["gh"].opened, case["gh"].closed), ([], [], []))

    def test_unknown_readiness_or_renamed_ref_cannot_publish_or_reread_the_same_head(self):
        changes = [(key, None) for key in ("draft", "labels", "head_sha", "head_ref", "base_ref")]
        changes += [("draft", 0), ("labels", [{"name": None}]), ("head_ref", "renamed")]
        for key, value in changes:
            with self.subTest(field=key, value=value), pending(self, two_heads=True) as case:
                case["finished"].wait(0.1)
                if value is None:
                    case["missing"].add(key)
                else:
                    case["current"][key] = value
                out = receive(self, case)
                self.assertFalse(any(ref.startswith("pr-1") for ref, _ in case["repo"].pushes))
                self.assertEqual((case["gh"].opened, case["gh"].closed), ([], []))
                self.assertFalse(any(number == 1 for number, _ in case["gh"].comments))
                self.assertIn(1, out["retry_later"])
                self.assertEqual(sum(head == "h1" for head, _, _ in case["calls"]), 1)
                self.assertEqual(len(case["calls"]), 3)
                self.assertEqual(case["gh"].merged, [2])
                self.assertEqual(out["held"], [])

    def test_a_real_stopped_train_precedes_rebuilt_publication(self):
        with pending(self, stop_train=True) as case:
            self.assertTrue(case["merged"].wait(0.4), "pending rebuilt read blocked the independent gate")
            out = receive(self, case)
            self.assertTrue(out["stopped"])
            self.assertEqual(out["reasons"].get("1"), "TRAIN_STOPPED")
            self.assertFalse(any(ref.startswith("pr-1") for ref, _ in case["repo"].pushes))
            self.assertEqual((case["gh"].opened, case["gh"].closed), ([], []))
            self.assertFalse(any(number == 1 for number, _ in case["gh"].comments))

    def test_pull_reports_observed_base_and_does_not_invent_missing_readiness(self):
        gh = RealGitHub("example/project")
        response = {"head": {"sha": "abc", "ref": "pr-1"}, "base": {"ref": "main"},
                    "state": "open", "draft": False, "labels": []}
        gh._gh = lambda *_: json.dumps(response)
        self.assertEqual(gh.pull(1).get("base_ref"), "main")
        response.pop("base")
        response.pop("draft")
        response.pop("labels")
        observed = gh.pull(1)
        self.assertIsNone(observed.get("base_ref"))
        self.assertIsNone(observed.get("draft"))
        self.assertIsNone(observed.get("labels"))

    def test_an_unread_pull_object_is_retried_without_publication(self):
        for response in (None, []):
            with self.subTest(response=response), pending(self) as case:
                case["finished"].wait(0.1)
                current = RealGitHub("example/project")
                current._gh = lambda *_: json.dumps(response)
                case["gh"].pull = current.pull
                out = receive(self, case)
                self.assertIn(1, out["retry_later"])
                self.assertIn("UNOBSERVED", out["reasons"]["1"])
                self.assertEqual((case["repo"].pushes, case["gh"].opened,
                                  case["gh"].closed, case["gh"].comments), ([], [], [], []))


if __name__ == "__main__":
    unittest.main()
