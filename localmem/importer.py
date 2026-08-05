"""Markdown instruction files → memory records (``PLAN.md`` §9).

The splitter is a deterministic line-oriented state machine: the same bytes always
produce the same records in the same order, which is what makes a re-import a no-op
beyond ``seen_count`` bumps. Nothing here consults the clock, the filesystem layout or
any dictionary ordering.

Record boundaries, per §9:

* each top-level bullet, with its nested children and lazy continuations, as one record;
* each paragraph;
* each fenced code block, kept whole and never re-interpreted;
* each heading's intro paragraph.

The nearest enclosing heading is prepended as ``"[Heading] body"``.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from localmem import store

#: ``PLAN.md`` §8 step 3, verbatim. One definition so ``init`` cannot paraphrase it.
SKIP_MESSAGE = "You can import anytime later with `localmem import` — skipping now loses nothing."

#: ``PLAN.md`` §9's post-import suggestion. A suggestion only: localmem never edits an
#: instruction file (``PLAN.md`` §12).
TRIM_SUGGESTION_TEMPLATE = (
    "Consider trimming the imported sections from {path} and replacing them with the "
    "pointer snippet (`localmem init` prints it, or see "
    "docs/migrating_from_instruction_files.md)."
)

IMPORT_KIND = "imported"
SOURCE_PREFIX = "import:"

#: How many rendered records ``--dry-run`` shows (``PLAN.md`` §9).
DRY_RUN_PREVIEW = 5

_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*)$")
_CLOSING_HASHES_RE = re.compile(r"[ \t]+#+[ \t]*$")
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
# Top level only: a bullet that is indented belongs to the record above it.
_BULLET_RE = re.compile(r"^(?:[-*+]|\d+[.)])[ \t]+\S")


@dataclass(frozen=True)
class ImportOutcome:
    """What importing one file did.

    Attributes:
        path: the file that was read.
        records: how many records it split into.
        added: records stored as new rows.
        merged: records that matched an existing row and bumped its ``seen_count``.
    """

    path: Path
    records: int
    added: int
    merged: int


def source_for(path: Path) -> str:
    """Return the ``source`` column value for records imported from ``path``."""
    return f"{SOURCE_PREFIX}{path.name}"


def parse_records(text: str) -> list[str]:
    """Split markdown ``text`` into record strings, heading prefixes already applied.

    Pure and deterministic: no filesystem, no clock, no set or dict iteration order
    influences the result.
    """
    return _Splitter().run(text)


def read_records(path: Path) -> list[str]:
    """Return the records ``path`` splits into.

    Raises:
        OSError: if the file cannot be read.
        UnicodeDecodeError: if it is not valid UTF-8.
    """
    return parse_records(path.read_text(encoding="utf-8"))


def import_file(conn: sqlite3.Connection, path: Path, workspace: str) -> ImportOutcome:
    """Store every record of ``path`` in ``workspace``, running dedup tiers 1 and 2.

    Re-importing an unchanged file adds no rows: each record hashes to the value it
    hashed to last time, so tier 1 merges it and bumps ``seen_count``.

    Raises:
        OSError: if the file cannot be read.
        UnicodeDecodeError: if it is not valid UTF-8.
        ValueError: if ``workspace`` is empty.
    """
    records = read_records(path)
    source = source_for(path)
    added = 0
    merged = 0
    for content in records:
        result = store.add_memory(conn, content, workspace, IMPORT_KIND, source)
        if result.status == store.STATUS_ADDED:
            added += 1
        else:
            merged += 1
    return ImportOutcome(path=path, records=len(records), added=added, merged=merged)


class _Splitter:
    """The line-oriented state machine behind :func:`parse_records`.

    Three states, checked in this order on every line: inside a fence, at a fence
    opener, everything else. Checking the fence first is what makes ``#`` and ``-``
    inside a code block ordinary text rather than heading and bullet syntax.
    """

    def __init__(self) -> None:
        self._records: list[str] = []
        self._buffer: list[str] = []
        self._heading: str | None = None
        self._fence: str | None = None

    def run(self, text: str) -> list[str]:
        """Consume ``text`` and return the records it yields."""
        for line in text.splitlines():
            self._consume(line)
        self._flush()
        return self._records

    def _consume(self, line: str) -> None:
        if self._fence is not None:
            self._buffer.append(line)
            if _closes_fence(line, self._fence):
                self._fence = None
                self._flush()
            return

        opener = _FENCE_RE.match(line)
        if opener is not None:
            self._flush()
            self._fence = opener.group(1)
            self._buffer.append(line)
            return

        heading = _HEADING_RE.match(line)
        if heading is not None:
            self._flush()
            self._heading = _clean_heading(heading.group(2))
            return

        if not line.strip():
            self._flush()
            return

        if _BULLET_RE.match(line):
            # A new top-level bullet ends the record above it; an indented one does
            # not match here and is appended below as a nested child.
            self._flush()

        self._buffer.append(line)

    def _flush(self) -> None:
        """Emit the buffered lines as one record, if they carry any content."""
        lines, self._buffer = self._buffer, []
        if not lines:
            return
        body = "\n".join(lines).strip("\n")
        # A horizontal rule or a row of punctuation is markup, not a memory.
        if not any(character.isalnum() for character in body):
            return
        self._records.append(f"[{self._heading}] {body}" if self._heading else body)


def _closes_fence(line: str, opener: str) -> bool:
    """Return whether ``line`` closes a fence opened by ``opener``.

    A closing fence uses the same marker character, is at least as long, and carries no
    info string — exactly CommonMark's rule.
    """
    match = _FENCE_RE.match(line)
    if match is None:
        return False
    marker = match.group(1)
    return marker[0] == opener[0] and len(marker) >= len(opener) and not match.group(2).strip()


def _clean_heading(raw: str) -> str | None:
    """Return the heading text without its closing hashes, or ``None`` if it is empty."""
    text = _CLOSING_HASHES_RE.sub("", raw).strip()
    return text or None
