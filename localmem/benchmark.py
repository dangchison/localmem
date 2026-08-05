"""Token cost of instruction files versus the pull-based alternative (``PLAN.md`` §10).

Every number here is the character-based approximation from ``PLAN.md`` §1, reused from
:mod:`localmem.tokens`. The "after" cost is a **single fixed overhead** — pointer
snippet plus the two MCP tool descriptions plus the workspace's core memory — not a
per-file charge, because an agent pays it once per session no matter how many files it
replaces.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from localmem.agents import POINTER_SNIPPET
from localmem.tokens import estimate_tokens

#: ``PLAN.md`` §10 step 4, verbatim. Printed on every run and carried in ``--json``.
CAVEAT = (
    "Estimates use a character-based approximation (±15%). Verify real numbers with "
    "`/context` in Claude Code before and after migrating."
)

_PERCENT = 100.0
_PERCENT_DIGITS = 1


@dataclass(frozen=True)
class FileCost:
    """One instruction file's estimated push cost per session.

    Attributes:
        path: the file measured.
        tokens: its estimated token count, or 0 if it could not be read.
        error: why it could not be read, or ``None``.
    """

    path: Path
    tokens: int
    error: str | None


@dataclass(frozen=True)
class Benchmark:
    """The full comparison: per-file push cost against the fixed pull cost."""

    files: tuple[FileCost, ...]
    before_tokens: int
    pointer_tokens: int
    tool_description_tokens: int
    core_memory_tokens: int
    after_tokens: int
    saved_tokens: int
    saved_pct: float


def tool_descriptions() -> str:
    """Return the two ``PLAN.md`` §4 tool descriptions an agent loads every session.

    Imported lazily: :mod:`localmem.mcp_server` pulls in the MCP SDK, and reading two
    frozen constants is not worth that cost on every ``localmem --help``. Reading them
    from their one definition is worth it — a copy here could drift from the API.
    """
    from localmem import mcp_server

    return f"{mcp_server.RECALL_DESCRIPTION}\n{mcp_server.ADD_DESCRIPTION}"


def after_text(core_memory_text: str) -> str:
    """Return everything a session loads once localmem replaces the instruction files."""
    parts = [POINTER_SNIPPET, tool_descriptions()]
    if core_memory_text:
        parts.append(core_memory_text)
    return "\n".join(parts)


def measure(paths: Sequence[Path], core_memory_text: str) -> Benchmark:
    """Compare the push cost of ``paths`` against the fixed pull cost.

    A file that cannot be read is reported with 0 tokens and its error, rather than
    aborting the run: a benchmark is a read-only report and one bad path should not
    cost the user the rest of the table.
    """
    files = tuple(_measure_file(path) for path in paths)
    before = sum(cost.tokens for cost in files)

    pointer = estimate_tokens(POINTER_SNIPPET)
    descriptions = estimate_tokens(tool_descriptions())
    core = estimate_tokens(core_memory_text)
    after = estimate_tokens(after_text(core_memory_text))

    saved = before - after
    return Benchmark(
        files=files,
        before_tokens=before,
        pointer_tokens=pointer,
        tool_description_tokens=descriptions,
        core_memory_tokens=core,
        after_tokens=after,
        saved_tokens=saved,
        saved_pct=saved_percentage(before, after),
    )


def saved_percentage(before: int, after: int) -> float:
    """Return the share of the push cost the pull cost removes, as a percentage.

    Zero when there is nothing to save from, and negative when the fixed pull cost
    exceeds the instruction files it replaces — which is the honest answer for a user
    whose files are already tiny.
    """
    if before <= 0:
        return 0.0
    return round((before - after) / before * _PERCENT, _PERCENT_DIGITS)


def _measure_file(path: Path) -> FileCost:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return FileCost(path=path, tokens=0, error=str(exc))
    return FileCost(path=path, tokens=estimate_tokens(text), error=None)
