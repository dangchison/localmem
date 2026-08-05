"""Database path resolution and workspace detection.

Nothing here touches the network; workspace detection shells out to ``git`` with a
fixed argument vector and degrades to directory-name detection when git is absent.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

DB_PATH_ENV_VAR = "LOCALMEM_DB"
DEFAULT_DB_RELATIVE_PATH = Path(".localmem") / "memory.db"
FALLBACK_WORKSPACE = "global"

#: Mode for a database directory this process creates: owner only. A directory that
#: already exists is never touched — its permissions are the user's decision, not ours
#: (``docs/design_decisions.md`` §32).
DB_DIRECTORY_MODE = 0o700

_GIT_TIMEOUT_SECONDS = 5


def resolve_db_path() -> Path:
    """Return the database location.

    Uses ``$LOCALMEM_DB`` when set, otherwise ``~/.localmem/memory.db``.

    Raises:
        ValueError: if ``$LOCALMEM_DB`` is set to an empty or blank value.
    """
    raw = os.environ.get(DB_PATH_ENV_VAR)
    if raw is None:
        return Path.home() / DEFAULT_DB_RELATIVE_PATH
    if not raw.strip():
        raise ValueError(
            f"{DB_PATH_ENV_VAR} is set but empty; unset it to use the default "
            f"~/{DEFAULT_DB_RELATIVE_PATH} or point it at a database file"
        )
    return Path(raw).expanduser()


def ensure_db_parent(path: Path) -> None:
    """Create the directory that holds the database file.

    A directory this call creates is restricted to :data:`DB_DIRECTORY_MODE`, so a
    default ``~/.localmem/`` is not readable by other accounts on a shared machine. A
    directory that was already there keeps whatever mode it had: the user may have
    chosen it deliberately, and silently tightening someone else's directory is a
    surprise, not a fix.

    Raises:
        OSError: if the directory cannot be created, with the offending path named.
    """
    parent = path.parent
    existed = parent.is_dir()
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise OSError(f"cannot create database directory {parent}: {exc}") from exc
    if not existed:
        restrict_mode(parent, DB_DIRECTORY_MODE)


def restrict_mode(path: Path, mode: int) -> None:
    """Set ``path`` to ``mode``, ignoring a filesystem that cannot express it.

    Permissions are a hardening measure, not a correctness requirement: a database on a
    filesystem with no POSIX modes (a mounted FAT volume, some network shares) must
    still open. The failure is swallowed for that reason and for no other.
    """
    try:
        path.chmod(mode)
    except OSError:
        return


def detect_workspace(cwd: Path | str | None = None) -> str:
    """Return the workspace name for ``cwd`` (defaults to the current directory).

    Resolution order: git repository root name, then the directory name, then
    ``"global"``. Never raises: a missing or failing ``git`` falls through.
    """
    base = Path(cwd) if cwd is not None else _current_directory()
    if base is None:
        return FALLBACK_WORKSPACE
    repo_name = _git_repo_name(base)
    if repo_name is not None:
        return repo_name
    return base.name or FALLBACK_WORKSPACE


def validate_workspace(workspace: str) -> str:
    """Return ``workspace`` stripped of surrounding whitespace.

    Raises:
        ValueError: if the value is empty or whitespace only.
    """
    stripped = workspace.strip()
    if not stripped:
        raise ValueError("workspace must be a non-empty name, e.g. --workspace my-project")
    return stripped


def _current_directory() -> Path | None:
    try:
        return Path.cwd()
    except OSError:
        return None


def _git_repo_name(cwd: Path) -> str | None:
    try:
        # Fixed argument vector, no shell, no user-supplied values.
        completed = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],  # noqa: S607 - resolved via PATH by design
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    toplevel = completed.stdout.strip()
    if not toplevel:
        return None
    return Path(toplevel).name or None
