# A tree provider for the family merges

Every commit SVRF lands is built from one merge step: a pull request's head merged with
the base (or with the fold of the earlier pull requests in its family), union-merge paths
resolved, committed with plumbing. Git builds those trees. If you have another merge
engine you want to run on real traffic, `[merge] tree_command` lets the train ask it for
each step's tree and cross-check the answer against git's, without ever trusting it.

```toml
[merge]
tree_command = "my-merge-engine --print-tree"   # "" (the default): git alone
tree_timeout_seconds = 120                      # per step; a timeout falls back to git
```

Unset, nothing changes: no command runs and the receipts are exactly as before.

## What the command is given

For each merge step the train builds — while folding a family for its gate, and again
while preparing each pull request's branch for its merge — the command is run with
`bash -c` in the train's clone, with:

| Variable | Value |
| --- | --- |
| `SVRF_MERGE_OURS` | the pull request's head (git's "ours") |
| `SVRF_MERGE_THEIRS` | the base, or the fold so far (git's "theirs") |
| `SVRF_MERGE_BASES` | their merge bases (`git merge-base --all`), space separated |
| `SVRF_CLONE` | the clone; also the working directory |

It prints the tree id of the merge on the first line of its output and exits 0. Only the
id is read: a step that uses the provider's answer commits the tree git itself wrote,
which is the same tree.

Steps git does not merge into a new commit are not offered: a head already in the base,
a base already in the head, a conflict, or a failed read. The pairwise conflict read, the
admission check, repairs and re-lands never run the command.

## How the answer is used

Git's own merge tree is always computed, exactly as without a provider. Then:

| The command | The step uses | Recorded |
| --- | --- | --- |
| printed git's tree | that tree | `tree_source: "provider"` |
| printed a different tree | git's tree | `tree_source: "git"`, `tree_provider: "PROVIDER_MISMATCH"`, `proposed_tree` |
| exited non-zero | git's tree | `PROVIDER_EXIT:<code>:<last stderr line>` |
| ran past `tree_timeout_seconds` | git's tree | `PROVIDER_TIMEOUT` (its whole process group is killed) |
| printed no tree id | git's tree | `PROVIDER_OUTPUT_INVALID` |
| could not be started, or the merge bases could not be read | git's tree | `PROVIDER_UNAVAILABLE:<error>`, `MERGE_BASES_UNREAD` |

The provider can never put a tree on the base that git did not produce, and nothing it
does can hold a pull request, fail a gate or stop a landing. Every landed tree is still
compared with the gated tree after its merge, as always.

## Where it shows up

Each step of a family in the round receipt, and each merge row, carries `tree_source`
(and the reason when git's tree was used). The receipt has the totals:

```json
"tree_provider": {"consulted": 4, "provider": 3, "git": 1, "reasons": {"PROVIDER_MISMATCH": 1}}
```

and `svrf run --once` prints the same totals for the round (also kept in the state's
`last_tick`). A reason's counted part is its first two fields, so exit codes are counted
separately and stderr text is not.

The command runs once per merge step: twice per landed pull request (once in the gated
fold, once when its branch is prepared), plus once per step of a replanned bisection
half. Keep it well inside the timeout; the gate and the landing wait for it.
