"""Configuration loading, the command line, and the packaging files."""

from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from fakes import ROOT

from svrf import cli
from svrf.config import ConfigError, from_dict, load
from svrf.locks import owner_lock

MINIMAL = {"repo": "example/project", "gate": {"commands": ["make test"]}}


class Config(unittest.TestCase):
    def test_minimal_config_has_safe_defaults(self):
        config = from_dict(MINIMAL)
        self.assertEqual((config.base, config.hold_label, config.history.order), ("main", "train:hold", "off"))
        self.assertEqual((config.train.family_size, config.train.jobs), (8, 2))
        self.assertTrue(config.repair)
        self.assertFalse(config.union_paths("package-lock.json"))
        self.assertEqual((config.ui.pr_comments, config.ui.status_checks, config.ui.dashboard_url),
                         (True, True, ""))

    def test_the_ui_section_can_turn_either_surface_off_and_set_a_dashboard_url(self):
        config = from_dict({**MINIMAL, "ui": {"pr_comments": False, "status_checks": False,
                                              "dashboard_url": "https://dash.example/x"}})
        self.assertEqual((config.ui.pr_comments, config.ui.status_checks, config.ui.dashboard_url),
                         (False, False, "https://dash.example/x"))

    def test_an_unknown_ui_key_is_refused(self):
        with self.assertRaises(ConfigError):
            from_dict({**MINIMAL, "ui": {"comments": True}})

    def test_the_admission_timeout_is_configured_in_minutes_and_defaults_to_ten(self):
        """An admission command that reads a large head (the intake runs every suite the head
        touches) needs longer than the class default under a loaded host; the operator sets it
        in [admission] like the gate's timeout, in minutes."""
        self.assertEqual(from_dict(MINIMAL).admission_timeout_minutes, 10)
        c = from_dict({**MINIMAL, "admission": {"command": "true", "timeout_minutes": 45}})
        self.assertEqual(c.admission_timeout_minutes, 45)
        with self.assertRaises(ConfigError):
            from_dict({**MINIMAL, "admission": {"command": "true", "timeout_minutes": "soon"}})

    def test_reland_is_wired_from_the_admission_command_even_with_the_built_in_check_off(self):
        # history.order == "off" (the default) used to force reland_enabled False no
        # matter what history.reland said, on the assumption that a reland-eligible
        # residual could only ever come from the built-in tests-first check. An
        # admission.command can report the same class of refusal too (reland_class
        # reads its `reland:REFUSED:<class>` lines), so the built-in check being off
        # must not silence it.
        from svrf.app import build
        on = from_dict({**MINIMAL, "admission": {"command": "true"}, "history": {"reland": True, "order": "off"}})
        self.assertTrue(build(on, github=object()).reland_enabled)
        off = from_dict({**MINIMAL, "history": {"reland": False, "order": "off"}})
        self.assertFalse(build(off, github=object()).reland_enabled)

    def test_unknown_keys_are_refused(self):
        for bad in ({**MINIMAL, "familysize": 3}, {**MINIMAL, "train": {"family": 3}},
                    {**MINIMAL, "repair": {"union": ["x"]}}, {**MINIMAL, "labels": {"skip": "x"}}):
            with self.assertRaises(ConfigError):
                from_dict(bad)

    def test_required_fields(self):
        with self.assertRaises(ConfigError):
            from_dict({"gate": {"commands": ["x"]}})
        with self.assertRaises(ConfigError):
            from_dict({"repo": "example/project"})
        with self.assertRaises(ConfigError):
            from_dict({**MINIMAL, "history": {"order": "sometimes"}})

    def test_the_example_config_and_every_preset_load(self):
        paths = [ROOT / "svrf.example.toml", *sorted((ROOT / "presets").glob("*.toml"))]
        self.assertGreaterEqual(len(paths), 2)
        for path in paths:
            config = load(path)
            self.assertTrue(config.gate.commands, path)

    def test_relative_paths_resolve_against_the_config_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "svrf.toml"
            path.write_text('repo = "a/b"\nclone = "clone"\nstate_dir = "state"\n[gate]\ncommands = ["true"]\n'
                            '[repair]\nunion_merge = ["CHANGELOG.md", "**/requirements*.txt"]\n')
            config = load(path)
            self.assertEqual(config.clone, Path(tmp).resolve() / "clone")
            self.assertEqual(config.lock, Path(tmp).resolve() / "state" / "svrf.lock")
            self.assertTrue(config.union_paths("svc/requirements-dev.txt"))


class CommandLine(unittest.TestCase):
    def config_file(self, tmp: Path) -> Path:
        path = tmp / "svrf.toml"
        path.write_text(f'repo = "a/b"\nclone = "{tmp / "clone"}"\nstate_dir = "{tmp / "state"}"\n'
                        '[gate]\ncommands = ["true"]\n')
        (tmp / "clone").mkdir()
        subprocess.run(["git", "-C", str(tmp / "clone"), "init", "-q"], check=True)
        return path

    def run_cli(self, *argv) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(list(argv))
        return code, out.getvalue()

    def test_a_second_owner_exits_3_and_reads_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            path = self.config_file(tmp)
            with owner_lock(tmp / "state" / "svrf.lock"):
                code, out = self.run_cli("--config", str(path), "run", "--once")
                self.assertEqual(code, 3)
                self.assertEqual(json.loads(out)["tick"], "LOCKED")
                code, out = self.run_cli("--config", str(path), "land", "--prs", "1")
                self.assertEqual(code, 3)

    def test_status_reads_the_state_without_a_network_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            path = self.config_file(tmp)
            (tmp / "state").mkdir()
            (tmp / "state" / "state.json").write_text(json.dumps({
                "schema_version": "svrf.state/1", "last_tick": {"tick": "RAN"},
                "held": {"7": {"reason": "GATE_RED", "head": "abc", "failing": ["FAIL x"]}}}))
            code, out = self.run_cli("--config", str(path), "status")
            self.assertEqual(code, 0)
            value = json.loads(out)
            self.assertEqual(value["held"], [{"number": 7, "reason": "GATE_RED", "head": "abc", "failing": ["FAIL x"]}])

    def test_the_replay_subcommand_reports_admission_and_families(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            origin = tmp / "origin.git"
            subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
            seed = tmp / "seed"
            seed.mkdir()
            env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                  "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

            def git(*args):
                subprocess.run(["git", "-C", str(seed), *args], check=True, capture_output=True, text=True, env=env)

            def out(*args):
                return subprocess.run(["git", "-C", str(seed), *args], check=True, capture_output=True, text=True,
                                      env=env).stdout.strip()

            git("init", "-q", "-b", "main")
            (seed / "a.txt").write_text("0\n")
            git("add", "-A")
            git("commit", "-qm", "base")
            git("remote", "add", "origin", str(origin))
            git("push", "-q", "origin", "main")
            git("checkout", "-q", "-b", "feature")
            (seed / "b.txt").write_text("1\n")
            git("add", "-A")
            git("commit", "-qm", "feat: b")
            git("checkout", "-q", "main")
            git("merge", "-q", "--no-ff", "--no-edit", "feature")
            merged = out("rev-parse", "HEAD")
            git("push", "-q", "origin", "main")

            clone = tmp / "clone"
            subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True, capture_output=True)
            path = tmp / "svrf.toml"
            path.write_text(f'repo = "a/b"\nclone = "{clone}"\nstate_dir = "{tmp / "state"}"\n'
                            '[gate]\ncommands = ["true"]\n')

            class FakeHub:
                def merge_commit(self, number):
                    return merged if number == 55 else None

            out_json = io.StringIO()
            with redirect_stdout(out_json):
                code = cli.main(["--config", str(path), "replay", "--merged", "55"], github=FakeHub())
            self.assertEqual(code, 0)
            report = json.loads(out_json.getvalue())
            self.assertEqual(report["admission"]["55"]["decision"], "ADMIT")
            self.assertEqual([f["prs"] for f in report["families"]], [[55]])

    def test_a_bad_config_exits_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "svrf.toml"
            path.write_text('repo = "a/b"\n')
            import contextlib

            with contextlib.redirect_stderr(io.StringIO()):
                code, _ = self.run_cli("--config", str(path), "status")
            self.assertEqual(code, 2)

    def test_the_module_entry_point_prints_help(self):
        done = subprocess.run(["python3", "-m", "svrf", "--help"], capture_output=True, text=True,
                              env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
        self.assertEqual(done.returncode, 0)
        for word in ("run", "watch", "plan", "status", "land"):
            self.assertIn(word, done.stdout)


class Packaging(unittest.TestCase):
    def test_the_systemd_service_runs_one_round_with_explicit_paths(self):
        service = (ROOT / "systemd" / "svrf.service").read_text(encoding="utf-8")
        self.assertIn("Type=oneshot", service)
        exec_line = next(line for line in service.splitlines() if line.startswith("ExecStart="))
        self.assertIn("run --once", exec_line)
        self.assertIn("--config", exec_line)
        self.assertNotIn("WorkingDirectory", service)
        timer = (ROOT / "systemd" / "svrf.timer").read_text(encoding="utf-8")
        self.assertIn("OnUnitInactiveSec=", timer)
        self.assertIn("WantedBy=timers.target", timer)

    def test_the_install_script_enables_only_when_asked(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            bindir = home / "bin"
            bindir.mkdir()
            log = home / "systemctl.log"
            (bindir / "systemctl").write_text(f"#!/bin/sh\necho \"$@\" >> {log}\n")
            (bindir / "systemctl").chmod(0o755)
            env = {**os.environ, "HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
                   "PATH": f"{bindir}:{os.environ['PATH']}"}
            script = ROOT / "systemd" / "install.sh"
            subprocess.run(["bash", str(script)], env=env, check=True, capture_output=True, cwd=tmp)
            units = home / ".config" / "systemd" / "user"
            self.assertTrue((units / "svrf.service").is_file())
            self.assertTrue((units / "svrf.timer").is_file())
            self.assertNotIn("enable", log.read_text())
            subprocess.run(["bash", str(script), "--enable"], env=env, check=True, capture_output=True, cwd=tmp)
            self.assertIn("enable --now svrf.timer", log.read_text())

    def test_the_container_files_exist_and_name_the_cli(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("gh", dockerfile)
        self.assertIn("svrf", dockerfile)
        compose = (ROOT / "docker-compose.example.yml").read_text(encoding="utf-8")
        self.assertIn("GH_TOKEN", compose)

    def test_the_license_is_polyform_shield_with_commercial_terms(self):
        license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
        self.assertIn("PolyForm Shield License 1.0.0", license_text)
        self.assertIn("Required Notice: Copyright 2026 Stephen Schweizer", license_text)
        self.assertIn('license = "LicenseRef-PolyForm-Shield-1.0.0"', (ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertIn("Required Notice:", (ROOT / "NOTICE").read_text(encoding="utf-8"))
        self.assertTrue((ROOT / "COMMERCIAL.md").is_file())


if __name__ == "__main__":
    unittest.main()
