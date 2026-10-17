"""`svrf doctor`: read-only checks that catch a broken setup before the first scheduled
round runs unattended. Every check is injectable (a `run` callable for subprocess calls,
a `github` object for the rate-budget read) so the whole module is testable without a
real token or network access.
"""

from __future__ import annotations

import json as _json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .errors import ReadFailed


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    fix: str = ""


def _run(argv: list[str], timeout: float = 20) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        return subprocess.CompletedProcess(argv, 127, "", str(error))


def check_git(run=_run) -> Check:
    # `-C .`: every git call in this package names its repository explicitly
    # (test_daemon.ExplicitRepository), even one that, like `--version`, does not
    # actually need one.
    done = run(["git", "-C", ".", "--version"])
    if done.returncode == 0:
        return Check("git", True, done.stdout.strip() or "installed")
    return Check("git", False, "git is not runnable", fix="install git")


def check_gh(run=_run) -> Check:
    done = run(["gh", "--version"])
    if done.returncode == 0:
        return Check("gh CLI", True, (done.stdout.splitlines() or ["installed"])[0])
    return Check("gh CLI", False, "gh is not runnable", fix="install https://cli.github.com/")


def check_gate_command(config: Config) -> Check:
    if not config.gate.commands:
        return Check("gate command", False, "no gate.commands configured",
                     fix="add [gate] commands = [...] to your svrf.toml")
    missing = []
    for command in config.gate.commands:
        exe = command.split()[0] if command.split() else ""
        if exe and shutil.which(exe) is None and not Path(exe).exists():
            missing.append(exe)
    if missing:
        return Check("gate command", False, f"not found on PATH: {', '.join(missing)}",
                     fix="install the missing tool, or run svrf where it is on PATH")
    return Check("gate command", True, f"{len(config.gate.commands)} command(s) found on PATH")


def check_token(repo: str, run=_run) -> Check:
    done = run(["gh", "api", f"repos/{repo}", "-i"])
    if done.returncode != 0:
        text = (done.stderr or done.stdout or "").strip()
        return Check("token access", False, (text.splitlines() or ["gh api failed"])[0][:160],
                     fix=f"grant the token access to {repo} (see docs/GITHUB_SETUP.md)")
    scopes_line = next((line for line in done.stdout.splitlines() if line.lower().startswith("x-oauth-scopes:")), "")
    if not scopes_line:
        # Fine-grained tokens and GitHub Apps report no OAuth-scopes header at all; a
        # successful read is the only signal available for them.
        return Check("token access", True, f"reached repos/{repo} (fine-grained token or App: no scopes header)")
    scopes = {s.strip() for s in scopes_line.split(":", 1)[1].split(",") if s.strip()}
    if "repo" not in scopes:
        return Check("token scopes", False, f"has {sorted(scopes)}, missing 'repo'",
                     fix="see docs/GITHUB_SETUP.md for the exact permissions svrf needs")
    return Check("token access", True, f"reached repos/{repo} (scopes: {', '.join(sorted(scopes))})")


def check_branch_protection(repo: str, base: str, run=_run) -> Check:
    done = run(["gh", "api", f"repos/{repo}/branches/{base}/protection"])
    if done.returncode != 0:
        return Check("branch protection", True, f"{base} has no protection rule that would block svrf's merges")
    try:
        data = _json.loads(done.stdout or "{}")
    except ValueError:
        return Check("branch protection", True, "could not parse the protection response; assuming nothing blocks svrf")
    contexts = ((data.get("required_status_checks") or {}).get("contexts")) or []
    if contexts:
        return Check("branch protection", False,
                     f"{base} requires status checks {contexts} that svrf does not report on its own",
                     fix="turn off required status checks for svrf's merge path, report them separately, or grant "
                         "the token bypass rights (see docs/GITHUB_SETUP.md)")
    return Check("branch protection", True, f"{base}'s protection does not require status checks svrf cannot supply")


def check_rate_budget(github) -> Check:
    try:
        budget = github.rate_limit()
    except ReadFailed as failure:
        return Check("GitHub rate budget", False, failure.reason, fix="check network access and the token")
    core = budget.get("core", {})
    remaining = int(core.get("remaining", 0))
    if remaining <= 0:
        return Check("GitHub rate budget", False, "core budget exhausted", fix="wait for the reported reset time")
    return Check("GitHub rate budget", True, f"core remaining: {remaining}")


def run(config: Config, *, github=None, subprocess_run=_run) -> list[Check]:
    if github is None:
        from .github import RealGitHub

        github = RealGitHub(config.repo, limit=config.train.snapshot_limit)
    return [
        check_git(subprocess_run),
        check_gh(subprocess_run),
        check_gate_command(config),
        check_token(config.repo, subprocess_run),
        check_branch_protection(config.repo, config.base, subprocess_run),
        check_rate_budget(github),
    ]


def render(checks: list[Check]) -> str:
    lines = []
    for check in checks:
        mark = "✓" if check.ok else "✗"
        lines.append(f"{mark} {check.name}: {check.detail}")
        if not check.ok and check.fix:
            lines.append(f"  fix: {check.fix}")
    return "\n".join(lines)
