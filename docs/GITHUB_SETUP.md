# GitHub setup

SVRF talks to GitHub entirely through the `gh` CLI (`src/svrf/github.py`), authenticated
by one token. This page covers what that token needs, where it goes, and how it interacts
with branch protection.

## Required permissions

Whichever kind of credential you use, it needs to be able to, on the one repository named
in `svrf.toml`:

- **Read and write repository contents** — to push branches (union-merge repairs,
  re-landed histories, retargets) and to push the fast-forward that closes an
  already-merged pull request cleanly.
- **Read and write pull requests** — to list open pull requests, read a single pull
  request, merge a pull request, and change a pull request's base branch (retargeting a
  stacked pull request once its parent has merged).
- **Write issues** — pull request comments are, on GitHub's API, issue comments; the
  optional round-end comment (`train.comment = true`) needs issues write.
- **Read metadata** — needed for essentially every other call to resolve the repository.
- **Commit statuses**: with `ui.status_checks = true` (the default), SVRF posts the `svrf` commit status using `POST /repos/{repo}/statuses/{sha}`. Grant **Commit statuses: Read and write** to the fine-grained token or GitHub App, or disable this optional surface with `ui.status_checks = false`. This is distinct from a GitHub Checks API check run: the actual gate executes `gate.commands` on the train's host.

## Fine-grained PAT vs. GitHub App

- **Fine-grained personal access token**, scoped to the one repository, with the
  permissions above (Contents: Read and write, Pull requests: Read and write, Issues:
  Read and write, Metadata: Read-only, Commit statuses: Read and write when enabled). This is the simplest route and what
  `svrf.example.toml` and the Docker/systemd setup assume. Its merges and pushes are
  attributed to the token's owning account.
- **GitHub App** installed on the repository, with the same permissions. An App's
  installation token is scoped to exactly the repositories it is installed on and can be
  narrower than a personal account's own access, and its actions are attributed to the App
  rather than a person. SVRF does not yet mint its own installation tokens (see the
  roadmap in the README); to use an App today, generate a short-lived installation token
  yourself (or with a small sidecar) and feed it in as `GH_TOKEN` the same way as a PAT,
  refreshing it before it expires (installation tokens are typically valid for one hour).

Either way, `gh_argv` in `src/svrf/github.py` refuses to run any `gh` call that does not
explicitly name the configured repository (`--repo <repo>` or a `repos/<repo>/...` path),
except the one free rate-limit read — so a token that is only scoped to this repository is
sufficient; SVRF never asks it to reach any other repository.

## Where the token goes

The `gh` CLI itself reads `GH_TOKEN` (or `GITHUB_TOKEN`) from the environment; SVRF does
not read or store the token itself.

- **Docker**: pass it as an environment variable on `docker run` or in your compose file:
  ```sh
  docker run -d --name svrf -e GH_TOKEN -v "$PWD/svrf.toml:/config/svrf.toml:ro" -v svrf-state:/var/lib/svrf svrf
  ```
  `docker-compose.example.yml` reads it the same way from the host's `GH_TOKEN`. The
  container's entrypoint (`docker/entrypoint.sh`) runs `gh auth setup-git` once at
  startup, which lets git itself push over HTTPS using the same token, then execs the
  `svrf` command.
- **systemd**: export `GH_TOKEN` for the service, for example in
  `~/.config/environment.d/svrf.conf` (`GH_TOKEN=...`), or add an `Environment=` line to
  `systemd/svrf.service` (prefer an `EnvironmentFile=` pointing at a file outside version
  control over a literal token in the unit file). `systemd/install.sh` installs the unit
  files as given; it does not manage the token.
- **Running `svrf` directly**: export `GH_TOKEN` in your shell, or run `gh auth login`
  once so `gh` has its own stored credential and no environment variable is needed.

Keep the token out of `svrf.toml` itself — the config file has no field for it, on
purpose, so it doesn't end up in receipts or logs.

## Branch protection interplay

SVRF merges a pull request with a REST call pinned to a specific commit sha
(`PUT /repos/{repo}/pulls/{number}/merge` with `sha=<head sha>`, in `RealGitHub.merge`):
GitHub refuses the merge outright if the head has moved since SVRF read it, which is what
keeps a merge from landing a tree nobody gated.

If the target repository's branch protection requires checks to pass before a
merge, the requirement applies to this call exactly as it would to a merge
button click or any other API merge. SVRF publishes an optional `svrf` commit status but does not publish the independently
required CI check names (see "Commit statuses" above), so one of the following must be true or
every merge attempt will be rejected by branch protection:

- Required status checks are turned off for the merge path SVRF uses (or the repository
  simply has none), and any other review requirements have been satisfied by the pull
  request already; or
- SVRF's gate is separately wired to publish the required check names (for example, a
  small script or CI job that runs the same gate commands and reports a check on the pull
  request) so the required checks are green independently of SVRF's own gate; or
- the token's account is granted the "bypass branch protection" allowance some
  organizations offer for automation, if your policy permits it.

Required reviews (as opposed to status checks) are outside SVRF's control entirely: a
pull request that branch protection holds for review is simply never mergeable, so SVRF's
merge call fails and, depending on which failure `classify_gh_failure` sees, either
retries the read or is left for its next admission pass.

## Which API calls are used, and when

All calls are in `src/svrf/github.py`; every call other than the rate-limit read names the
repository explicitly.

| Call | `gh` invocation | When |
| --- | --- | --- |
| `rate_limit()` | `gh api rate_limit` | Once at the start of every round (free; the only call that names no repository), and again if a rate-limited call didn't report its own reset time. |
| `snapshot()` | `gh pr list --repo <repo> --json ...` | Once per round: the one GraphQL list of open pull requests everything else in the round is read from. |
| `pull(number)` | `gh api repos/<repo>/pulls/<number>` | When a candidate pull request needs a fresh read beyond the snapshot (its `mergeable`/`state`/`draft` fields), and while gating, to confirm a head hasn't moved. |
| `pulls_with_head(branch)` | `gh api repos/<repo>/pulls?state=all&head=<owner>:<branch>` | When a pull request is stacked on another branch, to find that branch's own pull request and see whether it merged, is still open, or closed unmerged. |
| `retarget(number, base)` | `PATCH repos/<repo>/pulls/<number>` (`base=<base>`) | Once a stacked pull request's parent has merged: retarget it onto the base the parent used to point at. |
| `ready(number)` | `gh pr ready <number> --repo <repo>` | Marking a pull request ready, where used by the admission/repair flow. |
| `merge(number, sha)` | `PUT repos/<repo>/pulls/<number>/merge` (`merge_method=merge`, `sha=<sha>`) | Landing each pull request in a green family, one merge commit at a time, pinned to the exact sha the gate saw. |
| `comment(number, body)` | `POST repos/<repo>/issues/<number>/comments` | After a round, on each merged pull request, if `train.comment` is on. |
| `close(number)` | `PATCH repos/<repo>/pulls/<number>` (`state=closed`) | When an already-merged pull request's branch can't be fast-forwarded to record it merged, so it is closed with an explanatory comment instead. |
| `open_pr(head, base, title, body)` | `POST repos/<repo>/pulls` | Opening the superseding pull request for a re-landed (reordered) history. |

## Rate-limit behaviour

Before every round, SVRF reads `rate_limit` (free) and checks both the `graphql` and
`core` remaining budgets against `train.rate_floor` (default 200). If either is below the
floor, the round ends immediately as `RATE_FLOOR` without touching held memory, and the
train waits until GitHub's own reported reset time before trying again.

If a call fails with GitHub's rate-limit or abuse-detection response mid-round
(`RateLimited` in `src/svrf/errors.py`, matched on "rate limit", "secondary rate", "abuse
detection" or an HTTP 429), SVRF waits until the reset time that error reported, or, if
none was given, re-reads `rate_limit` for the latest reset and waits until that instead
(falling back to a five-minute wait if even that can't be read). A rate limit is a read
failure, never a verdict: it changes no held pull request's memory and is retried, exactly
like a network error or a gate machine falling over (`read-failure-never-holds` in
[proofs/README.md](../proofs/README.md)).
