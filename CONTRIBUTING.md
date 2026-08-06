# Contributing

Thanks for looking. This is a small, deliberately opinionated project; the notes below are
about *where* things land and *what evidence* a change is expected to carry.

## Branches

| Branch | What it is |
|---|---|
| `develop` | the default branch, and where every contribution goes |
| `main` | releases only; pull requests opened by the repository owner |

Open your pull request against **`develop`**. It is the default branch, so that is what
GitHub selects unless you change it. A pull request opened against `main` by anyone other
than the owner is closed automatically by `.github/workflows/guard-main.yml` with a
one-line retarget command — that is a routing rule, not a judgement about the change.

You do not need write access. Fork, branch, and open a pull request:

```bash
gh repo fork dangchison/localmem --clone
git switch -c my-change
# …
gh pr create --base develop
```

Issues are always welcome, and for anything larger than a bug fix an issue first will
save you work.

## The four checks this project runs on itself

```bash
python -m pytest -q
mypy localmem
ruff check localmem tests
ruff format --check localmem tests
```

`tests/e2e.sh` is the fifth: it builds a wheel, installs it into a throwaway virtualenv
and drives the CLI and the MCP server end to end. It redirects `HOME` and `LOCALMEM_DB`
into a sandbox and asserts afterwards that your real `~/.localmem` and agent configs are
byte-for-byte unchanged.

**Clear `__pycache__` before any before/after comparison.** Stale bytecode has produced a
wrong conclusion in this repository before — a constant was edited and restored, and the
imported module kept serving the old value long enough to make two passing tests look like
a pre-existing failure:

```bash
find . -name __pycache__ -type d -not -path "./.venv/*" -exec rm -rf {} +
```

## Changes to retrieval need a measurement, not an argument

Every constant in `localmem/retriever.py` and `localmem/dedup.py` is a recorded
measurement, and `docs/design_decisions.md` records how each one was arrived at —
including the ones that were measured and **rejected**.

If your change touches ranking, run the harness before and after:

```bash
localmem eval            # a table
localmem eval --json     # one object
```

`tests/fixtures/eval/baseline.json` pins the current numbers, and the test asserting it
fails on **any** movement — up as well as down, because an unexplained improvement is as
much a hole in the record as a regression. When you have decided the movement is correct:

```bash
LOCALMEM_UPDATE_BASELINE=1 pytest tests/test_evaluate.py
```

and put the diff in your pull request description, with the before/after table. Two things
the project has learned to ask for:

- **watch the `off-corpus silent` column, not only recall.** It counts queries whose answer
  is genuinely not stored and which correctly returned nothing. A retrieval change that
  lifts recall by getting noisier shows up there and nowhere else;
- **one query moving is not a result.** The fixture has 45 graded queries; a single one
  changing places is inside the noise. See `docs/design_decisions.md` §54, where exactly
  that was refused, and §58, where the same change was accepted once the corpus was large
  enough to tell.

A negative result is a welcome contribution. Several of the entries in
`docs/design_decisions.md` exist so that nobody repeats work that has already been measured
and found not to help.
