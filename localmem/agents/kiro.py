"""AWS Kiro writer: workspace ``./.kiro/settings/mcp.json``, else the user-level one."""

from __future__ import annotations

from pathlib import Path

from localmem.agents import base

KIRO_DIR_NAME = ".kiro"
SETTINGS_RELATIVE_PATH = Path("settings") / "mcp.json"


class KiroWriter:
    """Merges localmem into Kiro's MCP settings, workspace level taking precedence."""

    name = "AWS Kiro"
    slug = "kiro"

    def is_detected(self, home: Path, cwd: Path) -> bool:
        """Return whether ``{home}/.kiro/`` or ``{cwd}/.kiro/`` exists."""
        return (home / KIRO_DIR_NAME).exists() or (cwd / KIRO_DIR_NAME).exists()

    def target_path(self, home: Path, cwd: Path) -> Path | None:
        """Return the workspace settings file when ``{cwd}/.kiro/`` exists, else the user one."""
        return _settings_path(home, cwd)

    def render_config(self, home: Path, cwd: Path) -> str:
        """Return the config document. Reads and writes no config file."""
        return base.render_json_document()

    def apply(
        self, home: Path, cwd: Path, *, dry_run: bool = False, repair: bool = False
    ) -> base.ApplyResult:
        """Merge localmem in, preserving every other key and every other server."""
        return base.apply_json_config(
            self.name, _settings_path(home, cwd), dry_run=dry_run, repair=repair
        )


def _settings_path(home: Path, cwd: Path) -> Path:
    """Return the settings file Kiro reads: workspace level if present, else user level."""
    if (cwd / KIRO_DIR_NAME).exists():
        return cwd / KIRO_DIR_NAME / SETTINGS_RELATIVE_PATH
    return home / KIRO_DIR_NAME / SETTINGS_RELATIVE_PATH


WRITER = KiroWriter()
