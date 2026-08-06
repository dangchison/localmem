"""Deduplication: tier-1 exact hashing, tier-2 near-duplicate queueing and review.

Tier 1 normalizes and hashes; normalization only ever feeds the hash — the raw text is
what gets stored.

Tier 2 uses FTS5 purely to *generate candidates* and decides with normalized Jaccard
overlap alone (see ``docs/design_decisions.md``). It only ever inserts into
``dedup_queue``: nothing here deletes a memory without a reviewed, explicit instruction
from the user via :func:`resolve_merge`.

This module never imports :mod:`localmem.store` — ``store`` imports *it* for tier 1 —
so the one FTS5 sanitizer is passed in by the caller instead of being reached for.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import unicodedata
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass

from localmem import db

_BULLET_PREFIX_RE = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)")
_WHITESPACE_RUN_RE = re.compile(r"\s+")
_TOKEN_SEPARATOR_RE = re.compile(r"\W+", re.UNICODE)

# Tier 2. The Jaccard threshold is the one hard number the original spec §6 specifies and the only
# scale-invariant signal available: bm25 magnitudes on a personal-sized corpus are around
# 1e-06, so no bm25 gate can be set meaningfully. FTS5 therefore only narrows the field.
TIER2_JACCARD_THRESHOLD = 0.7
TIER2_MAX_CANDIDATES = 10
TIER2_TOP_TERMS = 5

#: The capture hook's redundancy gate — see :func:`nearest_neighbour`.
#:
#: **This is not :data:`TIER2_JACCARD_THRESHOLD` and must never be "harmonised" with it.**
#: The two answer different questions. Tier 2 asks "are these the same memory?" and may
#: delete one of them, so it demands near-total overlap. This gate asks "has this session
#: already been recorded, in other words?" and only ever declines to write, so it has to
#: fire on a restatement.
#:
#: Measured before it was chosen (``.corp/localmem-v1/gate-d-capture.md``): over a fixture
#: of restatements versus their originals the *minimum* overlap is **0.314**, and over
#: novel traces versus their nearest neighbour the *maximum* is **0.140** — separable,
#: with a gap of 0.174. Scored end to end through :func:`nearest_neighbour`, 0.25 skips
#: 3/3 restatements and wrongly skips 0/8 novel traces; so do 0.20 and 0.30, and 0.25 sits
#: in the middle of that band. At 0.40 the gate stops firing at all, and at tier-2's 0.70
#: it is dead code: restatements in different words share about a third of their tokens,
#: not seven tenths.
#:
#: The fixture is synthetic — the user's real database holds one row — so this number is
#: provisional by construction. ``localmem audit`` reports the observed distribution over
#: stored traces precisely so it can be re-derived from real data later.
CAPTURE_JACCARD_THRESHOLD = 0.25

QUEUE_STATUS_PENDING = "pending"
QUEUE_STATUS_MERGED = "merged"
QUEUE_STATUS_KEPT_BOTH = "kept_both"
QUEUE_RESOLVED_STATUSES = (QUEUE_STATUS_MERGED, QUEUE_STATUS_KEPT_BOTH)

GC_DEFAULT_DAYS = 30

# English only, and deliberately small: it exists to keep the FTS5 candidate query from
# being dominated by filler words, not to model language. It never touches the Jaccard
# score, where dropping words would inflate the overlap of short texts.
_STOPWORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "been", "being", "but", "by", "can",
        "could", "did", "do", "does", "for", "from", "had", "has", "have", "he", "i",
        "if", "in", "is", "it", "its", "may", "might", "not", "of", "on", "or", "shall",
        "she", "should", "that", "the", "these", "they", "this", "those", "to", "was",
        "we", "were", "will", "with", "would", "you",
    }
)  # fmt: skip

_CANDIDATE_SQL = """
SELECT m.id AS id, m.content AS content, m.seen_count AS seen_count
  FROM memories_fts
  JOIN memories AS m ON m.id = memories_fts.rowid
 WHERE memories_fts MATCH ?
   AND m.workspace = ?
   AND m.id != ?
 ORDER BY -bm25(memories_fts) DESC, m.id DESC
 LIMIT ?
"""

_INSERT_QUEUE_SQL = """
INSERT INTO dedup_queue (memory_id, candidate_id, score) VALUES (?, ?, ?)
"""

_SELECT_PAIRS_SQL = """
SELECT q.id         AS queue_id,
       q.score      AS score,
       q.status     AS status,
       q.created_at AS queued_at,
       m.workspace  AS workspace,
       m.id         AS memory_id,
       m.content    AS memory_content,
       m.seen_count AS memory_seen_count,
       m.created_at AS memory_created_at,
       c.id         AS candidate_id,
       c.content    AS candidate_content,
       c.seen_count AS candidate_seen_count,
       c.created_at AS candidate_created_at
  FROM dedup_queue AS q
  JOIN memories AS m ON m.id = q.memory_id
  JOIN memories AS c ON c.id = q.candidate_id
 WHERE q.status = ?
"""

_FOLD_SEEN_COUNT_SQL = """
UPDATE memories
   SET seen_count = seen_count + ?,
       updated_at = datetime('now')
 WHERE id = ?
"""

_SET_STATUS_SQL = "UPDATE dedup_queue SET status = ? WHERE id = ?"

# Run immediately before the one DELETE in this codebase; see `resolve_merge`. Bound as
# (kept, kept, removed): every row that named the removed memory as its replacement is
# handed the memory that survives it, except the kept row itself, which would otherwise be
# left pointing at itself.
_REPOINT_SUPERSEDE_SQL = """
UPDATE memories
   SET superseded_by = CASE WHEN id = ? THEN NULL ELSE ? END
 WHERE superseded_by = ?
"""

# The two placeholders below are bound from QUEUE_RESOLVED_STATUSES, in order; the SQL is
# written out in full rather than assembled, so nothing here can ever carry a value into
# the statement text. Adding a third resolved status means adding a third `?`.
_COUNT_PRUNABLE_SQL = """
SELECT COUNT(*) AS n
  FROM dedup_queue
 WHERE status IN (?, ?)
   AND created_at < datetime('now', ?)
"""

_PRUNE_SQL = """
DELETE FROM dedup_queue
 WHERE status IN (?, ?)
   AND created_at < datetime('now', ?)
"""


@dataclass(frozen=True)
class PairMember:
    """One side of a queued near-duplicate pair."""

    id: int
    content: str
    seen_count: int
    created_at: str


@dataclass(frozen=True)
class DuplicatePair:
    """A pending ``dedup_queue`` row resolved into its two memories.

    ``newer`` and ``older`` are ordered by ``created_at`` then id, so a merge always
    knows which row survives without depending on which side was inserted first.
    """

    queue_id: int
    score: float
    workspace: str
    queued_at: str
    newer: PairMember
    older: PairMember


@dataclass(frozen=True)
class Neighbour:
    """The closest existing memory to some text, by :func:`jaccard` overlap.

    Returned by :func:`nearest_neighbour`. ``score`` is the overlap itself, carried out
    so callers can apply their own threshold — and so ``audit`` can report the raw
    distribution instead of a yes/no.
    """

    id: int
    content: str
    seen_count: int
    score: float


@dataclass(frozen=True)
class Resolution:
    """The outcome of resolving one queued pair."""

    queue_id: int
    status: str
    kept_id: int
    removed_id: int | None
    seen_count: int


def normalize(text: str) -> str:
    """Return the canonical form of ``text`` used for exact-duplicate detection.

    Applied in order: NFC normalization (so composed and decomposed Vietnamese
    hash identically), markdown bullet prefix removal per line, lowercasing,
    whitespace-run collapsing, and outer stripping.
    """
    composed = unicodedata.normalize("NFC", text)
    unbulleted = "\n".join(_BULLET_PREFIX_RE.sub("", line) for line in composed.splitlines())
    return _WHITESPACE_RUN_RE.sub(" ", unbulleted.lower()).strip()


def content_hash(text: str) -> str:
    """Return the sha256 hex digest of the normalized form of ``text``."""
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()


def tokenize(text: str) -> set[str]:
    r"""Return the distinct tokens of :func:`normalize`'d ``text``, split on ``\W+``."""
    return {token for token in _TOKEN_SEPARATOR_RE.split(normalize(text)) if token}


def jaccard(left: str, right: str) -> float:
    """Return the normalized token overlap of two texts, in ``[0.0, 1.0]``.

    No stopword filtering: dropping filler words inflates the overlap of short texts,
    which is exactly the length at which a false merge is most likely.
    """
    left_tokens = tokenize(left)
    right_tokens = tokenize(right)
    union = left_tokens | right_tokens
    if not union:
        return 0.0
    return len(left_tokens & right_tokens) / len(union)


def top_terms(content: str, limit: int = TIER2_TOP_TERMS) -> list[str]:
    """Return the ``limit`` most frequent non-stopword tokens of ``content``.

    Ties break on first occurrence, so the same content always produces the same terms.
    """
    tokens = [
        token
        for token in _TOKEN_SEPARATOR_RE.split(normalize(content))
        if token and token not in _STOPWORDS
    ]
    counts = Counter(tokens)
    first_seen: dict[str, int] = {}
    for position, token in enumerate(tokens):
        first_seen.setdefault(token, position)
    ranked = sorted(counts, key=lambda token: (-counts[token], first_seen[token]))
    return ranked[:limit]


def enqueue_near_duplicates(
    conn: sqlite3.Connection,
    memory_id: int,
    content: str,
    workspace: str,
    build_match_expression: Callable[[str], str],
) -> list[int]:
    """Queue every existing memory that is a near-duplicate of the row just inserted.

    Runs inside the caller's transaction, right after the insert and its indexing, so the
    new row is already visible through the FTS5 trigger and is excluded by id.

    FTS5 narrows the field to at most :data:`TIER2_MAX_CANDIDATES` rows sharing the new
    content's top terms; the decision is then :func:`jaccard` ≥
    :data:`TIER2_JACCARD_THRESHOLD` alone.

    Args:
        conn: an open connection with a transaction already in progress.
        memory_id: the row that was just inserted.
        content: its raw text.
        workspace: the workspace to stay inside.
        build_match_expression: the FTS5 sanitizer, injected by the caller so this
            module does not have to import :mod:`localmem.store` back.

    Returns:
        The ids of the existing memories queued against ``memory_id``, possibly empty.
        The caller's ``AddResult`` never depends on this — a near-duplicate is recorded
        for review, never acted on.
    """
    terms = top_terms(content)
    if not terms:
        return []
    match_expression = build_match_expression(" ".join(terms))
    if not match_expression:
        return []

    queued: list[int] = []
    try:
        for candidate in _scored_candidates(conn, content, workspace, memory_id, match_expression):
            if candidate.score < TIER2_JACCARD_THRESHOLD:
                continue
            conn.execute(_INSERT_QUEUE_SQL, (memory_id, candidate.id, candidate.score))
            queued.append(candidate.id)
    except sqlite3.Error:
        # A tier-2 failure must never cost the user the memory they just wrote, so the
        # queue write is contained here. Anything that is not a database error is a bug
        # and propagates untouched.
        return queued
    return queued


def nearest_neighbour(
    conn: sqlite3.Connection,
    content: str,
    workspace: str,
    build_or_match_expression: Callable[[str], str],
    exclude_id: int = 0,
) -> Neighbour | None:
    """Return the existing memory closest to ``content``, or ``None`` if there is none.

    This is the read-only half of near-duplicate detection: it decides nothing and writes
    nothing. Two callers apply their own threshold to the score — the capture gate behind
    ``localmem add --if-novel`` (:data:`CAPTURE_JACCARD_THRESHOLD`) and ``localmem audit``,
    which reports the distribution rather than thresholding at all.

    **The candidate query is disjunctive, and that is the whole point.**
    :func:`enqueue_near_duplicates` builds a *conjunctive* expression, where every one of
    the top terms must appear; that is adequate at tier 2's 0.7, because a pair which
    overlaps that heavily shares nearly all its words anyway. It is useless here.
    Measured on the milestone-D fixture: a restatement shares only two or three of its
    five top terms with the original — that is precisely what makes it a restatement
    rather than a copy — so the conjunctive query returned **zero candidates for 3 of 3
    restatements** and the gate could never fire, at any threshold. The disjunctive
    expression returns them and reproduces the pairwise scores (min 0.314 over
    restatements, max 0.121 over novel traces).

    The lesson generalises: candidate *recall* has to match how loose the decision
    threshold is. A low threshold with a high-precision candidate query is dead code.

    Args:
        conn: an open connection; only ``SELECT`` runs here.
        content: the text to find a neighbour for.
        workspace: the workspace to stay inside.
        build_or_match_expression: the *disjunctive* FTS5 sanitizer, injected by the
            caller so this module does not have to import :mod:`localmem.store` back.
        exclude_id: a row to leave out — the row itself, when it is already stored. The
            default of ``0`` excludes nothing, since SQLite row ids start at 1.

    Returns:
        The highest-scoring candidate, or ``None`` when FTS5 proposes nobody. A database
        error is reported the same way: this function is only ever consulted for advice,
        and no caller should lose a write because the advice was unavailable.
    """
    terms = top_terms(content)
    if not terms:
        return None
    match_expression = build_or_match_expression(" ".join(terms))
    if not match_expression:
        return None
    try:
        candidates = _scored_candidates(conn, content, workspace, exclude_id, match_expression)
    except sqlite3.Error:
        return None
    return max(candidates, key=lambda candidate: (candidate.score, -candidate.id), default=None)


def _scored_candidates(
    conn: sqlite3.Connection,
    content: str,
    workspace: str,
    exclude_id: int,
    match_expression: str,
) -> list[Neighbour]:
    """Return every FTS5 candidate for ``content``, each scored by :func:`jaccard`.

    The one place a stored memory is compared against new text. Both near-duplicate
    tiers and the audit report go through it, so the overlap is computed by exactly one
    piece of code; only the match expression and the threshold differ between callers.
    """
    rows = conn.execute(
        _CANDIDATE_SQL, (match_expression, workspace, exclude_id, TIER2_MAX_CANDIDATES)
    ).fetchall()
    return [
        Neighbour(
            id=int(row["id"]),
            content=row["content"],
            seen_count=int(row["seen_count"]),
            score=jaccard(content, row["content"]),
        )
        for row in rows
    ]


def pending_pairs(conn: sqlite3.Connection, workspace: str | None = None) -> list[DuplicatePair]:
    """Return the pending near-duplicate pairs, oldest queue entry first."""
    sql = _SELECT_PAIRS_SQL
    params: list[object] = [QUEUE_STATUS_PENDING]
    if workspace is not None:
        sql += " AND m.workspace = ?"
        params.append(workspace)
    sql += " ORDER BY q.id ASC"
    return [_to_pair(row) for row in conn.execute(sql, params).fetchall()]


def resolve_merge(conn: sqlite3.Connection, queue_id: int) -> Resolution:
    """Keep the newer memory of pair ``queue_id``, fold ``seen_count``, drop the older.

    This is the only path in localmem that deletes a memory, and it runs only on a pair a
    human has just reviewed — it is not the automatic near-duplicate deletion the original spec §12
    forbids.

    Deleting the older row also removes, by the schema's ``ON DELETE CASCADE``, its
    ``memory_entities`` links **and every ``dedup_queue`` row that referenced it** —
    including this one. The pair is therefore resolved by disappearing from the queue
    rather than by leaving a ``merged`` row behind; see ``docs/design_decisions.md``.

    ``memories.superseded_by`` is the one reference that does **not** cascade: it is
    declared ``REFERENCES memories(id)`` with no ``ON DELETE`` clause, and foreign keys
    are on, so deleting a row some other row named as its replacement fails outright with
    ``FOREIGN KEY constraint failed`` — measured, not assumed. The supersede links are
    therefore moved onto the surviving twin first, which is also the better answer than
    dropping them: the pair was judged to be the same memory, so the kept row is the same
    correction. See ``docs/design_decisions.md`` §42.

    Raises:
        ValueError: if ``queue_id`` is unknown or has already been resolved.
    """
    with db.transaction(conn):
        pair = _load_pending(conn, queue_id)
        conn.execute(_FOLD_SEEN_COUNT_SQL, (pair.older.seen_count, pair.newer.id))
        conn.execute(_SET_STATUS_SQL, (QUEUE_STATUS_MERGED, queue_id))
        conn.execute(_REPOINT_SUPERSEDE_SQL, (pair.newer.id, pair.newer.id, pair.older.id))
        conn.execute("DELETE FROM memories WHERE id = ?", (pair.older.id,))
        row = conn.execute(
            "SELECT seen_count FROM memories WHERE id = ?", (pair.newer.id,)
        ).fetchone()
    return Resolution(
        queue_id=queue_id,
        status=QUEUE_STATUS_MERGED,
        kept_id=pair.newer.id,
        removed_id=pair.older.id,
        seen_count=int(row["seen_count"]),
    )


def resolve_keep_both(conn: sqlite3.Connection, queue_id: int) -> Resolution:
    """Mark pair ``queue_id`` reviewed, keeping both memories exactly as they are.

    Raises:
        ValueError: if ``queue_id`` is unknown or has already been resolved.
    """
    with db.transaction(conn):
        pair = _load_pending(conn, queue_id)
        conn.execute(_SET_STATUS_SQL, (QUEUE_STATUS_KEPT_BOTH, queue_id))
    return Resolution(
        queue_id=queue_id,
        status=QUEUE_STATUS_KEPT_BOTH,
        kept_id=pair.newer.id,
        removed_id=None,
        seen_count=pair.newer.seen_count,
    )


def count_prunable(conn: sqlite3.Connection, days: int = GC_DEFAULT_DAYS) -> int:
    """Return how many resolved queue rows are older than ``days``."""
    row = conn.execute(_COUNT_PRUNABLE_SQL, (*QUEUE_RESOLVED_STATUSES, _days_ago(days))).fetchone()
    return int(row["n"])


def prune_resolved(conn: sqlite3.Connection, days: int = GC_DEFAULT_DAYS) -> int:
    """Delete resolved queue rows older than ``days`` and return how many went.

    Pending pairs are never touched: an unreviewed near-duplicate is not garbage.
    """
    with db.transaction(conn):
        cursor = conn.execute(_PRUNE_SQL, (*QUEUE_RESOLVED_STATUSES, _days_ago(days)))
        pruned = cursor.rowcount
    return max(pruned, 0)


def vacuum(conn: sqlite3.Connection) -> None:
    """Rebuild the database file, reclaiming the space pruned rows left behind.

    Must run outside a transaction — SQLite refuses ``VACUUM`` inside one.
    """
    conn.execute("VACUUM")


def _days_ago(days: int) -> str:
    """Return the SQLite datetime modifier for ``days`` in the past."""
    return f"-{days} days"


def _load_pending(conn: sqlite3.Connection, queue_id: int) -> DuplicatePair:
    row = conn.execute(
        _SELECT_PAIRS_SQL + " AND q.id = ?", (QUEUE_STATUS_PENDING, queue_id)
    ).fetchone()
    if row is None:
        raise ValueError(
            f"no pending near-duplicate pair with id {queue_id}; "
            "run `localmem dedupe --list` to see what is still open"
        )
    return _to_pair(row)


def _to_pair(row: sqlite3.Row) -> DuplicatePair:
    memory = PairMember(
        id=int(row["memory_id"]),
        content=row["memory_content"],
        seen_count=int(row["memory_seen_count"]),
        created_at=row["memory_created_at"],
    )
    candidate = PairMember(
        id=int(row["candidate_id"]),
        content=row["candidate_content"],
        seen_count=int(row["candidate_seen_count"]),
        created_at=row["candidate_created_at"],
    )
    first, second = sorted([memory, candidate], key=lambda member: (member.created_at, member.id))
    return DuplicatePair(
        queue_id=int(row["queue_id"]),
        score=float(row["score"]),
        workspace=row["workspace"],
        queued_at=row["queued_at"],
        newer=second,
        older=first,
    )
