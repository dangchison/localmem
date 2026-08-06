# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.4.0] — 2026-08-06

The release where the memory store stops accumulating and starts **learning**.

Until now a wrong diagnosis written in month 1 competed on equal footing with the correction
written in month 6. `superseded_by` had been a column with no logic behind it since M1, where
the comment promised "logic lands in v0.2". It lands here, together with the two things that
make a correction expressible in the first place: a `lesson` kind, and a way to reclassify a
memory after the fact.

Nothing on the frozen MCP surface changed shape. `memory_add` takes two new *inputs*; both
payloads still carry exactly the key sets the original spec §4 froze, and the correction
attached to a retracted memory travels in `neighbors`, which was always there. The command
list is still fifteen.

### Added

- **`kind='lesson'`, a first-class kind on both surfaces.** A note is what you were *told*; a
  lesson is what the project *taught you the hard way*. Writable by an agent over MCP —
  deliberately, because the agent is the party that just watched a diagnosis be wrong — and by
  `localmem add --kind lesson`. It carries no ranking authority: a lesson is *pulled* by a
  recall like any other row, unlike `core`, which is *pushed* into every session and therefore
  stays human-only. Its content shape (`symptom — real cause — fix`) is taught by
  `memory_add`'s own tool description, at the moment a model is composing the call.
- **`localmem promote ID`**, which rewrites a memory's `kind` in place, **by id**. Re-adding
  the same text with a different `--kind` never worked and never will — tier-1 merges on the
  content hash and keeps the original kind — so promotion had to be addressed by id. Idempotent,
  and it warns on stderr when promoting into `core` pushes that tier past its ~400-token cap.
- **The supersede lifecycle: `--supersedes ID` on `localmem add` (repeatable) and
  `memory_add(..., supersedes=[…])` over MCP.** The agent declares that this memory corrects
  those ones, at write time. This is the ADD/UPDATE decision Mem0 spends a model call on,
  collapsed onto the party that was already running — which is what keeps recall free of any
  model. The link is written in the same `BEGIN IMMEDIATE` as the insert, so a database is
  never left holding the correction without the retraction, and an unknown id fails the whole
  call rather than storing a memory whose retraction quietly did nothing.
- **A superseded memory is demoted, never hidden.** Its score is multiplied by
  `SUPERSEDED_SCORE_PENALTY` (0.1) and, when its correction is in the same result set, capped
  below it — so **whenever both are found, the correction is read first**. The retracted memory
  stays in `search`, `stats` and `audit`, because "what did we think was wrong before?" is a
  question worth answering.
- **A superseded hit carries its correction with it**, as its **first** neighbour. An agent
  that recalls "we thought it was a memory leak" reads "actually the connection pool was
  exhausted" in the same response, with no second call and no API change — `neighbors` was
  already one of the eight frozen result keys.
- **A sixth `audit` section: superseded memories**, each printed with the memory that replaced
  it and which workspace that lives in. Read-only like the rest of the report; `--json` carries
  the same numbers under a new `superseded` key.

### Changed

- **`POINTER_SNIPPET` and `ADD_DESCRIPTION` split by responsibility.** Two sentences — the
  keyword rule and the lesson shape — were being paid for twice by every MCP session. The tool
  description now owns *how to form the call* and the snippet owns *when to reach for memory
  and where it is routed*. Measured: the snippet went ~133 → ~108 tokens and `benchmark`'s
  fixed `after_tokens` went **247 → 222**, with strictly more routing policy than v0.3.0
  carried. Milestone C added nothing to that number.
- **Core memory now excludes superseded rows.** It is the one *push* tier — loaded into every
  recall in the workspace — so a retracted convention must stop being pushed the moment its
  correction is written. Everywhere else a superseded row is merely ranked down.
- **`dedupe --merge` moves supersede links onto the surviving row** before deleting the older
  twin. Measured rather than assumed: `superseded_by` is `REFERENCES memories(id)` with no
  `ON DELETE` clause and foreign keys are on, so deleting a row that another names as its
  replacement fails outright with `FOREIGN KEY constraint failed`. Repointing is also the
  better answer than nulling — the pair was judged to be the same memory, so the twin that
  survives is the same correction.
- **`audit`'s promotion note names `localmem promote ID`**, now that the command it used to
  say did not exist does.

### Fixed

- **The supersede demotion does what it claims.** The multiply alone did not reorder the pair
  it exists for: `_min_max` maps the weakest candidate of a view to exactly 0.0, so a
  correction that is the weaker lexical match keeps nothing but its boosts while the retraction
  keeps a tenth of a much larger number. Measured on that pair, at 0.1 the retraction won both
  the "both old" and the "retraction old, correction new" case; no constant fixes it, because
  once recency has decayed out of both rows the correction sits at 0.0 and every positive
  factor leaves the retraction above it. The cap closes it as a guarantee rather than a
  tendency. See `docs/design_decisions.md` §43.

### Notes

- **Schema stays at version 3.** This release needed no migration: the supersede column has
  been there since M1, and the one delete path is handled in code.
- **Supersede links are local to one database.** `export` carries `superseded_by` for
  provenance but `restore` deliberately does not apply it, exactly as when the column was
  reserved — it holds a row id, and ids are reassigned on restore. Both memories survive a
  round trip; the link between them does not, and is re-declared with `--supersedes`.
- **A `global` memory may supersede a repo-local one; the reverse is refused.** The rule is
  exactly what a recall can see: a named workspace reads itself and `global`, so the
  replacement is always reachable from the retracted row's workspace. One repo cannot retract
  knowledge other repos depend on and cannot see.
- **A database with no superseded rows anywhere ranks identically to v0.3.0 + milestone B** —
  asserted by a test whose expected ids and scores were produced by running the same corpus
  against the previous commit in a detached worktree, not by recording what the new code prints.

## [0.3.0] — 2026-08-06

The release that fixes the product's most-felt weakness, and the first one whose headline
number moved the *wrong* way on purpose.

The weakness, finally measured instead of described: over 14 realistic query/memory pairs that
share no tokens — half Vietnamese, some crossing languages — v0.2.2 returned **nothing at all
for 13 of them**. Not a bad ranking: an empty result. `build_match_expression` is conjunctive,
so `"xử" "lý" "upload" "ảnh"` demands all four tokens and one unmatched word zeroes the query.

An embedding view was prototyped for this and **rejected on measurement** — no similarity
threshold separated signal from noise, at a cost of a 1 GB model. The measured winner is
agent-supplied keywords indexed alongside content, with a disjunctive retry behind them:
**11 of 14 correct in the top 3**. Keywords are the lever; OR alone gets 5 of 14.

### Added

- **`keywords`: alternative wordings, supplied by the agent, indexed as a second FTS5 column.**
  `memory_add(..., keywords=[...])` on the MCP surface and `localmem add -K 413 -K "tải lên"`
  on the CLI (`-K`/`--keyword`, repeatable). This is what lets a recall find a memory whose
  content shares no token with the query — a note about `client_max_body_size` answering
  "413 khi upload".
- **A disjunctive fallback**, run **only** when the lexical *and* relational views both come
  back empty. Its results carry `from_fallback` and print as
  `[weak: no exact match, any-word fallback]`.
- **`localmem search --context-fallback`**, to opt those weak hits back into the compact hook
  output that now drops them by default.
- **Schema version 3.** A forward-only migration adds `memories.keywords`, rebuilds
  `memories_fts` with both columns (FTS5 has no `ALTER` for a virtual table) and recreates all
  three triggers carrying the extra column. Measured at **under 10 ms for 5,000 rows**, paid
  once, on the first open after upgrading.

### Changed

- **bm25 now weights the two columns 1.0 / 0.35.** The weight is measured, not chosen: bm25
  rewards a hit in a short field, and a keyword list is the shortest field in the table, so an
  unweighted second column systematically out-ranks genuine content matches. Sweeping 0.2 → 1.0
  over a bilingual fixture set gives a safe band of **[0.25, 0.5]** — below it a keyword-only
  target falls out of the top 3, at 0.6 and above a *one-word* keyword list starts beating a
  paragraph that is genuinely about the term. The weights are **bound parameters**, so the
  ranking rule is defined once and no module composes a query string to carry it.
- **A duplicate merge now unions keywords** into the stored row. This is the only route by
  which an already-stored memory ever gains them.
- **The pointer snippet teaches keywords**, growing from ~97 to **~122** estimated tokens, and
  `memory_add`'s description from ~35 to ~60. `POINTER_SNIPPET_TOKEN_BUDGET` rises from 100 to
  125. All seven documents that paste the snippet are updated; a test still pins them to the
  constant.
- **`benchmark`'s worked example is now a net loss, and it is printed rather than hidden.**
  localmem's fixed overhead went from ~167 to **~218** estimated tokens, so the two fixtures in
  `tests/fixtures/` report **−21.8%** where v0.2.2 reported +6.7%. Break-even moves with it.
  The trade was deliberate and is argued in the README: a memory that cannot be found is worth
  less than the tokens it saves. The real-`CLAUDE.md` example still reports **57.2%** saved.

### Fixed

- **`restore` would have silently dropped `keywords`.** `export` is `SELECT *` and picks up a
  new column for free; `_RESTORE_SQL` is an explicit column list and does not. The existing
  round-trip test could not have caught it — it derives its expected columns from
  `PRAGMA table_info` at runtime, so it passes for any new column whether or not restore
  actually carries it. The new test pins keyword **values** through export → restore, and was
  confirmed to fail against the unfixed statement.

### Notes

- **The zero-token claim is now stated precisely rather than loosely.** The **recall** path
  remains 100% LLM-free — no model, no network, no embedding. Keywords are generated by the
  agent at **write** time and cost roughly 20-40 output tokens once per memory. Both READMEs
  say so plainly.
- **There is no keyword backfill and there cannot be one**: generating keywords needs a model
  and localmem calls none. Existing rows keep `keywords` NULL and **rank exactly as they did
  in v0.2.2** — proven by a test that builds the same corpus on the v2 single-column index and
  on the v3 two-column one and requires identical ids *and* identical scores.
- Tier-2 dedup candidate generation widens as a side effect (`_CANDIDATE_SQL` matches
  `memories_fts`, which now covers keywords) while the Jaccard gate stays content-only.
  Recorded in `docs/design_decisions.md` §36.

## [0.2.2] — 2026-08-06

A documentation release. **Not one line of `localmem/` changed except `__version__`** — no
schema change, no migration, no new command or flag, no dependency change, and the MCP
contract is untouched. What changed is that the documentation now describes the software that
actually exists, and that its first command runs.

### Fixed

- **The first install command in the README did not work.** Its `git clone` URL still had the
  account name as an unreplaced placeholder, which had survived publication. The real URL is
  `https://github.com/dangchison/localmem.git`; the placeholder appeared twice in `README.md`
  and in both release links at the bottom of this file, and all four are corrected.
- **The README's status line said v0.2.0** while the package was 0.2.1.
- **The README described the `mcp` dependency as `mcp>=2.0`**; `pyproject.toml` has pinned
  `mcp>=2.0,<3` since v0.2.1. The `[dev]` extra also omitted `pytest-cov`.
- **This file had no link definition for `[0.2.1]`.** It has one now.

### Added

- **Two `uv` install paths, both executed against the real repository URL before being
  written down.** `uv tool install git+https://github.com/dangchison/localmem.git` puts a
  `localmem` executable in `~/.local/bin` with no virtualenv to manage;
  `uvx --from git+… localmem --version` runs it without installing. **`uvx` must not be used
  in an agent's MCP config** — it re-resolves the git URL on every launch — and the README
  says so where a reader would otherwise reach for it. The question this answers is "is there
  an `npx` one-liner?", and the answer, stated plainly: `npx` is Node's runner, localmem is a
  Python package, `uv`/`uvx` is the equivalent.
- **Per-agent registration is now in the README itself**, one collapsible block per agent:
  install command, config path, the config written verbatim, how to check the agent really
  took it, and how to remove it. Previously this was a four-row table pointing at
  `examples/`.
- **A `PATH` warning at the head of that section.** Every agent config registers
  `{"command": "localmem", "args": ["serve"]}` — the bare name, resolved against the *agent's*
  `PATH`, which is frequently not the shell's. This is the most likely reason a registration
  appears to do nothing, and the README now says so and gives the absolute-path escape.
- **`README_VI.md`** — a Vietnamese guide complete enough to use the product from, not a
  translation of the English file: installation, quickstart, per-agent registration, the
  pointer snippet, the command table, environment variables, the cross-repo `global` tier,
  both hooks, backup, security, four Vietnamese-specific notes and a condensed limitations
  list, with pointers back to `README.md` for benchmark methodology, the full 19 limitations,
  roadmap and citation. The snippet inside it is `POINTER_SNIPPET` byte-for-byte: the
  drift test that already guarded six documents now guards seven, and the file is the reason
  it says seven.
- **An "Environment variables" section.** There are exactly two.
  `LOCALMEM_DB` set to an empty or whitespace-only value is an **error**, not a fall-back to
  the default. `LOCALMEM_NO_TRACKING` tests for **emptiness, not truthiness** — so
  `LOCALMEM_NO_TRACKING=0` disables tracking too, which is the trap worth printing.
- **An "Upgrading from v0.1" section.** A schema-1 database migrates itself to schema 2 in
  place on first open by a v0.2+ build, with no downgrade — so `localmem export` first if that
  matters, and expect `recalled_count` to read as zero for every pre-existing row.
- **The flags that existed but were documented nowhere.** `init --import-all` appeared in no
  file in the repository; `init --yes`, `init -w`, `benchmark [PATHS…]` and `localmem
  --version` were missing from the README. Defaults are now stated where they were assumed:
  `search -k` is 5, `gc --days` is 30.
- **`kind='imported'`.** Users saw it in `stats` and `audit` output without the README ever
  introducing it. It is produced only by `localmem import`, is not writable through MCP, and
  is retrieved exactly like a `note`.
- **The `memory_add` / `memory_recall` asymmetry about `workspace: "all"`.** Recall accepts it
  and the pointer snippet actively teaches it; `memory_add` rejects it, because a memory
  stored in a workspace named `all` would be unreachable by every ordinary recall. That was
  undocumented.
- **A "Permission-granular access" section in the README**, promoted from `antigravity.md`
  where it had been sitting as if it were Antigravity-specific. All four walkthroughs point at
  it.
- **The capture hook's real limits.** The README now states the 100,000-character cap and the
  `…[truncated by capture hook]` marker, names the two scripts that actually live in the repo
  — `examples/localmem-capture.sh` and `examples/localmem-auto-recall.sh` — and says both
  require `jq`, which localmem does not.
- **`[project.urls]` in `pyproject.toml`** — Homepage, Repository and Changelog. The package
  metadata previously pointed nowhere.

### Changed

- **The four per-agent walkthroughs are levelled up.** All four now carry an "Automatic
  capture and recall" section — previously **none of them mentioned either hook** — a line
  about the shared `global` tier, and a `PATH` diagnosis. `codex.md` gains the
  workspace-per-call explanation the other files already had.
- **Verification in the walkthroughs is now about the agent, not the file.** `codex.md` uses
  `codex mcp get localmem` and `codex mcp list`, Codex's own reader for its own config, which
  reports the entry as Codex parsed it. For Antigravity and Kiro **no in-agent verification
  command could be established, so none is claimed**: those two documents keep the
  `python3 -m json.tool` file check, add "restart and ask the agent to use `memory_recall` —
  if it calls the tool, registration worked", and label that second step explicitly as an
  indirect check. Claude Code's `/mcp` is unchanged and remains the only direct readout of the
  four.
- **The `## Tiếng Việt` section is gone from `README.md`**, replaced by a link to
  `README_VI.md` on the second line. It was 83 lines of a 634-line file and was missing agent
  setup, the pointer snippet, the command table, benchmark, migration, security, limitations
  and the hooks. All of that is in the new file.
- **The snippet-drift test covers seven documents**, up from six.

### Notes

- Tests: 554 passing, unchanged in count — the drift test gained a file, not a case. `ruff`,
  `mypy --strict` and `tests/e2e.sh` are clean.
- Both `uv` install paths in the README were run against `github.com/dangchison/localmem.git`
  before release, and the from-source path was exercised in a fresh clone into a temporary
  directory. The README's note that a stock macOS `python3` (3.9) fails with
  `editable mode currently requires a setuptools-based build` rather than a version error is
  an observation from that run, not a guess.

## [0.2.1] — 2026-08-05

Quick wins against five of the six weaknesses v0.2.0 shipped with. No new command, no schema
change, no new dependency — the MCP contract, the schema and the migration count are untouched.

### Added

- **`localmem search QUERY --context`** — compact output built for a prompt hook. **No hits
  prints nothing at all and exits 0**, because a hook runs on every prompt and the friendly
  "no memories matching…" line would become permanent noise. Hits print one header line and one
  line per memory, `- (workspace) content`, collapsed onto a single line and truncated at 400
  characters with `… (memory_recall id N for full text)`. Core memory is deliberately **not**
  included: it is charged once per session through an ordinary recall, not once per prompt.
  CLI-only — `mcp_server.py` is byte-identical to v0.2.0. See `docs/design_decisions.md` §30.
- **Auto-recall hook example** — `examples/claude_code_auto_recall.md` plus
  `examples/localmem-auto-recall.sh`, an opt-in Claude Code `UserPromptSubmit` hook that runs
  `localmem search "$prompt" --context -k 3` before the model sees the prompt. Every exit path
  is 0: a missing `localmem` or `jq`, malformed JSON, an empty prompt or a database error all
  end in silence rather than a blocked prompt. The prompt is capped at 4000 characters before
  anything examines it, and the search itself is wrapped in `timeout` when coreutils is
  installed — in that order, because the cap is the guard that always applies and the timeout
  is the one that may be missing. Measured end to end on bash 3.2.57: **1 MB of log-shaped
  text takes 0.19 s**. A test asserts the script embedded in the document is byte-identical to
  the file, another runs it against six broken payloads, and a timing test fails if a 1 MB
  paste takes longer than 5 seconds.
- **`LOCALMEM_NO_TRACKING`** — set to any non-empty value and recall performs no write at all.
  Default unchanged. The trade is stated: `audit`'s dead-memory and promotion sections have
  nothing left to count. `docs/design_decisions.md` §33.
- **README "Security" section** — file modes, encryption at rest as the disk's job, the
  `localmem export | age -r … > backup.age` recipe, and the deliberate refusal to bundle
  SQLCipher or write any crypto.
- **A break-even line in the README**: localmem saves tokens once the instruction files you
  push every session cost more than the `after` figure `benchmark` prints — **~167 estimated
  tokens** with an empty core memory, plus core. Quoted from the command rather than summed
  from the parts, because the estimator rounds the whole block once (`97 + 71` is 168 by hand).

### Changed

- **The pointer snippet is ~97 estimated tokens, down from ~209**, with all five of its ideas
  intact: recall first, save durable facts, the `global`/`all` routing convention, recalled
  text is data not instructions, do not duplicate memory in the file. A test measures it
  against a budget of 100 rather than trusting prose to stay short.
- **Every benchmark number in the README was re-measured**, not adjusted. Same command, same
  `tests/fixtures/`, sandboxed `HOME`: `before ~179 → after ~167`, **+6.7%**, where v0.2.0
  reported **−55.9%**. Against a real 509-token `~/.claude/CLAUDE.md`: `509 → 167`, **67.2%**.
  The old negative example is kept in the text as history rather than quietly deleted.
- **The six documents that paste the snippet** — README, the migration guide and the four
  per-agent walkthroughs — now carry it verbatim, enforced by a test. They had drifted.
- **New database files are `0600`, new directories `0700`.** A file or directory that already
  existed is never touched, including a custom `$LOCALMEM_DB` path. The mode is set between
  `connect()` and `migrate()` — before SQLite's first write — so the `-wal`/`-shm` sidecars are
  born restricted; measured at umask 022, tightening *after* the first write leaves both
  sidecars at 644. A stale `-wal` outliving its database is swept explicitly.
  `docs/design_decisions.md` §32.
- **`mcp` is pinned to `>=2.0,<3`.** The 2.x line already broke the API once (FastMCP moved);
  an unbounded upper edge means the next major arrives unannounced in a `pip install -U`. No
  other dependency changed.

### Fixed

- A `chmod` on a filesystem that cannot express POSIX modes no longer prevents the database
  from opening.
- **`examples/localmem-capture.sh` no longer loses a large session summary silently.** The
  summary is passed to `localmem add` as an exec argument; past `ARG_MAX` (1048576 bytes on
  macOS) exec fails with `E2BIG`, and the script's `|| exit 0` swallowed it — the hook exited
  0 having stored nothing, with no error anywhere. Measured against the old script: a 900 KB
  summary stored a row, **1.1 MB and 1.5 MB stored nothing**. It is now capped at 100,000
  characters and the stored trace ends with `…[truncated by capture hook]`, so a cut record
  says so instead of reading as a complete one. A summary that is only whitespace still
  stores nothing — the marker is appended after the blank and length tests, not before.
  Shipped in v0.2.0; found reviewing the v0.2.1 hooks.
- **`examples/localmem-capture.sh` no longer hangs on a whitespace-heavy session summary.**
  Its blank check used `[ -z "${summary//[[:space:]]/}" ]`, which is quadratic in bash 3.2.57
  — the version `/usr/bin/env bash` resolves to on a stock macOS — whenever the text is
  whitespace-heavy, and a summary quoting a log is exactly that. Measured on 3.2.57 against
  the old line: 50 KB took **523 seconds** and 100 KB did not finish. Both hooks now use a
  single linear `case "$var" in *[![:space:]]*)`, pinned by a timing test and by a test that
  reads the scripts for the pattern itself. Shipped in v0.2.0; found reviewing the v0.2.1
  hook, which had inherited it.
- **`search --context` no longer splits a Vietnamese letter at the 400-character cut.** In NFD
  `ế` is `e` + two combining marks; a fixed slice could leave the `e` behind. The cut now backs
  off while the first dropped codepoint is a combining mark.

## [0.2.0] — 2026-08-05

Knowledge that crosses repositories. A shared `global` recall tier, a read-only hygiene report,
backup and transfer, and the security hardening that had to land before any of it.

### Added

- **Shared `global` tier.** A query scoped to a *named* workspace now resolves to
  `workspace IN (?, 'global')` — its own rows **and** the shared tier — across all three views
  (lexical, relational, pure-recency). `None` ("all") and `'global'` itself are unchanged, and
  two named workspaces still cannot see each other. At an equal fused score the current
  workspace's row sorts first; there is no score penalty for being global. Core memory does the
  same in priority order: the workspace's own rows are fitted against the 400-token cap first
  and the shared rows fill the remainder, so a repo never loses a core rule to one it does not
  own. Evidence closure deliberately stays inside each result row's own workspace.
- **`localmem audit`** — memory hygiene in five sections: the tier-2 review queue with the
  commands that drain it, core-memory promotion candidates, distribution per workspace and
  kind, core-memory health against the cap, and dead memories (older than 30 days, never
  recalled). Deterministic, zero-token, `-w` and `--json`, always exits 0, and **writes
  nothing** — two tests snapshot the database file's bytes and mtime and compare afterwards.
- **`localmem export` / `localmem restore`** — the raw `memories` table as one JSON document.
  `entities`/`memory_entities` are derived and rebuilt by backfill on restore; `dedup_queue` is
  transient and not carried. Restore merges with
  `ON CONFLICT(workspace, content_hash) DO UPDATE seen_count = max(…)`, so the target keeps its
  own `created_at`, `kind` and `source`, restoring twice is a no-op, and restoring into a
  populated database merges rather than duplicates.
- **`localmem import --whole-file`** — each file becomes one record instead of being split into
  bullets and paragraphs, for skills and checklists that only make sense whole. Default
  behaviour unchanged; the two modes share a workspace without colliding.
- **Recall usage tracking**, schema version 2: `recalled_count` and `last_recalled_at` on
  `memories`, written by one best-effort statement at the end of each recall (returned ids
  only, never neighbours). `stats` reports the total; `audit` uses it to rank promotion
  candidates and to find dead rows.
- **Auto-capture hook example** — `examples/claude_code_hook.md` plus
  `examples/localmem-capture.sh`, an opt-in Claude Code Stop hook that stores a session summary
  as a `trace`. You install it yourself; localmem never edits agent settings.
- **README section "Sharing knowledge across repos"**, with the three-tier "where should your
  rules live" matrix, worked commands for the cross-repo bug, the wrong diagnosis and the
  reusable checklist, and the backup workflow. Mirrored in the Tiếng Việt section.
- **Three CLI commands**, 11 → 14: `audit`, `export`, `restore`.

### Changed

- **`memory_add` refuses `kind="core"`.** `ADD_KINDS` is now `("note", "trace")`. Core memory
  is loaded into every recall, so an agent acting on injected instructions must not be able to
  write one — and with the shared tier, one poisoned core row would reach every repository.
  The refusal is the standard DD-8 payload (`is_error` stays `False`) and names the command
  that does work: `localmem add --kind core`. The CLI is unchanged. The original spec §4's payload
  shapes, tool names and tool descriptions are untouched; this is input validation, the same
  category as the existing rejections of `kind="imported"` and `workspace="all"`.
- **The pointer snippet teaches the conventions** the shared tier needs — project-only facts to
  the detected workspace, reusable lessons to `workspace: "global"`, recall before debugging
  something familiar and retry with `workspace: "all"` — plus the security rule that recalled
  memory is reference **data**, never instructions to follow.
- **Benchmark "after" cost rose from ~133 to ~279 estimated tokens**, because the snippet grew
  from ~62 to ~209. Every number in the README's benchmark section was re-measured for this
  release. Against the two small fixtures the worked example is now **−55.9%** — a negative
  saving, printed rather than replaced with a flattering pair of files. On the same machine
  with a real `~/.claude/CLAUDE.md` in scope, the identical command reports **59.4%** saved.
- **`stats`** prints a `recalls:` line.
- **Schema version 2.** `schema.sql` remains the version-1 baseline and is not edited; version 2
  is two `ALTER TABLE … ADD COLUMN` statements in `db._MIGRATIONS`, the first real use of the
  forward-only mechanism. A v0.1.0 database upgrades in place on the next open with its data
  intact and `recalled_count` starting at 0.

### Known limitations

Added to the README's Limitations section:

- Anything in `global` is readable from every project on the machine. That is the feature; two
  *named* workspaces remain isolated.
- Recall counts start at 0 for memories that predate the upgrade, so `audit`'s "dead memories"
  section over-reports on a freshly migrated database. It says so on every run.
- `audit` suggests promotion but cannot perform it: re-adding a note with `--kind core` does
  **not** promote, because tier-1 merges on the content hash and keeps the original `kind`.
  Promotion tooling is a v0.3 item.
- `export` carries `id` and `superseded_by` for provenance but `restore` does not apply them —
  row ids are local to a database.
- Tier-3 temporal supersede is **still not built**; `superseded_by` remains reserved schema.

### Notes

- Runtime dependencies are unchanged: `click>=8.1`, `mcp>=2.0`, and `tomli>=2.0` on Python 3.10
  only. Nothing was added.
- Tests: 512 passing (419 in 0.1.0), plus `tests/e2e.sh`.

## [0.1.0] — 2026-08-05

First release. Local-first, zero-token memory for AI coding agents: one SQLite database, raw
traces, structured retrieval, no LLM call anywhere in the memory path.

### Added

- **Core store.** `~/.localmem/memory.db` (override with `LOCALMEM_DB`), schema version 1 with
  forward-only migrations, WAL, `busy_timeout=5000`, foreign keys on. Workspace detection from
  the git repository root name, falling back to the directory name and then to `global`.
  Deduplication is per workspace: `UNIQUE (workspace, content_hash)`.
- **Tier-1 deduplication**, inline on every write. sha256 over content normalized by NFC,
  markdown bullet-prefix removal, lowercasing and whitespace collapsing; an exact match bumps
  `seen_count` and returns `duplicate_merged`.
- **Regex entity indexer.** Seven classes — URL, @-mention, file path, quoted string, CamelCase,
  snake_case, ALL-CAPS acronym — applied in priority order with masking, maintaining `entities`
  and `memory_entities` with normalized occurrence weights. Capped at the first 4,096 characters
  and 50 entities per memory. `localmem backfill` indexes rows stored before indexing existed.
- **Dual-view retriever.** FTS5 bm25 (lexical) fused with an entity co-occurrence view
  (relational), each min-max normalized, weighted 0.6/0.4 and flipped to 0.4/0.6 when the query
  itself matched an entity; plus a 30-day half-life recency decay and a `log(seen_count)` boost.
  Evidence closure attaches up to two supporting neighbours per result. Recency cues in English
  and Vietnamese (`recent`, `last week`, `hôm qua`, `tuần trước`, …) are stripped from the
  lexical query and reweight the recency term; a query that is nothing but cues ranks purely by
  recency.
- **Core memory.** `kind='core'` rows concatenated into an always-load tier per workspace,
  capped at ~400 estimated tokens by dropping whole rows oldest-first — never splitting one.
- **Tier-2 near-duplicate queue.** FTS5 supplies at most 10 candidates from the new memory's
  top 5 non-stopword terms; the gate is normalized Jaccard token overlap ≥ 0.7 alone. Pairs are
  queued for review and **never merged automatically**. `localmem dedupe` lists, reviews,
  merges or keeps both; `localmem gc` prunes resolved rows and reclaims disk space.
- **MCP server** on stdio exposing exactly two tools, `memory_recall` and `memory_add`, with
  the payloads frozen at the original spec §4. Recall on an empty database returns a friendly message
  and is never an error. One SQLite connection per tool call, so no `-wal` sidecar accumulates.
- **Guided `localmem init`** in five steps: create the database, ask about each detected agent
  individually, offer to import instruction files as a separate question, print the pointer
  snippet, run a self-check. Declining every optional step leaves a fully working cold start.
- **Agent config writers** for Claude Code, Codex CLI, Google Antigravity and AWS Kiro. Each
  merges into what already exists, backs the original up to `*.bak`, writes atomically through
  a temp file, and refuses outright on a config it cannot parse — writing nothing, backing up
  nothing, and printing the block for manual addition. `~/.claude.json` is never opened for
  writing.
- **Markdown importer** for `CLAUDE.md`, `AGENTS.md` and Kiro steering files, splitting on
  top-level bullets, paragraphs, fenced code blocks and heading intros, with the enclosing
  heading prepended as context. Re-importing an unchanged file adds no rows. `--dry-run` writes
  nothing, `--select` confirms per file.
- **`localmem benchmark`**, comparing the estimated per-session push cost of instruction files
  against localmem's fixed pull cost (pointer snippet + the two tool descriptions + core
  memory), with a `--json` mode and a printed accuracy caveat. No universal savings figure is
  claimed anywhere.
- **Eleven CLI commands**: `add`, `agents`, `backfill`, `benchmark`, `dedupe`, `gc`, `import`,
  `init`, `search`, `serve`, `stats`. All work headless; prompts appear only on a terminal.
- **Documentation**: `README.md`, `docs/architecture.md`, `docs/design_decisions.md`,
  `docs/migrating_from_instruction_files.md`, and `examples/` — a runnable
  `basic_usage.py` plus a setup walkthrough per agent.
- **Tests**: 419 passing, plus `tests/e2e.sh`, an end-to-end acceptance script that installs
  into a fresh virtualenv with both `HOME` and `LOCALMEM_DB` sandboxed and asserts the real
  `~/.localmem/` and the real agent configs are untouched.

### Known limitations

Recorded in full in the README's Limitations section. The load-bearing ones:

- Retrieval is lexical plus a one-hop entity graph — there is **no semantic matching**.
  Embeddings are a v0.4 item.
- Entity extraction is regex-based and language-naive; it cannot tell an acronym from shouty
  prose. Optional spaCy/underthesea NER is a **v0.3 roadmap item and is not packaged** — there
  is no installable extra for it in this release.
- Single-user and local: no authentication, no multi-user isolation, no encryption at rest.
- **ChatGPT is not supported.** It needs a remote HTTP transport; v1 ships stdio only.
- `đ`/`Đ` is not folded to `d` by FTS5 or by recency-cue matching, so `dung` does not match a
  stored `đúng`, and `gan day` is not recognised as `gần đây` (`gan đay` is).
- **`session_id` is always `NULL` for memories written through MCP.** The frozen `memory_add`
  schema has no such parameter; only `localmem add --session-id` populates the column.
  Session-adjacency evidence closure therefore never fires for agent-written memories.
- The `superseded_by` column is reserved schema with **no logic behind it**; tier-3 temporal
  supersede was scheduled for v0.2 and is still open.
- HTTP/SSE transport exists as a single function parameter for v2's benefit and is **not
  reachable** from the CLI.

### Notes

- Not published to PyPI. Install from a checkout: `git clone … && pip install -e .`
- Requires Python ≥ 3.10. Runtime dependencies: `click>=8.1`, `mcp>=2.0`, and `tomli>=2.0` on
  Python 3.10 only.
- Inspired by *Zero-Mem: Zero-Token Memory Operations for LLM Agents* (arXiv:2607.29377); see
  the README's Citation section. The package is not affiliated with the paper's authors.

[0.3.0]: https://github.com/dangchison/localmem/releases/tag/v0.3.0
[0.2.2]: https://github.com/dangchison/localmem/releases/tag/v0.2.2
[0.2.1]: https://github.com/dangchison/localmem/releases/tag/v0.2.1
[0.2.0]: https://github.com/dangchison/localmem/releases/tag/v0.2.0
[0.1.0]: https://github.com/dangchison/localmem/releases/tag/v0.1.0
