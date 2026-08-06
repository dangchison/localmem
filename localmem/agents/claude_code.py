"""Claude Code writer: project ``.mcp.json`` inside a repo, a printed command outside.

``~/.claude.json`` is never touched. It is tens of kilobytes of unrelated session state,
and the user consented to *adding localmem*, not to having that file rewritten. See
``docs/design_decisions.md`` §21.
"""

from __future__ import annotations

from pathlib import Path

from localmem.agents import base, command

PROJECT_CONFIG_NAME = ".mcp.json"

_GIT_DIR_NAME = ".git"


def mcp_add_command() -> str:
    """Return the ``claude mcp add`` line printed when there is no project to write into.

    It carries the same absolute path the file writers embed (AC1.3). Claude Code stores
    what this line registers in a file localmem deliberately does not edit, so a bare
    ``localmem`` here has the identical failure mode — the entry is written once and the
    server silently never starts under a desktop launch.
    """
    return f"claude mcp add {base.SERVER_KEY} -- {command.resolve_server_command()} serve"


class ClaudeCodeWriter:
    """Registers localmem in the project-level ``.mcp.json`` Claude Code reads."""

    name = "Claude Code"
    slug = "claude-code"

    def is_detected(self, home: Path, cwd: Path) -> bool:
        """Return whether ``{home}/.claude/`` exists."""
        return (home / ".claude").exists()

    def target_path(self, home: Path, cwd: Path) -> Path | None:
        """Return ``{cwd}/.mcp.json`` inside a git repository, else ``None``."""
        if not _inside_git_repository(cwd):
            return None
        return cwd / PROJECT_CONFIG_NAME

    def render_config(self, home: Path, cwd: Path) -> str:
        """Return the ``.mcp.json`` document. Reads and writes no config file."""
        return base.render_json_document()

    def apply(
        self, home: Path, cwd: Path, *, dry_run: bool = False, repair: bool = False
    ) -> base.ApplyResult:
        """Merge into the project ``.mcp.json``, or print the ``claude mcp add`` command."""
        target = self.target_path(home, cwd)
        if target is None:
            return base.ApplyResult(
                agent=self.name,
                path=None,
                backup=None,
                printed_command=mcp_add_command(),
                action="printed",
                detail=(
                    f"{cwd} is not a git repository, so there is no project "
                    f"{PROJECT_CONFIG_NAME} to merge into; run the command above instead"
                ),
            )
        return base.apply_json_config(self.name, target, dry_run=dry_run, repair=repair)


def _inside_git_repository(cwd: Path) -> bool:
    """Return whether ``cwd`` or any ancestor holds a ``.git`` entry.

    Deliberately does not shell out to ``git``: a subprocess would inherit the real
    environment, and this answer only decides which path to write.
    """
    return any((candidate / _GIT_DIR_NAME).exists() for candidate in (cwd, *cwd.parents))


WRITER = ClaudeCodeWriter()
