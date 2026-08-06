"""Dual-view retrieval: FTS5 lexical + entity relational, fused, closed over evidence.

Implements the original spec §5. The pipeline is pure code — no model, no network, no LLM:

1. query profile — the FTS5 ``MATCH`` expression, the query's entities, its recency cue;
2. **view A**, lexical: bm25 over ``memories_fts``, workspace-filtered, top 20;
   a *named* workspace also reads the shared ``global`` tier (§24 of the design
   decisions); ``None`` ("every workspace") and ``"global"`` itself are unchanged;
3. **view B**, relational: memories sharing an entity with the query, scored by Σweight;
   with both views empty, view A is retried as a disjunction and its rows are marked
   ``from_fallback``;
4. fuse — each view min-max normalized independently, then weighted;
5. recency and ``seen_count`` boosts, then the supersede penalty;
6. evidence closure — up to two neighbors per result, a superseded row's replacement
   first;
7. the workspace's capped core memory.

Anything that depends on the clock takes an explicit ``now``, so tests pin time by
passing a value rather than by patching the standard library.
"""

from __future__ import annotations

import math
import os
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone

from localmem import core_memory, indexer, store
from localmem.config import validate_workspace

#: Set this to any non-empty value and a recall performs no write at all. The check is
#: on emptiness, not on truthiness — ``LOCALMEM_NO_TRACKING=0`` also disables tracking,
#: because an env var that has been set at all is a user who wants it off.
NO_TRACKING_ENV_VAR = "LOCALMEM_NO_TRACKING"

CANDIDATE_LIMIT = 20
MAX_NEIGHBORS = 2

#: The one number the supersede demotion is built from, used for **two** things in
#: :func:`_fuse`: a superseded row's score is multiplied by it, and — when the row's
#: replacement is also among the candidates — capped at ``replacement * penalty``.
#:
#: A tenth is a demotion, not a filter: the row stays in the candidate set, stays
#: rankable and stays findable, because "what did we think was wrong before?" is a
#: question worth answering. Whatever still ranks after it carries its correction out as
#: its first neighbour (:func:`_replacement_neighbor`).
#:
#: **The multiply alone does not reorder the pair, and that is why the cap exists.**
#: :func:`_min_max` maps the weakest candidate of a view to exactly 0.0, so when a query
#: matches only a retracted row and its correction and the retracted row is the better
#: lexical match, the correction normalizes to 0.0 and keeps nothing but its boosts.
#: Measured on that exact pair — multiply only, no cap, scores as ``[retraction,
#: correction]``::
#:
#:     penalty | both 60 days old        | retraction 60d, correction 1d
#:     --------|-------------------------|-------------------------------
#:     0.1     | wrong first [.0612 .0125] | wrong first [.0612 .0489]
#:     0.05    | wrong first [.0306 .0125] | fix first   [.0489 .0306]
#:     0.02    | fix first   [.0125 .0122] | fix first   [.0489 .0122]
#:
#: No constant is a fix. At 0.1 the retraction wins both cases; 0.05 only survives when
#: the correction is fresh enough for the recency term to carry it; 0.02 wins the harder
#: case by 0.0003, which is luck, not a guarantee — and once both memories are old
#: enough for recency to vanish from both, the correction sits at 0.0 and *every*
#: positive constant leaves the retraction above it. The cap removes the constant from
#: the question entirely (``docs/design_decisions.md`` §43).
SUPERSEDED_SCORE_PENALTY = 0.1

# The original spec §5 step 4: the relational view leads once the query itself yields an entity
# that some memory actually carries.
LEXICAL_WEIGHT = 0.6
RELATIONAL_WEIGHT = 0.4
LEXICAL_WEIGHT_ON_ENTITY_HIT = 0.4
RELATIONAL_WEIGHT_ON_ENTITY_HIT = 0.6

RECENCY_WEIGHT = 0.05
# The original spec §5 step 1: a query that asks for recent things gets a much heavier recency term.
# The decay curve itself is unchanged — only how much of it is added to the fused score.
RECENCY_CUE_WEIGHT = 0.25
RECENCY_HALF_LIFE_DAYS = 30.0
SEEN_COUNT_WEIGHT = 0.02

# Phrases that mean "prefer new things". Written with diacritics; matching folds them, so
# `tuần trước` and `tuan truoc` both count. Every entry is a whole-word sequence.
RECENCY_CUES = (
    "recent",
    "recently",
    "latest",
    "newest",
    "last week",
    "last month",
    "yesterday",
    "today",
    "hôm qua",
    "hôm nay",
    "tuần trước",
    "tháng trước",
    "gần đây",
    "mới nhất",
)

_CUE_TOKEN_SEPARATOR_RE = re.compile(r"\W+", re.UNICODE)
_COMBINING_MARK_CATEGORY = "Mn"


def _fold(text: str) -> str:
    """Return ``text`` lowercased and stripped of combining marks.

    Defined here rather than with the other private helpers because
    :data:`_CUE_TOKEN_SEQUENCES` below is derived from it at import time.

    ``đ`` has no canonical decomposition, so it survives folding — the same blind spot
    ``remove_diacritics 2`` has, documented in ``docs/design_decisions.md`` §2.
    """
    decomposed = unicodedata.normalize("NFD", text.lower())
    return "".join(
        character
        for character in decomposed
        if unicodedata.category(character) != _COMBINING_MARK_CATEGORY
    )


# Each cue as a folded token sequence, longest first so `last week` wins over any single
# token it contains. Comparing sequences rather than substrings is what makes matching
# whole-word: `todayish` tokenizes to one token that is not `today`.
_CUE_TOKEN_SEQUENCES: tuple[tuple[str, ...], ...] = tuple(
    sorted(
        {
            tuple(token for token in _CUE_TOKEN_SEPARATOR_RE.split(_fold(cue)) if token)
            for cue in RECENCY_CUES
        },
        key=len,
        reverse=True,
    )
)

# SQLite's datetime('now') emits exactly this, in UTC, with no offset. Parsing it with
# a fixed format rather than fromisoformat keeps us from silently accepting shapes the
# database never writes.
SQLITE_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"
SECONDS_PER_DAY = 86400.0

EMPTY_MESSAGE = "no memories yet — memories will accumulate as you work"

# Both views select the same columns so :func:`_read_view` can read either one. The list
# is spelled out per query rather than interpolated: composing SQL from Python fragments
# is the habit that hides an injection, even when the fragment is a constant.
# The two `?` in bm25() are the column weights, bound from `store.BM25_COLUMN_WEIGHTS`
# ahead of the MATCH expression. Binding them keeps the one ranking rule defined in one
# place without either module building a query string out of Python values.
_LEXICAL_SQL = """
SELECT m.id         AS id,
       m.content    AS content,
       m.workspace  AS workspace,
       m.kind       AS kind,
       m.source     AS source,
       m.seen_count AS seen_count,
       m.session_id AS session_id,
       m.created_at AS created_at,
       m.superseded_by AS superseded_by,
       -bm25(memories_fts, ?, ?) AS score
  FROM memories_fts
  JOIN memories AS m ON m.id = memories_fts.rowid
 WHERE memories_fts MATCH ?
"""

_LEXICAL_ORDER_BY = " ORDER BY score DESC, m.created_at DESC, m.id DESC LIMIT ?"

# Pure recency mode: the query was nothing but a cue, so newest-first *is* the ranking.
_RECENT_SQL = """
SELECT m.id         AS id,
       m.content    AS content,
       m.workspace  AS workspace,
       m.kind       AS kind,
       m.source     AS source,
       m.seen_count AS seen_count,
       m.session_id AS session_id,
       m.created_at AS created_at,
       m.superseded_by AS superseded_by
  FROM memories AS m
"""

_RECENT_ORDER_BY = " ORDER BY m.created_at DESC, m.id DESC LIMIT ?"

# ``{placeholders}`` is filled with one ``?`` per query entity — never with a name.
_RELATIONAL_SQL = """
SELECT m.id         AS id,
       m.content    AS content,
       m.workspace  AS workspace,
       m.kind       AS kind,
       m.source     AS source,
       m.seen_count AS seen_count,
       m.session_id AS session_id,
       m.created_at AS created_at,
       m.superseded_by AS superseded_by,
       SUM(me.weight) AS score
  FROM memory_entities AS me
  JOIN memories AS m ON m.id = me.memory_id
  JOIN entities AS e ON e.id = me.entity_id
 WHERE e.norm_name IN ({placeholders})
"""

_RELATIONAL_ORDER_BY = " GROUP BY m.id ORDER BY score DESC, m.id DESC LIMIT ?"

_SESSION_NEIGHBOR_SQL = """
SELECT m.id AS id, m.content AS content
  FROM memories AS m
 WHERE m.session_id = ?
   AND m.id != ?
   AND m.workspace = ?
"""

_SESSION_NEIGHBOR_ORDER_BY = " ORDER BY ABS(m.id - ?) ASC, m.id ASC LIMIT ?"

_ENTITY_NEIGHBOR_SQL = """
SELECT m.id AS id, m.content AS content, COUNT(*) AS shared
  FROM memory_entities AS me1
  JOIN memory_entities AS me2 ON me2.entity_id = me1.entity_id
  JOIN memories AS m ON m.id = me2.memory_id
 WHERE me1.memory_id = ?
   AND me2.memory_id != ?
   AND m.workspace = ?
"""

_ENTITY_NEIGHBOR_ORDER_BY = " GROUP BY m.id ORDER BY shared DESC, m.id ASC LIMIT ?"

# The replacement of a superseded hit, fetched by id. Deliberately **not** workspace
# filtered, unlike the two neighbor queries above: `store._supersede` only ever accepts a
# replacement the target's own workspace can already see, so the row this returns is
# always one a recall in that workspace could have returned by itself.
_REPLACEMENT_NEIGHBOR_SQL = (
    "SELECT m.id AS id, m.content AS content FROM memories AS m WHERE m.id = ?"
)

# The two workspace predicates a view can carry. Both bind every value; neither is ever
# built from a workspace name. Evidence closure keeps using the exact form against the
# result row's *own* workspace, so a global hit gathers global neighbors and a repo hit
# gathers repo neighbors — the tiers stay legible in the output.
_WORKSPACE_EXACT = "m.workspace = ?"
_WORKSPACE_WITH_SHARED = "m.workspace IN (?, ?)"

# The original spec §4's `workspace` is on every result, so an agent can see which tier answered.
# Tracking is best-effort and deliberately writes nothing else; see `_record_recall`.
_RECORD_RECALL_SQL = """
UPDATE memories
   SET recalled_count = recalled_count + 1,
       last_recalled_at = datetime('now')
 WHERE id IN ({placeholders})
"""


@dataclass(frozen=True)
class Neighbor:
    """A supporting memory attached to a result; carries only what §4 returns."""

    id: int
    content: str


@dataclass(frozen=True)
class RetrievedMemory:
    """One fused, ranked result with its provenance and evidence closure.

    Attributes:
        score: the fused score including the recency and ``seen_count`` boosts.
        lexical_score: the normalized view A score, or ``None`` if view A missed it.
        relational_score: the normalized view B score, or ``None`` if view B missed it.
        neighbors: up to :data:`MAX_NEIGHBORS` supporting memories, never a row that is
            itself in ``results``.
        from_fallback: the row was found by the disjunctive last-resort pass, after both
            views came back empty. A weaker claim than an ordinary hit — see
            :func:`retrieve` — and the reason ``search --context`` drops these by default.
    """

    id: int
    content: str
    workspace: str
    kind: str
    source: str | None
    seen_count: int
    created_at: str
    score: float
    lexical_score: float | None
    relational_score: float | None
    neighbors: tuple[Neighbor, ...]
    from_fallback: bool


@dataclass(frozen=True)
class RetrievalResult:
    """Everything one recall returns; shaped to satisfy the original spec §4 without rework.

    Attributes:
        results: the ranked memories, best first.
        core_memory: the workspace's always-load tier, possibly empty.
        core_memory_tokens: estimated token cost of ``core_memory``.
        core_memory_dropped: rows discarded to fit the core-memory cap.
        message: the friendly empty-result notice, or ``None`` when there are results.
    """

    results: tuple[RetrievedMemory, ...]
    core_memory: str
    core_memory_tokens: int
    core_memory_dropped: int
    message: str | None


@dataclass(frozen=True)
class _Row:
    """A candidate memory as read from either view, before scoring."""

    id: int
    content: str
    workspace: str
    kind: str
    source: str | None
    seen_count: int
    session_id: str | None
    created_at: str
    superseded_by: int | None


@dataclass(frozen=True)
class _View:
    """One view's candidates: the rows themselves and their raw, unnormalized scores."""

    memories: dict[int, _Row]
    scores: dict[int, float]


@dataclass(frozen=True)
class _Scored:
    """A candidate after fusion, still carrying the columns the public type omits."""

    row: _Row
    score: float
    lexical: float | None
    relational: float | None


def retrieve(
    conn: sqlite3.Connection,
    query: str,
    workspace: str | None = None,
    k: int = store.DEFAULT_LIMIT,
    now: datetime | None = None,
) -> RetrievalResult:
    """Return the ``k`` best memories for ``query``, fused across both views.

    Args:
        conn: an open connection.
        query: free text; FTS5 metacharacters are neutralized before matching.
        workspace: restrict to one workspace, which also reads the shared ``global``
            tier; ``None`` spans every workspace.
        k: number of results, from 1 to 20.
        now: the instant recency is measured against; defaults to the current UTC time.

    When **both** views come back empty the lexical query is retried disjunctively (see
    :func:`_fallback_view`) and every result of that pass is marked ``from_fallback``.
    The gate is deliberately "both empty", not "few results": as long as either view
    answered, the conjunctive ranking is the better one and is left alone.

    A recall is *almost* read-only: the ids it returns get their ``recalled_count``
    bumped by :func:`_record_recall`, best-effort and never able to fail the call.
    Setting :data:`NO_TRACKING_ENV_VAR` removes that write and makes it read-only.

    Raises:
        ValueError: if ``k`` is out of range or ``workspace`` is empty.
    """
    if not store.MIN_LIMIT <= k <= store.MAX_LIMIT:
        raise ValueError(f"k must be between {store.MIN_LIMIT} and {store.MAX_LIMIT}, got {k}")
    scope = validate_workspace(workspace) if workspace is not None else None
    moment = now if now is not None else datetime.now(timezone.utc)

    lexical_text, cued = strip_recency_cues(query)
    recency_weight = RECENCY_CUE_WEIGHT if cued else RECENCY_WEIGHT
    match_expression = store.build_match_expression(lexical_text)

    fallback = False
    if cued and not match_expression:
        # The query was nothing but a cue — "today", "tuần trước". There is no lexical or
        # relational question to ask, so the recency term becomes the whole ranking.
        top = _recent_view(conn, scope, k, moment)
    else:
        lexical = (
            _lexical_view(conn, match_expression, scope) if match_expression else _View({}, {})
        )
        # Entities come from the *full* query, not the stripped one: cue words cannot form
        # entities under any of the indexer's classes, so stripping them would only risk
        # two divergent query texts flowing through the pipeline.
        relational = _relational_view(conn, query_entities(query), scope)
        if not lexical.memories and not relational.scores:
            lexical, fallback = _fallback_view(conn, lexical_text, scope)
        top = _fuse(lexical, relational, moment, recency_weight, scope)[:k]

    selected = {scored.row.id for scored in top}
    results = tuple(
        RetrievedMemory(
            id=scored.row.id,
            content=scored.row.content,
            workspace=scored.row.workspace,
            kind=scored.row.kind,
            source=scored.row.source,
            seen_count=scored.row.seen_count,
            created_at=scored.row.created_at,
            score=scored.score,
            lexical_score=scored.lexical,
            relational_score=scored.relational,
            neighbors=_neighbors(conn, scored.row, selected),
            from_fallback=fallback,
        )
        for scored in top
    )
    core = core_memory.build_core_memory(conn, scope)
    _record_recall(conn, [memory.id for memory in results])
    return RetrievalResult(
        results=results,
        core_memory=core.text,
        core_memory_tokens=core.tokens,
        core_memory_dropped=core.dropped,
        message=None if results else EMPTY_MESSAGE,
    )


def tracking_disabled() -> bool:
    """Return whether :data:`NO_TRACKING_ENV_VAR` is set to a non-empty value.

    Read on every recall rather than cached at import: a long-lived MCP server and a
    test suite both change the environment after this module is imported.
    """
    return bool(os.environ.get(NO_TRACKING_ENV_VAR, ""))


def query_entities(query: str) -> list[str]:
    """Return the distinct ``norm_name`` values the entity extractor finds in ``query``.

    The extractor reports a span once per recognizing class, so the same normalized name
    can appear twice; this collapses them while preserving first-seen order.
    """
    names: dict[str, None] = {}
    for entity in indexer.extract_entities(query):
        names.setdefault(entity.norm_name, None)
    return list(names)


def strip_recency_cues(query: str) -> tuple[str, bool]:
    """Return ``query`` with its recency cues removed, and whether any were found.

    Only the spans that actually matched a cue are dropped, and multi-word cues drop as
    whole phrases: ``recent pnpm`` becomes ``pnpm``, ``notes from last week`` becomes
    ``notes from``. Everything else keeps its original spelling.

    Cue words must not survive into the lexical view. ``store.build_match_expression``
    builds a *conjunctive* FTS5 query, so a leftover ``recent`` would demand that every
    matching memory literally contain the word "recent" — which is how a cued query used
    to return nothing at all.

    Matching is case-insensitive, whole-word, and diacritic-insensitive, so a Vietnamese
    user who types ``tuan truoc`` gets the same answer as one who types ``tuần trước``.
    Folding combining marks mirrors what ``remove_diacritics 2`` already does for view A,
    and is done for cue detection only — nowhere else in the pipeline.

    When no cue is present the query is returned byte-for-byte unchanged, so the common
    path behaves exactly as it did before cues existed.
    """
    tokens = _CUE_TOKEN_SEPARATOR_RE.split(unicodedata.normalize("NFC", query))
    words = [token for token in tokens if token]
    folded = [_fold(word) for word in words]

    kept: list[str] = []
    found = False
    position = 0
    while position < len(words):
        length = _cue_length_at(folded, position)
        if length:
            found = True
            position += length
            continue
        kept.append(words[position])
        position += 1
    if not found:
        return query, False
    return " ".join(kept), True


def has_recency_cue(query: str) -> bool:
    """Return whether ``query`` asks for recent things."""
    return strip_recency_cues(query)[1]


def recency_boost(created_at: str, now: datetime, weight: float = RECENCY_WEIGHT) -> float:
    """Return the recency contribution of ``created_at``: ``weight · 2**(-age/30d)``.

    ``weight`` is :data:`RECENCY_WEIGHT` normally and :data:`RECENCY_CUE_WEIGHT` when the
    query asked for recent things; the decay curve is the same either way.

    A timestamp the database could not have written — a corrupted or hand-edited value —
    contributes nothing rather than failing the recall.
    """
    try:
        created = datetime.strptime(created_at, SQLITE_TIMESTAMP_FORMAT).replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return 0.0
    age_days = (now - created).total_seconds() / SECONDS_PER_DAY
    return weight * math.pow(2.0, -age_days / RECENCY_HALF_LIFE_DAYS)


def seen_count_boost(seen_count: int) -> float:
    """Return ``0.02 · ln(seen_count)``; a memory seen once contributes exactly 0.0.

    ``seen_count`` is clamped to at least 1 so a corrupted 0 cannot raise.
    """
    return SEEN_COUNT_WEIGHT * math.log(max(seen_count, 1))


def _cue_length_at(folded: list[str], position: int) -> int:
    """Return the length of the longest cue starting at ``position``, or 0 for none."""
    for sequence in _CUE_TOKEN_SEQUENCES:
        end = position + len(sequence)
        if tuple(folded[position:end]) == sequence:
            return len(sequence)
    return 0


def _lexical_view(conn: sqlite3.Connection, match_expression: str, workspace: str | None) -> _View:
    """View A: bm25 over the FTS5 index, best ``CANDIDATE_LIMIT`` rows."""
    predicate, scope = _workspace_scope(workspace)
    sql = _LEXICAL_SQL
    params: list[object] = [*store.BM25_COLUMN_WEIGHTS, match_expression]
    if predicate:
        sql += " AND " + predicate
        params.extend(scope)
    params.append(CANDIDATE_LIMIT)
    return _read_view(conn, sql + _LEXICAL_ORDER_BY, params)


def _fallback_view(
    conn: sqlite3.Connection, lexical_text: str, workspace: str | None
) -> tuple[_View, bool]:
    """Re-run view A disjunctively, and report whether it actually found anything.

    Reached only when the conjunctive query and the entity view both returned nothing —
    the case the CEO measured at 13 empty results out of 14 realistic queries. Requiring
    *any* token instead of *every* token turns most of those into a hit.

    It is a genuinely weaker signal and is labelled as one. An OR query cannot stay
    silent: over ten queries whose answer was not in the corpus at all it returned
    plausible-looking rows ten times out of ten. That is tolerable for a recall an agent
    reads and judges, and not tolerable for the per-prompt hook — which is why the flag
    travels all the way out to ``RetrievedMemory.from_fallback``.
    """
    expression = store.build_or_match_expression(lexical_text)
    if not expression:
        return _View({}, {}), False
    view = _lexical_view(conn, expression, workspace)
    return view, bool(view.memories)


def _relational_view(
    conn: sqlite3.Connection, entity_names: list[str], workspace: str | None
) -> _View:
    """View B: memories sharing an entity with the query, scored by Σ of link weights."""
    if not entity_names:
        # `IN ()` is a syntax error in SQLite, and a query with no entities has no
        # relational view to build anyway.
        return _View({}, {})
    predicate, scope = _workspace_scope(workspace)
    placeholders = ", ".join("?" for _ in entity_names)
    sql = _RELATIONAL_SQL.format(placeholders=placeholders)
    params: list[object] = list(entity_names)
    if predicate:
        sql += " AND " + predicate
        params.extend(scope)
    params.append(CANDIDATE_LIMIT)
    return _read_view(conn, sql + _RELATIONAL_ORDER_BY, params)


def _recent_view(
    conn: sqlite3.Connection, workspace: str | None, k: int, now: datetime
) -> list[_Scored]:
    """Rank the workspace newest-first, scored by the recency term alone.

    Reached only when the query consisted entirely of recency cues, so there is no
    lexical or relational signal to fuse — both view scores are reported as ``None``.
    """
    predicate, scope = _workspace_scope(workspace)
    sql = _RECENT_SQL
    params: list[object] = []
    if predicate:
        sql += " WHERE " + predicate
        params.extend(scope)
    params.append(k)
    return [
        _Scored(
            row=row,
            score=recency_boost(row.created_at, now, RECENCY_CUE_WEIGHT),
            lexical=None,
            relational=None,
        )
        for row in _read_rows(conn, sql + _RECENT_ORDER_BY, params)
    ]


def _workspace_scope(workspace: str | None) -> tuple[str, list[object]]:
    """Return a view's workspace predicate and the values it binds.

    Three cases, and only the middle one is new:

    * ``None`` — "every workspace"; no predicate at all.
    * a named workspace — that workspace **and** the shared
      :data:`localmem.core_memory.GLOBAL_WORKSPACE` tier. Two named workspaces stay
      fully isolated from each other; ``global`` is the one deliberately shared tier.
    * ``"global"`` itself — exactly itself, so the shared tier never widens to
      everything.
    """
    if workspace is None:
        return "", []
    if workspace == core_memory.GLOBAL_WORKSPACE:
        return _WORKSPACE_EXACT, [workspace]
    return _WORKSPACE_WITH_SHARED, [workspace, core_memory.GLOBAL_WORKSPACE]


def _record_recall(conn: sqlite3.Connection, memory_ids: list[int]) -> None:
    """Bump ``recalled_count`` and ``last_recalled_at`` for the rows just returned.

    Best-effort by design: usage tracking is a convenience for ``audit``, and a recall
    must never fail because of it. Only :class:`sqlite3.Error` is swallowed — a bug in
    this function still propagates. Neighbors are deliberately not counted: they were
    attached as evidence, not asked for.

    One statement, outside any transaction the caller owns, over the ids of the final
    top-k. The ``memories`` FTS triggers fire on ``UPDATE OF content`` only, so this
    write costs no index maintenance.

    :data:`NO_TRACKING_ENV_VAR` turns the statement off, which makes recall strictly
    read-only — at the cost of ``audit``'s dead-memory and promotion sections, which
    have nothing left to count.
    """
    if not memory_ids or tracking_disabled():
        return
    placeholders = ", ".join("?" for _ in memory_ids)
    try:
        conn.execute(_RECORD_RECALL_SQL.format(placeholders=placeholders), memory_ids)
    except sqlite3.Error:
        return


def _read_view(conn: sqlite3.Connection, sql: str, params: list[object]) -> _View:
    rows = conn.execute(sql, params).fetchall()
    memories = {int(row["id"]): _to_row(row) for row in rows}
    scores = {int(row["id"]): float(row["score"]) for row in rows}
    return _View(memories, scores)


def _read_rows(conn: sqlite3.Connection, sql: str, params: list[object]) -> list[_Row]:
    return [_to_row(row) for row in conn.execute(sql, params).fetchall()]


def _to_row(row: sqlite3.Row) -> _Row:
    return _Row(
        id=int(row["id"]),
        content=row["content"],
        workspace=row["workspace"],
        kind=row["kind"],
        source=row["source"],
        seen_count=int(row["seen_count"]),
        session_id=row["session_id"],
        created_at=row["created_at"],
        superseded_by=None if row["superseded_by"] is None else int(row["superseded_by"]),
    )


def _fuse(
    lexical: _View,
    relational: _View,
    now: datetime,
    recency_weight: float,
    workspace: str | None = None,
) -> list[_Scored]:
    """Combine both views into one ranking, best first.

    ``workspace`` only breaks ties: at an equal fused score the row that belongs to the
    workspace being searched outranks one from the shared ``global`` tier. There is no
    score penalty for being global — a genuinely better global row still wins.

    A row that has been superseded is the one thing here that *is* penalized, in two
    steps that :data:`SUPERSEDED_SCORE_PENALTY` serves both of:

    1. its score, boosts included, is **multiplied** by the penalty. The multiply comes
       last on purpose — a demotion applied before the recency term would be undone by
       it, and a retracted memory is usually the older of the pair;
    2. if its replacement is **also** among the candidates, its score is then **capped**
       at ``replacement * penalty`` (:func:`_cap_superseded`). The multiply alone cannot
       do this: :func:`_min_max`
       maps the weakest candidate of a view to exactly 0.0, so a correction that is the
       weaker lexical match keeps only its boosts and no constant factor is small enough
       to reliably get under them. The cap is what turns "ranked down" into the
       guarantee the milestone is for — *whenever both are found, the correction is
       read first* (``docs/design_decisions.md`` §43).

    The cap can land on 0.0, when the replacement is itself the weakest candidate and
    has no boosts left. The two rows then tie on score and the existing sort key decides:
    ``created_at`` then ``id``, both of which a correction wins, because it was written
    after the memory it corrects. The one residual is a correction in ``global`` tying at
    0.0 against a retracted row in the workspace being searched — the workspace tiebreak
    sits above ``created_at`` and hands that one to the retracted row.

    A database with no superseded rows anywhere is arithmetically unchanged: no factor
    and no cap is ever reached.
    """
    normalized_lexical = _min_max(lexical.scores)
    normalized_relational = _min_max(relational.scores)
    if relational.scores:
        weights = (LEXICAL_WEIGHT_ON_ENTITY_HIT, RELATIONAL_WEIGHT_ON_ENTITY_HIT)
    else:
        weights = (LEXICAL_WEIGHT, RELATIONAL_WEIGHT)

    candidates = {**relational.memories, **lexical.memories}
    penalized: dict[int, float] = {}
    views: dict[int, tuple[float | None, float | None]] = {}
    for memory_id, row in candidates.items():
        lexical_score = normalized_lexical.get(memory_id)
        relational_score = normalized_relational.get(memory_id)
        fused = weights[0] * (lexical_score if lexical_score is not None else 0.0)
        fused += weights[1] * (relational_score if relational_score is not None else 0.0)
        fused += recency_boost(row.created_at, now, recency_weight)
        fused += seen_count_boost(row.seen_count)
        if row.superseded_by is not None:
            fused *= SUPERSEDED_SCORE_PENALTY
        penalized[memory_id] = fused
        views[memory_id] = (lexical_score, relational_score)

    final = _cap_superseded(penalized, {key: row.superseded_by for key, row in candidates.items()})
    scored = [_Scored(row, final[key], *views[key]) for key, row in candidates.items()]
    return sorted(
        scored,
        key=lambda item: (
            item.score,
            item.row.workspace == workspace,
            item.row.created_at,
            item.row.id,
        ),
        reverse=True,
    )


def _cap_superseded(penalized: dict[int, float], links: dict[int, int | None]) -> dict[int, float]:
    """Cap each superseded candidate's score under its replacement's *final* score.

    ``penalized`` holds every candidate's score after the multiply and before any cap;
    ``links`` maps each candidate to the row that superseded it, if any.

    Each chain is resolved **from its newest end backwards**, so a cap is always taken
    against a value that is itself already capped. That is what keeps a chain ordered:
    with A corrected by B and B corrected by C, resolving A against B's *pre-cap* score
    would let A land above B — measured at 0.0061 against 0.0051 on a three-link chain,
    which is how this function came to exist in this shape. The result does not depend on
    the order candidates are visited.

    A replacement that is not among the candidates caps nothing: the query did not find
    the correction, so there is nothing to rank under, and the plain multiply stands. That
    row still carries its correction out as its first neighbour
    (:func:`_replacement_neighbor`).

    ``seen`` guards the walk. Cycles cannot be written — :func:`localmem.store._supersede`
    refuses them — but a hand-edited database must not be able to hang a recall.
    """
    final: dict[int, float] = {}
    for start in penalized:
        chain: list[int] = []
        seen: set[int] = set()
        current: int | None = start
        while current is not None and current not in final and current not in seen:
            seen.add(current)
            chain.append(current)
            replacement = links.get(current)
            current = replacement if replacement in penalized else None
        for memory_id in reversed(chain):
            score = penalized[memory_id]
            replacement = links.get(memory_id)
            if replacement is not None and replacement in final:
                score = min(score, final[replacement] * SUPERSEDED_SCORE_PENALTY)
            final[memory_id] = score
    return final


def _min_max(scores: dict[int, float]) -> dict[int, float]:
    """Scale ``scores`` into ``[0, 1]``.

    A view where every candidate scores the same — including a view with a single
    candidate — normalizes to 1.0 throughout: one perfect hit must not be zeroed out
    just because it has nothing to be compared against.
    """
    if not scores:
        return {}
    lowest = min(scores.values())
    highest = max(scores.values())
    if highest == lowest:
        return dict.fromkeys(scores, 1.0)
    span = highest - lowest
    return {key: (value - lowest) / span for key, value in scores.items()}


def _neighbors(conn: sqlite3.Connection, row: _Row, selected: set[int]) -> tuple[Neighbor, ...]:
    """Attach up to :data:`MAX_NEIGHBORS` supporting memories to ``row``.

    A superseded row's **replacement comes first**, ahead of both ordinary neighbor
    sources. That is what turns the demotion into learning rather than mere burial: an
    agent that recalls "we thought it was a memory leak" reads "actually the connection
    pool was exhausted" in the same response, without a second call and without a change
    to the frozen payload — ``neighbors`` was already a list of ``{id, content}``.

    Session adjacency comes next; today almost every row has a NULL ``session_id``, so
    the entity-sibling query carries the feature and also tops up a short session hit.
    """
    chosen: dict[int, Neighbor] = {}
    replacement = _replacement_neighbor(conn, row, selected)
    if replacement is not None:
        chosen[replacement.id] = replacement
    if row.session_id is not None:
        for neighbor in _session_neighbors(conn, row, selected):
            chosen.setdefault(neighbor.id, neighbor)
    if len(chosen) < MAX_NEIGHBORS:
        for neighbor in _entity_neighbors(conn, row, selected | set(chosen)):
            chosen.setdefault(neighbor.id, neighbor)
    return tuple(chosen.values())[:MAX_NEIGHBORS]


def _replacement_neighbor(
    conn: sqlite3.Connection, row: _Row, excluded: set[int]
) -> Neighbor | None:
    """Return the memory that superseded ``row``, or ``None``.

    ``excluded`` holds the ids already in the result list, so a correction that ranked
    into the results on its own merits is **not** repeated underneath the row it
    corrected — the agent reads it once, at the top, which is where it belongs. The
    interesting case is the other one: a query phrased in the wrong diagnosis's words
    finds only the wrong diagnosis, and the correction rides in as its evidence.
    """
    if row.superseded_by is None or row.superseded_by in excluded:
        return None
    found = conn.execute(_REPLACEMENT_NEIGHBOR_SQL, (row.superseded_by,)).fetchone()
    return None if found is None else Neighbor(id=int(found["id"]), content=found["content"])


def _session_neighbors(conn: sqlite3.Connection, row: _Row, excluded: set[int]) -> list[Neighbor]:
    clause, exclusions = _exclusion(excluded)
    params: list[object] = [row.session_id, row.id, row.workspace, *exclusions, row.id]
    params.append(MAX_NEIGHBORS)
    sql = _SESSION_NEIGHBOR_SQL + clause + _SESSION_NEIGHBOR_ORDER_BY
    return _read_neighbors(conn, sql, params)


def _entity_neighbors(conn: sqlite3.Connection, row: _Row, excluded: set[int]) -> list[Neighbor]:
    clause, exclusions = _exclusion(excluded)
    params: list[object] = [row.id, row.id, row.workspace, *exclusions, MAX_NEIGHBORS]
    sql = _ENTITY_NEIGHBOR_SQL + clause + _ENTITY_NEIGHBOR_ORDER_BY
    return _read_neighbors(conn, sql, params)


def _exclusion(excluded: set[int]) -> tuple[str, list[object]]:
    """Return the ``NOT IN`` clause and its bound ids — one placeholder per id.

    ``excluded`` always holds at least the result row itself, so no empty-``IN`` case
    can arise here.
    """
    ids = sorted(excluded)
    placeholders = ", ".join("?" for _ in ids)
    return f"   AND m.id NOT IN ({placeholders})\n", list(ids)


def _read_neighbors(conn: sqlite3.Connection, sql: str, params: list[object]) -> list[Neighbor]:
    return [
        Neighbor(id=int(row["id"]), content=row["content"])
        for row in conn.execute(sql, params).fetchall()
    ]
