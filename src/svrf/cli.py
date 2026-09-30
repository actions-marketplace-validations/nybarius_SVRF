"""The `svrf` command line.

    svrf run --once          one round: discover, admit, gate, land (the timer's mode)
    svrf watch               a round every train.interval_seconds
    svrf plan --dry-run      one round that gates and reports but pushes, merges and remembers nothing
    svrf land --prs 12,15    one batched run over the named pull requests
    svrf status              held pull requests and the last round
    svrf why 12              why one pull request is held or queued, and what to do next
    svrf forget 12,15        drop pull requests from the held memory
    svrf config              print the parsed configuration
    svrf init                detect this project and write svrf.toml + a scheduled workflow
    svrf doctor              read-only checks: token, branch protection, gate, rate budget
    svrf mcp                 an MCP server over stdio for coding agents (see docs/AGENTS.md)
    svrf dashboard --receipts DIR --out DIR   a static site from round receipts (no config needed)

Every command reads `--config` (default: $SVRF_CONFIG, else ./svrf.toml); `init` (which
creates that file) and `dashboard` (which reads only receipts) are the exceptions.
Exit codes: 0 ok, 1 a landed tree did not match its gated tree, 2 bad configuration,
3 another train owns the lock.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import time
from pathlib import Path

from . import __version__, dashboard, replay as replayer
from .app import build, ensure_clone
from .config import ConfigError, load
from .errors import ReadFailed
from .git import RealGit
from .github import RealGitHub
from .locks import owner_lock
from .train import Train

NEXT_STEPS = {
    "GATE_RED": "Fix the failing lines above on this branch and push a new commit; svrf reads it again automatically.",
    "CONFLICT": "Merge or rebase the base branch into this branch to resolve the conflicts, then push.",
    "REPAIR_CONFLICT": "The automatic union-merge repair hit a real conflict; resolve it by hand and push.",
    "RELAND_CONFLICT": "The automatic history reordering hit a conflict; resolve it by hand and push.",
    "RELAND_TREE_MISMATCH": "The reordered history produced a different tree than the original; ask a maintainer to look.",
    "ADMISSION_HELD": "Your admission.command failed; fix what its output above names and push.",
    "PARENT_CLOSED_UNMERGED": "The pull request this one is stacked on closed without merging; retarget this one onto the base yourself.",
}
DEFAULT_NEXT_STEP = "Push a new commit once you believe it is fixed; svrf reads the new head automatically."


def _numbers(text: str) -> list[int]:
    return [int(n) for n in text.replace(" ", ",").split(",") if n.strip()]


def _print(value) -> None:
    print(json.dumps(value, indent=1, sort_keys=True, default=str), flush=True)


def status(config) -> dict:
    state_dir = Path(config.state_dir)
    out: dict = {"repo": config.repo, "base": config.base, "state_dir": str(state_dir)}
    try:
        state = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
        out["last_tick"] = state.get("last_tick")
        out["held"] = [{"number": int(n), "reason": e.get("reason"), "head": (e.get("head") or "")[:12],
                        "failing": e.get("failing", [])[:3]} for n, e in sorted(state.get("held", {}).items(),
                                                                               key=lambda kv: int(kv[0]))]
    except (OSError, ValueError):
        out["last_tick"], out["held"] = None, []
    receipts = sorted(Path(config.receipts).glob("train-*.json"))
    if receipts:
        last = json.loads(receipts[-1].read_text(encoding="utf-8"))
        out["last_receipt"] = {"path": str(receipts[-1]), "started": last.get("started"),
                               "finished": last.get("finished"),
                               "merged": [m["number"] for m in last.get("merges", []) if m.get("identity")],
                               "gates": len([g for g in last.get("gates", []) if "reused" not in g]),
                               "held": [h["number"] for h in last.get("holds", [])],
                               "alerts": [a["alert"] for a in last.get("alerts", [])]}
    return out


def _last_receipt(config, number: int | None = None, tick: dict | None = None) -> dict | None:
    receipts = sorted(Path(config.receipts).glob("train-*.json"))
    if not receipts:
        return None
    selected = (tick or {}).get("receipts") or []
    # An automatic round may produce several groups. Read only its listed local
    # files, and let a later explicit train supersede it as before.
    receipts = [p for p in receipts if str(p) in selected] if str(receipts[-1]) in selected else receipts[-1:]
    for path in reversed(receipts):
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (number is None or any(m.get("number") == number for m in receipt.get("merges", []))
                or any(number in f.get("prs", []) for f in receipt.get("families", []))
                or str(number) in receipt.get("out", {})):
            return receipt
    return None


def why(config, number: int) -> dict:
    """Why one pull request is held or where it sits in the last round's queue, with its
    failing lines and a plain next step, read entirely from local state: no network call."""
    out: dict = {"repo": config.repo, "number": number}
    try:
        state = json.loads((Path(config.state_dir) / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {"held": {}}
    held = (state.get("held") or {}).get(str(number))
    if held:
        reason = held.get("reason")
        out.update(state="HELD", reason=reason, head=(held.get("head") or "")[:12],
                   failing=held.get("failing", [])[:24], paths=held.get("paths", []), since=held.get("at"),
                   next_step=NEXT_STEPS.get(reason, DEFAULT_NEXT_STEP))
        return out
    last = _last_receipt(config, number, state.get("last_tick"))
    if last:
        merged = {int(m["number"]) for m in last.get("merges", []) if m.get("identity")}
        if number in merged:
            out.update(state="MERGED", next_step="Already merged; nothing to do.")
            return out
        for family in last.get("families", []):
            prs = [int(p) for p in family.get("prs", [])]
            if number in prs:
                status_ = family.get("status", "PLANNED")
                out.update(state=status_, batch=family.get("id"), queue_position=prs.index(number) + 1,
                           batch_size=len(prs))
                if status_ == "RED" or status_ == "BISECTED":
                    out["next_step"] = ("This batch went red together with others; svrf bisects it automatically "
                                        "and holds only the pull request(s) actually at fault. Run svrf why again "
                                        "shortly, or after the next round.")
                elif status_ == "HELD":
                    out["next_step"] = "Held after bisection; check the next round's state or svrf status for the failing lines."
                else:
                    out["next_step"] = "Admitted and queued in the last round; run svrf status for the outcome."
                return out
        out_map = last.get("out") or {}
        entry = out_map.get(str(number))
        if entry is not None:
            out.update(state="OUT_THIS_ROUND", detail=entry,
                       next_step="Conflicted with a larger compatible batch this round; it is read again next round.")
            return out
    out.update(state="UNKNOWN",
               next_step=("Not currently held and not seen in the last round's receipt; it may not be open against "
                          "this base, may already be merged, or svrf has not read it yet (see svrf status)."))
    return out


def main(argv: list[str] | None = None, *, github=None) -> int:
    ap = argparse.ArgumentParser(prog="svrf", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"svrf {__version__}")
    ap.add_argument("--config", default=os.environ.get("SVRF_CONFIG") or "svrf.toml")
    sub = ap.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="one round")
    run.add_argument("--once", action="store_true", help="one round, then exit (the default)")
    run.add_argument("--dry-run", action="store_true")
    watch = sub.add_parser("watch", help="a round every interval")
    watch.add_argument("--interval", type=float, default=None)
    watch.add_argument("--dry-run", action="store_true")
    plan = sub.add_parser("plan", help="one round without effects")
    plan.add_argument("--dry-run", action="store_true", default=True, help="always on for plan")
    land = sub.add_parser("land", help="one batched run over named pull requests")
    land.add_argument("--prs", required=True)
    land.add_argument("--dry-run", action="store_true")
    status_p = sub.add_parser("status", help="held pull requests and the last round")
    status_p.add_argument("--json", action="store_true", help="present for scripts and agents; status is always JSON")
    why_p = sub.add_parser("why", help="why one pull request is held or queued, and what to do next")
    why_p.add_argument("number", type=int)
    forget = sub.add_parser("forget", help="drop pull requests from the held memory")
    forget.add_argument("numbers")
    sub.add_parser("config", help="print the parsed configuration")
    replay_cmd = sub.add_parser("replay", help="admission and planning against an already-merged snapshot")
    replay_cmd.add_argument("--merged", required=True, help="already-merged pull request numbers, e.g. 101,102")
    replay_cmd.add_argument("--base", default=None,
                            help="the shared round base (a sha); default: every listed pull request's own "
                                 "reconstructed base, which must then be identical")
    replay_cmd.add_argument("--gate", action="store_true", help="also run the configured gate on each family")
    dash = sub.add_parser("dashboard", help="write a static site (index.html) from round receipts")
    dash.add_argument("--receipts", required=True, help="directory of train-*.json receipts")
    dash.add_argument("--out", required=True, help="directory to write index.html into")
    dash.add_argument("--title", default="SVRF merge train")
    sub.add_parser("doctor", help="read-only checks: token, branch protection, gate command, rate budget")
    sub.add_parser("mcp", help="an MCP server over stdio: queue_status, why_held, requeue (see docs/AGENTS.md)")
    init_p = sub.add_parser("init", help="detect this project and write svrf.toml + a scheduled workflow")
    init_p.add_argument("--yes", action="store_true", help="accept every detected default without asking")
    init_p.add_argument("--force", action="store_true", help="overwrite an existing svrf.toml or workflow file")
    init_p.add_argument("--repo", default=None, help="owner/name (default: detected from the git remote)")
    init_p.add_argument("--base", default=None, help="base branch pull requests land on (default: detected)")
    init_p.add_argument("--gate", default=None, help="gate command (default: detected from the project type)")
    init_p.add_argument("--dir", default=".", help="project directory (default: the current directory)")
    args = ap.parse_args(argv)

    if args.command == "dashboard":
        index = dashboard.build(Path(args.receipts), Path(args.out), title=args.title)
        _print({"dashboard": str(index)})
        return 0

    if args.command == "init":
        from . import init as init_module

        try:
            result = init_module.run(Path(args.dir), config_name=args.config or "svrf.toml", repo=args.repo,
                                     base=args.base, gate=args.gate, yes=args.yes, force=args.force)
        except ValueError as error:
            print(f"svrf: init: {error}", file=sys.stderr)
            return 2
        print(init_module.describe(result))
        return 0

    try:
        config = load(args.config)
    except (OSError, ConfigError, ValueError) as error:
        print(f"svrf: configuration {args.config}: {error}", file=sys.stderr)
        return 2

    if args.command == "config":
        value = dataclasses.asdict(config)
        _print(value)
        return 0
    if args.command == "status":
        _print(status(config))
        return 0
    if args.command == "replay":
        try:
            ensure_clone(config)
        except ReadFailed as failure:
            _print({"tick": "RETRY", "reason": failure.reason})
            return 0
        hub = github or RealGitHub(config.repo)
        git = RealGit(config.clone, base=config.base)
        numbers = _numbers(args.merged)
        found, problems = replayer.reconstruct(git, hub.merge_commit, numbers)
        report = {"requested": numbers, "problems": problems}
        if found:
            try:
                report.update(replayer.run(config, found, base=args.base, gate=args.gate))
            except replayer.ReplayError as error:
                report["error"] = str(error)
                _print(report)
                return 2
        _print(report)
        return 0
    if args.command == "why":
        _print(why(config, args.number))
        return 0
    if args.command == "doctor":
        from . import doctor as doctor_module

        checks = doctor_module.run(config)
        print(doctor_module.render(checks))
        return 0 if all(c.ok for c in checks) else 1
    if args.command == "mcp":
        from . import mcp as mcp_module

        mcp_module.serve(config)
        return 0

    dry_run = bool(getattr(args, "dry_run", False)) or args.command == "plan"
    try:
        ensure_clone(config)
    except ReadFailed as failure:
        _print({"tick": "RETRY", "reason": failure.reason})
        return 0
    daemon = build(config, dry_run=dry_run)

    if args.command == "forget":
        _print(daemon.forget(_numbers(args.numbers)))
        return 0
    if args.command == "land":
        with owner_lock(config.lock) as owned:
            if not owned:
                _print({"tick": "LOCKED", "lock": str(config.lock)})
                return 3
            train = Train(daemon.git, daemon.gh, daemon.gate, receipts=config.receipts, dry_run=dry_run,
                          is_union=config.union_paths, **daemon.train_options)
            receipt = train.run(_numbers(args.prs))
        _print({"receipt": str(train.path), "merged": [m["number"] for m in receipt["merges"] if m["identity"]],
                "held": [h["number"] for h in receipt["holds"]], "out": sorted(int(n) for n in receipt["out"]),
                "retry_later": [u["number"] for u in receipt["retry_later"]], "alerts": receipt["alerts"],
                "gates": len(receipt["gates"])})
        return 1 if any(a["alert"] == "TREE_MISMATCH" for a in receipt["alerts"]) else 0
    interval = getattr(args, "interval", None) or config.train.interval_seconds
    while True:
        summary = daemon.tick()
        _print(summary)
        if summary.get("tick") == "LOCKED" and args.command != "watch":
            return 3
        if args.command != "watch":
            return 0
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
