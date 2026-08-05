"""Agent registry, detection, and the one pointer snippet the product prints.

Nothing in this package resolves the user's home directory. ``home`` and ``cwd`` are
parameters of every function here and of every writer method, which is what makes a
sandboxed test structurally incapable of reaching a real config file
(``docs/design_decisions.md`` §22).
"""

from __future__ import annotations

from pathlib import Path

from localmem.agents import antigravity, base, claude_code, codex, kiro
from localmem.agents.base import (
    AgentWriter,
    ApplyResult,
    MalformedConfigError,
)

#: Every writer, in the order `init` offers them.
WRITERS: tuple[AgentWriter, ...] = (
    claude_code.WRITER,
    codex.WRITER,
    antigravity.WRITER,
    kiro.WRITER,
)

#: The block ``init`` step 4 prints and ``benchmark`` charges as the "after" cost
#: (``PLAN.md`` §8 step 4 and §10 step 3). One definition, so the advice the user is
#: given and the savings they are quoted can never drift apart.
POINTER_SNIPPET = (
    "## Memory\n"
    "\n"
    "Before answering questions about project history, prior decisions, or user "
    "preferences, call the `memory_recall` tool. When you learn a durable fact or "
    "decision, save it with `memory_add`. Do not duplicate long-term memory in this "
    "file.\n"
)

#: ``PLAN.md`` §8 step 3's scan set, relative to ``home`` and ``cwd``.
HOME_INSTRUCTION_FILES = (Path(".claude") / "CLAUDE.md",)
CWD_INSTRUCTION_FILES = (Path("CLAUDE.md"), Path("AGENTS.md"))
CWD_INSTRUCTION_GLOB = ".kiro/steering/*.md"

__all__ = [
    "CWD_INSTRUCTION_FILES",
    "CWD_INSTRUCTION_GLOB",
    "HOME_INSTRUCTION_FILES",
    "POINTER_SNIPPET",
    "WRITERS",
    "AgentWriter",
    "ApplyResult",
    "MalformedConfigError",
    "base",
    "detect_agents",
    "find_instruction_files",
    "slugs",
    "writer_for",
]


def detect_agents(home: Path, cwd: Path) -> list[AgentWriter]:
    """Return the writers whose agent appears to be installed, in registry order."""
    return [writer for writer in WRITERS if writer.is_detected(home, cwd)]


def find_instruction_files(home: Path, cwd: Path) -> list[Path]:
    """Return the instruction files ``import`` and ``benchmark`` both scan for.

    The order is fixed and the glob is sorted, so two runs over the same filesystem
    always produce the same list. Paths that resolve to the same file — ``cwd`` being
    ``{home}/.claude``, say — appear once.
    """
    candidates = [home / relative for relative in HOME_INSTRUCTION_FILES]
    candidates += [cwd / relative for relative in CWD_INSTRUCTION_FILES]
    candidates += sorted(cwd.glob(CWD_INSTRUCTION_GLOB))

    found: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        if not candidate.is_file():
            continue
        key = _identity(candidate)
        if key in seen:
            continue
        seen.add(key)
        found.append(candidate)
    return found


def writer_for(slug: str) -> AgentWriter | None:
    """Return the writer registered under ``slug``, or ``None`` if there is none."""
    for writer in WRITERS:
        if writer.slug == slug:
            return writer
    return None


def slugs() -> tuple[str, ...]:
    """Return every registered agent slug, in registry order."""
    return tuple(writer.slug for writer in WRITERS)


def _identity(candidate: Path) -> Path:
    """Return a comparison key that collapses two paths naming one file."""
    try:
        return candidate.resolve()
    except OSError:  # pragma: no cover - resolve() only fails on a broken filesystem
        return candidate
