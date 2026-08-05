# localmem

**Local-first, zero-token memory for AI coding agents.** One SQLite database, raw traces,
structured retrieval — no LLM call anywhere in the memory path.

Coding agents keep their long-term memory in instruction files: `CLAUDE.md`, `AGENTS.md`,
Kiro steering files. Those are **push-based** — the whole file enters context at the start
of every session whether it is relevant or not, it grows without bound, and it is siloed
per project and per agent.

localmem is **pull-based**. Memories live in one SQLite database at `~/.localmem/memory.db`,
partitioned by workspace. Agents reach them through two MCP tools, `memory_recall` and
`memory_add`. Storing, indexing, deduplicating and ranking are all pure Python and SQL; the
only tokens you ever pay are the ones your agent spends reading the evidence it actually
asked for.

- **One database, four agents.** Claude Code, Codex CLI, Google Antigravity and AWS Kiro all
  connect to the same file through the same stdio MCP server.
- **Nothing leaves your machine.** No cloud, no account, no telemetry, no network call.
- **Your files are yours.** localmem detects agent configs automatically but never writes one
  without a yes, and it never edits an instruction file at all.

Status: **v0.1.0**, first release. Python ≥ 3.10. MIT licensed.

---

## Install

localmem is not on PyPI. Install it from a checkout:

```bash
git clone https://github.com/<your-account>/localmem.git
cd localmem
python3 -m venv .venv
.venv/bin/pip install -e .
```

`python3` must be **3.10 or newer** — check with `python3 --version` first, and use an
explicit `python3.11`/`python3.12`/`python3.13` if your default `python3` is older.

Runtime dependencies are `click>=8.1`, `mcp>=2.0`, and `tomli>=2.0` on Python 3.10 only
(`tomllib` is stdlib from 3.11). That is the whole list. Add `".[dev]"` instead of `.` if you
want pytest, ruff and mypy.

Put `.venv/bin` on your `PATH`, or prefix the commands below with `.venv/bin/`.

---

## Quickstart

### 1. Set up

```bash
localmem init
```

`init` runs five steps and is safe to re-run:

1. **Database** — creates and migrates `~/.localmem/memory.db`. This step is unconditional;
   it is the only thing `init` does without asking.
2. **Agent configuration** — detects installed agents and asks about each one *individually*.
   Default answer is no. With no terminal to ask on it prints what it *would* do and writes
   nothing.
3. **Import** — a separate question, never bundled with step 2. Offers any `CLAUDE.md`,
   `AGENTS.md` or `.kiro/steering/*.md` it found, with `[a] import all / [s] select files /
   [n] skip`.
4. **Pointer snippet** — prints a block for you to paste into your instruction file.
   localmem never edits that file for you.
5. **Self-check** — runs one real recall and prints where to go next.

Set `LOCALMEM_DB` to put the database somewhere else.

### 2. Store and recall

```bash
localmem add "use pnpm, not npm"
localmem add "the staging deploy needs a manual approval step"
localmem search "pnpm"
localmem stats
```

`add` prints JSON: `{"status": "added", "id": 1, "seen_count": 1}`. Adding the same fact
again returns `duplicate_merged` and bumps `seen_count` instead of creating a second row —
normalization folds case, whitespace runs and markdown bullet prefixes, so
`- Use   PNPM, not npm` is the same memory as `use pnpm, not npm`.

Memories are scoped to a **workspace**, detected from the git repository root name, falling
back to the directory name and then to `global`. Override it anywhere with `-w NAME`, and
search across all of them at once:

```bash
localmem search "pnpm" --all
```

### 3. Register localmem with your agent

`localmem agents` shows what was detected and where each config lives. `localmem agents
--install NAME` registers one agent — naming it is the consent.

| Agent | Slug | What localmem writes |
|---|---|---|
| Claude Code | `claude-code` | project `./.mcp.json` **inside a git repo**; outside one it writes nothing and prints `claude mcp add localmem -- localmem serve` |
| Codex CLI | `codex` | appends `[mcp_servers.localmem]` to `~/.codex/config.toml` |
| Google Antigravity | `antigravity` | merges into `~/.gemini/config/mcp_config.json` |
| AWS Kiro | `kiro` | merges into `./.kiro/settings/mcp.json` when `./.kiro/` exists, else `~/.kiro/settings/mcp.json` |

Every writer merges into what is already there — other MCP servers and other keys survive —
backs the original up to `*.bak` before modifying it, and **refuses outright** if the existing
file cannot be parsed, printing the block for you to add by hand. `~/.claude.json` is never
opened for writing.

Step-by-step walkthroughs per agent: [`examples/claude_code.md`](examples/claude_code.md),
[`examples/codex.md`](examples/codex.md), [`examples/antigravity.md`](examples/antigravity.md),
[`examples/kiro.md`](examples/kiro.md).

### 4. Tell the agent to use it

Paste this into the instruction file your agent already loads (`localmem init` prints it too):

```markdown
## Memory

Before answering questions about project history, prior decisions, or user preferences, call the `memory_recall` tool. When you learn a durable fact or decision, save it with `memory_add`. Do not duplicate long-term memory in this file.
```

### The full command set

Eleven commands, no more:

| Command | What it does |
|---|---|
| `localmem init` | guided setup — the five steps above |
| `localmem add TEXT` | store a memory; `-w`, `--kind {note,trace,core}`, `--source`, `--session-id` |
| `localmem search QUERY` | ranked recall; `-w`, `-k N` (1–20), `--all` |
| `localmem import PATH…` | import markdown instruction files; `-w`, `--dry-run`, `--select` |
| `localmem agents` | list detected agents; `--install NAME` registers one |
| `localmem serve` | run the MCP server on stdio — this is what agent configs invoke |
| `localmem stats` | row counts, entity graph size, queue depth, core-memory cost |
| `localmem benchmark` | estimate instruction-file cost against localmem's fixed cost; `-w`, `--json` |
| `localmem dedupe` | review the near-duplicate queue; `--review`, `--list`, `--merge ID`, `--keep-both ID`, `-w`, `--json` |
| `localmem backfill` | extract entities for memories stored before indexing; `-w` |
| `localmem gc` | prune resolved queue rows and reclaim disk space; `--dry-run`, `--days N` |

Every command works headless. Prompts appear only when stdin is a terminal.

---

## How it works

```
localmem add / import / memory_add
        │
        ├─ tier-1 dedup ── sha256 of normalized text, per workspace ── merge or insert
        ├─ FTS5 index ──── kept in sync by triggers
        ├─ entity index ── regex extraction → entities / memory_entities
        └─ tier-2 dedup ── FTS5 finds candidates, Jaccard ≥ 0.7 queues a pair for review
                                        │
localmem search / memory_recall         ▼
        │                          dedup_queue (never auto-merged)
        ├─ view A, lexical ──── FTS5 bm25, workspace-filtered, top 20
        ├─ view B, relational ─ memories sharing an entity with the query, Σ link weight
        ├─ fuse ─────────────── each view min-max normalized, 0.6/0.4 lexical/relational
        │                       (flipped to 0.4/0.6 when view B found anything)
        ├─ boosts ──────────── recency decay (half-life 30 days) + log(seen_count)
        ├─ evidence closure ── up to 2 supporting neighbours per result
        └─ core memory ─────── kind='core' rows, capped at ~400 estimated tokens
```

Nothing in that path calls a model. `docs/architecture.md` has the data flow and the schema;
`docs/design_decisions.md` records every deviation from the plan and why it was made.

The MCP surface is two tools and is frozen:

- **`memory_recall(query, workspace?, k?)`** → `{"results": [...], "core_memory": str,
  "message": str|null}`. Each result carries `id`, `content`, `workspace`, `kind`, `source`,
  `created_at`, `score`, `neighbors`. An empty database is **never an error** — it returns
  `results: []` and a friendly message.
- **`memory_add(content, workspace?, kind?, source?)`** → `{"status": "added" |
  "duplicate_merged", "id": int, "seen_count": int}`.

Transport is stdio. That is the only transport v1 ships.

---

## Benchmark

`localmem benchmark` estimates what your instruction files cost you every session, against
localmem's fixed per-session cost: the pointer snippet, the two MCP tool descriptions, and
your workspace's core memory. The "after" figure is charged **once per session**, not per file.

A worked example you can reproduce exactly — run from `tests/fixtures/` in a checkout, with a
sandboxed `HOME` so that nothing on the measuring machine is in scope. Real output, with only
the absolute path prefix elided:

```
$ localmem benchmark
workspace: localmem
  <repo>/tests/fixtures/CLAUDE.md  ~133 estimated tokens
  <repo>/tests/fixtures/AGENTS.md  ~46 estimated tokens

before (pushed every session): ~179 estimated tokens
after  (pulled on demand):     ~133 estimated tokens
    pointer snippet:   ~62
    tool descriptions: ~71
    core memory:       ~0
saved: ~46 estimated tokens (25.7%)

Estimates use a character-based approximation (±15%). Verify real numbers with `/context` in Claude Code before and after migrating.
```

**That 25.7% is not a promise, and no number here is.** Run the same command on a machine
whose own `~/.claude/CLAUDE.md` is also in scope and the identical invocation reports
`before_tokens 688 → after_tokens 133`, **80.7%** saved — same command, same fixed "after"
cost, a completely different headline, because the only thing that moved was how much
instruction file the scan happened to find. The savings are a function of *your* files, and
of nothing else. A user whose instruction files are already tiny will see a negative number,
which is the honest answer.

So: run `localmem benchmark` yourself, and read the caveat line it prints. Use `--json` for
machine-readable output — `before_tokens`, `after_tokens`, `saved_tokens`, `saved_pct`,
`after_breakdown` and the caveat, in one object.

---

## Migrating from instruction files

Short version:

```bash
localmem import ./CLAUDE.md --dry-run   # see what it would create, write nothing
localmem import ./CLAUDE.md             # import for real
localmem search "pnpm"                  # check you can get it back before trimming anything
```

Then trim the imported sections out of `CLAUDE.md` by hand and leave the pointer snippet in
their place. Static directives that must always apply — build commands, style rules the model
has to obey unconditionally — should **stay** in the instruction file. It is the accumulated,
occasionally-relevant knowledge that belongs in localmem.

Re-importing an unchanged file adds no rows: every record hashes to what it hashed to last
time, merges, and bumps `seen_count`.

**localmem never edits your instruction files.** Not on import, not on `init`, not ever. The
trimming is yours to do. Full guide, including what to keep and what to move:
[`docs/migrating_from_instruction_files.md`](docs/migrating_from_instruction_files.md).

---

## Limitations

Read this section before deciding localmem is right for you. Everything below is measured
behaviour of v0.1.0, not speculation.

1. **BM25 has no semantic matching.** Retrieval is lexical plus an entity graph. A query
   that shares no words and no entities with a memory will not find it — "how do we install
   packages" does not retrieve "use pnpm, not npm". Embeddings are planned for v0.4.
2. **Entity extraction is regex-based and language-naive.** No model, no dictionary. It
   recognizes URLs, @-mentions, file paths, quoted strings, CamelCase, snake_case and
   ALL-CAPS runs — and it cannot tell a real acronym from shouty prose, so `THIS IS URGENT`
   produces three entities. Optional spaCy/underthesea NER is a **roadmap item for v0.3**; it
   is not packaged today and there is no installable extra for it.
3. **Single-user, local, no isolation.** One database per user account, no authentication, no
   multi-user separation, no encryption at rest. Anything that can read the file can read
   every memory in it.
4. **ChatGPT is not supported in v1.** It needs a remote HTTP transport; localmem ships stdio
   only. That is a v2 item, and shipping it needs an auth story first.
5. **`đ`/`Đ` is not folded to `d`.** FTS5's `remove_diacritics 2` strips Vietnamese tone
   marks, but `đ` is a separate letter with no Unicode decomposition. Searching `dung` does
   **not** match a stored `đúng`; searching `đúng` does. See the Tiếng Việt section.
6. **Near-duplicate detection gates on Jaccard token overlap ≥ 0.7, and on nothing else.**
   FTS5 supplies at most 10 candidates from the new memory's top 5 terms; the decision is
   Jaccard alone. (A bm25 threshold was specified originally and removed — bm25 magnitudes on
   a personal-sized corpus are around 1e-06, so no fixed threshold could ever fire.) Two texts
   that are similar but share few of those top terms are never even considered.
7. **Entity extraction is capped** at the first 4,096 characters and 50 entities per memory.
   The memory is still stored in full and still fully searchable by FTS5 — only its *entity*
   view is abridged. The caps are silent.
8. **`dedupe --merge` deletes the older memory permanently.** It keeps the newer row and folds
   the older row's `seen_count` into it. This is the only path in localmem that deletes a
   memory, and it runs only on a pair you have just reviewed. The queue row disappears with it
   (foreign-key cascade), so a merged pair leaves no `merged` row behind — `gc` therefore only
   ever prunes `kept_both` rows.
9. **Core memory drops whole rows at the cap.** `kind='core'` rows are concatenated and capped
   at ~400 estimated tokens; over the cap, whole rows are dropped oldest-first — never split.
   A single core memory longer than 400 tokens is dropped entirely and is invisible to recall.
   `localmem stats` warns when rows are being hidden.
10. **A malformed agent config is refused, never rewritten.** If your existing `.mcp.json`,
    `mcp_config.json`, `mcp.json` or `config.toml` does not parse, localmem writes nothing,
    backs up nothing, and prints the block for you to add by hand. It will not "repair" the
    file, because repairing it means dropping your other MCP servers.
11. **The entity view is one hop.** Memories sharing an entity with the query are scored by
    the sum of their link weights. There is no multi-hop traversal and no PageRank.
12. **All token counts are estimates.** A character-based approximation, ±15%, switching to a
    denser divisor above 15% non-ASCII characters. They are labelled `~estimated` everywhere
    they appear.
13. **`session_id` is always empty for memories written through MCP.** The column exists and
    `localmem add --session-id` populates it, but the frozen `memory_add` tool schema has no
    such parameter, so every memory an agent writes stores `NULL`. Evidence closure therefore
    falls back to entity siblings for MCP-written memories; session-adjacency neighbours only
    ever appear for memories written by the CLI with an explicit `--session-id`.

Also **not** built, and not described anywhere in this repo as if they were: the
`superseded_by` column is reserved schema with no logic behind it (tier-3 temporal supersede
is a v0.2 item); HTTP/SSE transport exists as a single function parameter for v2's benefit and
is not reachable from the CLI; there is no two-way sync back into instruction files, and none
is planned.

---

## Roadmap

Recorded, not implemented.

- **v0.2** — tier-3 temporal supersede using the reserved `superseded_by` column; a Claude
  Code `SessionEnd` hook example for automatic trace capture; per-agent `source` analytics in
  `stats`.
- **v0.3** — promotion tooling that suggests high-`seen_count` notes for `kind='core'`; richer
  NER as genuine optional extras (spaCy for English, underthesea for Vietnamese) — this is
  where an installable `[ner]` extra would first exist.
- **v0.4** — optional semantic view via `sqlite-vec`; the fuse becomes three views.
- **v2** — streamable HTTP transport plus an auth token, which is what ChatGPT and other
  remote connectors need, shipped with explicit security documentation.

---

## Citation

Inspired by *Zero-Mem: Zero-Token Memory Operations for LLM Agents* (arXiv:2607.29377). The
paper's finding — that agent memory does not need LLM-generated summaries, and that keeping
raw traces under non-generative retrieval structures beats summarize-and-store on both quality
and cost — is the design principle this package is built on.

"Zero-Mem" is the paper's name and belongs to its authors. This package is `localmem` and is
not affiliated with them.

```bibtex
@article{zeromem2026,
  title   = {Zero-Mem: Zero-Token Memory Operations for LLM Agents},
  journal = {arXiv preprint arXiv:2607.29377},
  year    = {2026},
  eprint  = {2607.29377},
  archivePrefix = {arXiv}
}
```

---

## License

MIT — see [LICENSE](LICENSE).

---

## Tiếng Việt

**localmem** là lớp bộ nhớ cục bộ, không tốn token, cho các AI coding agent. Toàn bộ dữ liệu
nằm trong một file SQLite ở `~/.localmem/memory.db`; mọi thao tác lưu, đánh chỉ mục, khử trùng
lặp và xếp hạng đều là code thuần — không có lần gọi model nào. Agent truy cập qua hai MCP
tool: `memory_recall` và `memory_add`.

### Cài đặt

```bash
git clone https://github.com/<your-account>/localmem.git
cd localmem
python3 -m venv .venv        # cần Python >= 3.10
.venv/bin/pip install -e .
```

### Dùng nhanh

```bash
localmem init                         # tạo DB, hỏi từng agent một, đề nghị import
localmem add "dùng pnpm thay vì npm"
localmem search "pnpm"
localmem stats
```

### Ba lưu ý riêng cho tiếng Việt

1. **`đ`/`Đ` không được quy về `d`.** FTS5 bỏ dấu thanh nên gõ `dung` vẫn tìm được `dùng`,
   nhưng `đ` là một chữ cái riêng và Unicode không tách nó ra. Vì vậy tìm `dung` **không** ra
   `đúng`; phải gõ đúng chữ `đ`.
2. **Cùng giới hạn đó áp dụng cho từ khoá "gần đây".** localmem nhận các cụm chỉ thời gian
   (`hôm qua`, `hôm nay`, `tuần trước`, `tháng trước`, `gần đây`, `mới nhất`) và bỏ dấu khi so
   khớp — nên `tuan truoc` vẫn nhận ra `tuần trước`. Nhưng `gan day` thì **không** được nhận
   là `gần đây`, còn `gan đay` thì có. Danh sách này là cố định, không suy diễn biến thể:
   `vài hôm trước` không được coi là chỉ thời gian.
3. **Trích xuất thực thể gây nhiễu với chữ IN HOA và từ viết tắt.** Lớp `ACRONYM` là
   `\b[A-Z]{2,10}\b`, không có từ điển. Nó lấy đúng `UBND`, `API`, `SQL` — nhưng câu viết hoa
   toàn bộ như `THIS IS URGENT` cũng sinh ra ba thực thể. Tiếng Việt viết tắt nhiều nên chịu
   ảnh hưởng rõ hơn. Các thực thể nhiễu có trọng số thấp nên bị xếp hạng xuống; NER tốt hơn
   (underthesea) nằm trong kế hoạch v0.3, hiện chưa đóng gói.

Ngoài ra: ước lượng token chuyển sang công thức dày hơn khi văn bản có hơn 15% ký tự
non-ASCII, tức là hầu hết câu tiếng Việt — mọi con số đều là ước lượng ±15%. Danh sách stopword
dùng cho tier-2 chỉ có tiếng Anh, ảnh hưởng độ phủ chứ không ảnh hưởng tính đúng đắn.

Phần còn lại của tài liệu bằng tiếng Anh — xem mục **Limitations** ở trên trước khi dùng thật.
