# Architecture

How a memory gets in, how it comes back out, and what the database looks like in between.

This document describes **what v0.2.0 does**. `design_decisions.md` is the companion record of
**why** — every deviation from the original spec and every accepted limitation, with its measurement.
Where the two touch, this file links rather than repeats.

## Modules

```
localmem/
├── config.py       resolve $LOCALMEM_DB → path; detect workspace from git/dirname
├── db.py           connection PRAGMAs, BEGIN IMMEDIATE transactions, forward-only migrations
├── schema.sql      the canonical schema, version 1
├── store.py        add_memory(), the FTS5 MATCH sanitizer, stats aggregation, trace prune, forget
├── dedup.py        tier-1 hashing, tier-2 candidate + Jaccard gate, nearest_neighbour, queue review, gc
├── indexer.py      regex entity extraction, entities/memory_entities maintenance, backfill
├── core_memory.py  the always-load tier: kind='core' rows, token-capped, two-tier merge
├── retriever.py    dual-view retrieval, fusion, boosts, evidence closure, recall tracking
├── tokens.py       character-based token estimation (imports nothing from localmem)
├── importer.py     markdown → records (split, or one record per file)
├── benchmark.py    push cost vs. fixed pull cost
├── audit.py        the read-only hygiene report; every statement is a SELECT
├── transfer.py     export/restore of the raw memories table
├── mcp_server.py   the two frozen tools, stdio
├── cli.py          sixteen commands; the only module that calls Path.home()
└── agents/         one config writer per supported agent, over one resolved command path
```

Two structural rules are enforced by tests rather than by convention:

- **Nothing under `agents/` resolves the home directory.** `home` and `cwd` are parameters of
  every function and every writer method. `cli.py` is the only caller of `Path.home()`. A test
  scans the source of all six `agents/` modules for `Path.home()`, `expanduser`, `os.environ`
  and `getenv` and fails if any appears. This makes a test reaching a real config file
  structurally impossible, not merely unlikely.
- **`tokens.py` imports nothing from `localmem`.** Both `core_memory.py` and `benchmark.py`
  depend on one estimator without creating a cycle.

## Data flow

### Write path

`localmem add`, `localmem import`, and the MCP tool `memory_add` all funnel into
`store.add_memory()`, which runs everything below inside **one** `BEGIN IMMEDIATE`
transaction:

```
content
  │
  ├─ 1. tier-1 dedup ─ dedup.content_hash(): NFC-normalize, strip markdown bullet prefixes
  │                    per line, lowercase, collapse whitespace runs, strip, sha256.
  │                    SELECT … WHERE workspace = ? AND content_hash = ?
  │                      hit  → seen_count += 1, updated_at = now, keywords unioned
  │                                                                → "duplicate_merged"
  │                      miss → INSERT (raw content, verbatim)     → continue
  │
  ├─ 1b. keywords ──── store.normalize_keywords(): lowercase, dedupe preserving order,
  │                    cap 20 keywords x 64 chars, stored space-separated. NULL when the
  │                    caller supplied none, which is what keeps such a row ranking
  │                    exactly as it did before schema version 3.
  │
  ├─ 2. FTS5 index ─── the AFTER INSERT trigger mirrors content *and* keywords into
  │                    memories_fts.
  │
  ├─ 3. entity index ─ indexer.index_memory(): seven regex classes in priority order over a
  │                    working copy, each masking class blanking its matches so a lower
  │                    class cannot re-match the same characters. Upserts entities and
  │                    memory_entities with a normalized occurrence weight in (0, 1].
  │
  └─ 4. tier-2 dedup ─ dedup.enqueue_near_duplicates(): FTS5 MATCH over the new content's
                       top 5 non-stopword terms returns ≤ 10 same-workspace candidates;
                       each is scored by Jaccard over normalized token sets and queued into
                       dedup_queue when the score is ≥ 0.7. Never blocks, never deletes.
```

Two consequences worth knowing:

- A merge does **not** re-index. The content is unchanged, so the entity links attached to
  that row are still exactly correct.
- Tier 2's decision is Jaccard ≥ 0.7 **alone**. FTS5 only narrows the candidate field. See
  `design_decisions.md` §8 for the measurements that killed the bm25 half of the gate.

### Read path

`localmem search` and the MCP tool `memory_recall` both call `retriever.retrieve()`.

```
query
  │
  ├─ 1. query profile
  │      • strip_recency_cues(): removes matched cue spans ("recent", "last week",
  │        "tuần trước", …) from the text that reaches view A. Cues left in place would
  │        make the conjunctive FTS5 query demand the literal cue word.
  │      • query_entities(): the same extractor the indexer uses, run over the *full*
  │        original query.
  │      • workspace: the parameter, or detected from cwd; None means every workspace.
  │        A *named* workspace resolves to `workspace IN (?, 'global')` — it reads its own
  │        rows and the shared tier. `'global'` itself and None are unchanged, and two
  │        named workspaces never see each other. See design_decisions.md §24.
  │
  ├─ 2. view A — lexical: bm25 over memories_fts, workspace-filtered, top 20. The two
  │      columns are weighted content 1.0 / keywords 0.35, bound as parameters rather
  │      than formatted into the SQL. See design_decisions.md §34.
  │
  ├─ 3. view B — relational: memories sharing an entity with the query, scored by
  │      SUM(memory_entities.weight), top 20. One hop. No traversal, no PageRank.
  │
  ├─ 3b. fallback: if view A *and* view B both came back empty, view A is re-run with a
  │      disjunctive MATCH ("any token" instead of "every token") and its rows are marked
  │      from_fallback. It is a weaker claim and is labelled as one: `search --context`
  │      drops these entirely. See design_decisions.md §35.
  │
  ├─ 4. fuse: each view min-max normalized into [0, 1] independently, then
  │      0.6·lexical + 0.4·relational — flipped to 0.4/0.6 when view B found anything.
  │      Ties break towards the current workspace; there is no penalty for being global.
  │
  ├─ 5. boosts: + weight·2^(-age_days/30) recency decay, + 0.02·ln(seen_count).
  │      The recency weight is 0.05 normally and 0.25 when the query carried a cue; the
  │      decay curve itself never changes.
  │
  ├─ 5b. supersede: a row with superseded_by set has its score multiplied by 0.1, and —
  │      if its replacement is also among the candidates — capped at replacement × 0.1,
  │      so the correction is always read first when both are found. Never filtered: the
  │      retracted memory stays findable. See design_decisions.md §43.
  │
  ├─ 6. evidence closure: up to 2 supporting neighbours per result, never a row already
  │      in the results. A superseded row's replacement takes the first slot, then
  │      session-adjacent rows, then top co-occurring entity siblings. See "session_id"
  │      below — in practice the last is the entity path.
  │
  ├─ 7. core memory: kind='core' rows for the workspace, oldest first, joined by newline,
  │      capped at ~400 estimated tokens by dropping whole rows from the front. For a named
  │      workspace its own rows are fitted first and the shared 'global' rows fill the
  │      remainder, so the cap costs the shared tier before it costs the repo's own.
  │
  └─ 8. recall tracking: one best-effort UPDATE bumping recalled_count and last_recalled_at
         for the returned ids — not for neighbours. Wrapped in `except sqlite3.Error`, so a
         recall can never fail because of it, and it adds no field to the §4 payload.
```

Evidence closure keeps each hit inside its **own** workspace: a global hit gathers global
neighbours, a repo hit gathers repo neighbours. The fallback widens which rows can be *found*,
not which rows count as evidence for one another. The **replacement** of a superseded hit is the
one neighbour fetched without a workspace filter, because the write-time rule
(`design_decisions.md` §41) already guarantees it is a row that workspace can see.

A query consisting of nothing but recency cues (`today`, `hôm qua`) skips both views and
returns the workspace ordered `created_at DESC`, scored by the recency term alone. That is a
legitimate query, not an error.

### The MCP layer

`mcp_server.py` is a serialization layer and nothing more. It resolves inputs, calls
`retriever.retrieve()` or `store.add_memory()`, and shapes the answer into the frozen payload.

- **One SQLite connection per tool call**, not one per process. A `sqlite3.Connection` has one
  transaction state, and a connection held open for the life of a server suppresses WAL
  auto-checkpointing so the `-wal` sidecar grows without bound. Opening a local file costs well
  under a millisecond.
- **A tool never raises.** Both handlers wrap their body in `except Exception` — the only two
  places in the codebase where that is allowed — and return the failure as a payload with a
  `localmem error: ` prefix. `BaseException` still propagates, so cancellation and interrupts
  are unaffected.
- **stdout is the protocol channel.** Nothing in the server prints; diagnostics go to stderr.
- **Timestamps are converted here and nowhere else.** SQLite stores `YYYY-MM-DD HH:MM:SS`; the
  wire format is RFC 3339 `YYYY-MM-DDTHH:MM:SSZ`.
- **The database path is resolved per call**, so `$LOCALMEM_DB` is read at call time and
  workspace detection follows the process's current directory rather than being pinned at
  startup.

`serve(transport=...)` takes a transport argument so that a future streamable-HTTP transport is
a flag rather than a rewrite. **v1 exposes stdio only** — `localmem serve` passes no argument
and nothing in the CLI can select another value. It is a seam, not a feature.

`sqlite3` calls block the server's event loop. Accepted for v1: queries run against a local
file, are sub-millisecond at personal corpus sizes, and the realistic load is one agent issuing
one tool call at a time. Revisit when a recall becomes measurable in tens of milliseconds, or
when a transport that fields genuinely concurrent clients lands.

## Schema

Version **3**. `localmem/schema.sql` is the version-1 baseline and is never edited; migrations
are forward-only, and `db.migrate()` reads `meta.schema_version` and applies the numbered steps
after it, idempotently. Version 2 is `db._add_recall_tracking`, two `ALTER TABLE ... ADD COLUMN`
statements — the first real use of the mechanism built in M1. Version 3 is `db._add_keywords`:
it adds the column, then **drops and recreates** `memories_fts` with a second column, because
FTS5 has no `ALTER` for a virtual table. All three triggers are recreated carrying the extra
column — the delete-side rows must pass the OLD value of every indexed column or the index
corrupts silently — and the step ends with a `'rebuild'`, measured at under 10 ms for 5,000
rows. A v0.1.0 database upgrades in place on the next open, with its data untouched,
`recalled_count` starting at 0 and `keywords` NULL.

```sql
memories(
    id, content, content_hash, workspace, kind, source, session_id,
    seen_count, superseded_by, created_at, updated_at,
    recalled_count, last_recalled_at,          -- schema version 2
    keywords,                                  -- schema version 3
    UNIQUE (workspace, content_hash)
)
memories_fts        -- external-content FTS5 over memories.content + memories.keywords,
                    -- tokenize='unicode61 remove_diacritics 2', synced by three triggers
entities(id, name, norm_name, UNIQUE (norm_name))
memory_entities(memory_id, entity_id, weight, PRIMARY KEY (memory_id, entity_id))
dedup_queue(id, memory_id, candidate_id, score, status, created_at)
meta(key, value)
```

Connection PRAGMAs, re-applied on every connect: `journal_mode=WAL` (a persistent database
property), `busy_timeout=5000`, `foreign_keys=ON`.

### Column notes

**`UNIQUE (workspace, content_hash)`, not a global unique on `content_hash`.** the original spec §3
declared the constraint globally, which would make the same fact learned in two projects
collide and leak one project's memory into another's recall. Deduplication is per workspace by
design; workspaces are the privacy and relevance boundary of the product.

**`kind`** is one of `note` (default), `trace`, `lesson`, `imported`, `core`. The MCP
`memory_add` tool accepts `note`, `trace` and `lesson`: `imported` belongs to the importer, and
`core` is the always-load tier and therefore human-curated — an agent acting on injected
instructions must not be able to write one. `localmem add --kind core` still does. See
`design_decisions.md` §23.

`lesson` (v0.4.0) is what the project taught the hard way, as opposed to what someone was told;
its content shape — `<symptom> — <the real cause> — <the fix>` — is taught in prose rather than
enforced by a column, and it is written either directly or by `localmem promote ID`, which
rewrites `kind` in place by id. Nothing in the retrieval path reads `kind`: a lesson ranks
exactly as a note does. See `design_decisions.md` §37 and §38.

**`session_id`** is populated only by `localmem add --session-id`. The frozen `memory_add` tool
schema has no such parameter, so **every memory written through MCP stores `NULL`**. The
retriever's session-adjacency neighbour query is guarded by `row.session_id is not None`, which
means it never fires for MCP-written memories; those results get entity-sibling neighbours
instead. The column is reserved for CLI provenance and for a future `SessionEnd` hook.

**`superseded_by`** carries the temporal supersede tier as of v0.4.0 — the column has been
reserved since M1 and is written for the first time here. It is set by
`localmem add --supersedes ID` / `memory_add(..., supersedes=[…])`, in the same transaction as
the insert, and only where the replacement is visible from the retracted row's workspace
(§41). Three consequences worth knowing:

- **the retracted row is demoted, never hidden.** Recall multiplies its score by 0.1 and caps it
  under its replacement's when both are found (§43), and attaches the replacement as its first
  neighbour. `localmem search`, `stats` and `audit` all still see it.
- **core memory is the exception**: a superseded `kind='core'` row is excluded outright, because
  core memory is *pushed* into every recall and a retracted convention must stop being pushed.
- **`transfer.restore` still does not carry it**, exactly as when the column was reserved: it
  holds a row id, and ids are reassigned on restore. Supersede links are local to one database
  and are lost on export/restore; both memories travel, the link does not.

No schema change was needed for any of it — `dedupe --merge` moves links onto the surviving row
before deleting (§42), so the missing `ON DELETE` clause never bites.

**`recalled_count` / `last_recalled_at`** arrive with schema version 2 and are written by one
best-effort statement at the end of `retriever.retrieve()`. They exist for `audit` and `stats`;
nothing in retrieval reads them, and no MCP payload carries them. Rows that predate the upgrade
start at 0, which `audit` states on every run rather than letting it read as history.

**`keywords`** arrives with schema version 3: a space-separated string of the alternative
wordings the *writing agent* supplied, indexed as the second column of `memories_fts`. It is
the one model-authored value in the database, and it is written once, at write time — the
recall path calls no model. There is **no backfill**: generating keywords needs a model, so
pre-existing rows stay NULL and rank exactly as they did in v0.2.2. The only way an existing
row gains keywords is the duplicate merge, which unions the two sets. See
`design_decisions.md` §34.

**`dedup_queue.status`** is `pending`, `merged` or `kept_both`. Both `memory_id` and
`candidate_id` declare `ON DELETE CASCADE`, so resolving a pair with `--merge` — which deletes
the older memory — removes the queue row along with it. A merged pair therefore leaves *no*
row behind, and `gc` only ever prunes `kept_both` rows. See `design_decisions.md` §11.

**`memories.superseded_by` is the one reference with no `ON DELETE` clause**, so deleting a row
another row names as its replacement fails with `FOREIGN KEY constraint failed`. The two paths
that delete a memory handle it differently and both are deliberate: `dedupe --merge` repoints
the links onto the surviving twin (§42), while `gc --prune-traces` **excludes referenced rows
from the prune entirely** (§47) — a prune has no twin to repoint to, and nulling the link would
restore a corrected memory to full rank.

**Two Jaccard thresholds, and they are not the same number.** Tier 2 queues near-duplicates for
human review at ≥ 0.7 using *conjunctive* candidate generation; the capture gate behind
`add --if-novel` declines a write at ≥ 0.25 using *disjunctive* generation. The pairing is not
arbitrary — a low threshold behind a high-precision candidate query never fires at all, which is
measured in `design_decisions.md` §44.1.

**`entities.norm_name` is `UNIQUE` and there is no class column.** The extractor can report one
span under two classes (a quoted `"ConfigLoader"` is both `QUOTED_STRING` and `CAMEL_CASE`);
both collapse into one `entities` row and one link. The class is extraction metadata, not
stored state.

## Concurrency and durability

- Every write runs under `BEGIN IMMEDIATE`, so the write lock is taken up front and concurrent
  agents serialize on read-then-write sequences instead of racing. A lost `UNIQUE` race is
  caught as `IntegrityError` and converted into the merge path.
- `busy_timeout=5000` gives a blocked writer five seconds before it gives up.
- A memory row and its entity links commit or roll back together; a row can never be visible
  without its entities. Deletion is the same statement in reverse: `forget` and
  `gc --prune-traces` both run the delete and the orphaned-entity sweep inside one
  transaction, so an entity is never left unreachable and never removed while still linked.
- Agent config writes go through a temp file in the same directory followed by `os.replace`, so
  a config is never observed half-written. The original is backed up to `*.bak` first, and a
  backup taken for a write that then fails is removed — a refusal leaves the directory exactly
  as it found it.

## What is deliberately absent

No embeddings or vector index. No HTTP transport. No LLM call, anywhere. No network call, ever.
No telemetry. No two-way sync back into instruction files. No automatic deletion of
near-duplicates. No tier-3 supersede logic. See the README's Limitations and Roadmap sections
for what that means in practice and what is planned.
