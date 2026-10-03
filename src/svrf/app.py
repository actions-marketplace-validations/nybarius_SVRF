"""Wiring: build the train's parts from a Config."""

from __future__ import annotations

import subprocess
import importlib
from pathlib import Path

from . import history
from .admission import Admission
from .config import Config, ConfigError
from .daemon import Daemon
from .errors import ReadFailed
from .gate import CommandGate, MemoryGuard
from .git import RealGit
from .github import RealGitHub
from .globs import PathSet
from .tree_provider import TreeCommand


def ensure_clone(config: Config) -> None:
    """Clone the repository into `config.clone` if it is not there yet."""
    clone = Path(config.clone)
    if (clone / ".git").exists() or (clone / "HEAD").exists():
        return
    clone.parent.mkdir(parents=True, exist_ok=True)
    done = subprocess.run(["git", "-C", str(clone.parent), "clone", "-q", f"https://github.com/{config.repo}.git",
                           str(clone)], capture_output=True, text=True)
    if done.returncode != 0:
        raise ReadFailed(f"CLONE_FAILED:{done.stderr.strip()[:160]}")


def kind_of(config: Config):
    return history.path_kind(PathSet(config.history.tests), PathSet(config.history.docs))


def build(config: Config, *, github=None, dry_run: bool = False, clock=None, sleep=None) -> Daemon:
    union = config.union_paths
    git = RealGit(config.clone, identity=(config.git_name, config.git_email), remote=config.remote,
                  base=config.base, union=union)
    gate = CommandGate(config.clone, config.worktrees, config.train.jobs, config.gate.commands,
                       setup=config.gate.setup, logs=config.logs, timeout_minutes=config.gate.timeout_minutes,
                       infra_patterns=config.gate.infra_patterns, failing_pattern=config.gate.failing_pattern,
                       env=config.gate.env)
    kind = kind_of(config)
    prefixes = tuple(config.history.refactor_prefixes)
    # A dedicated worktree pool (never the gate's own `config.worktrees` slots) so a
    # concurrent admission read and a concurrent gate can never fight over the same
    # slot directory.
    admission = Admission(git, order=config.history.order, kind=kind, refactor_prefixes=prefixes,
                          command=config.admission_command, pool=Path(config.worktrees) / "admission",
                          slots=config.train.jobs, timeout=config.admission_timeout_minutes * 60)
    memory = MemoryGuard(need_gb=config.gate.memory_gb, reserve_gb=config.gate.memory_reserve_gb) \
        if config.gate.memory_gb else None
    extra = {}
    if clock is not None:
        extra["clock"] = clock
    if sleep is not None:
        extra["sleep"] = sleep
    train_options = {"jobs": config.train.jobs, "family_size": config.train.family_size,
                     "memory": memory, "rate_floor": config.train.rate_floor,
                     "max_rounds": config.train.max_rounds, "comment": config.train.comment,
                     "pr_comments": config.ui.pr_comments, "status_checks": config.ui.status_checks,
                     "dashboard_url": config.ui.dashboard_url}
    if config.merge.tree_command:
        train_options["tree_provider"] = TreeCommand(config.merge.tree_command, config.clone,
                                                     timeout=config.merge.tree_timeout_seconds)
    if config.demand_driver:
        try:
            module, factory = config.demand_driver.split(":", 1)
            demand = getattr(importlib.import_module(module), factory)(config)
            if not callable(getattr(demand, "drain", None)):
                raise TypeError("driver must provide drain(owner)")
            extra["demand"] = demand
        except (ImportError, AttributeError, TypeError, ValueError) as exc:
            raise ConfigError(f"demand driver unavailable: {exc}") from exc
    return Daemon(git, github or RealGitHub(config.repo), gate, admission, state_dir=config.state_dir,
                  receipts=config.receipts, base=config.base, dry_run=dry_run, rate_floor=config.train.rate_floor,
                  lock_path=config.lock, hold_label=config.hold_label, admission_watch=config.admission_watch,
                  is_union=union, repair=config.repair, jobs=config.train.jobs, memory=memory,
                  # history.reland alone gates this: reland_class only ever returns a class
                  # from a `history:REFUSED:` residual (the built-in tests-first check,
                  # order != "off") or a `reland:REFUSED:` one an admission.command reports
                  # itself, so there is nothing to disable when the built-in check is off
                  # but a command supplies the same class of refusal.
                  reland=config.history.reland, kind=kind,
                  history_verdict=lambda b, h: history.verdict(git, b, h, kind, prefixes),
                  train_options=train_options,
                  **extra)
