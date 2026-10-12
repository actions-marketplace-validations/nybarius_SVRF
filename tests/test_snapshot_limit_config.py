"""[train] snapshot_limit bounds how many open pull requests one snapshot may list.

The snapshot refuses a listing longer than its limit (SNAPSHOT_LIMIT_EXCEEDED), so a runaway listing never becomes
a round. The limit was fixed at 300. A repository with more open pull requests than that read no round at all:
every tick retried, and nothing was admitted or landed, including the configuration change that would have raised
it. The default is now 1000 (ten 100-row pages), a [train] snapshot_limit overrides it, and every place that builds
the repository client reads it.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from svrf.app import build
from svrf.config import ConfigError, from_dict


def _config(**train):
    tmp = Path(tempfile.mkdtemp())
    doc = {"repo": "o/r", "clone": str(tmp / "clone"), "state_dir": str(tmp / "state"),
           "gate": {"commands": ["true"]}}
    if train:
        doc["train"] = train
    return from_dict(doc)


class SnapshotLimitConfig(unittest.TestCase):
    def test_the_default_lists_ten_pages(self):
        self.assertEqual(_config().train.snapshot_limit, 1000)

    def test_a_configured_limit_is_read(self):
        self.assertEqual(_config(snapshot_limit=2000).train.snapshot_limit, 2000)

    def test_a_limit_that_is_not_a_positive_whole_count_is_refused(self):
        for bad in (0, -1, 2.5, "1000", True):
            with self.assertRaises(ConfigError, msg=repr(bad)):
                _config(snapshot_limit=bad)

    def test_the_daemon_s_repository_client_reads_the_configured_limit(self):
        daemon = build(_config(snapshot_limit=2000))
        self.assertEqual(daemon.gh.limit, 2000)
        self.assertEqual(build(_config()).gh.limit, 1000)


if __name__ == "__main__":
    unittest.main()
