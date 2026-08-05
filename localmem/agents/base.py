"""Shared types and filesystem primitives for the per-agent config writers.

Two rules govern everything in :mod:`localmem.agents` and are enforced here:

* **The user's home directory is never resolved inside this package.** ``home`` and
  ``cwd`` are parameters of every entry point, so a caller that passes a temporary
  directory cannot reach a real config file. See ``docs/design_decisions.md`` §22.
* **An existing config is merged, never replaced.** A file that cannot be parsed is
  refused outright: nothing is written and nothing is backed up (§20).
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

#: The MCP server key every agent config registers localmem under.
SERVER_KEY = "localmem"

#: The JSON object that holds one entry per MCP server, in all three JSON formats.
MCP_SERVERS_KEY = "mcpServers"

SERVER_COMMAND = "localmem"
SERVER_ARGS = ("serve",)

JSON_INDENT = 2
BACKUP_SUFFIX = ".bak"

#: Mode a new config file is created with, before the umask is applied.
DEFAULT_FILE_MODE = 0o666

#: What :meth:`AgentWriter.apply` did, or would have done under ``dry_run``.
Action = Literal["written", "merged", "already_present", "printed", "refused", "skipped"]


class MalformedConfigError(ValueError):
    """An existing config file cannot be parsed, so it must not be rewritten."""


@dataclass(frozen=True)
class ApplyResult:
    """The outcome of one writer's :meth:`AgentWriter.apply`.

    Attributes:
        agent: the writer's display name.
        path: the config file involved, or ``None`` for a print-only agent.
        backup: the ``.bak`` copy taken before modifying an existing file.
        printed_command: a command for the user to run instead of a file edit.
        action: what happened; ``"refused"`` means nothing at all was written.
        detail: one human-readable line explaining ``action``.
    """

    agent: str
    path: Path | None
    backup: Path | None
    printed_command: str | None
    action: Action
    detail: str


class AgentWriter(Protocol):
    """The interface every agent config writer implements.

    ``render_config`` is pure by contract: it performs no filesystem access at all, so
    tests can exercise the rendered document without a sandbox.
    """

    name: str
    slug: str

    def is_detected(self, home: Path, cwd: Path) -> bool:
        """Return whether this agent appears to be installed."""
        ...

    def target_path(self, home: Path, cwd: Path) -> Path | None:
        """Return the config file to write, or ``None`` for a print-only agent."""
        ...

    def render_config(self, home: Path, cwd: Path) -> str:
        """Return the config document or block. Pure — touches no filesystem."""
        ...

    def apply(self, home: Path, cwd: Path, *, dry_run: bool = False) -> ApplyResult:
        """Register localmem for this agent, merging into whatever already exists."""
        ...


def server_entry() -> dict[str, object]:
    """Return the MCP server entry localmem registers, as a fresh mutable dict."""
    return {"command": SERVER_COMMAND, "args": list(SERVER_ARGS)}


def render_json_document() -> str:
    """Return a complete config document registering only localmem.

    Pure: this is what a writer emits when no config file exists yet, and what the CLI
    prints for the user to paste when an existing file is refused.
    """
    return _dump_json({MCP_SERVERS_KEY: {SERVER_KEY: server_entry()}})


def merge_json_document(raw: str) -> str | None:
    """Return ``raw`` with localmem added, or ``None`` when it is already registered.

    Every other key and every other server survives: only
    ``mcpServers.localmem`` is touched, and ``mcpServers`` is created only if absent.

    Raises:
        MalformedConfigError: if ``raw`` is not a JSON object, or ``mcpServers`` is
            present but is not an object. The caller must then write nothing.
    """
    try:
        parsed: object = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MalformedConfigError(
            f"not valid JSON: {exc.msg} at line {exc.lineno} column {exc.colno}"
        ) from exc
    if not isinstance(parsed, dict):
        raise MalformedConfigError(f"the top level is a {type(parsed).__name__}, not a JSON object")
    document: dict[str, object] = parsed
    servers = document.get(MCP_SERVERS_KEY)
    if servers is None:
        servers = {}
        document[MCP_SERVERS_KEY] = servers
    elif not isinstance(servers, dict):
        raise MalformedConfigError(
            f"{MCP_SERVERS_KEY!r} is a {type(servers).__name__}, not a JSON object"
        )
    entries: dict[str, object] = servers
    if entries.get(SERVER_KEY) == server_entry():
        return None
    entries[SERVER_KEY] = server_entry()
    return _dump_json(document)


def apply_json_config(agent: str, target: Path, *, dry_run: bool) -> ApplyResult:
    """Merge localmem into the JSON config at ``target``, or create it.

    Shared by every JSON-format agent. An unreadable or unparseable file is refused —
    no write, no backup — because replacing it would silently drop the user's other
    MCP servers.
    """
    if not target.exists():
        if dry_run:
            return ApplyResult(agent, target, None, None, "written", f"would create {target}")
        try:
            write_atomic(target, render_json_document())
        except OSError as exc:
            return refusal(agent, target, f"cannot be created: {exc}")
        return ApplyResult(agent, target, None, None, "written", f"created {target}")

    try:
        raw = read_text_verbatim(target)
    except (OSError, UnicodeDecodeError) as exc:
        return refusal(agent, target, f"cannot be read: {exc}")
    try:
        merged = merge_json_document(raw)
    except MalformedConfigError as exc:
        return refusal(agent, target, str(exc))
    if merged is None:
        return ApplyResult(
            agent, target, None, None, "already_present", f"{target} already registers localmem"
        )
    if dry_run:
        return ApplyResult(
            agent, target, None, None, "merged", f"would merge localmem into {target}"
        )
    try:
        backup = replace_atomically(target, merged)
    except OSError as exc:
        return refusal(agent, target, f"cannot be updated: {exc}")
    return ApplyResult(
        agent,
        target,
        backup,
        None,
        "merged",
        f"merged localmem into {target} (previous contents kept at {backup.name})",
    )


def back_up(target: Path) -> Path:
    """Copy ``target`` to ``<name><ext>.bak``, replacing any previous backup.

    Returns:
        The backup path, which holds the original bytes.
    """
    backup = target.with_name(target.name + BACKUP_SUFFIX)
    shutil.copy2(target, backup)
    return backup


def replace_atomically(target: Path, content: str) -> Path:
    """Back up ``target``, then replace it with ``content``.

    Returns:
        The backup path, holding the original bytes.

    Raises:
        OSError: if the backup or the write fails. A failed *write* removes the backup
            it just took, so an unsuccessful attempt leaves no trace beside the config:
            the original is still in place, untouched, and there is no stray ``.bak``.
    """
    backup = back_up(target)
    try:
        write_atomic(target, content)
    except OSError:
        backup.unlink(missing_ok=True)
        raise
    return backup


def write_atomic(target: Path, content: str) -> None:
    """Write ``content`` to ``target`` through a temp file in the same directory.

    ``os.replace`` is atomic on POSIX, so a config file is never observed
    half-written. Newline translation is disabled on both read and write so that
    appending to a CRLF file does not silently rewrite every line ending.

    Permissions follow what the user would get by hand: an existing file keeps its own
    mode, a new one takes the umask default. Without this a config created by localmem
    would be ``0600`` — the mode ``mkstemp`` gives its temp file.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp"
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if target.exists():
            shutil.copymode(target, temp_path)
        else:
            os.chmod(temp_path, default_file_mode())
        os.replace(temp_path, target)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def default_file_mode() -> int:
    """Return the mode a newly created file would get from the current umask.

    The umask can only be read by setting it, so it is set to 0 and immediately put
    back. localmem's writers run from a single-threaded CLI process, so nothing else
    can create a file inside that window.
    """
    current = os.umask(0)
    os.umask(current)
    return DEFAULT_FILE_MODE & ~current


def read_text_verbatim(target: Path) -> str:
    """Read ``target`` as UTF-8 with newline translation disabled.

    Raises:
        OSError: if the file cannot be read.
        UnicodeDecodeError: if it is not valid UTF-8.
    """
    with target.open("r", encoding="utf-8", newline="") as stream:
        return stream.read()


def refusal(agent: str, target: Path, reason: str) -> ApplyResult:
    """Return a ``refused`` result: nothing written, nothing backed up, reason named."""
    return ApplyResult(
        agent,
        target,
        None,
        None,
        "refused",
        f"{target} {reason}; nothing was written and no backup was made",
    )


def _dump_json(document: dict[str, object]) -> str:
    return json.dumps(document, indent=JSON_INDENT, ensure_ascii=False) + "\n"
