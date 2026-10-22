# Contributing to SVRF

Thanks for looking at SVRF. This is a small project; the bar for a good contribution is
mostly "does it come with a test that fails before the change and passes after."

## Dev setup

You need Python 3.11+, `git`, and the [`gh` CLI](https://cli.github.com/) (for the parts
that talk to GitHub; most of the test suite does not need a real token). Proofs need
[elan](https://github.com/leanprover/elan); the toolchain is pinned in
`proofs/lean-toolchain`, so `elan` picks the right Lean version automatically the first
time you build them.

```sh
git clone https://github.com/nybarius/SVRF.git
cd SVRF
pip install -e .
make check    # Python tests (denylist scan + demo included) and the Lean proofs
```

`make check` is everything CI runs. There are no runtime Python dependencies beyond the
standard library, so there is nothing else to install for the Python side.

Individual pieces, if you want to run less than everything:

```sh
make test     # just the Python suite
make proofs   # just the Lean build and axiom check
make demo     # just the local demo (no GitHub, no network)
```

## Before you open a pull request

- **Write the test first.** For any change in behavior — a new rule, a fixed bug, a new
  configuration key — add or change a test that fails against the old code and passes
  against your change, then implement the change. A pull request that changes
  `src/svrf/` without a corresponding test in `tests/` will usually be asked to add one.
  Refactors that do not change behavior (renames, extracted helpers) do not need a new
  test, but should not change what any existing test asserts.
- **Keep `src/svrf/rules.py` and `proofs/` honest.** If your change touches one of the
  decision rules listed in `RULES` in `src/svrf/rules.py`, check whether the Lean
  statement it names in `proofs/` still describes what the code does; `tests/test_proof_map.py`
  checks that every rule names code and a Lean statement that actually exist, not that the
  two still agree in meaning — that part is a human judgment call.
- **Run `make check` locally** before opening the pull request. CI runs the same three
  jobs (Python tests, Lean proofs, container build) from `.github/workflows/ci.yml`.
- **Keep current files independent.** `tests/test_denylist.py` scans current file contents
  and paths for a short list of terms from the private project this code was originally
  extracted from. Reword any copied internal text in plain engineering vocabulary.
  Admission checks the current source and package. Historical commit messages and author
  identities remain audit metadata and do not invalidate a verified current tree.
- **Keep the README's claims supportable.** The "compared with other merge queues" table
  and the benchmark numbers should only ever say what is documented and reproducible
  (see `docs/BENCHMARK.md`); if you improve or re-run the benchmark, update the numbers
  and the date together.

## Sign-off

No DCO is required. SVRF is distributed under the PolyForm Shield License 1.0.0 and also
under commercial terms (see `COMMERCIAL.md`). By opening a pull request you license your
contribution to the maintainer under the Apache License 2.0, so it can be distributed under
both the project's license and its commercial terms, and you confirm you have the right to
do so.

## Reporting a bug or asking a question

Use the issue templates under `.github/ISSUE_TEMPLATE/`. For a security issue, see
`SECURITY.md` instead of opening a public issue.

## Code style

There is no linter configured yet. Match the surrounding file: type hints on public
functions, dataclasses for structured results, small pure functions in `src/svrf/rules.py`
kept separate from anything that touches git, GitHub, or the filesystem.
