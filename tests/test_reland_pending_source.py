"""A rebuilt read may finish after the original pull request changes."""

import tempfile
import threading
import time
import unittest
from pathlib import Path

from fakes import DaemonGitHub, DaemonRepo, FakeGate
from test_daemon import daemon
from svrf.errors import ReadFailed


class PendingRebuiltSource(unittest.TestCase):
    def test_changed_or_unread_original_does_not_publish_a_completed_rebuild(self):
        for change in ("head", "closed", "draft", "hold", "base", "unread"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temporary:
                repo = DaemonRepo([1])
                gh = DaemonGitHub(repo)
                entered = threading.Event()
                release = threading.Event()
                finished = threading.Event()
                errors = []
                calls = []
                answer = {}
                original_pull = gh.pull

                def pull(number):
                    if answer.get("unread"):
                        raise ReadFailed("CURRENT_PULL_UNAVAILABLE")
                    return {**original_pull(number), "base_ref": "main", **answer}

                gh.pull = pull

                def admission(head, base):
                    calls.append((head, base))
                    if head == "h1":
                        return {"verdict": "HELD", "residuals": ["reland:REFUSED:MIXED"]}
                    entered.set()
                    if not release.wait(5):
                        raise RuntimeError("test did not release rebuilt read")
                    return {"verdict": "MERGEABLE", "residuals": []}

                owner = daemon(repo, gh, FakeGate(repo), admission, Path(temporary),
                               admission_jobs=1, admission_round_reads=1, tick_seconds=0.02)

                def tick():
                    try:
                        deadline = time.monotonic() + 2
                        while True:
                            owner.tick()
                            if entered.is_set() or time.monotonic() >= deadline:
                                break
                    except BaseException as error:
                        errors.append(repr(error))
                    finally:
                        finished.set()

                worker = threading.Thread(target=tick, daemon=True)
                worker.start()
                try:
                    self.assertTrue(entered.wait(2), "rebuilt read was not exercised")
                    finished.wait(0.1)
                    if change == "head":
                        repo.move_head(1)
                    elif change == "closed":
                        answer["state"] = "closed"
                    elif change == "draft":
                        answer["draft"] = True
                    elif change == "hold":
                        answer["labels"] = [{"name": "train:hold"}]
                    elif change == "base":
                        answer["base_ref"] = "other"
                    else:
                        answer["unread"] = True
                    release.set()
                    worker.join(2)
                    self.assertFalse(worker.is_alive())
                    if owner.inflight:
                        owner.inflight[1]["future"].result(timeout=1)
                        owner.tick()
                    self.assertEqual(errors, [])
                    self.assertEqual(len(calls), 2)
                    self.assertEqual((repo.pushes, gh.opened, gh.closed, gh.comments), ([], [], [], []))
                finally:
                    release.set()
                    worker.join(2)
                    while owner.inflight and not worker.is_alive():
                        owner.tick(drain=True)
                    owner.close()


if __name__ == "__main__":
    unittest.main()
