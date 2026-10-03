"""An optional tree provider for the family merges (`[merge] tree_command`).

The command is an alternative merge engine. For each merge step the train builds (a pull
request's head merged with the base or the fold so far), it is run in the train's clone
with the step's sides and merge bases in its environment, and prints the tree it would
produce. The train always computes git's own merge tree as well and uses the proposed
tree only when it is the identical tree; this module only runs the command and reads its
answer. Nothing here can fail a landing: every failure is a reason, never an exception.

    SVRF_MERGE_OURS     the pull request's head (git's "ours")
    SVRF_MERGE_THEIRS   the base, or the fold so far (git's "theirs")
    SVRF_MERGE_BASES    their merge bases (`git merge-base --all`), space separated
    SVRF_CLONE          the clone the command runs in (also its working directory)
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
from pathlib import Path

from .redact import redact

TREE_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def reason_class(reason: str) -> str:
    """The counted part of a reason: `PROVIDER_EXIT:1:<stderr tail>` counts as `PROVIDER_EXIT:1`."""
    return ":".join(reason.split(":")[:2])


class TreeCommand:
    def __init__(self, command: str, root: Path | str, *, timeout: float = 120):
        self.command = command
        self.root = Path(root).expanduser().resolve()
        self.timeout = timeout

    def _merge_bases(self, ours: str, theirs: str) -> list[str] | None:
        done = subprocess.run(["git", "-C", str(self.root), "merge-base", "--all", ours, theirs],
                              capture_output=True, text=True)
        if done.returncode not in (0, 1):
            return None
        return done.stdout.split()

    def propose(self, ours: str, theirs: str) -> tuple[str | None, str | None]:
        """(tree, None) when the command printed a tree id, else (None, reason)."""
        bases = self._merge_bases(ours, theirs)
        if bases is None:
            return None, "MERGE_BASES_UNREAD"
        env = {**os.environ, "SVRF_MERGE_OURS": ours, "SVRF_MERGE_THEIRS": theirs,
               "SVRF_MERGE_BASES": " ".join(bases), "SVRF_CLONE": str(self.root)}
        try:
            # Its own session, so a timeout kills everything the command started.
            proc = subprocess.Popen(["bash", "-c", self.command], cwd=str(self.root), env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                    start_new_session=True)
        except OSError as error:
            return None, f"PROVIDER_UNAVAILABLE:{type(error).__name__}"
        try:
            stdout, stderr = proc.communicate(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            return None, "PROVIDER_TIMEOUT"
        if proc.returncode != 0:
            tail = [line.strip() for line in redact(stderr, env).splitlines() if line.strip()]
            return None, f"PROVIDER_EXIT:{proc.returncode}" + (f":{tail[-1][:160]}" if tail else "")
        lines = [line.strip() for line in stdout.splitlines() if line.strip()]
        if not lines or not TREE_ID.fullmatch(lines[0]):
            return None, "PROVIDER_OUTPUT_INVALID"
        return lines[0], None
