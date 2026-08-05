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
