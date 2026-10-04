# What the proofs cover

This directory is a Lean 4 model of the merge train's decision rules: which trees may be
folded together, when a batched landing is sound, when a held pull request is read again,
and when re-landing a history in a different commit order is safe. It is not a proof about
git, and it does not run inside the train. The code checks the one thing the proofs assume
and cannot check on their own: that the tree the base branch actually holds after a merge
is the tree the model says a merge step produces. Every landing compares the observed tree
with the gated tree and refuses to proceed on a mismatch; that comparison is what makes the
model's hypotheses true of the real repository, round after round.

## What is modelled, what is abstracted

- **Git trees are modelled as an opaque type `Tree`** (or, in the commutation lemmas, as a
  function from a `Path` to a `Val`, i.e. a set of file contents keyed by path). The proofs
  never inspect a tree's structure; they only ever ask whether two trees are equal.
- **A merge is modelled as a partial function `step : Tree → Head → Option Tree`**, `none`
  standing for a conflict. That git's merge of a prepared branch behaves like `step` is
  *not* a theorem here — it is the modelling boundary stated in
  `proofs/BraidedTrain/Landing.lean` and `proofs/BraidedTrain/Braid.lean`. The code does
  not trust it either: after every merge it reads the tree the base branch now holds and
  checks it against the tree the model predicted (`checkLanding` / `Replays`). A mismatch
  stops the family instead of being explained away.
- **A union merge on configured paths is modelled line by line** (`unionLines`, `Edit`,
  `change` in `Union.lean`): a union edit appends the pull request's new lines once, a
  replacement sets a path's content. Two changes commute up to the order of union-merged
  lines when every path both edit is a union edit on both sides (`UnionOnlyOverlap`, the
  condition `RealGit.union_step` checks before it resolves a conflict). Exact tree
  equality across fold orders does not hold (`union_order_visible`), which is why the fold
  order is fixed at planning and replayed at landing. Path-disjoint writes (`write`,
  `apply`) commute exactly.
- **A gate is modelled as a predicate, or a function of the tree's content restricted to
  the paths it reads.** The proofs never open the gate command; they only ask whether its
  verdict is a function of what it read (used by the retry and history-reorder results).
- **Held memory and retry are modelled as a key (`RetryKey`: a head sha plus the watched
  content) and a verdict that is a pure function of that key.** The code computes the same
  key (`svrf.rules.retry_due`, `held_retry`) and reuses a held verdict exactly when the key
  has not changed.

## Rule map

Every decision rule the code enforces (`svrf.rules.RULES`) names the Lean theorem that
states it and the function or method that enforces it in the running train.

| Rule | Theorem | Enforced by |
| --- | --- | --- |
| `family-fold` | `fold` | `Train.plan` |
| `gate-green` | `landed_passes` | `Train.gate_family` |
| `tree-identity` | `checkLanding_iff` | `Train.land_family` |
| `outside-move-refused` | `checkLanding_refuses_moved` | `Train.land_family` |
| `bisect-lands-gated` | `segments_land_last_gated` | `Train.settle_red` |
| `prefix-ungated` | `prefix_is_fold` | `Train.land_family` |
| `one-gate-per-merge-commit` | `all_gated_iff_each_prefix` | `Train.land_family` |
| `land` | `landed_eq_gated` | `Train.land_family` |
| `hold` | `landed_passes` | `Train.settle_red` |
| `admit` | `admitted_iff` | `admission_decision` |
| `opt-out` | `admitted_iff` | `admission` |
| `already-merged` | `admitted_iff` | `admission` |
| `held-retry` | `retry_iff` | `retry_due` |
| `held-verdict-reuse` | `held_verdict_stands` | `held_retry` |
| `no-reopen-outside-watched-paths` | `no_reopen_outside_reads` | `held_retry` |
| `read-failure-never-holds` | `read_failure_never_holds` | `admission_decision` |
| `compatible-families` | `strands_comm` | `choose_families` |
| `interleaved-owners` | `interleaved_landing` | `choose_families` |
| `repair-mechanical` | `retry_iff` | `repair_class` |
| `ordered-reland` | `reland_tree` | `reland_class` |
| `union-merge` | `change_comm` | `union_attributes` |
| `speculative-stacking` | `stack_lands_gated` | `Train.round` |
| `speculation-void` | `stackStatus_void_iff` | `Train.round` |
| `bisect-holds-exactly-red` | `settle_outcome` | `Train.settle_red` |
| `bisect-gate-bound` | `bisection_gates_le` | `Train.settle_red` |
| `maximal-family` | `bk_maximal` | `families` |
| `left-out-names-partner` | `chosen_family_maximal` | `choose_families` |
| `maximum-family` | `chosen_family_maximum` | `choose_families` |
| `stacked-retarget` | `retarget_after_parent` | `Daemon.stacked` |

`tests/test_proof_map.py` checks this table against `svrf.rules.RULES` directly: every
rule name and theorem name in the code must also appear in this file, every named theorem
or definition must exist in `proofs/BraidedTrain/*.lean`, and every named check must be a
real callable (a method of `Train` or `Daemon`, or a function in `svrf.rules`). If you add
or rename a rule, update the table above and the code together; the test fails otherwise.

## Files

| File | Contents |
| --- | --- |
| `BraidedTrain/Landing.lean` | `fold`, `Replays`, `landed`, `checkLanding`; landing a family lands the gated tree; admission and read-failure memory. |
| `BraidedTrain/Braid.lean` | The ungated-window invariant (`AllGated`, `mains`); retry as a function of a read key; commutation of disjoint writes (`strands_comm`). |
| `BraidedTrain/Interleaving.lean` | Two owners landing path-disjoint families interleaved still land one tree (`interleaved_landing`); when a gate's own read paths miss the other owner's writes, no joint gate is needed (`two_owners_end_gated`). |
| `BraidedTrain/Reland.lean` | Re-landing a history as tests-then-code-then-docs commits lands the identical tree (`reland_tree`), so any tree-reading gate's verdict is unchanged (`reland_gate`). |
| `BraidedTrain/Union.lean` | The line-level union merge (`unionLines`, the line-set model of git's `merge=union`, which `union_attributes` declares for the union paths): exact associativity (`unionLines_assoc`), commutation up to line order (`unionLines_comm`, sharp by `union_order_visible`), and at the path level `change_comm` / `family_perm` under `UnionOnlyOverlap`, the union repair's hypothesis. |
| `BraidedTrain/Stacking.lean` | Speculative stacking: families gated on the fold of every family before them land gated trees at every family boundary (`stack_lands_gated`); a stacked verdict does not carry past a red family (`stack_fold_through`, `stacked_verdict_does_not_carry`); the round's bookkeeping voids exactly the families above the first red one (`stackStatus_void_iff`, `stackStatus_landed_iff`, `stackStatus_bisected_iff`). |
| `BraidedTrain/Bisection.lean` | Bisection of a red family (`settle`, the model of `Train.settle_red`) terminates (well-founded on family length), holds exactly the bad pull requests and lands the rest under a monotone gate (`settle_outcome`), and costs at most `2·r·⌈log₂ n⌉ + 1` gates including the family's own (`settle_gates_le`, `bisection_gates_le`). |
| `BraidedTrain/Families.lean` | The Bron–Kerbosch recursion of `families`, with arbitrary pivot and iteration order: every family it reports is a maximal compatible set (`bk_maximal`), so the family `choose_families` keeps holds no conflicting pair and every pull request left out conflicts with a kept one (`chosen_family_maximal`). With a pivot drawn from the candidates or excluded nodes (the code's rule, `codePivot_mem`) and an order visiting every node, it reports every maximal compatible set (`bk_complete`), so the first family of the size-sorted list is a maximum compatible set (`chosen_family_maximum`). |
| `BraidedTrain/Retarget.lean` | A retarget changes only a pull request's base ref (`retarget_head`, `retarget_headTree`, `retarget_fold`); after the parent landed by a replayed landing, the base branch holds the parent's tree and the stacked pull request lands exactly what it would have landed on its parent (`retarget_lands_same`, `retarget_after_parent`). |
| `BraidedTrain/Examples.lean` | Concrete instances that exercise the general lemmas against small, fully-written-out cases. |
| `CheckAxioms.lean` | Prints the axioms each main theorem depends on; `make proofs` fails if any line mentions `sorryAx` or `Classical.choice`. |

## How to build

```sh
cd proofs && lake build
```

The toolchain is pinned in `proofs/lean-toolchain` (`leanprover/lean4:v4.33.0`); `elan`
picks it up automatically. `lake build` compiles the whole `BraidedTrain` library.

## Axiom check

```sh
cd proofs && lake env lean CheckAxioms.lean
```

This prints, for every theorem an invariant in the table above depends on, the axioms Lean
used to prove it. `make proofs` runs this and fails the build if any printed line contains
`sorryAx` (an unfinished proof) or `Classical.choice` (a nonconstructive step none of these
proofs need — they are finite-list inductions and well-founded recursion on list length
throughout). `tests/test_proof_map.py`
separately checks the source text of `proofs/BraidedTrain/*.lean` for `sorry` and
`axiom` declarations, so neither can slip back in unnoticed.

## Model boundary

What the proofs above do not cover, and what stands in for it in the running train:

- **Git's merge.** That `git merge-tree` of a prepared branch is the model's `step`
  (`fold`, `unionStep`, `change`) is not a theorem. Every landing reads the tree the base
  branch actually holds and compares it with the gated tree (`checkLanding`), and a
  mismatch stops the train. Git's hunk-level merge of a non-union file edited on both
  sides is read as a conflict by the path-level model.
- **GitHub's API.** That a retarget changes no commit, that a merge pinned to a sha merges
  that sha, and that mergeability reads are current are GitHub's behaviour. The train
  re-reads the head sha before every merge (`HEAD_MOVED`), pins the merge to the prepared
  sha, and reads the landed tree after it; a failed or rate-limited read is retried, never
  counted as a verdict.
- **The gate command.** The proofs treat a gate as a predicate on trees; the bisection
  results assume it is monotone (a set is green exactly when it holds no bad pull
  request). A flaky or order-dependent gate breaks that hypothesis, not the train's
  landing check.
- **Sets and sorting in `families`.** The model iterates lists where the code iterates
  Python sets; the proofs hold for any pivot drawn from `P ∪ X` and any iteration order
  that visits exactly the nodes of its list, which covers every order the code's sets and
  `sorted` can produce. That the code's final sort puts a largest family first is read
  off the sort key (`-len`), not proved.
- **Concurrency and timing.** Parallel gates, rate-limit waits and the receipt file are
  engineering around the model, not part of it.
