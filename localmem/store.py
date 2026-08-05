"""Write and read primitives over the ``memories`` table.

Every statement is parameterized. The one value that cannot be bound — the FTS5
``MATCH`` expression — is rebuilt from scratch by :func:`build_match_expression`
rather than interpolated from user input.
"""

from __future__ import annotations

import re
import sqlite3
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

_TOKEN_SEPARATOR_RE = re.compile(r"\W+", re.UNICODE)

_INSERT_MEMORY_SQL = """
INSERT INTO memories (content, content_hash, workspace, kind, source, session_id)
VALUES (?, ?, ?, ?, ?, ?)
"""

_SELECT_BY_HASH_SQL = """
SELECT id, seen_count FROM memories WHERE workspace = ? AND content_hash = ?
"""

_MERGE_DUPLICATE_SQL = """
UPDATE memories
   SET seen_count = seen_count + 1,
       updated_at = datetime('now')
 WHERE workspace = ? AND content_hash = ?
"""

_SEARCH_SQL = """
SELECT m.id            AS id,
       m.content       AS content,
       m.workspace     AS workspace,
       m.kind          AS kind,
       m.source        AS source,
       m.seen_count    AS seen_count,
       m.created_at    AS created_at,
       -bm25(memories_fts) AS score
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
) -> AddResult:
    """Store ``content``, merging it into an existing row on an exact-hash match.

    The raw ``content`` is stored verbatim; only its normalized form is hashed.
    Lookup and insert share one ``BEGIN IMMEDIATE`` transaction so concurrent
    writers cannot both insert the same fact.

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

    with db.transaction(conn):
        existing = conn.execute(_SELECT_BY_HASH_SQL, (target_workspace, digest)).fetchone()
        if existing is not None:
            return AddResult(
                STATUS_DUPLICATE_MERGED, *_merge_duplicate(conn, target_workspace, digest)
            )
        try:
            cursor = conn.execute(
                _INSERT_MEMORY_SQL,
                (content, digest, target_workspace, kind, source, session_id),
            )
        except sqlite3.IntegrityError:
            # A concurrent writer won the race on UNIQUE(workspace, content_hash).
            return AddResult(
                STATUS_DUPLICATE_MERGED, *_merge_duplicate(conn, target_workspace, digest)
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
    params: list[object] = [match_expression]
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
    """
    tokens = [token for token in _TOKEN_SEPARATOR_RE.split(query) if token]
    return " ".join('"{}"'.format(token.replace('"', '""')) for token in tokens)


def collect_stats(conn: sqlite3.Connection, db_path: Path) -> Stats:
    """Return row counts per workspace and kind, the entity graph, queue and core sizes.

    ``core_memory_tokens`` sums each workspace's core memory *after* its own cap, because
    that is the cost a recall actually pays; ``core_memory_dropped`` reports how many
    rows that cap is currently hiding.
    """
    total_row = conn.execute("SELECT COUNT(*) AS total FROM memories").fetchone()
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


def _merge_duplicate(conn: sqlite3.Connection, workspace: str, digest: str) -> tuple[int, int]:
    conn.execute(_MERGE_DUPLICATE_SQL, (workspace, digest))
    row = conn.execute(_SELECT_BY_HASH_SQL, (workspace, digest)).fetchone()
    if row is None:
        raise RuntimeError(
            f"memory with hash {digest[:12]}… vanished from workspace {workspace!r} mid-merge"
        )
    return int(row["id"]), int(row["seen_count"])
