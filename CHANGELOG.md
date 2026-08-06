# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[0.2.0]: https://github.com/<your-account>/localmem/releases/tag/v0.2.0
[0.1.0]: https://github.com/<your-account>/localmem/releases/tag/v0.1.0
