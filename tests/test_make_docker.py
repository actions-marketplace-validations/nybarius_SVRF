"""`make docker` is the container gate (CI's "container" job runs it). It must smoke test the image it just built on the
engine it builds on, with no more privilege than the caller already has.

Behind a rootless Podman socket, `docker build` is Buildx with a docker-container builder: without `--load` the image
stays in the builder's cache and the smoke run looks for an image the engine never received. The root Docker daemon is
the privileged route, so when the caller has its own rootless Podman socket (`$XDG_RUNTIME_DIR/podman/podman.sock`) and
names no `DOCKER_HOST`, the gate uses that socket.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

FAKE_DOCKER = """#!/bin/sh
printf '%s|%s\\n' "${DOCKER_HOST:-}" "$*" >> "$DOCKER_CALLS"
"""


@unittest.skipUnless(shutil.which("make"), "make is not installed")
class MakeDocker(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        (bin_dir / "docker").write_text(FAKE_DOCKER)
        (bin_dir / "docker").chmod(0o755)
        self.calls = self.tmp / "calls"
        self.runtime = self.tmp / "run"
        self.runtime.mkdir()
        self.env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "DOCKER_CALLS": str(self.calls),
                    "XDG_RUNTIME_DIR": str(self.runtime), "HOME": str(self.tmp)}

    def podman_socket(self) -> Path:
        path = self.runtime / "podman" / "podman.sock"
        path.parent.mkdir()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(server.close)
        server.bind(str(path))
        return path

    def make_docker(self, **env) -> list[tuple[str, str]]:
        done = subprocess.run(["make", "-s", "docker"], cwd=ROOT, env={**self.env, **env}, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        return [tuple(line.split("|", 1)) for line in self.calls.read_text().splitlines()]

    def test_the_built_image_is_loaded_into_the_engine_the_smoke_test_runs_on(self):
        (build, run) = self.make_docker()
        self.assertTrue(build[1].startswith("build "), build)
        self.assertIn("--load", build[1].split())
        self.assertEqual(run[1], "run --rm --entrypoint svrf svrf --version")

    def test_the_callers_rootless_podman_socket_serves_both_build_and_smoke(self):
        path = self.podman_socket()
        calls = self.make_docker()
        self.assertEqual([host for host, _ in calls], [f"unix://{path}"] * 2)

    def test_an_explicit_docker_host_is_kept(self):
        self.podman_socket()
        calls = self.make_docker(DOCKER_HOST="tcp://builder.invalid:2376")
        self.assertEqual([host for host, _ in calls], ["tcp://builder.invalid:2376"] * 2)

    def test_without_a_podman_socket_the_default_engine_is_used(self):
        calls = self.make_docker()
        self.assertEqual([host for host, _ in calls], ["", ""])


if __name__ == "__main__":
    unittest.main()
