"""Memory hygiene report — deterministic, zero-token, and strictly read-only.

``localmem audit`` answers "what is rotting in here?" without a model and without
writing a byte. Every statement in this module is a ``SELECT``; the report is a report,
not a gate, so the command always exits 0 and never repairs anything it finds.

Seven sections, each reusing infrastructure that already exists:

1. the tier-2 near-duplicate queue, with the exact commands that drain it;
2. rows that look like core-memory candidates — suggestions only, see
   :data:`PROMOTION_NOTE`;
3. distribution per workspace, kind and age, plus the entity graph and file size;
4. core-memory health against the 400-token cap;
5. dead memories: old and never once recalled;
6. superseded memories, each shown with what replaced it;
7. lesson health — whether the store is actually *learning*, and what the capture gate
   would decide today.

Section 7 is the feedback loop for milestone D. The two capture thresholds were chosen
against a synthetic fixture, because the real database had one row in it; the observed
Jaccard distribution over stored traces is what lets them be re-derived from real data
instead. It counts what a prune would remove and deletes nothing.

Scoping note: ``workspace`` filters *exactly*, with no shared-``global`` fallback. A
recall reads two tiers on purpose (``docs/design_decisions.md`` §24); an inventory of
what is stored where must not blur them.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from localmem import core_memory, dedup, retriever, store
from localmem.config import validate_workspace

#: The kind section 7 is about. ``lesson`` is what v0.4.0 made first-class: the shape a
#: memory takes when it records a stumble rather than a fact.
LESSON_KIND = "lesson"

#: A row stored at least this often is "kept coming back"…
UNREAD_SEEN_COUNT_THRESHOLD = 3

#: …and one recalled fewer than this many times has never been read back. Together they
#: pick out memories the store keeps *writing* and never *uses* — the population that
#: either wants promoting into core memory or merging away as a duplicate.
UNREAD_RECALLED_COUNT_THRESHOLD = 1

#: Lower edges of the similarity histogram, chosen so the capture threshold falls on a
#: boundary: the bucket at :data:`localmem.dedup.CAPTURE_JACCARD_THRESHOLD` and everything
#: above it is exactly what the gate would skip today.
SIMILARITY_BUCKET_EDGES: tuple[float, ...] = (0.0, 0.1, 0.2, 0.25, 0.3, 0.4, 0.7)

#: How many traces the similarity scan examines. Each one costs an FTS5 candidate query
#: plus up to :data:`localmem.dedup.TIER2_MAX_CANDIDATES` token-set comparisons, so the
#: scan is linear — but ``audit`` is run interactively and must not stall on a large
#: store. Past this many traces the report says it sampled rather than implying it counted.
SIMILARITY_SCAN_LIMIT = 200

#: Verbatim in both output modes. Every number in section 7 that mentions recall — stale
#: lessons, the unread rows, and prune eligibility itself — is derived from
#: ``recalled_count``, which stops being written at all when tracking is off. Reporting
#: those as facts would turn "we stopped counting" into "nothing is ever used", which is
#: precisely the wrong conclusion to draw and would justify deleting the store.
TRACKING_DISABLED_NOTE = (
    f"{retriever.NO_TRACKING_ENV_VAR} is set, so recalls are not being counted. Every "
    "figure below that depends on recall — stale lessons, never-read rows, and the "
    "prunable-trace count — is measuring missing data, not disuse. Treat them as unknown "
    "rather than as zero, and do not prune on this evidence."
)

#: Verbatim in both output modes: the point of the section is that the numbers behind the
#: capture gate are provisional and this is how they stop being.
SIMILARITY_NOTE = (
    "This is the distribution the capture gate decides on. "
    f"`localmem add --if-novel` skips a write at Jaccard >= {dedup.CAPTURE_JACCARD_THRESHOLD}, "
    "a threshold chosen against a synthetic fixture because no real corpus existed yet. "
    "Re-derive it from the buckets above once real traces have accumulated: the gate is "
    "working if the mass sits well below the cut and restatements sit well above it."
)

#: Verbatim in both output modes. `gc` deletes no memory unless asked, and a report that
#: counts deletable rows must say how they get deleted — and that they have not been.
PRUNE_NOTE = (
    "Nothing here has been deleted — `localmem gc` never removes a memory unless you ask. "
    "Preview with `localmem gc --prune-traces N --dry-run`, then drop the flag to apply. "
    "Traces named as another memory's replacement are never pruned, however old. "
    "Note that `gc --prune-traces` has no `-w` flag and acts on EVERY workspace, so a "
    "count shown here for one workspace is smaller than what that command would remove; "
    "the dry run reports the real total."
)

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
#: v0.4.0 makes good on the note this replaced — ``localmem promote`` now exists, so the
#: report names it. The warning about re-adding stays, because that is still the mistake
#: a reader makes: tier-1 merges on the hash and leaves the original ``kind`` alone.
PROMOTION_NOTE = (
    "Promote by id: `localmem promote ID --kind core` (or --kind lesson, the default) "
    "rewrites the kind in place. Re-adding the same text with --kind core does not "
    "promote it, because tier-1 merges on the content hash and keeps the original kind."
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

#: Verbatim in both output modes. A superseded row is demoted, never hidden, and that is
#: a design decision worth restating where a reader meets the count.
SUPERSEDED_NOTE = (
    "Superseded memories are kept and stay searchable — their score is multiplied by "
    "0.1, and a recall that returns one attaches the replacement as its first "
    "neighbour, so the correction travels with the memory it corrected. Supersede "
    "links are local to this database and are not carried by `localmem export`."
)

# `m` and `r` are the retracted row and its replacement. The join is what turns "17 rows
# are superseded" into a report a person can act on.
_SUPERSEDED_SQL = """
SELECT m.id         AS id,
       m.workspace  AS workspace,
       m.kind       AS kind,
       m.created_at AS created_at,
       m.content    AS content,
       r.id         AS replacement_id,
       r.workspace  AS replacement_workspace,
       r.content    AS replacement_content
  FROM memories AS m
  JOIN memories AS r ON r.id = m.superseded_by
 WHERE m.superseded_by IS NOT NULL
"""

_SUPERSEDED_ORDER_BY = " ORDER BY m.id ASC LIMIT ?"

_SUPERSEDED_COUNT_SQL = """
SELECT COUNT(*) AS n
  FROM memories AS m
 WHERE m.superseded_by IS NOT NULL
"""

#: The retracted row's workspace, qualified: :data:`_SUPERSEDED_SQL` joins ``memories``
#: to itself, so the bare column name of :data:`_WORKSPACE_FILTER` would be ambiguous.
_SUPERSEDED_WORKSPACE_FILTER = " AND m.workspace = ?"

# Section 7. "Active" means a lesson no later memory has retracted: a superseded lesson is
# still stored and still searchable, but it is no longer what the store believes.
_ACTIVE_LESSONS_SQL = """
SELECT workspace, COUNT(*) AS n
  FROM memories
 WHERE kind = ?
   AND superseded_by IS NULL
"""

_ACTIVE_LESSONS_GROUP_BY = " GROUP BY workspace ORDER BY n DESC, workspace ASC"

_SUPERSEDED_LESSONS_SQL = """
SELECT COUNT(*) AS n
  FROM memories
 WHERE kind = ?
   AND superseded_by IS NOT NULL
"""

_STALE_LESSONS_SQL = """
SELECT id, workspace, created_at, content
  FROM memories
 WHERE kind = ?
   AND superseded_by IS NULL
   AND recalled_count = 0
   AND created_at < datetime('now', ?)
"""

_STALE_LESSONS_ORDER_BY = " ORDER BY created_at ASC, id ASC LIMIT ?"

_STALE_LESSONS_COUNT_SQL = """
SELECT COUNT(*) AS n
  FROM memories
 WHERE kind = ?
   AND superseded_by IS NULL
   AND recalled_count = 0
   AND created_at < datetime('now', ?)
"""

# Stored over and over, never read back. Ordered by the gap itself so the worst offender
# leads, which is also the row most worth promoting or merging.
_UNREAD_SQL = """
SELECT id, workspace, kind, seen_count, recalled_count, content
  FROM memories
 WHERE seen_count >= ?
   AND recalled_count < ?
"""

_UNREAD_ORDER_BY = " ORDER BY seen_count DESC, recalled_count ASC, id ASC LIMIT ?"

_UNREAD_COUNT_SQL = """
SELECT COUNT(*) AS n
  FROM memories
 WHERE seen_count >= ?
   AND recalled_count < ?
"""

_TRACES_SQL = """
SELECT id, content, workspace
  FROM memories
 WHERE kind = ?
"""

_TRACES_ORDER_BY = " ORDER BY id DESC LIMIT ?"

_TRACE_COUNT_SQL = "SELECT COUNT(*) AS n FROM memories WHERE kind = ?"

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
class SupersededMemory:
    """Section 6 — a memory a later one corrected, shown with its replacement.

    Attributes:
        replacement_workspace: where the correction lives. Usually the same workspace;
            ``global`` when a shared lesson retracted a repo-local one, which is the one
            cross-workspace direction :func:`localmem.store._supersede` permits.
    """

    id: int
    workspace: str
    kind: str
    created_at: str
    content: str
    replacement_id: int
    replacement_workspace: str
    replacement_content: str


@dataclass(frozen=True)
class StaleLesson:
    """Section 7 — an active lesson old enough to have been useful, and never recalled."""

    id: int
    workspace: str
    created_at: str
    age_days: float | None
    content: str


@dataclass(frozen=True)
class UnreadMemory:
    """Section 7 — stored repeatedly, never read back.

    High ``seen_count`` means something keeps re-deriving this fact; ``recalled_count`` at
    zero means no recall ever surfaced it. Either the memory is phrased in words nobody
    searches for, or it is a duplicate of one that is — which makes these the best
    candidates for ``promote`` and for the dedup queue alike.
    """

    id: int
    workspace: str
    kind: str
    seen_count: int
    recalled_count: int
    content: str


@dataclass(frozen=True)
class SimilarityBucket:
    """One column of the trace-similarity histogram, ``lower`` inclusive."""

    lower: float
    upper: float
    count: int


@dataclass(frozen=True)
class TraceSimilarity:
    """Section 7 — how alike the stored traces are, as the capture gate would score them.

    Every trace is scored by :func:`localmem.dedup.nearest_neighbour` against the rest of
    its own workspace, which is the identical code path ``add --if-novel`` runs. So
    ``at_or_above_threshold`` is not an estimate of what the gate would have skipped — it
    is the answer, for the traces that are actually stored.

    Attributes:
        total: how many traces exist in scope.
        scanned: how many were scored, capped at :data:`SIMILARITY_SCAN_LIMIT`.
        with_neighbour: how many had any candidate at all. A trace with none scores 0.0
            and is counted in the lowest bucket, so this distinguishes "nothing like it"
            from "nothing proposed".
        threshold: the gate's cut, carried so the report never hardcodes it twice.
        at_or_above_threshold: how many scanned traces the gate would skip today.
        buckets: the histogram, lower edges from :data:`SIMILARITY_BUCKET_EDGES`.
        median: the middle score, or ``None`` when nothing was scanned.
    """

    total: int
    scanned: int
    with_neighbour: int
    threshold: float
    at_or_above_threshold: int
    buckets: tuple[SimilarityBucket, ...]
    median: float | None


@dataclass(frozen=True)
class LessonHealth:
    """Section 7 — is the store learning, and is the capture gate earning its keep?

    ``superseded_lessons`` is a count only, on purpose: section 6 already lists every
    retracted row beside the memory that replaced it, and repeating that here would be a
    second rendering of the same rows. The count exists so the lesson tally adds up —
    active plus superseded is every lesson stored.
    """

    active_total: int
    active_per_workspace: tuple[tuple[str, int], ...]
    superseded_lessons: int
    stale: tuple[StaleLesson, ...]
    stale_total: int
    stale_age_days: int
    unread: tuple[UnreadMemory, ...]
    unread_total: int
    prunable: store.TracePruneReport
    similarity: TraceSimilarity
    tracking_disabled: bool


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
    superseded: tuple[SupersededMemory, ...]
    superseded_total: int
    lessons: LessonHealth


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
        superseded=_superseded_memories(conn, scope),
        superseded_total=_superseded_total(conn, scope),
        lessons=_lesson_health(conn, scope),
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


def _superseded_memories(
    conn: sqlite3.Connection, workspace: str | None
) -> tuple[SupersededMemory, ...]:
    """Return up to :data:`LISTING_LIMIT` retracted rows, oldest id first.

    ``workspace`` filters on the *retracted* row, not on its replacement: the question
    this section answers is "what in here has been corrected?", and the answer belongs to
    the workspace that holds the stale memory.
    """
    sql = _SUPERSEDED_SQL
    params: list[object] = []
    if workspace is not None:
        sql += _SUPERSEDED_WORKSPACE_FILTER
        params.append(workspace)
    params.append(LISTING_LIMIT)
    return tuple(
        SupersededMemory(
            id=int(row["id"]),
            workspace=row["workspace"],
            kind=row["kind"],
            created_at=row["created_at"],
            content=row["content"],
            replacement_id=int(row["replacement_id"]),
            replacement_workspace=row["replacement_workspace"],
            replacement_content=row["replacement_content"],
        )
        for row in conn.execute(sql + _SUPERSEDED_ORDER_BY, params).fetchall()
    )


def _superseded_total(conn: sqlite3.Connection, workspace: str | None) -> int:
    sql = _SUPERSEDED_COUNT_SQL
    params: list[object] = []
    if workspace is not None:
        sql += _SUPERSEDED_WORKSPACE_FILTER
        params.append(workspace)
    return int(conn.execute(sql, params).fetchone()["n"])


def _lesson_health(conn: sqlite3.Connection, workspace: str | None) -> LessonHealth:
    """Assemble section 7. Read-only, like everything else in this module."""
    active = _active_lessons(conn, workspace)
    return LessonHealth(
        active_total=sum(count for _name, count in active),
        active_per_workspace=active,
        superseded_lessons=_scalar(conn, _SUPERSEDED_LESSONS_SQL, [LESSON_KIND], workspace),
        stale=_stale_lessons(conn, workspace),
        stale_total=_scalar(
            conn, _STALE_LESSONS_COUNT_SQL, [LESSON_KIND, _DEAD_MEMORY_CUTOFF], workspace
        ),
        stale_age_days=DEAD_MEMORY_AGE_DAYS,
        unread=_unread_memories(conn, workspace),
        unread_total=_scalar(
            conn,
            _UNREAD_COUNT_SQL,
            [UNREAD_SEEN_COUNT_THRESHOLD, UNREAD_RECALLED_COUNT_THRESHOLD],
            workspace,
        ),
        # Scoped like every other number in this section. `gc --prune-traces` itself has
        # no `-w` and deletes across the whole database, which is why PRUNE_NOTE says so:
        # an inventory that silently counted other workspaces would contradict the
        # similarity figures printed directly beneath it.
        prunable=store.count_prunable_traces(conn, DEAD_MEMORY_AGE_DAYS, workspace),
        similarity=_trace_similarity(conn, workspace),
        tracking_disabled=retriever.tracking_disabled(),
    )


def _scalar(conn: sqlite3.Connection, sql: str, params: list[object], workspace: str | None) -> int:
    """Run a ``COUNT(*) AS n`` statement, optionally narrowed to one workspace."""
    if workspace is not None:
        sql += _WORKSPACE_FILTER
        params = [*params, workspace]
    return int(conn.execute(sql, params).fetchone()["n"])


def _active_lessons(conn: sqlite3.Connection, workspace: str | None) -> tuple[tuple[str, int], ...]:
    sql = _ACTIVE_LESSONS_SQL
    params: list[object] = [LESSON_KIND]
    if workspace is not None:
        sql += _WORKSPACE_FILTER
        params.append(workspace)
    rows = conn.execute(sql + _ACTIVE_LESSONS_GROUP_BY, params).fetchall()
    return tuple((row["workspace"], int(row["n"])) for row in rows)


def _stale_lessons(conn: sqlite3.Connection, workspace: str | None) -> tuple[StaleLesson, ...]:
    sql = _STALE_LESSONS_SQL
    params: list[object] = [LESSON_KIND, _DEAD_MEMORY_CUTOFF]
    if workspace is not None:
        sql += _WORKSPACE_FILTER
        params.append(workspace)
    params.append(LISTING_LIMIT)
    return tuple(
        StaleLesson(
            id=int(row["id"]),
            workspace=row["workspace"],
            created_at=row["created_at"],
            age_days=_age_days(conn, row["created_at"]),
            content=row["content"],
        )
        for row in conn.execute(sql + _STALE_LESSONS_ORDER_BY, params).fetchall()
    )


def _unread_memories(conn: sqlite3.Connection, workspace: str | None) -> tuple[UnreadMemory, ...]:
    sql = _UNREAD_SQL
    params: list[object] = [UNREAD_SEEN_COUNT_THRESHOLD, UNREAD_RECALLED_COUNT_THRESHOLD]
    if workspace is not None:
        sql += _WORKSPACE_FILTER
        params.append(workspace)
    params.append(LISTING_LIMIT)
    return tuple(
        UnreadMemory(
            id=int(row["id"]),
            workspace=row["workspace"],
            kind=row["kind"],
            seen_count=int(row["seen_count"]),
            recalled_count=int(row["recalled_count"]),
            content=row["content"],
        )
        for row in conn.execute(sql + _UNREAD_ORDER_BY, params).fetchall()
    )


def _trace_similarity(conn: sqlite3.Connection, workspace: str | None) -> TraceSimilarity:
    """Score each stored trace against its nearest neighbour and bin the results.

    Deliberately runs the *production* code path — :func:`localmem.dedup.nearest_neighbour`
    with :func:`localmem.store.build_or_match_expression` — rather than an independent
    pairwise sweep. A report that measured similarity differently from the gate it informs
    would be worse than no report: it would justify moving a threshold against numbers the
    threshold never sees.

    Each trace is scored inside its own workspace and excludes itself by id, so a stored
    trace is scored exactly as it would have been at the moment it was written.
    """
    sql = _TRACES_SQL
    params: list[object] = [store.TRACE_KIND]
    if workspace is not None:
        sql += _WORKSPACE_FILTER
        params.append(workspace)
    params.append(SIMILARITY_SCAN_LIMIT)
    rows = conn.execute(sql + _TRACES_ORDER_BY, params).fetchall()

    scores: list[float] = []
    with_neighbour = 0
    for row in rows:
        neighbour = dedup.nearest_neighbour(
            conn,
            row["content"],
            row["workspace"],
            store.build_or_match_expression,
            exclude_id=int(row["id"]),
        )
        if neighbour is None:
            scores.append(0.0)
            continue
        with_neighbour += 1
        scores.append(neighbour.score)

    return TraceSimilarity(
        total=_scalar(conn, _TRACE_COUNT_SQL, [store.TRACE_KIND], workspace),
        scanned=len(scores),
        with_neighbour=with_neighbour,
        threshold=dedup.CAPTURE_JACCARD_THRESHOLD,
        at_or_above_threshold=sum(
            1 for score in scores if score >= dedup.CAPTURE_JACCARD_THRESHOLD
        ),
        buckets=_histogram(scores),
        median=_median(scores),
    )


def _histogram(scores: list[float]) -> tuple[SimilarityBucket, ...]:
    """Bin ``scores`` by :data:`SIMILARITY_BUCKET_EDGES`, each bucket lower-inclusive."""
    edges = [*SIMILARITY_BUCKET_EDGES, 1.0]
    counts = [0] * len(SIMILARITY_BUCKET_EDGES)
    for score in scores:
        counts[_bucket_index(score)] += 1
    return tuple(
        SimilarityBucket(lower=edges[index], upper=edges[index + 1], count=count)
        for index, count in enumerate(counts)
    )


def _bucket_index(score: float) -> int:
    """Return the bucket ``score`` belongs to: the highest edge it reaches.

    Every bucket is ``[lower, upper)`` except the top one, which is closed at 1.0 —
    two identical texts score exactly 1.0 and have to land somewhere.
    """
    for index in reversed(range(len(SIMILARITY_BUCKET_EDGES))):
        if score >= SIMILARITY_BUCKET_EDGES[index]:
            return index
    return 0


def _median(scores: list[float]) -> float | None:
    """Return the middle score, or ``None`` when there is nothing to take a middle of."""
    if not scores:
        return None
    ordered = sorted(scores)
    middle = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return round(ordered[middle], 3)
    return round((ordered[middle - 1] + ordered[middle]) / 2, 3)


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
