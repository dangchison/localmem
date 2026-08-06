"""Codex CLI writer for ``~/.codex/config.toml``: parse to decide, append to write.

Detection **parses** the file and looks up ``mcp_servers.localmem``. Writing stays
**append-only**, because rewriting from parsed data would destroy the comments and
formatting that append-only exists to preserve.

The split matters. TOML has many spellings that all bind the same key — a table header,
an array of tables, an inline table, a dotted key, a quoted or escaped key — and a
regex over ``[…]`` headers cannot see most of them. Three blockers came out of trying;
``docs/design_decisions.md`` §19 records them and why the reader is here.

v0.5.1 adds the one edit that is not an append: ``--repair`` has to change an existing
``command`` value, and appending a second ``[mcp_servers.localmem]`` is a TOML duplicate
key. It stays inside the same discipline — **propose a one-line edit, then prove it by
re-parsing** against the document the caller expects, so a spelling the line matcher got
wrong is rejected instead of written (``docs/design_decisions.md`` §51).
"""

from __future__ import annotations

import contextlib
import copy
import os
import re
import sys
from collections.abc import Iterator
from pathlib import Path

from localmem.agents import base, command

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - the 3.10 floor; the test suite runs on 3.13
    import tomli as tomllib

CONFIG_RELATIVE_PATH = Path(".codex") / "config.toml"

#: The table every MCP server is registered under, and localmem's key within it.
SERVERS_TABLE = "mcp_servers"
SERVER_KEY = "localmem"

#: The block :func:`config_block` renders, with the resolved command still to substitute.
#: ``{command}`` is replaced literally rather than through :meth:`str.format`, so a path
#: holding a brace cannot turn into a format field.
CONFIG_BLOCK_TEMPLATE = """
# Added by localmem init
[mcp_servers.localmem]
command = {command}
args = ["serve"]
"""

#: The placeholder :data:`CONFIG_BLOCK_TEMPLATE` carries.
COMMAND_PLACEHOLDER = "{command}"

#: The line ending an appended block uses when the file has none to match.
DEFAULT_NEWLINE = "\n"

#: Candidate line endings, LF first so it wins a tie.
NEWLINES = ("\n", "\r\n", "\r")

#: Splits a line at its ``command =``, whatever precedes the key — a plain key inside a
#: table, a dotted key, or an inline table. Deliberately loose: every candidate it
#: produces is checked by re-parsing, so a false positive costs a rejected candidate and
#: never a wrong write.
_COMMAND_ASSIGNMENT_RE = re.compile(r"^(?P<head>.*?\bcommand\s*=\s*)(?P<value>.*)$")

#: Characters a TOML basic string must escape, in the order they are applied — the
#: backslash first, or the escapes added after it would be escaped again.
_TOML_ESCAPES = (("\\", "\\\\"), ('"', '\\"'), ("\n", "\\n"), ("\r", "\\r"), ("\t", "\\t"))


def config_block() -> str:
    """Return the TOML block registering localmem at its resolved absolute path."""
    return CONFIG_BLOCK_TEMPLATE.replace(
        COMMAND_PLACEHOLDER, toml_string(command.resolve_server_command())
    )


def toml_string(value: str) -> str:
    """Return ``value`` as a TOML basic string, quotes and backslashes escaped.

    A path is normally boring, but it is user data reaching a document localmem promises
    stays parseable — and ``tomllib`` is read-only, so there is no writer to defer to.
    """
    escaped = value
    for character, replacement in _TOML_ESCAPES:
        escaped = escaped.replace(character, replacement)
    return f'"{escaped}"'


def declares_localmem(text: str) -> bool:
    """Return whether ``text`` binds ``mcp_servers.localmem``, however it is spelled.

    Raises:
        tomllib.TOMLDecodeError: if ``text`` is not valid TOML.
    """
    document = tomllib.loads(text)
    servers = document.get(SERVERS_TABLE)
    return isinstance(servers, dict) and SERVER_KEY in servers


def declared_command(text: str) -> str | None:
    """Return the command ``mcp_servers.localmem`` binds, or ``None`` if it binds none.

    Raises:
        tomllib.TOMLDecodeError: if ``text`` is not valid TOML.
    """
    document = tomllib.loads(text)
    servers = document.get(SERVERS_TABLE)
    if not isinstance(servers, dict):
        return None
    return base.declared_command(servers.get(SERVER_KEY))


def repaired(text: str, new_command: str) -> str | None:
    """Return ``text`` with localmem's ``command`` changed, or ``None`` if it cannot be.

    Propose, then prove. Each line that assigns ``command`` is rewritten in turn, and a
    candidate is accepted only when re-parsing it yields *exactly* the original document
    with that one value changed — so every other table, every other server and every
    other key are proved untouched rather than assumed to be. Comments and formatting
    survive because only one line's value moved.

    ``None`` means no candidate passed: the caller must write nothing and tell the user
    to edit the file by hand.
    """
    original = tomllib.loads(text)
    servers = original.get(SERVERS_TABLE)
    if not isinstance(servers, dict) or not isinstance(servers.get(SERVER_KEY), dict):
        return None
    expected = copy.deepcopy(original)
    expected[SERVERS_TABLE][SERVER_KEY][base.COMMAND_KEY] = new_command
    for candidate in _command_edits(text, new_command):
        try:
            if tomllib.loads(candidate) == expected:
                return candidate
        except tomllib.TOMLDecodeError:
            continue
    return None


def _command_edits(text: str, new_command: str) -> Iterator[str]:
    """Yield ``text`` with one ``command`` assignment rewritten, one candidate per try.

    Two candidates per matching line: the first keeps whatever follows a ``#`` on that
    line, so ``command = "localmem"  # added by hand`` keeps its comment; the second
    replaces the whole tail, for the case where that ``#`` was inside the old value.
    """
    literal = toml_string(new_command)
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        body, ending = _split_line_ending(line)
        match = _COMMAND_ASSIGNMENT_RE.match(body)
        if match is None:
            continue
        head, value = match["head"], match["value"]
        comment = value[value.index("#") :] if "#" in value else ""
        for tail in (f"{literal}  {comment}".rstrip(), literal) if comment else (literal,):
            edited = list(lines)
            edited[index] = head + tail + ending
            yield "".join(edited)


def _split_line_ending(line: str) -> tuple[str, str]:
    """Split ``line`` into its text and its line ending, either of which may be empty."""
    for ending in ("\r\n", "\n", "\r"):
        if line.endswith(ending):
            return line[: -len(ending)], ending
    return line, ""


def dominant_newline(text: str) -> str:
    r"""Return the line ending ``text`` mostly uses, defaulting to ``\n``.

    An appended block has to match, or a file localmem promised to preserve
    byte-for-byte comes back with mixed line endings. Ties go to ``\n``.
    """
    crlf = text.count("\r\n")
    counts = {"\r\n": crlf, "\n": text.count("\n") - crlf, "\r": text.count("\r") - crlf}
    # max() returns the first candidate holding the maximum, and NEWLINES leads with
    # "\n", so a tie resolves to LF.
    best = max(NEWLINES, key=lambda ending: counts[ending])
    return best if counts[best] else DEFAULT_NEWLINE


def block_with_newline(newline: str) -> str:
    """Return :func:`config_block` re-terminated with ``newline``.

    The block holds no carriage returns of its own, so a plain replacement is exact.
    """
    block = config_block()
    if newline == DEFAULT_NEWLINE:
        return block
    return block.replace(DEFAULT_NEWLINE, newline)


class CodexWriter:
    """Registers localmem in the Codex CLI config by appending its table."""

    name = "Codex CLI"
    slug = "codex"

    def is_detected(self, home: Path, cwd: Path) -> bool:
        """Return whether ``{home}/.codex/`` exists."""
        return (home / ".codex").exists()

    def target_path(self, home: Path, cwd: Path) -> Path | None:
        """Return ``{home}/.codex/config.toml``."""
        return home / CONFIG_RELATIVE_PATH

    def render_config(self, home: Path, cwd: Path) -> str:
        """Return the TOML block to append. Reads and writes no config file."""
        return config_block()

    def apply(
        self, home: Path, cwd: Path, *, dry_run: bool = False, repair: bool = False
    ) -> base.ApplyResult:
        """Append the localmem table unless the config already binds it.

        The file is parsed to decide and never rewritten from that parse: it is either
        left completely untouched, extended by exactly :func:`config_block` re-terminated
        with its own line endings, or — under ``repair`` — has the single ``command``
        line of an existing localmem table replaced, proved by :func:`repaired`.
        """
        target = home / CONFIG_RELATIVE_PATH
        if not target.exists():
            return self._create(target, dry_run=dry_run)

        try:
            raw = base.read_text_verbatim(target)
        except (OSError, UnicodeDecodeError) as exc:
            return base.refusal(self.name, target, f"cannot be read: {exc}")

        try:
            present = declares_localmem(raw)
        except tomllib.TOMLDecodeError as exc:
            # The same rule the JSON writers follow: a config localmem cannot understand
            # is one it must not touch. See docs/design_decisions.md §20.
            return base.refusal(self.name, target, f"is not valid TOML ({exc})")

        if present:
            return self._already_registered(target, raw, dry_run=dry_run, repair=repair)
        if dry_run:
            return base.ApplyResult(
                self.name, target, None, None, "merged", f"would append localmem to {target}"
            )
        return self._append(target, raw)

    def _already_registered(
        self, target: Path, raw: str, *, dry_run: bool, repair: bool
    ) -> base.ApplyResult:
        """Report — or, under ``repair``, correct — a localmem table that is already there."""
        previous = declared_command(raw)
        resolved = command.resolve_server_command()
        if previous == resolved:
            return base.ApplyResult(
                self.name,
                target,
                None,
                None,
                "already_present",
                base.already_present_detail(target),
            )
        if not repair:
            return base.ApplyResult(
                self.name, target, None, None, "stale", base.stale_detail(target, previous)
            )
        corrected = repaired(raw, resolved)
        if corrected is None:
            return base.ApplyResult(
                self.name,
                target,
                None,
                None,
                "refused",
                f"{target} registers {SERVERS_TABLE}.{SERVER_KEY} in a spelling localmem "
                f"cannot edit without rewriting the file, which would lose its comments; "
                f"nothing was written — set command to {resolved!r} by hand",
            )
        if dry_run:
            return base.ApplyResult(
                self.name,
                target,
                None,
                None,
                "repaired",
                f"would update localmem in {target}",
            )
        return self._replace(target, corrected, previous)

    def _replace(self, target: Path, corrected: str, previous: str | None) -> base.ApplyResult:
        """Write a repaired document, verifying it and rolling back if it does not hold."""
        try:
            backup = base.replace_atomically(target, corrected)
        except OSError as exc:
            return base.refusal(self.name, target, f"cannot be updated: {exc}")
        problem = _verify(target)
        if problem is not None:
            return self._roll_back(target, backup, problem)
        return base.ApplyResult(
            self.name,
            target,
            backup,
            None,
            "repaired",
            f"{base.repaired_detail(target, previous)} (previous contents kept at {backup.name})",
        )

    def _create(self, target: Path, *, dry_run: bool) -> base.ApplyResult:
        if dry_run:
            return base.ApplyResult(
                self.name, target, None, None, "written", f"would create {target}"
            )
        try:
            base.write_atomic(target, config_block().lstrip("\n"))
        except OSError as exc:
            return base.refusal(self.name, target, f"cannot be created: {exc}")
        problem = _verify(target)
        if problem is None:
            return base.ApplyResult(self.name, target, None, None, "written", f"created {target}")
        with contextlib.suppress(OSError):
            target.unlink()
        return base.ApplyResult(
            self.name,
            target,
            None,
            None,
            "refused",
            f"{target} was not created: what localmem wrote {problem}, so it was removed",
        )

    def _append(self, target: Path, raw: str) -> base.ApplyResult:
        # The block adopts the file's own line endings. write_atomic passes the text
        # through untranslated, so a CRLF config that localmem appends to stays pure
        # CRLF instead of coming back with two conventions mixed into it.
        block = block_with_newline(dominant_newline(raw))
        try:
            backup = base.replace_atomically(target, raw + block)
        except OSError as exc:
            return base.refusal(self.name, target, f"cannot be updated: {exc}")
        problem = _verify(target)
        if problem is None:
            return base.ApplyResult(
                self.name,
                target,
                backup,
                None,
                "merged",
                f"appended localmem to {target} (previous contents kept at {backup.name})",
            )
        return self._roll_back(target, backup, problem)

    def _roll_back(self, target: Path, backup: Path, problem: str) -> base.ApplyResult:
        """Undo a write that failed the post-write check, restoring from the backup."""
        try:
            # os.replace restores the original and removes the backup in one atomic
            # step, so a rolled-back attempt leaves the directory exactly as it was.
            os.replace(backup, target)
        except OSError as exc:
            return base.ApplyResult(
                self.name,
                target,
                backup,
                None,
                "refused",
                f"{target} {problem} after writing, and restoring it from {backup.name} "
                f"also failed ({exc}); the original contents are still in {backup.name}",
            )
        return base.ApplyResult(
            self.name,
            target,
            None,
            None,
            "refused",
            f"{target} was left unchanged: after writing it {problem}, which would stop "
            "Codex reading the file, so the write was rolled back",
        )


def _verify(target: Path) -> str | None:
    """Return why the file localmem just wrote is unsafe, or ``None`` if it is fine.

    An independent check, not a re-run of the decision: the written file is re-read and
    re-parsed, and must parse, bind ``mcp_servers.localmem``, and bind it to the command
    this install resolved to. Appending to a config that already bound the key under a
    spelling the decision missed shows up here as a parse error — TOML forbids declaring
    the same key twice — which is precisely what the old regex-based net could not see,
    because it shared its blind spot with the decision it was supposed to be checking.

    The command check is v0.5.1's: a file that parses and binds the key but points at
    something else is exactly the failure the release exists to remove, so leaving it
    unchecked would let a repair silently do nothing.
    """
    try:
        written = base.read_text_verbatim(target)
    except (OSError, UnicodeDecodeError) as exc:
        return f"could not be read back ({exc})"
    try:
        present = declares_localmem(written)
        bound = declared_command(written)
    except tomllib.TOMLDecodeError as exc:
        return f"no longer parses as TOML ({exc})"
    if not present:
        return f"does not bind {SERVERS_TABLE}.{SERVER_KEY}"
    resolved = command.resolve_server_command()
    if bound != resolved:
        return f"binds {SERVERS_TABLE}.{SERVER_KEY}.command to {bound!r}, not {resolved!r}"
    return None


WRITER = CodexWriter()
