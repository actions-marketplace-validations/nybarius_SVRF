"""What SVRF renders on a pull request: a commit status and a living comment, so a
developer sees the train's decision about their head without leaving the pull request.

Pure rendering only: every function here takes a plain `event` dict and returns text: no
git, no GitHub, no clock. `train.post_surface` is the one place that posts an `event` to
GitHub; `Train._surface_event` builds the family/gate/hold events and `Daemon.hold` the
admission holds'.

An `event` carries: `number` (int), `phase` (`QUEUED`, `GATING`, `LANDED`, `HELD`),
`history` (phases already reached, oldest first), `position` (queue position, `QUEUED`
only), `batch` (family id) and `members` (the family's pull-request numbers), `gate_seconds`
(once a gate ran), and, once held, `reason_line`, `failing` (residual lines) and, when the
hold is a conflict with a named partner, `conflict_with` and `conflict_paths`; an admission
hold also carries the held `head` sha.

A pull request's title, branch name and gate output are attacker-controlled (anyone who
can open a pull request controls them); `escape_md` and `_fence` keep them literal text in
the rendered Markdown, never new structure or raw HTML.
"""

from __future__ import annotations

import hashlib
import re

CONTEXT = "svrf"
MARKER = "<!-- svrf-pr-surface"

EMOJI = {"QUEUED": "⏳", "GATING": "⚙️", "LANDED": "✅", "HELD": "\U0001f6d1"}
PHASE_LABEL = {"QUEUED": "queued", "GATING": "gating", "LANDED": "landed", "HELD": "held"}
STATUS_STATE = {"QUEUED": "pending", "GATING": "pending", "LANDED": "success", "HELD": "failure"}
TIMELINE_ORDER = ("QUEUED", "GATING")

_ESCAPE = re.compile(r"([\\`*_\[\]()|~^])")


def _strip_control(text) -> str:
    return "".join(ch for ch in str(text) if ch in "\n\t" or ord(ch) >= 0x20)


def one_line(text, limit: int = 120) -> str:
    """Collapsed to one line, for a commit status description (GitHub allows 140 bytes)."""
    text = " ".join(_strip_control(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def escape_md(text) -> str:
    """Literal Markdown: an attacker-controlled path, branch name or gate line can never
    open new Markdown structure or raw HTML."""
    text = _strip_control(text)
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return _ESCAPE.sub(r"\\\1", text)


def fmt_duration(seconds) -> str:
    seconds = max(0, round(float(seconds or 0)))
    minutes, rest = divmod(int(seconds), 60)
    return f"{minutes}m{rest:02d}s" if minutes else f"{rest}s"


def _fence(lines: list[str]) -> str:
    """A fenced code block long enough to swallow any run of backticks already in the
    content, so held gate output cannot break out of the block."""
    body = "\n".join(lines)
    longest = max((len(run) for run in re.findall(r"`+", body)), default=0)
    tick = "`" * max(3, longest + 1)
    return f"{tick}\n{body}\n{tick}"


def next_step(event: dict) -> str:
    """What a developer does next, in one line."""
    partners = event.get("conflict_with")
    if partners:
        where = event.get("conflict_paths") or []
        tail = f" on {', '.join(escape_md(p) for p in where[:3])}" if where else ""
        return "conflicts with " + " ".join(f"#{p}" for p in partners) + tail
    return "push a fix; SVRF retries automatically when your head changes"


def status_line(event: dict) -> tuple[str, str]:
    """The commit status for `event`: (state, description)."""
    phase = event["phase"]
    if phase == "QUEUED":
        desc = f"queued (position {event.get('position')}, batch {event.get('batch')})"
    elif phase == "GATING":
        others = " ".join(f"#{m}" for m in (event.get("members") or []) if m != event["number"])
        desc = f"gating batch {event.get('batch')}" + (f" with {others}" if others else "")
    elif phase == "LANDED":
        desc = f"landed in batch {event.get('batch')} (gate {fmt_duration(event.get('gate_seconds'))})"
    elif phase == "HELD":
        desc = f"held: {one_line(event.get('reason_line') or 'held')}"
    else:
        raise ValueError(phase)
    return STATUS_STATE[phase], one_line(desc, limit=140)


def _timeline(event: dict) -> str:
    seen = set(event.get("history") or ()) | {event["phase"]}
    done, pending = "✅", "○"
    parts = [f"{done if p in seen else pending} {PHASE_LABEL[p]}" for p in TIMELINE_ORDER]
    if "LANDED" in seen:
        parts.append("✅ landed")
    elif "HELD" in seen:
        parts.append("✖️ held")
    else:
        parts.append("○ landed/held")
    return " → ".join(parts)


def _collapse_blanks(lines: list[str]) -> list[str]:
    out: list[str] = []
    for line in lines:
        if line == "" and out and out[-1] == "":
            continue
        out.append(line)
    return out


def render_comment(event: dict) -> str:
    """The living comment's full body, ending with the hidden marker (carrying this
    body's hash) that finds and identifies it on the next update."""
    number = event["number"]
    phase = event["phase"]
    members = [m for m in (event.get("members") or []) if m != number]
    lines = [f"{EMOJI[phase]} **SVRF: {PHASE_LABEL[phase]}**", ""]
    batch = event.get("batch")
    if batch:
        line = f"Batch `{escape_md(batch)}`"
        if members:
            line += " with " + " ".join(f"#{m}" for m in members)
        lines.append(line)
    if phase == "QUEUED" and event.get("position") is not None:
        lines.append(f"Queue position **{event['position']}**")
    if event.get("head"):
        lines.append(f"Head `{re.sub(r'[^0-9A-Za-z]', '', str(event['head']))[:64]}`")
    lines.append("")
    lines.append(_timeline(event))
    if event.get("gate_seconds") is not None:
        lines.append("")
        lines.append(f"Gate: {fmt_duration(event['gate_seconds'])}")
    if phase == "LANDED":
        lines.append("")
        lines.append("**landed tree = gated tree** ✔️")
    if phase == "HELD":
        lines.append("")
        lines.append(f"**Next:** {next_step(event)}")
        failing = [_strip_control(x)[:300] for x in (event.get("failing") or []) if str(x).strip()][:24]
        if failing:
            lines.append("")
            lines.append("<details><summary>Why it's held</summary>")
            lines.append("")
            lines.append(_fence(failing))
            lines.append("")
            lines.append("</details>")
    lines = _collapse_blanks(lines)
    body = "\n".join(lines).rstrip() + "\n"
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
    return f"{body}\n{MARKER} hash={digest} -->\n"


_HASH_RE = re.compile(re.escape(MARKER) + r" hash=([0-9a-f]{16}) -->")


def comment_hash(body: str) -> str | None:
    match = _HASH_RE.search(body or "")
    return match.group(1) if match else None


def is_surface_comment(body: str) -> bool:
    return MARKER in (body or "")
