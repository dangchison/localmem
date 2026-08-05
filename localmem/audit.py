"""Memory hygiene report — deterministic, zero-token, and strictly read-only.

``localmem audit`` answers "what is rotting in here?" without a model and without
writing a byte. Every statement in this module is a ``SELECT``; the report is a report,
not a gate, so the command always exits 0 and never repairs anything it finds.

Five sections, each reusing infrastructure that already exists:

1. the tier-2 near-duplicate queue, with the exact commands that drain it;
2. rows that look like core-memory candidates — suggestions only, see
   :data:`PROMOTION_NOTE`;
3. distribution per workspace, kind and age, plus the entity graph and file size;
4. core-memory health against the 400-token cap;
5. dead memories: old and never once recalled.

Scoping note: ``workspace`` filters *exactly*, with no shared-``global`` fallback. A
recall reads two tiers on purpose (``docs/design_decisions.md`` §24); an inventory of
what is stored where must not blur them.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from localmem import core_memory, dedup, store
from localmem.config import validate_workspace

#: A row seen this many times is worth a look as core memory.
PROMOTION_SEEN_COUNT_THRESHOLD = 5

#: How many promotion candidates and dead memories the report lists.
LISTING_LIMIT = 10

#: How many pending pairs are shown in full.
QUEUE_SAMPLE_LIMIT = 3

#: A memory older than this and never recalled is reported as dead.
DEAD_MEMORY_AGE_DAYS = 30

#: The same age as a SQLite datetime modifier, built once from the constant above.
_DEAD_MEMORY_CUTOFF = f"-{DEAD_MEMORY_AGE_DAYS} days"

#: Verbatim in both output modes: audit must not imply a command that does not exist.
#: Re-adding with ``--kind core`` does *not* promote — tier-1 merges on the hash and
#: leaves the original ``kind`` alone (verified in M1).
PROMOTION_NOTE = (
    "Promotion is manual in v0.2: re-adding with --kind core does not promote, because "
    "tier-1 merges on the content hash and keeps the original kind. Add the text again "
    "as a core memory in its own words, or wait for the promote tooling in v0.3."
)

#: Verbatim in both output modes. Usage tracking arrived with schema version 2, so a
#: database upgraded from v0.1.0 reports every pre-existing row as never recalled until
#: it is recalled again. Saying so beats letting the number be read as history.
DEAD_MEMORY_NOTE = (
    "Recall tracking starts at schema version 2. Memories stored before this database "
    "was upgraded count as never recalled until the next time they are returned."
)

#: Verbatim in both output modes: the queue only ever drains by human review.
QUEUE_COMMANDS = (
    "localmem dedupe --list",
    "localmem dedupe --review",
    "localmem dedupe --merge ID",
)

_PROMOTION_SQL = """
SELECT id, workspace, kind, seen_count, recalled_count, content
  FROM memories
 WHERE kind != ?
   AND seen_count >= ?
"""

_PROMOTION_ORDER_BY = " ORDER BY recalled_count DESC, seen_count DESC, id ASC LIMIT ?"

_DEAD_SQL = """
SELECT id, workspace, kind, created_at, content
  FROM memories
 WHERE recalled_count = 0
   AND created_at < datetime('now', ?)
"""

_DEAD_ORDER_BY = " ORDER BY created_at ASC, id ASC LIMIT ?"

_DEAD_COUNT_SQL = """
SELECT COUNT(*) AS n
  FROM memories
 WHERE recalled_count = 0
   AND created_at < datetime('now', ?)
"""

_DISTRIBUTION_SQL = """
SELECT workspace,
       kind,
       COUNT(*)       AS n,
       MIN(created_at) AS oldest,
       MAX(created_at) AS newest
  FROM memories
"""

_DISTRIBUTION_GROUP_BY = " GROUP BY workspace, kind ORDER BY workspace ASC, kind ASC"

_WORKSPACE_FILTER = " AND workspace = ?"
_WORKSPACE_ONLY_FILTER = " WHERE workspace = ?"

_AGE_DAYS_SQL = "SELECT julianday('now') - julianday(?) AS age"

_AGE_DIGITS = 1


@dataclass(frozen=True)
class QueueReport:
    """Section 1 — the tier-2 near-duplicate queue.

    Attributes:
        pending: how many pairs are waiting for review.
        per_workspace: pending count per workspace, busiest first.
        oldest_queued_at: when the oldest pending pair was queued, or ``None``.
        oldest_age_days: how long that pair has waited, or ``None``.
        samples: up to :data:`QUEUE_SAMPLE_LIMIT` pairs, oldest queue entry first.
    """

    pending: int
    per_workspace: tuple[tuple[str, int], ...]
    oldest_queued_at: str | None
    oldest_age_days: float | None
    samples: tuple[dedup.DuplicatePair, ...]


@dataclass(frozen=True)
class PromotionCandidate:
    """Section 2 — a row that keeps coming back and may belong in core memory."""

    id: int
    workspace: str
    kind: str
    seen_count: int
    recalled_count: int
    content: str


@dataclass(frozen=True)
class WorkspaceSummary:
    """Section 3 — one workspace's inventory."""

    workspace: str
    total: int
    per_kind: tuple[tuple[str, int], ...]
    oldest_created_at: str
    newest_created_at: str


@dataclass(frozen=True)
class CoreHealth:
    """Section 4 — one workspace's core memory against the cap.

    ``tokens`` is what a recall in this workspace actually loads, so for a named
    workspace it already includes whatever fits from the shared ``global`` tier.
    """

    workspace: str
    tokens: int
    cap: int
    dropped: int


@dataclass(frozen=True)
class DeadMemory:
    """Section 5 — old, and never recalled once since usage tracking began."""

    id: int
    workspace: str
    kind: str
    created_at: str
    age_days: float | None
    content: str


@dataclass(frozen=True)
class Audit:
    """The whole report. Assembled by :func:`run`, rendered by the CLI."""

    workspace: str | None
    db_path: Path
    db_size_bytes: int
    total_memories: int
    total_entities: int
    total_entity_links: int
    queue: QueueReport
    promotion_candidates: tuple[PromotionCandidate, ...]
    workspaces: tuple[WorkspaceSummary, ...]
    core_health: tuple[CoreHealth, ...]
    dead: tuple[DeadMemory, ...]
    dead_total: int


def run(conn: sqlite3.Connection, db_path: Path, workspace: str | None = None) -> Audit:
    """Return the hygiene report for ``workspace`` (``None`` covers every workspace).

    Writes nothing, ever. The connection is used for reads only, and no statement here
    opens a transaction.

    Raises:
        ValueError: if ``workspace`` is empty or whitespace only.
    """
    scope = validate_workspace(workspace) if workspace is not None else None
    stats = store.collect_stats(conn, db_path)
    return Audit(
        workspace=scope,
        db_path=stats.db_path,
        db_size_bytes=stats.db_size_bytes,
        total_memories=stats.total,
        total_entities=stats.total_entities,
        total_entity_links=stats.total_entity_links,
        queue=_queue_report(conn, scope),
        promotion_candidates=_promotion_candidates(conn, scope),
        workspaces=_workspace_summaries(conn, scope),
        core_health=_core_health(conn, scope),
        dead=_dead_memories(conn, scope),
        dead_total=_dead_total(conn, scope),
    )


def _queue_report(conn: sqlite3.Connection, workspace: str | None) -> QueueReport:
    pairs = dedup.pending_pairs(conn, workspace)
    counts: dict[str, int] = {}
    for pair in pairs:
        counts[pair.workspace] = counts.get(pair.workspace, 0) + 1
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    by_age = sorted(pairs, key=lambda pair: (pair.queued_at, pair.queue_id))
    oldest = by_age[0].queued_at if by_age else None
    return QueueReport(
        pending=len(pairs),
        per_workspace=tuple(ordered),
        oldest_queued_at=oldest,
        oldest_age_days=_age_days(conn, oldest),
        samples=tuple(by_age[:QUEUE_SAMPLE_LIMIT]),
    )


def _promotion_candidates(
    conn: sqlite3.Connection, workspace: str | None
) -> tuple[PromotionCandidate, ...]:
    sql = _PROMOTION_SQL
    params: list[object] = [core_memory.CORE_KIND, PROMOTION_SEEN_COUNT_THRESHOLD]
    if workspace is not None:
        sql += _WORKSPACE_FILTER
        params.append(workspace)
    params.append(LISTING_LIMIT)
    return tuple(
        PromotionCandidate(
            id=int(row["id"]),
            workspace=row["workspace"],
            kind=row["kind"],
            seen_count=int(row["seen_count"]),
            recalled_count=int(row["recalled_count"]),
            content=row["content"],
        )
        for row in conn.execute(sql + _PROMOTION_ORDER_BY, params).fetchall()
    )


def _workspace_summaries(
    conn: sqlite3.Connection, workspace: str | None
) -> tuple[WorkspaceSummary, ...]:
    sql = _DISTRIBUTION_SQL
    params: list[object] = []
    if workspace is not None:
        sql += _WORKSPACE_ONLY_FILTER
        params.append(workspace)
    rows = conn.execute(sql + _DISTRIBUTION_GROUP_BY, params).fetchall()

    kinds: dict[str, list[tuple[str, int]]] = {}
    totals: dict[str, int] = {}
    oldest: dict[str, str] = {}
    newest: dict[str, str] = {}
    for row in rows:
        name = row["workspace"]
        kinds.setdefault(name, []).append((row["kind"], int(row["n"])))
        totals[name] = totals.get(name, 0) + int(row["n"])
        oldest[name] = min(oldest.get(name, row["oldest"]), row["oldest"])
        newest[name] = max(newest.get(name, row["newest"]), row["newest"])
    return tuple(
        WorkspaceSummary(
            workspace=name,
            total=totals[name],
            per_kind=tuple(sorted(kinds[name], key=lambda item: (-item[1], item[0]))),
            oldest_created_at=oldest[name],
            newest_created_at=newest[name],
        )
        for name in sorted(totals)
    )


def _core_health(conn: sqlite3.Connection, workspace: str | None) -> tuple[CoreHealth, ...]:
    names = [workspace] if workspace is not None else core_memory.core_workspaces(conn)
    return tuple(_one_core_health(conn, name) for name in names)


def _one_core_health(conn: sqlite3.Connection, workspace: str) -> CoreHealth:
    built = core_memory.build_core_memory(conn, workspace)
    return CoreHealth(
        workspace=workspace,
        tokens=built.tokens,
        cap=core_memory.CORE_MEMORY_TOKEN_CAP,
        dropped=built.dropped,
    )


def _dead_memories(conn: sqlite3.Connection, workspace: str | None) -> tuple[DeadMemory, ...]:
    sql = _DEAD_SQL
    params: list[object] = [_DEAD_MEMORY_CUTOFF]
    if workspace is not None:
        sql += _WORKSPACE_FILTER
        params.append(workspace)
    params.append(LISTING_LIMIT)
    return tuple(
        DeadMemory(
            id=int(row["id"]),
            workspace=row["workspace"],
            kind=row["kind"],
            created_at=row["created_at"],
            age_days=_age_days(conn, row["created_at"]),
            content=row["content"],
        )
        for row in conn.execute(sql + _DEAD_ORDER_BY, params).fetchall()
    )


def _dead_total(conn: sqlite3.Connection, workspace: str | None) -> int:
    sql = _DEAD_COUNT_SQL
    params: list[object] = [_DEAD_MEMORY_CUTOFF]
    if workspace is not None:
        sql += _WORKSPACE_FILTER
        params.append(workspace)
    return int(conn.execute(sql, params).fetchone()["n"])


def _age_days(conn: sqlite3.Connection, timestamp: str | None) -> float | None:
    """Return how many days ago ``timestamp`` was, or ``None`` if it is unusable.

    SQLite does the arithmetic so the report needs no clock of its own; a value the
    database could not have written yields ``NULL`` from ``julianday`` and is reported
    as unknown rather than guessed at.
    """
    if timestamp is None:
        return None
    age = conn.execute(_AGE_DAYS_SQL, (timestamp,)).fetchone()["age"]
    return None if age is None else round(float(age), _AGE_DIGITS)
