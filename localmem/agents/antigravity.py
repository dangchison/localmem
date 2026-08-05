"""Google Antigravity writer: ``~/.gemini/config/mcp_config.json``."""

from __future__ import annotations

from pathlib import Path

from localmem.agents import base

CONFIG_RELATIVE_PATH = Path(".gemini") / "config" / "mcp_config.json"


class AntigravityWriter:
    """Merges localmem into Antigravity's user-level MCP config."""

    name = "Google Antigravity"
    slug = "antigravity"

    def is_detected(self, home: Path, cwd: Path) -> bool:
        """Return whether ``{home}/.gemini/`` exists."""
        return (home / ".gemini").exists()

    def target_path(self, home: Path, cwd: Path) -> Path | None:
        """Return ``{home}/.gemini/config/mcp_config.json``."""
        return home / CONFIG_RELATIVE_PATH

    def render_config(self, home: Path, cwd: Path) -> str:
        """Return the config document. Pure — touches no filesystem."""
        return base.render_json_document()

    def apply(self, home: Path, cwd: Path, *, dry_run: bool = False) -> base.ApplyResult:
        """Merge localmem in, preserving every other key and every other server."""
        return base.apply_json_config(self.name, home / CONFIG_RELATIVE_PATH, dry_run=dry_run)


WRITER = AntigravityWriter()
