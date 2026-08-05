# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
  that does work: `localmem add --kind core`. The CLI is unchanged. `PLAN.md` §4's payload
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
  the payloads frozen at `PLAN.md` §4. Recall on an empty database returns a friendly message
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
