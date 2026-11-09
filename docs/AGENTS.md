# Agent hooks

SVRF holds and re-reads pull requests automatically. When PR surfacing is enabled,
a held head's status and failing lines are also visible on GitHub. These local
interfaces provide structured evidence for an agent's own tool loop:

| Surface | What it answers |
| --- | --- |
| `svrf why <pr>` | Why this one pull request is held or where it sits in the queue, its failing lines, and a plain next step. |
| `svrf status --json` | Every held pull request and the outcome of the last round, from local state only (no network call). |
| `svrf mcp` | The same two reads, plus `requeue`, exposed as MCP tools over stdio for an agent's own tool-calling loop. |

All three read local state only (`<state_dir>/state.json`, `<state_dir>/receipts/`): they
never call GitHub, so an agent can call them as often as it likes without spending rate
budget.

## `svrf why` and `svrf status`

```sh
svrf why 128
```

```json
{
 "repo": "owner/name",
 "number": 128,
 "state": "HELD",
 "reason": "GATE_RED",
 "head": "abc123def456",
 "failing": ["FAILED tests/test_thing.py::test_it"],
 "paths": [],
 "since": "20260925T120000Z",
 "next_step": "Fix the failing lines above on this branch and push a new commit; svrf reads it again automatically."
}
```

`state` is one of `HELD`, `MERGED`, `OUT_THIS_ROUND`, a family status from the last round
(`PLANNED`, `GATED`, `RED`, `BISECTED`, ...), or `UNKNOWN` when svrf has not read this
pull request yet. `svrf status --json` (the `--json` flag is accepted for scripts and
agents; the command's output is JSON either way) gives the same held list for every open
pull request at once, plus the last round's summary.

## `svrf mcp`

A minimal MCP server over stdio (newline-delimited JSON-RPC 2.0), started with:

```sh
svrf --config svrf.toml mcp
```

It exposes three tools:

- **`queue_status`** — no arguments; the same object as `svrf status --json`.
- **`why_held(pr)`** — the same object as `svrf why <pr>`.
- **`requeue(pr)`** — clears `pr`'s hold record so svrf reads its head again next round.
  This is the *only* tool with a side effect, and the side effect is narrow: it deletes
  one entry from local state. It never merges, gates, pushes, or touches GitHub in any
  way — the next scheduled round decides what happens to the pull request from scratch,
  exactly as if it had never been held.

### Wiring it into an agent

Most MCP-capable coding agents take a stdio server command in their own config. For
example, in an agent that reads a JSON list of MCP servers:

```json
{
  "mcpServers": {
    "svrf": {
      "command": "svrf",
      "args": ["--config", "svrf.toml", "mcp"]
    }
  }
}
```

### Example agent prompt

A prompt for an agent that just pushed a pull request and wants to close the loop itself,
instead of waiting on a human to notice a hold:

> Your pull request may be picked up by SVRF, a merge train that gates and lands pull
> requests automatically. If it has not merged after a few minutes, call the `why_held`
> tool with your pull request number. If it reports `state: "HELD"`, read `failing` and
> `next_step`, fix the problem on your branch, and push — SVRF re-reads the new head on
> its own; you do not need to call `requeue`. Only call `requeue` if you believe the hold
> was based on a now-stale state (for example, you already pushed a fix and SVRF has not
> re-read it in longer than one round's interval). Never merge, force-push, or bypass the
> gate yourself; `requeue` only clears a hold record, and SVRF's own gate decides whether
> your fix actually passes.

## Security

Every tool here is read-only except `requeue`, and `requeue`'s only effect is described
above. None of the three surfaces runs a shell command with attacker-controlled input:
`svrf why`/`svrf status` read JSON files under `state_dir`, and the MCP server dispatches
on a fixed tool name to the same in-process functions the CLI uses. No surface accepts or
constructs a git ref, branch name, or shell command from its arguments.
