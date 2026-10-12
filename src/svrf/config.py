"""`svrf.toml`: everything the train needs to know about one repository.

Only `repo` and `gate.commands` are required. See the README's configuration reference.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .globs import PathSet

DEFAULT_TESTS = ["tests/**", "test/**", "test_*", "*_test.*", "*.test.*", "*_spec.*"]
DEFAULT_DOCS = ["docs/**", "*.md", "*.rst", "*.txt"]


class ConfigError(ValueError):
    pass


@dataclass
class GateConfig:
    commands: list[str] = field(default_factory=list)
    setup: list[str] = field(default_factory=list)
    timeout_minutes: float = 60
    memory_gb: float = 0          # 0: no memory guard
    memory_reserve_gb: float = 2
    infra_patterns: list[str] = field(default_factory=list)
    failing_pattern: str = r"error|FAIL|Traceback"
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class TrainConfig:
    family_size: int | str = 8        # a whole count, or "adaptive" (svrf.batching)
    family_cap: int = 8               # the most an adaptive family may hold
    jobs: int = 2
    max_rounds: int = 3
    rate_floor: int = 200
    comment: bool = True
    interval_seconds: float = 300
    snapshot_limit: int = 1000       # the most open pull requests one snapshot lists (ten 100-row pages)


@dataclass
class UiConfig:
    pr_comments: bool = True
    status_checks: bool = True
    dashboard_url: str = ""


@dataclass
class MergeConfig:
    tree_command: str = ""        # "": git alone builds every merge step
    tree_timeout_seconds: float = 120


@dataclass
class HistoryConfig:
    order: str = "off"            # "off" or "tests-first"
    reland: bool = True
    tests: list[str] = field(default_factory=lambda: list(DEFAULT_TESTS))
    docs: list[str] = field(default_factory=lambda: list(DEFAULT_DOCS))
    refactor_prefixes: list[str] = field(default_factory=lambda: ["refactor", "perf"])


@dataclass
class Config:
    repo: str
    base: str = "main"
    clone: Path = Path("~/.local/share/svrf/clone")
    state_dir: Path = Path("~/.local/share/svrf/state")
    remote: str = "origin"
    git_name: str = "svrf"
    git_email: str = "svrf@localhost"
    hold_label: str = "train:hold"
    union_merge: list[str] = field(default_factory=list)
    repair: bool = True
    admission_command: str = ""
    admission_timeout_minutes: float = 10
    admission_slots: int | None = None   # None: borrow train.jobs
    admission_capacity_command: str | None = None   # None: the configured slots are the round's concurrency
    admission_priority_command: str | None = None   # None: a backlog reads newest first, last tick's unread last
    admission_round_reads: int | None = None   # None: a round reads every candidate
    demand_driver: str = ""
    admission_watch: list[str] = field(default_factory=list)
    gate: GateConfig = field(default_factory=GateConfig)
    train: TrainConfig = field(default_factory=TrainConfig)
    history: HistoryConfig = field(default_factory=HistoryConfig)
    ui: UiConfig = field(default_factory=UiConfig)
    merge: MergeConfig = field(default_factory=MergeConfig)

    # ---- derived paths
    @property
    def receipts(self) -> Path:
        return self.state_dir / "receipts"

    @property
    def worktrees(self) -> Path:
        return self.state_dir / "worktrees"

    @property
    def logs(self) -> Path:
        return self.state_dir / "logs"

    @property
    def lock(self) -> Path:
        return self.state_dir / "svrf.lock"

    @property
    def union_paths(self) -> PathSet:
        return PathSet(self.union_merge)


_SECTIONS = {"gate": GateConfig, "train": TrainConfig, "history": HistoryConfig, "ui": UiConfig,
             "merge": MergeConfig}
_TOP = {"repo", "base", "clone", "state_dir", "remote", "git_name", "git_email", "hold_label", "admission_command", "demand_driver"}


def from_dict(value: dict, *, root: Path | None = None) -> Config:
    """Build a Config from a parsed TOML document. Unknown keys are refused, so a typo
    never silently turns a safety setting off."""
    value = dict(value)
    known = _TOP | set(_SECTIONS) | {"labels", "repair", "admission"}
    unknown = sorted(set(value) - known)
    if unknown:
        raise ConfigError(f"unknown keys: {', '.join(unknown)}")
    if not value.get("repo") or "/" not in str(value["repo"]):
        raise ConfigError("repo must be given as owner/name")
    kwargs: dict = {k: value[k] for k in _TOP if k in value}
    labels = value.get("labels") or {}
    if set(labels) - {"hold"}:
        raise ConfigError(f"unknown keys in [labels]: {sorted(set(labels) - {'hold'})}")
    if "hold" in labels:
        kwargs["hold_label"] = labels["hold"]
    repair = value.get("repair") or {}
    if set(repair) - {"union_merge", "enabled"}:
        raise ConfigError(f"unknown keys in [repair]: {sorted(set(repair) - {'union_merge', 'enabled'})}")
    kwargs["union_merge"] = list(repair.get("union_merge") or [])
    kwargs["repair"] = bool(repair.get("enabled", True))
    admission = value.get("admission") or {}
    known = {"command", "watch", "timeout_minutes", "slots", "capacity_command", "priority_command", "round_reads"}
    if set(admission) - known:
        raise ConfigError(f"unknown keys in [admission]: {sorted(set(admission) - known)}")
    if "capacity_command" in admission:
        if not isinstance(admission["capacity_command"], str) or not admission["capacity_command"].strip():
            raise ConfigError("[admission] capacity_command must be a command line")
        kwargs["admission_capacity_command"] = admission["capacity_command"]
    if "priority_command" in admission:
        if not isinstance(admission["priority_command"], str) or not admission["priority_command"].strip():
            raise ConfigError("[admission] priority_command must be a command line")
        kwargs["admission_priority_command"] = admission["priority_command"]
    if "round_reads" in admission:
        reads = admission["round_reads"]
        if isinstance(reads, bool) or not isinstance(reads, int) or reads < 1:
            raise ConfigError("[admission] round_reads must be a whole number of at least 1")
        kwargs["admission_round_reads"] = reads
    if "slots" in admission:
        slots = admission["slots"]
        if isinstance(slots, bool) or not isinstance(slots, int) or slots < 1:
            raise ConfigError("[admission] slots must be a whole number of at least 1")
        kwargs["admission_slots"] = slots
    kwargs["admission_watch"] = list(admission.get("watch") or [])
    if "command" in admission:
        kwargs["admission_command"] = admission["command"]
    if "timeout_minutes" in admission:
        minutes = admission["timeout_minutes"]
        if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or minutes <= 0:
            raise ConfigError("[admission] timeout_minutes must be a positive number of minutes")
        kwargs["admission_timeout_minutes"] = float(minutes)
    for name, cls in _SECTIONS.items():
        section = value.get(name) or {}
        fields = set(cls.__dataclass_fields__)
        bad = sorted(set(section) - fields)
        if bad:
            raise ConfigError(f"unknown keys in [{name}]: {', '.join(bad)}")
        kwargs[name] = cls(**section)
    limit = kwargs["train"].snapshot_limit
    if type(limit) is not int or limit < 1:
        raise ConfigError("[train] snapshot_limit must be a positive whole count")
    for key in ("clone", "state_dir"):
        if key in kwargs:
            path = Path(os.path.expandvars(str(kwargs[key]))).expanduser()
            if root is not None and not path.is_absolute():
                path = root / path
            kwargs[key] = path
    config = Config(**kwargs)
    config.clone = Path(config.clone).expanduser()
    config.state_dir = Path(config.state_dir).expanduser()
    if config.history.order not in ("off", "tests-first"):
        raise ConfigError("history.order must be \"off\" or \"tests-first\"")
    size = config.train.family_size
    if isinstance(size, str):
        if size != "adaptive":
            raise ConfigError('train.family_size must be a whole number or "adaptive"')
    elif isinstance(size, bool) or not isinstance(size, int) or size < 1:
        raise ConfigError("train.family_size and train.jobs must be at least 1")
    if config.train.jobs < 1:
        raise ConfigError("train.family_size and train.jobs must be at least 1")
    if isinstance(config.train.family_cap, bool) or not isinstance(config.train.family_cap, int) \
            or config.train.family_cap < 1:
        raise ConfigError("train.family_cap must be a whole number of at least 1")
    timeout = config.merge.tree_timeout_seconds
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ConfigError("[merge] tree_timeout_seconds must be a positive number of seconds")
    if not isinstance(config.merge.tree_command, str):
        raise ConfigError("[merge] tree_command must be a string")
    if not config.gate.commands:
        raise ConfigError("gate.commands must name at least one command")
    return config


def load(path: Path | str) -> Config:
    path = Path(path).expanduser()
    with open(path, "rb") as handle:
        value = tomllib.load(handle)
    return from_dict(value, root=path.resolve().parent)
