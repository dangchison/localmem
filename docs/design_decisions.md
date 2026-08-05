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
