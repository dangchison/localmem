"""Resolve the absolute path of the installed ``localmem`` console script.

Every agent config localmem writes has to name a command the agent can actually execute.
A bare ``localmem`` only works for a process that inherited a login shell's PATH, and a
desktop app does not: measured on macOS, ``launchctl getenv PATH`` is **empty**, so an
application launched from the Dock hands its MCP servers ``/usr/bin:/bin:/usr/sbin:/sbin``
and nothing else. ``env -i /bin/sh -c 'command -v localmem'`` finds nothing, and an MCP
server that cannot be spawned fails **silently** — no error surfaces anywhere the user
looks. That is the whole of defect 1 (``docs/design_decisions.md`` §48).

This module is the single source of that path. It is deliberately a leaf: it imports
nothing from :mod:`localmem`, so every writer, the printed ``claude mcp add`` line and the
Codex TOML block all read the same answer.
"""

from __future__ import annotations

import functools
import os
import shutil
import sys
import tempfile
from pathlib import Path

#: The console script ``pyproject.toml`` declares, and the name searched for on PATH.
SERVER_NAME = "localmem"

#: The install that gives localmem a stable path, named verbatim in every refusal so the
#: user is told the fix rather than left to find it.
INSTALL_COMMAND = "uv tool install git+https://github.com/dangchison/localmem.git"

#: Directory names that mark a path as living in a cache rather than an installation.
#: Measured: ``uvx localmem`` resolves to
#: ``~/.cache/uv/archive-v0/<hash>/bin/localmem`` and :func:`shutil.which` **finds it**,
#: because uvx puts that directory on PATH for the child process. Detecting the ephemeral
#: case therefore cannot be "resolution failed"; it has to be a property of the path.
#: ``Caches`` covers the macOS spelling, ``.cache`` the XDG one that uv and pipx use.
CACHE_DIR_NAMES = frozenset({".cache", "Caches"})

#: Environment variables naming a cache root explicitly. Checked in addition to
#: :data:`CACHE_DIR_NAMES`, because either may point somewhere with no telling name.
CACHE_ROOT_VARIABLES = ("UV_CACHE_DIR", "XDG_CACHE_HOME", "PIPX_HOME")


class UnresolvableCommandError(RuntimeError):
    """localmem has no stable absolute path, so no config may be written.

    Raised rather than falling back to the bare name. A config pointing at a cache that
    uv garbage-collects is *worse* than no config: it fails silently, weeks later, in an
    app the user is not looking at — which is the exact failure this release fixes.
    """


def resolve_server_command() -> str:
    """Return the absolute path agent configs should invoke.

    Resolution order — :func:`shutil.which` first, ``sys.argv[0]`` only as a fallback:

    * ``shutil.which(SERVER_NAME)`` answers "what does ``localmem`` mean on this
      machine's PATH", which is the same question every other tool the user runs will
      ask. It returns an existing, executable file by construction, so :data:`AC1.1
      <localmem.agents.base.server_entry>`'s guarantee comes for free;
    * ``sys.argv[0]`` names the exact interpreter entry point of *this* process, which is
      more specific but not more reliable. It may be ``-c``, a ``-m`` module path, or the
      script of whatever wrapper is running (``pytest``, a hook); and it may be
      **relative** — resolving it after any :func:`os.chdir` yields a confidently wrong
      absolute path with no error. It is therefore accepted only when PATH has no answer
      *and* it names the ``localmem`` console script itself.

    ``sys.executable`` is not a candidate at all: it points at the Python interpreter, not
    at the console script, so the emitted command would start a REPL.

    Resolved once and memoized — a single CLI run writes up to four configs and prints a
    fifth command, and they must agree (AC1.3).

    Raises:
        UnresolvableCommandError: if nothing resolves, or the resolution lands in a cache
            directory (``uvx``, ``pipx run``, a temporary virtualenv). The message names
            :data:`INSTALL_COMMAND`.
    """
    return _resolve()


def is_ephemeral(path: Path) -> bool:
    """Return whether ``path`` lives somewhere that gets garbage-collected.

    Both the path itself and its fully-resolved form are tested, so a stable-looking
    symlink pointing into a cache is still caught. The converse case is why the symlink
    is not simply resolved away first: ``uv tool install`` leaves
    ``~/.local/bin/localmem`` pointing at ``~/.local/share/uv/tools/localmem/bin/localmem``,
    and neither of those is a cache.
    """
    return any(_under_a_cache(candidate) for candidate in (path, Path(os.path.realpath(path))))


@functools.lru_cache(maxsize=1)
def _resolve() -> str:
    """Do the work behind :func:`resolve_server_command`, once per process."""
    found = _candidate()
    if found is None:
        raise UnresolvableCommandError(
            f"cannot find an installed {SERVER_NAME!r} command to point agent configs at. "
            f"A config has to name an absolute path, because an app launched from the "
            f"desktop does not inherit your shell's PATH. Install it with "
            f"`{INSTALL_COMMAND}` and re-run this command."
        )
    if is_ephemeral(found):
        raise UnresolvableCommandError(
            f"{SERVER_NAME} is running from {found}, which is a cache directory that gets "
            f"cleaned up — an `{SERVER_NAME}` run through `uvx` or `pipx run` has no "
            f"lasting path. Writing that path into an agent config would work today and "
            f"fail silently once the cache is pruned, so nothing was written. Install it "
            f"with `{INSTALL_COMMAND}` and re-run this command."
        )
    return str(found)


def _candidate() -> Path | None:
    """Return the best absolute path for the console script, or ``None``."""
    on_path = shutil.which(SERVER_NAME)
    if on_path is not None:
        return Path(os.path.abspath(on_path))
    return _argv_candidate()


def _argv_candidate() -> Path | None:
    """Return ``sys.argv[0]`` when it is the ``localmem`` console script, else ``None``.

    The name check is what keeps a wrapper out of the config: under pytest ``sys.argv[0]``
    is ``.venv/bin/pytest``, which exists and is executable and would otherwise be written
    into four agent configs as the MCP server command.
    """
    if not sys.argv or not sys.argv[0]:
        return None
    argv0 = Path(os.path.abspath(sys.argv[0]))
    if argv0.name != SERVER_NAME:
        return None
    if not argv0.is_file() or not os.access(argv0, os.X_OK):
        return None
    return argv0


def _under_a_cache(path: Path) -> bool:
    """Return whether ``path`` sits under a cache root or a temporary directory."""
    if CACHE_DIR_NAMES & set(path.parts):
        return True
    return any(_is_under(path, root) for root in _cache_roots())


def _cache_roots() -> tuple[Path, ...]:
    """Return the cache and temp roots to test against, read fresh from the environment."""
    roots = [Path(tempfile.gettempdir())]
    for variable in CACHE_ROOT_VARIABLES:
        value = os.environ.get(variable)
        if value:
            roots.append(Path(value))
    return tuple(Path(os.path.abspath(root)) for root in roots)


def _is_under(path: Path, root: Path) -> bool:
    """Return whether ``path`` is ``root`` or lives inside it."""
    try:
        return path == root or path.is_relative_to(root)
    except (OSError, ValueError):  # pragma: no cover - only a malformed root reaches this
        return False
