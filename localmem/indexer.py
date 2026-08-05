"""Regex entity extraction and ``entities`` / ``memory_entities`` maintenance.

The extractor is pure stdlib ``re``: no model, no network, no optional dependency.
Entity classes are applied in a fixed priority order over a working copy of the text;
every match of a masking class is blanked out (offsets preserved) so a lower-priority
class cannot re-match the same characters. ``QUOTED_STRING`` deliberately does not
mask, so identifiers written inside quotes stay indexable.

Nothing here opens a transaction: :func:`index_memory` runs bare statements inside the
caller's transaction, so a memory row and its entity links commit or roll back together.
"""

from __future__ import annotations

import enum
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass

from localmem import db
from localmem.config import validate_workspace

# Extraction is capped before any regex runs: it bounds worst-case backtracking and
# keeps a pathological paste from producing thousands of links.
MAX_EXTRACT_CHARS = 4096
MAX_ENTITIES_PER_MEMORY = 50
BACKFILL_BATCH_SIZE = 500

_INSERT_ENTITY_SQL = """
INSERT INTO entities (name, norm_name) VALUES (?, ?) ON CONFLICT(norm_name) DO NOTHING
"""

_SELECT_ENTITY_ID_SQL = "SELECT id FROM entities WHERE norm_name = ?"

_UPSERT_LINK_SQL = """
INSERT INTO memory_entities (memory_id, entity_id, weight) VALUES (?, ?, ?)
ON CONFLICT(memory_id, entity_id) DO UPDATE SET weight = excluded.weight
"""

_SELECT_UNINDEXED_SQL = """
SELECT m.id AS id, m.content AS content
  FROM memories AS m
 WHERE m.id > ?
   AND NOT EXISTS (SELECT 1 FROM memory_entities AS me WHERE me.memory_id = m.id)
"""


class EntityClass(str, enum.Enum):
    """Kind of entity a span was recognized as, declared in priority order."""

    URL = "URL"
    MENTION = "MENTION"
    FILE_PATH = "FILE_PATH"
    QUOTED_STRING = "QUOTED_STRING"
    CAMEL_CASE = "CAMEL_CASE"
    SNAKE_CASE = "SNAKE_CASE"
    ACRONYM = "ACRONYM"


@dataclass(frozen=True)
class ExtractedEntity:
    """One deduplicated entity found in a memory.

    Attributes:
        name: the first-seen raw match, kept as the display form.
        norm_name: the lookup key — quotes stripped, trimmed and lowercased.
        entity_class: the class that recognized the first occurrence.
        count: how many extracted spans in the memory share ``norm_name``.
        weight: ``count`` divided by the memory's highest count, so in ``(0.0, 1.0]``.
    """

    name: str
    norm_name: str
    entity_class: EntityClass
    count: int
    weight: float


@dataclass(frozen=True)
class _ClassRule:
    """A regex class plus whether its matches are blanked out for later classes."""

    entity_class: EntityClass
    pattern: re.Pattern[str]
    masks: bool


@dataclass(frozen=True)
class _Span:
    """A single raw match, before duplicate spans are folded together."""

    start: int
    name: str
    norm_name: str
    entity_class: EntityClass


_RULES: tuple[_ClassRule, ...] = (
    _ClassRule(
        EntityClass.URL,
        re.compile(r"(?:https?|ftp)://[^\s<>\"']+", re.IGNORECASE),
        masks=True,
    ),
    _ClassRule(EntityClass.MENTION, re.compile(r"@[A-Za-z0-9_-]{1,50}"), masks=True),
    # First alternative: a slash-joined path, optionally rooted (`/etc/hosts`) or
    # dot-prefixed (`./run.sh`, `../up/one.txt`). The leading segment is part of the
    # match, so `src/main.py` does not degrade to `/main.py`.
    # Second alternative: a bare `filename.ext`. The `{2,6}` extension length keeps
    # prose abbreviations such as `e.g.` out.
    _ClassRule(
        EntityClass.FILE_PATH,
        re.compile(
            r"(?:\.{0,2}/)?[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+|[A-Za-z0-9_-]+\.[A-Za-z]{2,6}"
        ),
        masks=True,
    ),
    # Quoted strings keep their interior visible to the identifier classes below:
    # the text inside quotes is exactly the text worth indexing.
    _ClassRule(
        EntityClass.QUOTED_STRING,
        re.compile(r"\"[^\"\n]{2,80}\"|'[^'\n]{2,80}'"),
        masks=False,
    ),
    _ClassRule(
        EntityClass.CAMEL_CASE, re.compile(r"\b[A-Z][a-z]+(?:[A-Z][a-z0-9]+)+\b"), masks=True
    ),
    _ClassRule(
        EntityClass.SNAKE_CASE, re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z][a-z0-9]*)+\b"), masks=True
    ),
    _ClassRule(EntityClass.ACRONYM, re.compile(r"\b[A-Z]{2,10}\b"), masks=True),
)


def extract_entities(content: str) -> list[ExtractedEntity]:
    """Return the entities of ``content``, best-supported first.

    Only the first :data:`MAX_EXTRACT_CHARS` characters are examined. Spans are folded
    by ``(norm_name, entity_class)``; the resulting list is ordered by descending
    ``count``, then by first-occurrence offset, then by ``norm_name``, and truncated to
    :data:`MAX_ENTITIES_PER_MEMORY` entries. Weights are computed before that cut, which
    does not change them because the cut only ever discards low-count entities.
    """
    spans = _collect_spans(content[:MAX_EXTRACT_CHARS])
    if not spans:
        return []
    occurrences = Counter(span.norm_name for span in spans)
    max_count = max(occurrences.values())
    ranked = sorted(
        _first_occurrences(spans),
        key=lambda span: (-occurrences[span.norm_name], span.start, span.norm_name),
    )
    return [
        ExtractedEntity(
            name=span.name,
            norm_name=span.norm_name,
            entity_class=span.entity_class,
            count=occurrences[span.norm_name],
            weight=occurrences[span.norm_name] / max_count,
        )
        for span in ranked[:MAX_ENTITIES_PER_MEMORY]
    ]


def index_memory(conn: sqlite3.Connection, memory_id: int, content: str) -> int:
    """Write the entity links of ``content`` inside the caller's transaction.

    The caller owns the transaction — this function never begins, commits or rolls one
    back — so entity links land atomically with the memory row that produced them.
    Re-indexing the same memory refreshes existing weights instead of failing.

    Args:
        conn: an open connection with a transaction already in progress.
        memory_id: the ``memories.id`` the links belong to.
        content: the raw memory text.

    Returns:
        The number of distinct ``memory_entities`` rows written.

    Raises:
        RuntimeError: if an entity cannot be read back right after being inserted.
    """
    linked: set[int] = set()
    for entity in extract_entities(content):
        conn.execute(_INSERT_ENTITY_SQL, (entity.name, entity.norm_name))
        row = conn.execute(_SELECT_ENTITY_ID_SQL, (entity.norm_name,)).fetchone()
        if row is None:
            raise RuntimeError(
                f"entity {entity.norm_name!r} vanished immediately after insert; "
                "the database looks corrupt — inspect it or point LOCALMEM_DB elsewhere"
            )
        entity_id = int(row["id"])
        conn.execute(_UPSERT_LINK_SQL, (memory_id, entity_id, entity.weight))
        linked.add(entity_id)
    return len(linked)


def backfill_all(conn: sqlite3.Connection, workspace: str | None = None) -> tuple[int, int]:
    """Index every memory that has no entity links yet.

    Rows are processed in batches of :data:`BACKFILL_BATCH_SIZE` per transaction, so a
    large database is not held under one long write lock.

    Args:
        conn: an open connection; this function owns its own transactions.
        workspace: restrict to one workspace; ``None`` covers every workspace.

    Returns:
        ``(processed, links_created)``. Re-running is safe but ``processed`` does not
        fall to zero: a memory with no extractable entity produces no link, so it stays
        in the unindexed selection forever. Idempotency shows up as ``links_created``
        being 0 and the ``memory_entities`` row count being unchanged.

    Raises:
        ValueError: if ``workspace`` is empty or whitespace only.
    """
    sql = _SELECT_UNINDEXED_SQL
    scope: list[object] = []
    if workspace is not None:
        sql += " AND m.workspace = ?"
        scope.append(validate_workspace(workspace))
    sql += " ORDER BY m.id LIMIT ?"

    processed = 0
    links_created = 0
    last_id = 0
    while True:
        rows = conn.execute(sql, [last_id, *scope, BACKFILL_BATCH_SIZE]).fetchall()
        if not rows:
            return processed, links_created
        with db.transaction(conn):
            for row in rows:
                links_created += index_memory(conn, int(row["id"]), row["content"])
                processed += 1
        last_id = int(rows[-1]["id"])


def _collect_spans(text: str) -> list[_Span]:
    """Run every class over ``text`` in priority order, masking as configured."""
    working = text
    spans: list[_Span] = []
    for rule in _RULES:
        matches = list(rule.pattern.finditer(working))
        for match in matches:
            # A masking class can never match a blanked character (none of their
            # patterns admit whitespace), so slicing the original text is safe and
            # keeps the display form of a quoted span intact.
            raw = text[match.start() : match.end()]
            norm_name = _normalize_name(raw, rule.entity_class)
            if not norm_name:
                continue
            spans.append(_Span(match.start(), raw, norm_name, rule.entity_class))
        if rule.masks and matches:
            working = _mask(working, matches)
    return spans


def _mask(working: str, matches: list[re.Match[str]]) -> str:
    """Return ``working`` with every matched span overwritten by spaces."""
    characters = list(working)
    for match in matches:
        for index in range(match.start(), match.end()):
            characters[index] = " "
    return "".join(characters)


def _normalize_name(raw: str, entity_class: EntityClass) -> str:
    """Return the lookup key for ``raw``: quotes dropped, then trimmed and lowercased."""
    inner = raw[1:-1] if entity_class is EntityClass.QUOTED_STRING else raw
    return inner.strip().lower()


def _first_occurrences(spans: list[_Span]) -> list[_Span]:
    """Keep the earliest span per ``(norm_name, entity_class)``, in encounter order."""
    seen: dict[tuple[str, EntityClass], _Span] = {}
    for span in spans:
        seen.setdefault((span.norm_name, span.entity_class), span)
    return list(seen.values())
