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

#: :func:`promote_memory`'s two outcomes. ``unchanged`` is not a failure — it is what
#: makes the command idempotent, so a script may run it twice without special-casing.
STATUS_PROMOTED = "promoted"
STATUS_UNCHANGED = "unchanged"

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

_SELECT_BY_ID_SQL = "SELECT id, workspace, kind FROM memories WHERE id = ?"

_SELECT_SUPERSEDE_TARGET_SQL = "SELECT id, workspace, superseded_by FROM memories WHERE id = ?"

# `mem_au` fires only `AFTER UPDATE OF content, keywords`, so pointing one row at another
# costs no FTS5 index maintenance — the same property `_PROMOTE_SQL` relies on.
_SUPERSEDE_SQL = """
UPDATE memories
   SET superseded_by = ?,
       updated_at = datetime('now')
 WHERE id = ?
"""

_SELECT_SUPERSEDED_BY_SQL = "SELECT superseded_by FROM memories WHERE id = ?"

# `kind` is not an indexed FTS5 column and `mem_au` fires only `AFTER UPDATE OF content,
# keywords`, so this statement costs no index maintenance and cannot desynchronize
# `memories_fts`. The entity graph is derived from `content`, which is likewise untouched.
_PROMOTE_SQL = """
UPDATE memories
   SET kind = ?,
       updated_at = datetime('now')
 WHERE id = ?
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


class PromoteResult(NamedTuple):
    """Outcome of :func:`promote_memory`.

    Attributes:
        status: :data:`STATUS_PROMOTED` or :data:`STATUS_UNCHANGED`.
        id: the row that was addressed.
        workspace: where that row lives — the caller needs it to size the core tier.
        kind: the kind the row carries now.
        previous_kind: the kind it carried before, equal to ``kind`` when unchanged.
    """

    status: str
    id: int
    workspace: str
    kind: str
    previous_kind: str


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
    supersedes: Sequence[int] | None = None,
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

    ``supersedes`` names the memories this one corrects. Each referenced row gets its
    ``superseded_by`` set to the id this call resolves to, **inside the same
    transaction** as the insert, so a database is never left holding the correction
    without the link. A superseded row is not hidden: it stays retrievable and is ranked
    down by :data:`localmem.retriever.SUPERSEDED_SCORE_PENALTY`, and a recall that
    returns it attaches its replacement as evidence. This is the ADD/UPDATE decision
    Mem0 pays a model call to make, collapsed onto the agent at write time — which is
    what keeps recall free of any model (``docs/design_decisions.md`` §40).

    A new row is indexed by :func:`localmem.indexer.index_memory` in the same
    transaction. A merge does not re-index: the stored content is unchanged, so the
    links already attached to that row stay correct.

    Tier-2 near-duplicate detection then runs over the new row and silently queues any
    pair it finds. It never blocks, never deletes and never changes the returned result.

    Returns:
        ``("added", id, 1)`` for a new row, ``("duplicate_merged", id, n)`` when an
        existing row in the same workspace was bumped.

    Raises:
        ValueError: if ``content`` is blank, ``workspace`` is empty, or any
            ``supersedes`` id is unknown, is the row being written, or is out of reach
            of this workspace (see :func:`_supersede`). The whole call rolls back in
            that case — the memory is not stored either, because a correction whose
            link failed is not the memory the caller asked for.
    """
    if not content.strip():
        raise ValueError("content is empty; pass the text you want to remember")
    target_workspace = validate_workspace(workspace)
    digest = dedup.content_hash(content)
    keyword_text = normalize_keywords(keywords)
    targets = _unique_ids(supersedes)

    with db.transaction(conn):
        existing = conn.execute(_SELECT_BY_HASH_SQL, (target_workspace, digest)).fetchone()
        if existing is not None:
            result = AddResult(
                STATUS_DUPLICATE_MERGED,
                *_merge_duplicate(conn, target_workspace, digest, keyword_text),
            )
        else:
            try:
                cursor = conn.execute(
                    _INSERT_MEMORY_SQL,
                    (content, digest, target_workspace, kind, source, session_id, keyword_text),
                )
            except sqlite3.IntegrityError:
                # A concurrent writer won the race on UNIQUE(workspace, content_hash).
                result = AddResult(
                    STATUS_DUPLICATE_MERGED,
                    *_merge_duplicate(conn, target_workspace, digest, keyword_text),
                )
            else:
                row_id = cursor.lastrowid
                if row_id is None:
                    raise RuntimeError("SQLite did not report a row id for the inserted memory")
                indexer.index_memory(conn, row_id, content)
                dedup.enqueue_near_duplicates(
                    conn, row_id, content, target_workspace, build_match_expression
                )
                result = AddResult(STATUS_ADDED, row_id, 1)
        if targets:
            _supersede(conn, result.id, target_workspace, targets)
        return result


def promote_memory(conn: sqlite3.Connection, memory_id: int, kind: str) -> PromoteResult:
    """Change the ``kind`` of the row ``memory_id``, in place.

    Promotion is **by id**, not by text, because the two are not interchangeable here:
    re-adding the same words with a different ``--kind`` reaches :func:`add_memory`,
    which merges on the content hash and leaves the stored ``kind`` exactly as it was.
    An id names one row unambiguously, so the update is a single ``UPDATE`` with no
    hashing, no merge and no second copy of the text
    (``docs/design_decisions.md`` §38).

    Nothing else about the row moves: ``content``, ``keywords``, ``seen_count``,
    ``created_at`` and the entity links are all untouched, so a promoted memory keeps
    its history and its position in the index. Only ``updated_at`` advances.

    Idempotent: promoting a row to the kind it already has writes nothing and reports
    :data:`STATUS_UNCHANGED`.

    ``kind`` is not checked against a whitelist, exactly as :func:`add_memory` does not
    check it — the command line's ``click.Choice`` is the one gate, and this function is
    not reachable from the MCP tool surface.

    Raises:
        ValueError: if no memory carries ``memory_id``.
    """
    with db.transaction(conn):
        row = conn.execute(_SELECT_BY_ID_SQL, (memory_id,)).fetchone()
        if row is None:
            raise ValueError(
                f"no memory with id {memory_id}; `localmem search` prints the id of every hit"
            )
        previous = str(row["kind"])
        workspace = str(row["workspace"])
        if previous == kind:
            return PromoteResult(STATUS_UNCHANGED, memory_id, workspace, kind, previous)
        conn.execute(_PROMOTE_SQL, (kind, memory_id))
    return PromoteResult(STATUS_PROMOTED, memory_id, workspace, kind, previous)


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


def _unique_ids(ids: Sequence[int] | None) -> tuple[int, ...]:
    """Return ``ids`` de-duplicated with first-seen order preserved."""
    if not ids:
        return ()
    unique: dict[int, None] = {}
    for value in ids:
        unique.setdefault(int(value), None)
    return tuple(unique)


def _supersede(
    conn: sqlite3.Connection,
    replacement_id: int,
    replacement_workspace: str,
    targets: tuple[int, ...],
) -> None:
    """Point every row in ``targets`` at ``replacement_id``, or raise and roll back.

    Four rules, each enforced here rather than left to the caller:

    * **an unknown id is an error**, never a silent no-op. The agent believes it just
      retracted something; being told "no such memory" is the only honest answer;
    * **a row may not supersede itself.** Re-adding a memory's own text with
      ``--supersedes <its own id>`` reaches this through the duplicate-merge path, where
      the resolved id *is* the target — a self-loop that would demote the row against
      its own correction;
    * **an already-superseded row may be superseded again.** Corrections get corrected,
      and ``superseded_by`` simply moves to the newest one. This is the case the
      milestone exists for, so it is allowed by design;
    * **the replacement must be reachable from the target's workspace.** A row may
      supersede one in its own workspace, and a ``global`` row may supersede one
      anywhere — but a repo-local row may not retract knowledge in another workspace.
      The rule is exactly :func:`localmem.retriever._workspace_scope`'s visibility:
      a recall scoped to the target's workspace must be able to return the replacement,
      or the retraction would demote a memory while hiding the reason
      (``docs/design_decisions.md`` §41).

    Cycles are refused by :func:`_would_cycle`. They cannot arise from an ordinary
    insert — a freshly inserted row has ``superseded_by`` NULL and nothing pointing at
    it — but they can through the merge path, where the resolved id is an existing row
    that may already be superseded.
    """
    for target_id in targets:
        row = conn.execute(_SELECT_SUPERSEDE_TARGET_SQL, (target_id,)).fetchone()
        if row is None:
            raise ValueError(
                f"no memory with id {target_id} to supersede; "
                "`localmem search` prints the id of every hit"
            )
        if target_id == replacement_id:
            raise ValueError(
                f"memory {target_id} cannot supersede itself; a correction is a different "
                "memory, and re-adding the same text merges into the row it already has"
            )
        target_workspace = str(row["workspace"])
        if not _may_supersede(replacement_workspace, target_workspace):
            raise ValueError(
                f"a memory in workspace {replacement_workspace!r} cannot supersede memory "
                f"{target_id} in workspace {target_workspace!r}; store the correction in "
                f"{target_workspace!r} or in {core_memory.GLOBAL_WORKSPACE!r}, which every "
                "workspace reads"
            )
        if _would_cycle(conn, replacement_id, target_id):
            raise ValueError(
                f"memory {target_id} already supersedes {replacement_id}, directly or "
                "through a chain; pointing them at each other would leave neither as the "
                "current answer"
            )
        conn.execute(_SUPERSEDE_SQL, (replacement_id, target_id))


def _may_supersede(replacement_workspace: str, target_workspace: str) -> bool:
    """Return whether a row in one workspace may supersede a row in another."""
    return replacement_workspace in (target_workspace, core_memory.GLOBAL_WORKSPACE)


def _would_cycle(conn: sqlite3.Connection, replacement_id: int, target_id: int) -> bool:
    """Return whether ``target_id → replacement_id`` would close a supersede cycle.

    Walks the chain forward from the replacement: if the row being retracted is already
    somewhere ahead of it, the new edge would join the two ends. The ``seen`` set also
    makes the walk terminate on a database that somehow already holds a cycle, so this
    can never hang a write.
    """
    seen: set[int] = set()
    current: int | None = replacement_id
    while current is not None and current not in seen:
        if current == target_id:
            return True
        seen.add(current)
        row = conn.execute(_SELECT_SUPERSEDED_BY_SQL, (current,)).fetchone()
        current = None if row is None or row["superseded_by"] is None else int(row["superseded_by"])
    return False


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
