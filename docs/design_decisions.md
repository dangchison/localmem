# Design decisions

Deliberate deviations from `PLAN.md` and known limitations, recorded as they are made.

## 1. `UNIQUE(workspace, content_hash)` instead of a global `UNIQUE` on `content_hash`

**Milestone:** M1

`PLAN.md` §3 declares `content_hash TEXT NOT NULL UNIQUE`. That constraint is global, which
conflicts with the workspace partitioning that the rest of the design depends on: the same
fact learned in two projects (`use pnpm not npm`) would collide, and the second workspace
would silently receive a `duplicate_merged` pointing at another workspace's row — leaking
one project's memory into another's recall results.

**Decision:** the schema declares `UNIQUE (workspace, content_hash)` and drops the standalone
`UNIQUE` on `content_hash`. Tier-1 dedup looks up existing rows with
`WHERE workspace = ? AND content_hash = ?`, so a duplicate merges only within its own
workspace.

**Consequences**

- Identical content in `N` workspaces yields `N` rows, each with its own `seen_count`.
- The index backing the constraint also serves the `(workspace, content_hash)` lookup on
  every write, so there is no extra cost.
- Cross-workspace deduplication is not attempted and is not planned; workspaces are the
  privacy and relevance boundary of the product.

## 2. `remove_diacritics 2` does not fold `đ` / `Đ` to `d`

**Milestone:** M1

The FTS5 index uses `tokenize='unicode61 remove_diacritics 2'`, which strips Vietnamese tone
marks so a query typed without diacritics still matches. This holds for tone marks and for
vowel modifications, but `đ`/`Đ` is a separate letter of the Vietnamese alphabet, not a
diacritic on `d`, and Unicode does not decompose it. Observed behavior (v0.1.0):

| stored | query | matches |
|---|---|---|
| `Dùng pnpm thay vì npm` | `dung pnpm` | yes |
| `đúng rồi bạn nhé` | `roi` | yes |
| `đúng rồi bạn nhé` | `dung` | **no** |
| `đúng rồi bạn nhé` | `đúng` | yes |

**Decision:** accepted for v1. Searching a word that begins with `đ` requires typing the `đ`.

**Options rejected for now:** a custom SQLite tokenizer (needs a C extension, contradicts the
stdlib-only constraint) and pre-folding `đ→d` into a shadow indexed column (doubles storage
and desynchronizes the FTS content table from `memories.content`). Revisit if a semantic
view lands in v0.4, which would sidestep the issue.

## 3. Entity extraction is capped at 4 096 characters and 50 entities per memory

**Milestone:** M2

`indexer.extract_entities` truncates its input to `MAX_EXTRACT_CHARS = 4096` before any regex
runs, and keeps at most `MAX_ENTITIES_PER_MEMORY = 50` entities after deduplication.

The character cap is the backtracking guard. The file-path and URL patterns end in unbounded
`+` repetitions over negated character classes, so an adversarial paste — 50 000 characters of
`./a/b.` — is exactly the shape that makes a backtracking engine quadratic. Capping the input
bounds the worst case by construction rather than by trusting the pattern; measured, that input
now extracts in 0.0003 s.

The entity cap bounds the fan-out of `memory_entities`. A pasted stack trace or a chunk of code
can easily contain hundreds of distinct identifiers, most of them noise, and every one of them
would become a row that M3's relational view has to join through.

**Decision:** both caps are silent — no warning, no error, no truncation marker. A memory is
still stored in full and still fully searchable through FTS5; only its *entity* view is
abridged. Entities are ranked by occurrence count, so the 50 that survive are the ones the
memory is actually about.

**Consequences**

- Weights are computed *before* the cut, so `max_count` — and therefore every weight — is
  unaffected by capping; the cut only ever discards low-count entities.
- Ties are broken by first-occurrence offset, then by `norm_name`, so the retained set is
  deterministic across runs. Without that rule the cut would be arbitrary for the common case
  where every candidate occurs exactly once.
- An entity mentioned only after character 4 096 of a very long memory is invisible to the
  relational view. Accepted: the lexical view still finds it.

## 4. Merging a duplicate does not re-index

**Milestone:** M2

`add_memory` calls `indexer.index_memory` only on the path that inserts a new row. Neither the
hash-match branch nor the `IntegrityError` race branch re-indexes.

**Decision:** a merge only bumps `seen_count` and `updated_at`; `content` is untouched, so the
entity links already attached to that row are still exactly correct. Re-extracting would burn
CPU on every repeat write to produce byte-identical rows.

**Consequences**

- Adding the same content twice leaves `COUNT(*)` of both `entities` and `memory_entities`
  unchanged.
- Indexing a new row happens inside the same `BEGIN IMMEDIATE` transaction as the `memories`
  insert, so a memory and its links commit or roll back together — a row can never be visible
  without its entities.
- If the extractor's rules change in a later version, existing rows keep their old links until
  someone re-indexes them. `backfill` does not do this: it only visits rows that have *no*
  links. A re-index command is deliberately left to whichever milestone changes the rules.

## 5. `backfill` idempotency is defined by links created, not by rows processed

**Milestone:** M2

`backfill` selects rows with `NOT EXISTS (SELECT 1 FROM memory_entities WHERE memory_id = ...)`.
A memory with no extractable entity — `"hello world"` — produces no link, so it never leaves
that selection and is revisited on every single run. The intuitive contract "a second run
processes 0 memories" is therefore unachievable without a schema change.

**Options rejected:** an `indexed_at` column on `memories` (correct, but needs migration v2,
which M2 rules out of scope) and writing a sentinel link to a placeholder entity (pollutes the
entity graph with a row that means "nothing here").

**Decision:** redefine the reported quantity instead of the schema. `backfill_all` returns
`(processed, links_created)` and the CLI prints `processed N memories, created M links`.
**Idempotency is `links_created == 0` and an unchanged `memory_entities` row count** — not
`processed == 0`.

**Consequences**

- On a database whose memories are all entity-free, `backfill` prints
  `processed N memories, created 0 links` forever. That is correct, not a bug.
- Re-running is always safe: an already-linked memory is skipped entirely, and re-extraction of
  an entity-free one writes nothing.
- Work is batched at 500 rows per transaction, so a large backfill does not hold one long write
  lock against concurrent agents.

## 6. `ACRONYM` matches shouty prose and Vietnamese abbreviations

**Milestone:** M2

The `ACRONYM` class is `\b[A-Z]{2,10}\b`. It has no dictionary and no language model, so it
cannot tell an acronym from any other run of capitals. Observed behavior (v0.1.0):

| content | entities extracted |
|---|---|
| `UBND đã duyệt` | `ubnd` (ACRONYM) |
| `THIS IS URGENT` | `this`, `is`, `urgent` (all ACRONYM) |
| `Fix the API call` | `api` (ACRONYM) |

**Decision:** accepted for v1 and pinned by tests that document the behavior rather than
suppress it. The alternatives all cost more than the noise does: a stopword list would be
English-only and would break the Vietnamese case that motivates the class; requiring a
lowercase neighbour would drop a genuine acronym at the start of a sentence; and dropping the
class entirely would lose `API`, `SQL`, `UBND` and friends, which are among the most useful
entities in this corpus.

**Consequences**

- A memory written in shouty case produces one entity per word. The cardinality cap bounds the
  damage at 50, and low-count noise entities carry low weights, so M3's relational view
  discounts them automatically.
- Vietnamese text is affected the most, since Vietnamese abbreviates far more aggressively than
  English. Revisit with `underthesea` as an optional extra, never a required dependency.

## 7. Two entity classes may share one `norm_name`, and then share one `entities` row

**Milestone:** M2

`extract_entities` deduplicates by `(norm_name, entity_class)`, so a span can be reported twice
under two different classes. `QUOTED_STRING` deliberately does not mask its interior, which
makes this routine: `use "ConfigLoader" now` yields a `QUOTED_STRING` entity and a `CAMEL_CASE`
entity that both normalize to `configloader`.

Occurrence counts, however, are keyed on `norm_name` alone — the count of an entity is the
number of extracted spans sharing its normalized name, regardless of which class recognized
them. Both entries above therefore report `count=2, weight=1.0`.

**Decision:** the split lives only in the extractor's return value. `entities` declares
`UNIQUE (norm_name)` and has no class column, so the two entries collapse into a single row and
a single `memory_entities` link. `index_memory` consequently returns the number of **distinct
link rows written**, not the number of entities `extract_entities` returned; for the example
above that is 1, not 2.

**Consequences**

- The class is extraction metadata, not stored state. Nothing downstream can ask "was this
  matched as a quoted string or as an identifier?" — and nothing needs to, since `norm_name` is
  the only column anything joins on.
- The reported link count stays truthful, which matters because `backfill`'s idempotency
  contract (§5) is expressed in links created.
- The display `name` stored is the first-seen raw match, so a quoted span keeps its quote
  characters (`"ConfigLoader"`). Accepted: preserving the raw match verbatim is the rule that
  needs no exceptions, and `name` is never joined on.

## 8. Tier-2 gates on Jaccard alone — the bm25 half of §6 is unusable at this scale

**Milestone:** M3

`PLAN.md` §6 specifies tier-2 as "any existing row with bm25 score above threshold **and**
normalized Jaccard token overlap ≥ 0.7". The conjunction cannot be implemented as written,
because there is no bm25 threshold that means anything on a personal-sized corpus.

**Measured.** `memories_fts` is a small index with near-zero IDF, so `-bm25()` values collapse
towards zero. Real numbers from this repository:

| corpus | query | max `-bm25()` |
|---|---|---|
| 31 rows of near-identical content | the shared top terms | **5.0e-06** |
| 2 rows, `use pnpm not npm` / `use pnpm, not npm!` | `"pnpm"` | **1.0e-06** |
| 2 rows, `alpha beta gamma` / `gamma beta alpha` | `"alpha" "beta"` | **2.0e-06** |

The analyst's proposed constant was `TIER2_BM25_THRESHOLD = 0.5`. That is five to six orders of
magnitude above anything the index produces. A gate at 0.5 can never open, so tier 2 would have
been silent dead code — and, worse, code that passes its own tests by never running.

Scaling the threshold down is not a fix either: bm25 magnitude is a function of corpus size and
term rarity, so any absolute constant that fires on a 30-row database will fire on everything
once the database has 30 000 rows, and vice versa. There is no scale-invariant bm25 cut-off.

**Decision:** FTS5 is used for **candidate generation only**. The MATCH expression is built from
the new content's top 5 non-stopword tokens by frequency and returns at most
`TIER2_MAX_CANDIDATES = 10` rows in the same workspace, excluding the new row itself. The
decision gate is then `jaccard(new, candidate) >= TIER2_JACCARD_THRESHOLD` (0.7) — the one hard
number §6 actually specifies, and the only signal here that does not move with corpus size.

Jaccard operates on the token sets of `dedup.normalize(text)` (this is the "normalized" in
"normalized Jaccard"), split on `\W+`, with **no** stopword filtering: dropping filler words
inflates the overlap of short texts, which is exactly the length at which a false pair is most
likely.

**Consequences**

- `use pnpm not npm` vs `use pnpm, not npm!` scores 1.0 and queues. That is precisely the gap
  tier 1 leaves open: the two hash differently because punctuation is meaningful text.
- Unrelated content scores 0.0 and never queues; `alpha bravo charlie delta` vs
  `alpha bravo charlie echo foxtrot` scores 0.5 and does not queue either.
- FTS5's conjunctive MATCH over the top 5 terms is the real recall bound. Two texts that are
  Jaccard-similar but share fewer than those 5 terms are never even considered. Accepted: tier 2
  is a cheap inline assist, not an exhaustive similarity search.
- The stopword list is English-only. Vietnamese filler words are not filtered, so a Vietnamese
  memory's top terms may include them. This costs recall, never correctness — the Jaccard gate
  is unaffected.
- Tier 2 is capped at 10 candidates per insert, so the cost of a write stays bounded regardless
  of corpus size.

## 9. `estimate_tokens` switches regime at 15% non-ASCII characters

**Milestone:** M3

`PLAN.md` §1 fixes two divisors — `ceil(chars/4)` for English, `ceil(chars/2.5)` for
"CJK/Vietnamese-heavy" text — but never says where one ends and the other begins.

**Decision:** `tokens.NON_ASCII_RATIO_CUTOFF = 0.15`. Above that fraction of non-ASCII
characters, the dense divisor applies.

The value is chosen from the two failure modes. A mostly English sentence carrying a couple of
accented loanwords (`the naïve café rule…`, 2 non-ASCII of 43 = 0.047) must stay on the English
estimate, or every European name inflates the number. A genuinely Vietnamese sentence crosses
0.15 after only a handful of diacritics (`Dùng pnpm thay vì npm` = 0.19), which is the intent.

**Consequences**

- Every number this produces is an approximation (±15%) and is labelled `~estimated` wherever it
  is printed, per §1 and §10.
- A short mixed string can land on either side of the cutoff; nothing in the product makes a
  correctness decision on the result — it drives the core-memory cap and, later, the benchmark
  table.
- The estimator lives in `localmem/tokens.py`, which imports nothing from `localmem`, so
  `core_memory.py` (M3) and `benchmark.py` (M5) share one implementation.

## 10. Core memory drops whole rows, oldest first, and never splits one

**Milestone:** M3

`PLAN.md` §5 step 7 caps core memory at 400 estimated tokens and says to "truncate oldest-first".
Truncation could mean cutting the character stream, which would leave a half-sentence at the top
of every recall.

**Decision:** rows are ordered `created_at ASC, id ASC`, joined with newlines, and while the
result exceeds `CORE_MEMORY_TOKEN_CAP = 400` estimated tokens **whole rows are removed from the
front**. A row is never split. `build_core_memory` returns the text, its token estimate and the
number of rows dropped; `stats` surfaces the drop count as a warning.

The `id ASC` tiebreak is required, not cosmetic: `datetime('now')` has one-second resolution, so
several core rows written in the same second would otherwise have no defined order and "oldest
first" would be arbitrary.

**Consequences**

- A single core row larger than 400 tokens is dropped entirely, leaving an empty core memory.
  That is the same rule applied consistently — the alternative is emitting a fragment of a
  preference, which is worse than emitting none.
- `stats` sums each workspace's core memory *after* its own cap, because that is the cost a
  recall actually pays. A global concatenation would report a number nobody is charged.
- `build_core_memory` never raises. A database error is reported as an empty core memory:
  losing the always-load tier must not fail an otherwise good recall.

## 11. `dedupe --merge` deletes the older memory, and the queue row goes with it

**Milestone:** M3

`PLAN.md` §12 forbids "automatic deletion of near-duplicates". `--merge` is not that: it acts on
one pair, on an explicit user instruction, after the pair has been shown side by side. It keeps
the **newer** row, adds the older row's `seen_count` to it, and deletes the older row.

Deleting the older row is what makes the merge honest. `memory_entities` has
`ON DELETE CASCADE`, so leaving the row in place while stripping its links would produce a
memory that is searchable lexically but invisible to the relational view — a silently degraded
record. Either the row is a duplicate and goes, or it is not and stays.

**The queue row goes too, and this is unavoidable.** `dedup_queue.candidate_id` also declares
`ON DELETE CASCADE`, so deleting the older memory removes every queue row that referenced it —
including the one just marked `merged`. Verified on a live database: after
`UPDATE dedup_queue SET status='merged'` followed by `DELETE FROM memories WHERE id=<older>`,
`SELECT * FROM dedup_queue` returns no rows.

**Decision:** the status is set to `merged` before the delete, and the row's subsequent removal
by the cascade is accepted. The observable postcondition of a merge is therefore *"no pending
pair remains for these two memories"*, not *"a row with status `merged` exists"*.

**Consequences**

- Re-running `--merge` on the same id reports `no pending near-duplicate pair with id N` and
  exits non-zero without changing anything. It is a clean error, not a crash, and not a
  double-merge.
- Any *other* pending pair that referenced the deleted memory is cascaded away as well. That is
  correct: those pairs are no longer decidable.
- `gc` consequently only ever prunes `kept_both` rows, since `merged` rows do not survive to be
  aged out. Pending rows are never pruned — an unreviewed near-duplicate is not garbage.
- Keeping the queue row alive would require re-pointing `candidate_id` at the surviving memory,
  which would record a pair of a row with itself. Rejected: a false provenance record is worse
  than an absent one.

**Stated plainly, because it will surprise someone reading the schema:** `dedup_queue` rows for
merged pairs **are removed**, not retained with `status='merged'`. `gc` therefore only ever
prunes `kept_both` rows. Retaining a merged pair's audit trail would require changing
`dedup_queue.candidate_id`'s foreign key from `ON DELETE CASCADE` to `ON DELETE SET NULL` (plus
making the column nullable), which is a schema change and a migration. Both are **deferred past
v1** — the schema is frozen at version 1 for this release.

## 12. The recency cue reweights the recency term; it does not change the decay curve

**Milestone:** M3 (fix round 1)

`PLAN.md` §5 step 1 puts a "recency cue" in the query profile — a regex for `recent`,
`last week`, `hôm qua`, `tuần trước` — that "adds an `ORDER BY created_at` boost". It names no
magnitude, and §5 step 5 already fixes the recency contribution at `0.05 · decay`. A literal
second `ORDER BY` term would have competed with that formula.

**Decision:** a cue changes *how much* of the decay curve is added, and nothing else.
`RECENCY_WEIGHT = 0.05` applies normally, `RECENCY_CUE_WEIGHT = 0.25` when the query carries a
cue. The decay function stays `2 ** (-age_days / 30.0)` in both cases, and no other term in the
fuse is affected. At five times the normal weight the cue can reorder results that the fused
view scores close together, without ever overturning a genuinely better lexical or relational
match.

Cues are detected in the **query only**, case-insensitively, on whole-word boundaries, against
a fixed list: `recent`, `recently`, `latest`, `newest`, `last week`, `last month`, `yesterday`,
`today`, `hôm qua`, `hôm nay`, `tuần trước`, `tháng trước`, `gần đây`, `mới nhất`.

**Diacritics are folded for cue detection only.** Both the cue list and the query are NFD
normalized with `Mn`-category combining marks dropped before comparison, so `tuan truoc` matches
`tuần trước`. This mirrors what `unicode61 remove_diacritics 2` already does for the lexical
view, keeping the two consistent. The folding is applied nowhere else in the pipeline —
`dedup.normalize`, entity `norm_name` and `content_hash` are all untouched by it.

**Consequences**

- `đ` has no canonical decomposition, so folding leaves it in place. `gần đây` typed as
  `gan day` is **not** recognized; `gan đay` is. This is exactly the §2 limitation, reproduced
  deliberately rather than papered over in one place and not the other.
- The cue list is fixed, not a regex over inflections. `last fortnight` and `vài hôm trước` are
  not cues. Adding one is a one-line change to `RECENCY_CUES`.
## 13. Cue words are stripped from the lexical query, and a cue-only query ranks by recency

**Milestone:** M3 (fix round 2)

Detecting a cue is not enough on its own. `store.build_match_expression` builds a *conjunctive*
FTS5 query over every token of the query, so a cue word left in place demands that every matching
memory literally contain it. Measured on a two-row database holding
`we switched the deploy pipeline to pnpm` and `the pnpm lockfile was regenerated`, before this
change:

| query | MATCH expression | cue detected | results |
|---|---|---|---|
| `pnpm` | `"pnpm"` | no | 2 |
| `recent pnpm` | `"recent" "pnpm"` | yes | **0** |

The cue was detected correctly and then had nothing left to reorder. In the phrasing a user is
most likely to type, adding the feature made recall strictly worse than not having it.

**Decision:** cue spans are removed from the query before the view-A MATCH expression is built.
Only spans that actually matched are dropped, multi-word cues drop whole
(`notes from last week` → `notes from`), and everything else keeps its original spelling. A query
with no cue is returned byte-for-byte unchanged, so the common path cannot regress.

Two consequences follow, both deliberate:

1. **Literal matching on cue words is given up.** `search "today"` no longer finds rows because
   they contain the word "today". This is the intended trade: in a memory system that query
   almost certainly means *"what did I record today"*, not *"grep for the string today"*, and
   that is what `PLAN.md` §5 step 1 is reaching for when it says a cue "adds `ORDER BY
   created_at` boost". A user who genuinely wants the literal word can still reach it through
   any other term in the same query — only the cue span is removed, never the rest.
2. **A query that is nothing but cues enters pure recency mode.** `today`, `tuần trước`,
   `hôm qua` return the workspace ordered by `created_at DESC`, limited to `k`, scored by
   `RECENCY_CUE_WEIGHT · decay` alone. Both view scores are reported as `None`, because neither
   view was asked anything. Evidence closure and core memory behave exactly as on the normal
   path. This is a legitimate query, not an error.

**Consequences**

- Entity extraction still runs on the **full original query**. Cue words cannot form entities
  under any of the M2 classes, so stripping would change nothing except to put two divergent
  query texts into the pipeline. View B is untouched by any of this.
- A query that is empty *and* carries no cue — pure punctuation, `"!!!"` — keeps the old
  behavior: empty results and the friendly message. Recency mode is entered only on a cue.
- The query is NFC-normalized before cue tokenization, because a decomposed `hôm` would
  otherwise split into `ho` and `m` on `\W+` and never match. This normalization is confined to
  cue handling; when no cue is found the original string is what continues down the pipeline.
- Verified end to end: `search "recent pnpm"` now returns the same two rows as `search "pnpm"`,
  scored 0.85/0.25 instead of 0.65/0.05 — same rows, heavier recency term.

## 14. The `PLAN.md` §4 payloads are frozen, and the internal dataclasses carry more than they emit

**Milestone:** M4

`retriever.RetrievalResult` and `retriever.RetrievedMemory` were shaped in M3 to satisfy §4
without rework, and they carry a little more than §4 lists. `mcp_server.py` emits the §4 subset
and nothing else. The API freezes at this milestone, so the difference is recorded rather than
left to be rediscovered.

**`memory_recall`** emits exactly three top-level keys — `results`, `core_memory`, `message` —
and each result object exactly eight:

```jsonc
{ "id": int, "content": str, "workspace": str, "kind": str,
  "source": str|null, "created_at": "YYYY-MM-DDTHH:MM:SSZ", "score": float,
  "neighbors": [ { "id": int, "content": str } ] }
```

Withheld on purpose:

| carried internally | why it is not on the wire |
|---|---|
| `RetrievalResult.core_memory_tokens` | a cost estimate; §4 has no field for it and `localmem stats` already reports it |
| `RetrievalResult.core_memory_dropped` | same — a `stats` concern, not a recall answer |
| `RetrievedMemory.seen_count` | §4's result object does not list it; `memory_add` returns it because §4 *does* list it there |
| `RetrievedMemory.lexical_score` / `relational_score` | fusion diagnostics; the fused `score` is the answer |
| `session_id` | provenance the agent did not supply and cannot use |

**`memory_add`** emits exactly `{"status", "id", "seen_count"}`. `store.add_memory` accepts a
`session_id`, but §4's input schema has no such parameter, so the MCP layer always passes `None`.
Adding it later is additive; removing a shipped field is not.

**Failure payloads keep the success shape** and add the reason, so a client never has to branch
on the transport to read an answer:

```jsonc
{"results": [], "core_memory": "", "message": "localmem error: …"}
{"status": "error", "id": 0, "seen_count": 0, "message": "localmem error: …"}
```

`memory_add`'s failure payload is the one place a key appears that success does not have. That is
deliberate: `message` present *means* the call failed, and a successful add has nothing to say.

**Timestamps are converted in the MCP layer only.** SQLite writes `datetime('now')` as
`YYYY-MM-DD HH:MM:SS` in UTC with no offset; §4's wire format is RFC 3339 (`…T…Z`).
`mcp_server._to_wire_timestamp` bridges the two. `retriever.py` is untouched — it owns no wire
format, and the CLI legitimately prints the stored spelling. A value the database could not have
written is passed through unchanged rather than raising, matching how `retriever.recency_boost`
treats the same column.

**`score` is passed through, rounded to 4 places.** §4's `0.87` is illustrative, not a bound.
Fused scores land in that neighbourhood because each view is min-max normalized into `[0, 1]`
*before* fusion (§8 above explains why the raw bm25 values, ~1e-6, cannot be used directly).

## 15. `except Exception` is authorized at the MCP tool boundary, and nowhere else in the codebase

**Milestone:** M4

`PLAN.md` §4 says a recall on an empty database is "**Never an error**". That promise cannot be
kept by an unguarded handler: a corrupt database, an unreadable file, a `ValueError` from
workspace validation would all become an MCP protocol error, and a traceback rendered into the
protocol stream is worse than a degraded payload — it costs the agent the answer *and* the
session.

**Decision:** `mcp_server.memory_recall` and `mcp_server.memory_add` each wrap their whole body
in `except Exception`. These two functions are the only place in `localmem` where that is
allowed. Everywhere else, exceptions are caught by type.

Three properties make this a boundary guard rather than a swallow:

- **It is not a bare `except:`.** `BaseException` — `KeyboardInterrupt`, `SystemExit`, and
  anyio's cancellation exceptions — still propagates, so shutdown and cancellation are
  unaffected. A test asserts this.
- **Nothing is lost.** The full exception is logged to stderr with `Logger.exception`, and the
  message reaches the caller in the payload behind the `localmem error: ` prefix.
- **It is one function deep.** Each tool delegates immediately to a private `_recall` / `_add`
  that raises normally, so the guarded region is a single call and no logic hides inside it.

Input validation uses the same path on purpose: `k` out of range, `kind='imported'`, blank
content and `workspace='all'` on a write all raise `ValueError` and surface as the same error
payload. One shape for every failure means the client has one thing to check.

## 16. One SQLite connection per MCP tool call, not one per server process

**Milestone:** M4

The obvious design — open the database when the server starts, reuse it — is wrong twice over.

1. **Concurrency.** `store.add_memory` runs a `BEGIN IMMEDIATE` read-then-write sequence. A
   `sqlite3.Connection` has one transaction state; two calls interleaving on one connection can
   commit each other's work or fail on a nested `BEGIN`. Separate connections serialize through
   SQLite's own locking, which is what `busy_timeout=5000` is there for.
2. **WAL growth.** SQLite auto-checkpoints when the *last* connection to a database closes. A
   connection held open for the life of a long-running agent session suppresses that, and the
   `-wal` sidecar grows without bound.

**Decision:** `mcp_server._connection()` opens the database at handler entry and closes it in a
`finally`. Measured on a local file this is well under a millisecond — far below the cost of the
FTS5 query it wraps.

**Consequences**

- After a session of ten tool calls the database directory holds `memory.db` alone: no `-wal`,
  no `-shm`. This is asserted by a test that drives a real `localmem serve` subprocess.
- The database path is resolved per call, so `$LOCALMEM_DB` is read at call time, not pinned at
  startup. So is workspace detection: one server process outlives any single directory an agent
  asks about.
- **A leak found while testing this, fixed in M4 fix round 1:** `db.connect` applies its PRAGMAs
  *after* `sqlite3.connect` returns, and `db.open_database`'s `try/except BaseException: close()`
  guard wrapped only `migrate()`. Against a corrupt database file `PRAGMA journal_mode=WAL`
  raises and the connection object was dropped unclosed — one `ResourceWarning` per tool call
  for a server whose database is corrupt. `db.connect` now guards its own setup with
  `try/except BaseException: conn.close(); raise`, so the exception callers see is unchanged
  (`cli._session` and the MCP tool boundary both match on `sqlite3.Error`) while the half-built
  connection is closed. The regression test asserts both halves — that the connection object is
  closed, and that nothing reaches `sys.unraisablehook` — because either alone passes vacuously:
  a `ResourceWarning` raised during finalization goes through the unraisable hook, not the
  `warnings` filter, so `warnings.simplefilter("error", ResourceWarning)` does **not** catch it.

## 17. `sqlite3` blocks the MCP server's event loop — accepted for v1

**Milestone:** M4

The tool handlers are ordinary synchronous functions. Every database call inside them — connect,
FTS5 query, insert, close — blocks the thread the MCP server's event loop runs on, so a single
slow query stalls every other in-flight request on that connection.

**Decision:** accepted for v1, deliberately, because the alternative buys nothing here. Queries
run against a local file, are sub-millisecond at personal corpus sizes, and the realistic load is
one agent issuing one tool call at a time. Wrapping each handler in `anyio.to_thread.run_sync`
would add a thread hop and a concurrency model to reason about in exchange for latency that is
not currently observable.

**Revisit when** either becomes true: the corpus grows to where a recall is measurable in tens of
milliseconds, or the streamable-HTTP transport (§12 of `PLAN.md`, v2) lands and one server starts
fielding genuinely concurrent clients. The change is contained — the handlers already delegate to
`_recall` / `_add`, so making them `async def` and moving those two calls onto a worker thread
touches nothing else.

## 18. mcp 2.x is snake_case in Python and camelCase on the wire — do not "fix" either side

**Milestone:** M4

`mcp` 2.0.0 renamed the Python model fields to snake_case. The JSON-RPC payload did **not**
change: it is still camelCase, because that is what the MCP protocol specifies and what every
other client and server implementation emits.

| concept | Python attribute (mcp 2.x) | JSON on the wire |
|---|---|---|
| tool input schema | `Tool.input_schema` | `inputSchema` |
| tool output schema | `Tool.output_schema` | `outputSchema` |
| call failed | `CallToolResult.is_error` | `isError` |
| structured result body | `CallToolResult.structured_content` | `structuredContent` |
| pagination cursor | `ListToolsResult.next_cursor` | `nextCursor` |
| negotiated version | `InitializeResult.protocol_version` | `protocolVersion` |
| server identity | `InitializeResult.server_info` | `serverInfo` |

Verified by capturing the raw stdout of `localmem serve` driven over a plain pipe: the
`initialize` response carries `protocolVersion`, `serverInfo` and `listChanged`, and a
`tools/call` response carries `isError`.

**This is a warning, not a curiosity.** Both spellings are correct in their own layer. A reader
who sees `is_error` in `mcp_server.py` next to `"isError"` in a captured frame will be tempted to
make them agree; doing so breaks whichever side they change. Pydantic's alias generator handles
the translation, and neither side is ours to normalize.

Applies to test code too: assert on `result.is_error` when holding a parsed `CallToolResult`, and
on `msg["result"]["isError"]` when parsing raw JSON off the pipe.

## 19. Codex config: parse to decide, append to write — and why there is a TOML dependency

**Milestone:** M5 (rewritten in fix round 3, after three blockers)

`PLAN.md` §8 step 2 says to "append `[mcp_servers.localmem]` block to `~/.codex/config.toml`". Doing
that safely needs one decision: *is localmem already registered?*

**Decision, current:** parse the file and look up the key.

```python
document = tomllib.loads(text)
servers = document.get("mcp_servers")
present = isinstance(servers, dict) and "localmem" in servers
```

Writing stays **append-only** — the file is never rewritten from the parsed data, because that
would destroy the comments, table order and formatting that append-only exists to preserve. **Parse
to decide, append to write.**

### Why there is a `tomli` dependency, and why removing it will break things

`dependencies` carries `tomli>=2.0; python_version < '3.11'`, and `codex.py` does:

```python
if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib
```

`tomllib` is stdlib from 3.11, `requires-python` is `>=3.10`, and `tomli` is the backport. `tomli`
2.4.1 has **zero** transitive dependencies. `requires-python` stays `>=3.10`.

This dependency was originally forbidden, on the reasoning that parsing buys only "is the block
present?" and an anchored regex answers that exactly. **That reasoning was wrong, and it cost three
blockers.** The user reversed the constraint once that became clear. Anyone tempted to "simplify
away" the dependency later should read the next section first.

### What went wrong three times

Each attempt detected the table with a regex over `[…]` headers. Each time an existing, working
declaration went undetected, `apply()` appended a *second* declaration, and TOML forbids that:

```
TOMLDecodeError: Cannot declare ('mcp_servers', 'localmem') twice
```

Codex then fails to load the whole file, taking **every** MCP server in it down — and every one of
these runs reported `action="merged"`, a success.

| # | trigger | why the regex missed it |
|---|---|---|
| 1 | `[mcp_servers.localmem]  # hand added earlier` | TOML allows a comment after a table header; the pattern was anchored to `]` |
| 2 | a CRLF-terminated header | `re.MULTILINE`'s `$` matches before `\n`, **not** before the `\r` of a CRLF line |
| 3 | `["mcp_servers".localmem]` | the pattern allowed a quoted child key but not a quoted parent |

Blocker 2 also exposed the deeper problem: the post-write safety net re-scanned with the *same*
blind pattern, saw only the newly appended header, and let the write stand. **A guard that shares a
scan with the decision can never catch that scan's blind spot.**

### The measurement that ended the argument

TOML has many spellings that all bind `mcp_servers.localmem`. Measured against the last regex-based
implementation:

| spelling | binds the key | regex saw it |
|---|---|---|
| `[mcp_servers.localmem]` | yes | yes |
| `["mcp_servers".localmem]` | yes | yes (only after blocker 3) |
| `[mcp_servers."localmem"]` | yes | **no** |
| `[[mcp_servers.localmem]]` (array of tables) | yes | **no** |
| `mcp_servers = { localmem = { … } }` (inline table) | yes | **no** |
| `mcp_servers.localmem.command = "…"` (dotted key) | yes | **no** |
| `[mcp_servers]` + `localmem.command = "…"` | yes | **no** |

The last four have **no `[…]` header naming localmem at all**, so no regex over table headers can
ever see them — this was not a pattern that needed one more fix. All four were confirmed to produce
an unparseable config through the real `apply()`. The parser detects all of them, verified end to
end.

### What the parser also fixed for free

- **Malformed TOML is now refused.** `tomllib.loads` raising means `action="refused"`, nothing
  written, no `.bak`, and the block printed for manual addition — the same DD-6 rule the JSON
  writers always followed, and something append-only could never do because it never parsed.
- **The post-write invariant is now real.** `_verify` re-reads and re-parses the written file: it
  must parse **and** bind `mcp_servers.localmem`. A binding the decision missed shows up here as
  `Cannot declare … twice`, which is exactly the failure the old net could not see. On failure the
  append path restores the original with `os.replace(backup, target)` — atomic, and it consumes the
  `.bak` in the same step — while the create path removes the file it just made.
- **Three guards were deleted, not left dormant.** The multi-line-string blanking, the odd-quote
  refusal and the scan-time line-ending normalisation are all gone, along with `TABLE_HEADER_RE`,
  `MCP_SERVERS_HEADER_RE` and `escaped_server_header`. Keeping them would mean two sources of truth.
  The odd-quote guard also produced false refusals on legal TOML like `x = '"""'`; that now
  registers normally.
- **The residual hole of rounds 1 and 2 is closed.** Two `"""` sequences in separate comments used
  to make the scan blank a stretch of real TOML between them. A parser reads comments as comments.

**The appended block still adopts the file's own line endings.** `dominant_newline()` picks
whichever of `\n`, `\r\n`, `\r` the file mostly uses (ties go to LF; a new file gets LF), and the
block is re-terminated to match, so a CRLF config does not come back with two conventions mixed
into it. `render_config()` still returns the canonical LF block: it is what the CLI prints for a
user to paste, and it is pure by contract.

**Consequences**

- The file is byte-identical except for the appended block. Comments, table order, alignment and
  every unrelated table survive, because nothing rewrites them. Asserted against a fixture carrying
  `[desktop]`, `[features]`, `[mcp_servers.codegraph.env]` and comments.
- Reading and writing both disable newline translation (`newline=""`), so appending to a CRLF config
  does not rewrite every line ending in the file.
- **A lone-CR file is now refused, and that is the honest answer.** TOML 1.0 defines a newline as LF
  or CRLF only, so `[mcp_servers.localmem]\r` is not valid TOML and Codex cannot read it either.
  Fix round 2 taught the regex to "detect" headers in such a file; that was solving a non-problem.
- **`[mcp_servers.localmem.env]` alone now counts as registered.** TOML creates the intermediate
  table implicitly, so the key path genuinely is bound. This is a deliberate behaviour change — the
  regex rejected that line and would have appended. Reporting `already_present` is the fail-safe
  reading: it writes nothing. A config with env vars for a server that is not defined is already
  incomplete, and the user is told localmem is registered rather than having their file edited.
- **If `mcp_servers.localmem` exists but its value is wrong, localmem leaves it alone** and reports
  `already_present`. Correcting it would require rewriting the file from parsed data, which is the
  thing append-only exists to avoid. The user edits it, or removes it and re-runs.
- `[mcp_servers.LocalMem]` is correctly ignored — TOML keys are case-sensitive, so it is a different
  table that legitimately coexists.

## 20. A malformed agent config is refused — no write, no backup

**Milestone:** M5

The analyst proposed that when an existing JSON config cannot be parsed, localmem should back it up
and then write "a fresh config containing only the localmem entry".

**Rejected.** That silently drops every other MCP server from the *live* file. A user whose
`~/.gemini/config/mcp_config.json` had a stray trailing comma would find their other servers gone,
and would most likely not notice until something broke — at which point the `.bak` is a file they
have no reason to look for. A backup does not make it acceptable: the user consented to *adding
localmem*, not to *having their config replaced*.

**Decision:** on a config that cannot be parsed, `apply()` returns `action="refused"`, **writes
nothing and backs up nothing**, and the message names the parse error. The CLI then prints the exact
block for the user to add by hand. `init` still exits 0 — a refusal is a normal outcome, not a crash
— but the refusal is visible in the step-2 summary.

Four inputs take this path, all with the same reasoning: invalid JSON, an empty file, a top level
that is not an object, and an `mcpServers` value that is not an object.

**Consequences**

- An empty (zero-byte) config file is refused rather than treated as a blank document. It is
  unparseable, and one rule with no exceptions is easier to trust than two. The user deletes the
  empty file and re-runs, or pastes the printed block.
- **Codex follows this rule too, as of M5 fix round 3.** It could not before: append-only
  never parsed, so a broken TOML config was appended to regardless. Now `tomllib.loads`
  raising produces the same refusal, with the same postcondition (§19).
- The same refusal shape carries genuine I/O failures — an unreadable file, an undecodable file, a
  directory that cannot be created, a backup that cannot be written. In every case the postcondition
  is identical and checkable: **the original file is byte-for-byte what it was**.
- Writes go through a temp file in the same directory followed by `os.replace`, so a config is never
  observed half-written, and a failed write removes its own temp file.
- **A backup taken for a write that then fails is removed.** `replace_atomically` backs up, writes,
  and unlinks the backup if the write raises. Without that, an out-of-disk failure left a `.bak`
  beside an unmodified config — a file that looks like evidence of a change that never happened.
  The postcondition is uniform: a refusal leaves the directory exactly as it found it.
- **A config localmem creates takes the umask default, not `0600`.** `tempfile.mkstemp` creates its
  temp file at `0600`, and `os.replace` carries that mode to the destination, so every config
  localmem created was owner-only — unlike one the user would have created by hand. `write_atomic`
  now copies the mode of an *existing* target and applies `0o666 & ~umask` to a *new* one. Reading
  the umask requires setting it and putting it back; localmem's writers run in a single-threaded CLI
  process, so nothing else can create a file in that window.

## 21. `~/.claude.json` is never written

**Milestone:** M5

`PLAN.md` §8 step 2 says of Claude Code: "prefer printing the exact command over editing global
files". Measured on this machine, `~/.claude.json` is **~60 KB across 64 top-level keys** — project
history, session state, onboarding flags — of which `mcpServers` is one.

**Decision:** localmem never opens that file for writing. Inside a git repository it merges into the
project `./.mcp.json` under the §20 rules. Outside a repository it writes nothing at all and prints
`claude mcp add localmem -- localmem serve`, returning `action="printed"`.

Repository membership is decided by walking `cwd` and its parents for a `.git` entry, not by
shelling out to `git`: a subprocess would inherit the real environment, and this answer only selects
which path to write.

**Consequences**

- A test asserts the sha256 of a fake 60 KB `~/.claude.json` is unchanged after a full `init --yes`,
  and that no `.claude.json.bak` appears. This is the single highest-consequence assertion in M5.
- Registering localmem for Claude Code outside a repository is a manual step. Accepted: one printed
  command is cheaper than one rewritten state file.

## 22. No function under `localmem/agents/` resolves the home directory

**Milestone:** M5

M5 is the first milestone whose code writes outside the repository, into the user's own agent
configs. The failure mode that matters is a *test* reaching a *real* config file.

Monkeypatching `$HOME` is the usual mitigation and it is not enough on its own: it protects only the
paths that happen to route through the environment, and a single missed `Path.home()` in a module
nobody re-read defeats it silently.

**Decision:** `home` and `cwd` are **parameters** of every function and every writer method in
`localmem/agents/`. No module in that package contains `Path.home()`, `expanduser`, `os.environ` or
`getenv` — asserted by a test that scans the source of all six modules, so the rule is enforced
against future edits rather than remembered. `cli.py` is the only place that calls `Path.home()`,
and it passes the result down.

This makes touching the real `~/.codex/config.toml` from a test *structurally impossible* rather
than merely unlikely: there is nothing to intercept.

`tests/conftest.py` additionally redirects `$HOME` to a temporary directory for every test, autouse.
That is the second layer, not the first.

**Consequences**

- `render_config(home, cwd)` takes both parameters and uses neither in three of the four writers.
  That is deliberate: one signature for every writer keeps the `Protocol` structural and keeps the
  registry loop free of special cases.
- `render_config` is additionally **pure** — no filesystem access at all — which a test enforces by
  making `Path.open`, `Path.exists`, `Path.glob` and `builtins.open` raise for the duration.
- The whole package is testable with `tmp_path` alone. No fixture has to remember to sandbox
  anything.
