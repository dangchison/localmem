"""Write and read primitives over the ``memories`` table.

Every statement is parameterized. The one value that cannot be bound — the FTS5
``MATCH`` expression — is rebuilt from scratch by :func:`build_match_expression`
rather than interpolated from user input.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from localmem import core_memory, db, dedup, indexer
from localmem.config import validate_workspace

DEFAULT_KIND = "note"
DEFAULT_LIMIT = 5
MIN_LIMIT = 1
MAX_LIMIT = 20

STATUS_ADDED = "added"
STATUS_DUPLICATE_MERGED = "duplicate_merged"

#: Caps on the agent-supplied keyword list, in the same spirit as the indexer's
#: :data:`localmem.indexer.MAX_EXTRACT_CHARS` / :data:`~localmem.indexer.MAX_ENTITIES_PER_MEMORY`:
#: a keyword list is a hint, and a pathological one must not become an index of its own.
MAX_KEYWORDS = 20
MAX_KEYWORD_CHARS = 64

#: The reference weight: a hit in ``content`` scores exactly what it scored in v0.2.2.
CONTENT_COLUMN_WEIGHT = 1.0

#: ``keywords`` is deliberately worth *less* than ``content``. bm25 rewards a hit in a
#: short field, and a keyword list is by far the shortest field in the table, so an
#: unweighted second column systematically out-ranks genuine content matches. 0.35 is
#: measured, not guessed: sweeping 0.2 → 1.0 over a bilingual fixture set, the band
#: ``[0.25, 0.5]`` recalls all 14 keyword-only targets *and* keeps every content query
#: ranked on its content; below it recall drops, at 0.6 and above a one-word keyword
#: list starts beating a paragraph that is genuinely about the term
#: (``docs/design_decisions.md`` §34).
KEYWORDS_COLUMN_WEIGHT = 0.35

#: The weights, in FTS5 column order, ready to bind. They are **bound**, not formatted
#: into the SQL: SQLite accepts ``bm25(memories_fts, ?, ?)`` with the weights as ordinary
#: parameters, so the one ranking rule stays defined once here without this module
#: composing a query string — the habit its docstring warns about.
BM25_COLUMN_WEIGHTS: tuple[float, float] = (CONTENT_COLUMN_WEIGHT, KEYWORDS_COLUMN_WEIGHT)

_TOKEN_SEPARATOR_RE = re.compile(r"\W+", re.UNICODE)

_INSERT_MEMORY_SQL = """
INSERT INTO memories (content, content_hash, workspace, kind, source, session_id, keywords)
VALUES (?, ?, ?, ?, ?, ?, ?)
"""

_SELECT_BY_HASH_SQL = """
SELECT id, seen_count, keywords FROM memories WHERE workspace = ? AND content_hash = ?
"""

_MERGE_DUPLICATE_SQL = """
UPDATE memories
   SET seen_count = seen_count + 1,
       updated_at = datetime('now')
 WHERE workspace = ? AND content_hash = ?
"""

# The keyword-carrying variant of the merge. Kept as a separate statement rather than
# always setting the column, because `mem_au` fires on any UPDATE that *mentions*
# `keywords` — changed value or not — and re-indexing a row on every duplicate merge is a
# cost the far more common keyword-free merge should not pay.
_MERGE_DUPLICATE_WITH_KEYWORDS_SQL = """
UPDATE memories
   SET seen_count = seen_count + 1,
       keywords = ?,
       updated_at = datetime('now')
 WHERE workspace = ? AND content_hash = ?
"""

# The two `?` in bm25() are the column weights; see BM25_COLUMN_WEIGHTS. They bind ahead
# of the MATCH expression, because that is the order they appear in.
_SEARCH_SQL = """
SELECT m.id            AS id,
       m.content       AS content,
       m.workspace     AS workspace,
       m.kind          AS kind,
       m.source        AS source,
       m.seen_count    AS seen_count,
       m.created_at    AS created_at,
       -bm25(memories_fts, ?, ?) AS score
  FROM memories_fts
  JOIN memories AS m ON m.id = memories_fts.rowid
 WHERE memories_fts MATCH ?
"""


class AddResult(NamedTuple):
    """Outcome of :func:`add_memory`."""

    status: str
    id: int
    seen_count: int


@dataclass(frozen=True)
class SearchHit:
    """One ranked search result; ``score`` is descending (higher is better)."""

    id: int
    content: str
    workspace: str
    kind: str
    source: str | None
    seen_count: int
    created_at: str
    score: float


@dataclass(frozen=True)
class Stats:
    """Aggregate counts for the ``stats`` command."""

    db_path: Path
    db_size_bytes: int
    total: int
    per_workspace: tuple[tuple[str, int], ...]
    per_kind: tuple[tuple[str, int], ...]
    total_entities: int
    total_entity_links: int
    total_recalled: int
    queue_depth: int
    core_memory_tokens: int
    core_memory_dropped: int


def add_memory(
    conn: sqlite3.Connection,
    content: str,
    workspace: str,
    kind: str = DEFAULT_KIND,
    source: str | None = None,
    session_id: str | None = None,
    keywords: Sequence[str] | None = None,
) -> AddResult:
    """Store ``content``, merging it into an existing row on an exact-hash match.

    The raw ``content`` is stored verbatim; only its normalized form is hashed.
    Lookup and insert share one ``BEGIN IMMEDIATE`` transaction so concurrent
    writers cannot both insert the same fact.

    ``keywords`` are the caller's alternative wordings for this memory — synonyms,
    the other language's term, an error code, the symptom the user will actually type.
    They are normalized by :func:`normalize_keywords` and indexed as a second FTS5
    column, which is what lets a recall find a memory whose *content* shares no token
    with the query. On a merge the two keyword sets are **unioned**: that is the only
    route by which an already-stored memory ever gains keywords, since generating them
    needs a model and nothing in localmem calls one.

    A new row is indexed by :func:`localmem.indexer.index_memory` in the same
    transaction. A merge does not re-index: the stored content is unchanged, so the
    links already attached to that row stay correct.

    Tier-2 near-duplicate detection then runs over the new row and silently queues any
    pair it finds. It never blocks, never deletes and never changes the returned result.

    Returns:
        ``("added", id, 1)`` for a new row, ``("duplicate_merged", id, n)`` when an
        existing row in the same workspace was bumped.

    Raises:
        ValueError: if ``content`` is blank or ``workspace`` is empty.
    """
    if not content.strip():
        raise ValueError("content is empty; pass the text you want to remember")
    target_workspace = validate_workspace(workspace)
    digest = dedup.content_hash(content)
    keyword_text = normalize_keywords(keywords)

    with db.transaction(conn):
        existing = conn.execute(_SELECT_BY_HASH_SQL, (target_workspace, digest)).fetchone()
        if existing is not None:
            return AddResult(
                STATUS_DUPLICATE_MERGED,
                *_merge_duplicate(conn, target_workspace, digest, keyword_text),
            )
        try:
            cursor = conn.execute(
                _INSERT_MEMORY_SQL,
                (content, digest, target_workspace, kind, source, session_id, keyword_text),
            )
        except sqlite3.IntegrityError:
            # A concurrent writer won the race on UNIQUE(workspace, content_hash).
            return AddResult(
                STATUS_DUPLICATE_MERGED,
                *_merge_duplicate(conn, target_workspace, digest, keyword_text),
            )
        row_id = cursor.lastrowid
        if row_id is None:
            raise RuntimeError("SQLite did not report a row id for the inserted memory")
        indexer.index_memory(conn, row_id, content)
        dedup.enqueue_near_duplicates(
            conn, row_id, content, target_workspace, build_match_expression
        )
        return AddResult(STATUS_ADDED, row_id, 1)


def search_memories(
    conn: sqlite3.Connection,
    query: str,
    workspace: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> list[SearchHit]:
    """Return up to ``limit`` bm25-ranked hits, best first.

    Args:
        conn: an open connection.
        query: free text; FTS5 metacharacters are neutralized before matching.
        workspace: restrict to one workspace; ``None`` searches every workspace.
        limit: number of results, from 1 to 20.

    Raises:
        ValueError: if ``limit`` is out of range or ``workspace`` is empty.
    """
    if not MIN_LIMIT <= limit <= MAX_LIMIT:
        raise ValueError(f"k must be between {MIN_LIMIT} and {MAX_LIMIT}, got {limit}")
    match_expression = build_match_expression(query)
    if not match_expression:
        return []

    sql = _SEARCH_SQL
    params: list[object] = [*BM25_COLUMN_WEIGHTS, match_expression]
    if workspace is not None:
        sql += " AND m.workspace = ?"
        params.append(validate_workspace(workspace))
    sql += " ORDER BY score DESC, m.created_at DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(sql, params).fetchall()
    return [
        SearchHit(
            id=row["id"],
            content=row["content"],
            workspace=row["workspace"],
            kind=row["kind"],
            source=row["source"],
            seen_count=row["seen_count"],
            created_at=row["created_at"],
            score=float(row["score"]),
        )
        for row in rows
    ]


def build_match_expression(query: str) -> str:
    """Turn free text into a safe FTS5 ``MATCH`` expression.

    Splits on non-word boundaries and quotes each token, so operators (``AND``,
    ``NEAR``), wildcards and unbalanced punctuation are matched as literals
    instead of parsed as syntax. Returns ``""`` when nothing searchable remains.

    Adjacent quoted tokens are an implicit ``AND`` in FTS5, so this expression is
    **conjunctive**: every token must appear. That is the right default — it is also why
    :func:`build_or_match_expression` exists for the case where it returns nothing.
    """
    return " ".join(_quoted_tokens(query))


def build_or_match_expression(query: str) -> str:
    """Turn free text into a *disjunctive* FTS5 ``MATCH`` expression: any token matches.

    The same tokens and the same quoting as :func:`build_match_expression` — only the
    joiner differs. Used by the retriever's last-resort pass, never as the first query:
    an OR match over a corpus that holds no answer returns confident-looking noise every
    time, which is why its results are marked and why ``search --context`` drops them.
    """
    return " OR ".join(_quoted_tokens(query))


def normalize_keywords(keywords: Sequence[str] | None) -> str | None:
    """Return ``keywords`` as the single space-separated string the column stores.

    Lowercased, de-duplicated with first-seen order preserved, each entry trimmed to
    :data:`MAX_KEYWORD_CHARS` and the list to :data:`MAX_KEYWORDS`. A multi-word keyword
    is kept as written — FTS5 tokenizes the column anyway, so ``"tải lên"`` indexes as
    the two tokens a query would use.

    Returns ``None``, not ``""``, when nothing survives: the column stays NULL, which is
    what keeps a keyword-free database byte-identical to a v0.2.2 one.
    """
    if not keywords:
        return None
    unique: dict[str, None] = {}
    for keyword in keywords:
        cleaned = " ".join(keyword.lower().split())[:MAX_KEYWORD_CHARS].strip()
        if cleaned:
            unique.setdefault(cleaned, None)
        if len(unique) == MAX_KEYWORDS:
            break
    return " ".join(unique) or None


def _quoted_tokens(query: str) -> list[str]:
    """Return ``query``'s searchable tokens, each quoted as an FTS5 string literal."""
    tokens = [token for token in _TOKEN_SEPARATOR_RE.split(query) if token]
    return ['"{}"'.format(token.replace('"', '""')) for token in tokens]


def collect_stats(conn: sqlite3.Connection, db_path: Path) -> Stats:
    """Return row counts per workspace and kind, the entity graph, queue and core sizes.

    ``core_memory_tokens`` sums each workspace's core memory *after* its own cap, because
    that is the cost a recall actually pays; ``core_memory_dropped`` reports how many
    rows that cap is currently hiding. Since a named workspace also loads the shared
    ``global`` tier, that tier is charged once per workspace that would load it.

    ``total_recalled`` counts recalls, not memories: one row returned five times
    contributes five.
    """
    total_row = conn.execute("SELECT COUNT(*) AS total FROM memories").fetchone()
    recalled_row = conn.execute(
        "SELECT COALESCE(SUM(recalled_count), 0) AS n FROM memories"
    ).fetchone()
    entity_row = conn.execute("SELECT COUNT(*) AS n FROM entities").fetchone()
    link_row = conn.execute("SELECT COUNT(*) AS n FROM memory_entities").fetchone()
    queue_row = conn.execute(
        "SELECT COUNT(*) AS n FROM dedup_queue WHERE status = ?", (dedup.QUEUE_STATUS_PENDING,)
    ).fetchone()
    core_tokens, core_dropped = core_memory.core_memory_totals(conn)
    per_workspace = conn.execute(
        "SELECT workspace, COUNT(*) AS n FROM memories "
        "GROUP BY workspace ORDER BY n DESC, workspace"
    ).fetchall()
    per_kind = conn.execute(
        "SELECT kind, COUNT(*) AS n FROM memories GROUP BY kind ORDER BY n DESC, kind"
    ).fetchall()
    return Stats(
        db_path=db_path,
        db_size_bytes=database_size_bytes(db_path),
        total=int(total_row["total"]),
        per_workspace=tuple((row["workspace"], int(row["n"])) for row in per_workspace),
        per_kind=tuple((row["kind"], int(row["n"])) for row in per_kind),
        total_entities=int(entity_row["n"]),
        total_entity_links=int(link_row["n"]),
        total_recalled=int(recalled_row["n"]),
        queue_depth=int(queue_row["n"]),
        core_memory_tokens=core_tokens,
        core_memory_dropped=core_dropped,
    )


def database_size_bytes(db_path: Path) -> int:
    """Return the on-disk size of the database including its WAL sidecar files."""
    total = 0
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(f"{db_path}{suffix}")
        try:
            total += candidate.stat().st_size
        except OSError:
            continue
    return total


def _merge_duplicate(
    conn: sqlite3.Connection, workspace: str, digest: str, keywords: str | None
) -> tuple[int, int]:
    """Bump the existing row's ``seen_count`` and union ``keywords`` into it.

    The keyword-carrying statement runs only when the union actually adds something, so
    the ordinary "same fact seen twice" merge writes exactly what it wrote in v0.2.2.
    """
    stored = conn.execute(_SELECT_BY_HASH_SQL, (workspace, digest)).fetchone()
    merged = _union_keywords(None if stored is None else stored["keywords"], keywords)
    if merged is None:
        conn.execute(_MERGE_DUPLICATE_SQL, (workspace, digest))
    else:
        conn.execute(_MERGE_DUPLICATE_WITH_KEYWORDS_SQL, (merged, workspace, digest))
    row = conn.execute(_SELECT_BY_HASH_SQL, (workspace, digest)).fetchone()
    if row is None:
        raise RuntimeError(
            f"memory with hash {digest[:12]}… vanished from workspace {workspace!r} mid-merge"
        )
    return int(row["id"]), int(row["seen_count"])


def _union_keywords(stored: str | None, incoming: str | None) -> str | None:
    """Return the union of two normalized keyword strings, or ``None`` for "no change".

    ``None`` means the caller must not write the column at all: either the incoming set
    is empty, or it is already wholly contained in what is stored. Order is the stored
    row's first, so a memory's original keywords keep their position.

    The stored string is split on whitespace, which re-splits a multi-word keyword into
    its words. That is deliberate and lossless at the index level — the column is
    tokenized either way — and it keeps the union a plain set operation.
    """
    if incoming is None:
        return None
    combined = normalize_keywords((stored or "").split() + incoming.split())
    return None if combined == stored else combined
