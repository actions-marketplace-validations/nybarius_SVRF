# Using SVRF as a GitHub Action

`action.yml` at the repository root runs **one round** (`svrf run --once`) of the train:
one discovery/admission/gate/land pass, then it exits. It does not watch continuously —
schedule it with a workflow's `on.schedule`, the same way you would run any periodic job.

> **Release note:** `v0.3.0` is the latest tagged release. `main` contains
> additional unreleased admission and retry changes. Pin a reviewed release or
> commit for reproducibility.

## Zero-config

The Action works with no `svrf.toml` in the repository at all: if the file the `config`
input names is not on disk, `action/run.sh` runs the same auto-detection `svrf init
--yes` does (project type, gate command, base branch, repository) before the round, using
`GITHUB_REPOSITORY` for the repository slug. The generated config is not committed back;
it is written into the job's own checkout and regenerated every round. For a project type
`svrf init` recognizes (`pyproject.toml`/`setup.py`/`requirements.txt`, `package.json`,
`Cargo.toml`, `go.mod`, a Lean `lakefile`, or a Makefile `test:` target) this needs no
setup at all. For anything else it falls back to a placeholder gate command that will
fail `svrf doctor` and the round's own gate step — run `svrf init` yourself once (with
`--gate` if needed), commit the resulting `svrf.toml`, and the Action will use it instead
of guessing.

This does mean the checkout step for a zero-config workflow needs the whole repository,
not the `sparse-checkout: svrf.toml` trick the example below uses — detection has to see
`pyproject.toml`, `package.json`, and the rest to guess anything. Once `svrf.toml` is
committed (whether you wrote it or the Action's own fallback did, copied back out of a
run's logs), you can switch to the narrower sparse checkout below.

## Inputs

| Input | Required | Default | Meaning |
| --- | --- | --- | --- |
| `github-token` | yes | | A token scoped to this repository with the permissions in `docs/GITHUB_SETUP.md` (contents and pull requests read/write, issues write, metadata read, and commit statuses write when `ui.status_checks` is enabled). Exposed to the round as `GH_TOKEN`. |
| `config` | no | `svrf.toml` | Path to the config file, relative to the job's working directory. |
| `python-version` | no | `3.12` | Python used to install and run `svrf` itself — not your gate's own toolchain. |

## Scheduled workflow example

```yaml
name: svrf
on:
  schedule:
    - cron: "*/10 * * * *"
  workflow_dispatch: {}

jobs:
  round:
    runs-on: ubuntu-latest        # see "Which runner" below
    concurrency:
      group: svrf-round
      cancel-in-progress: false   # never cancel a round mid-merge
    steps:
      - uses: actions/checkout@v4   # only needed so svrf.toml exists in the workspace
        with:
          sparse-checkout: |
            svrf.toml
          sparse-checkout-cone-mode: false

      # Optional: persist SVRF's own clone and held-pull-request memory between runs on a
      # hosted (ephemeral) runner, so every scheduled round doesn't reclone from scratch
      # and re-read pull requests it already knows are held.
      - uses: actions/cache@v4
        with:
          path: ~/.local/share/svrf
          key: svrf-state-${{ github.repository }}

      - uses: nybarius/SVRF@v0.3.0
        with:
          github-token: ${{ secrets.SVRF_TOKEN }}
          config: svrf.toml
```

`secrets.SVRF_TOKEN` should be a repository or fine-grained personal access token
secret you created with the permissions listed in [GITHUB_SETUP.md](GITHUB_SETUP.md) — the workflow's
own automatic `GITHUB_TOKEN` does not have the pull-request or issues write scopes SVRF
needs by default, and using it would attribute every merge to `github-actions[bot]`
without the permissions to actually make one.

## Which runner

- **Hosted runners** (`ubuntu-latest` etc.) work fine for a small repository whose
  `gate.commands` finish quickly and don't need much memory or a warm build cache: the
  round itself (listing, planning, landing) is light, and the gate is the only heavy part.
  Because hosted runners are ephemeral, cache `~/.local/share/svrf` (above) if you want
  held-pull-request memory and the train's own clone to survive between scheduled runs;
  without it, SVRF just re-clones and re-reads everything each round, which is correct
  but slower.
- **Self-hosted runners** are the better fit once `gate.commands` are the expensive part:
  a large test suite, a compiler with its own cache, anything that benefits from a warm
  disk between rounds (see the Lean monorepo numbers in `docs/BENCHMARK.md`, which were
  run on a self-hosted machine, not this Action). A self-hosted runner also naturally
  keeps `~/.local/share/svrf` on disk between runs, so the `actions/cache` step above is
  unnecessary there.

Either way, only one round should run at a time per repository (`owner_lock` in
`src/svrf/locks.py` already refuses a second concurrent owner and exits 3), which is what
the `concurrency` block in the example is for — it keeps GitHub Actions from starting an
overlapping run rather than relying on the lock alone.

## Continuous instead of scheduled

If you'd rather run `svrf watch` continuously instead of one round per schedule tick, run
it directly (Docker or `pip install`, see the README's quickstart) on a long-lived
machine or self-hosted runner instead of through this Action — a GitHub Actions job has a
maximum runtime and isn't meant to run forever.
