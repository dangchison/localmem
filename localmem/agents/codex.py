"""Codex CLI writer for ``~/.codex/config.toml``: parse to decide, append to write.

Detection **parses** the file and looks up ``mcp_servers.localmem``. Writing stays
**append-only**, because rewriting from parsed data would destroy the comments and
formatting that append-only exists to preserve.

The split matters. TOML has many spellings that all bind the same key — a table header,
an array of tables, an inline table, a dotted key, a quoted or escaped key — and a
regex over ``[…]`` headers cannot see most of them. Three blockers came out of trying;
``docs/design_decisions.md`` §19 records them and why the reader is here.
"""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path

from localmem.agents import base

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - the 3.10 floor; the test suite runs on 3.13
    import tomli as tomllib

CONFIG_RELATIVE_PATH = Path(".codex") / "config.toml"

#: The table every MCP server is registered under, and localmem's key within it.
SERVERS_TABLE = "mcp_servers"
SERVER_KEY = "localmem"

#: Appended verbatim, re-terminated with the file's own line endings. The leading blank
#: line separates the block from whatever the file already ends with; it is dropped when
#: the file is created from scratch.
CONFIG_BLOCK = """
# Added by localmem init
[mcp_servers.localmem]
command = "localmem"
args = ["serve"]
"""

#: The line ending an appended block uses when the file has none to match.
DEFAULT_NEWLINE = "\n"

#: Candidate line endings, LF first so it wins a tie.
NEWLINES = ("\n", "\r\n", "\r")


def declares_localmem(text: str) -> bool:
    """Return whether ``text`` binds ``mcp_servers.localmem``, however it is spelled.

    Raises:
        tomllib.TOMLDecodeError: if ``text`` is not valid TOML.
    """
    document = tomllib.loads(text)
    servers = document.get(SERVERS_TABLE)
    return isinstance(servers, dict) and SERVER_KEY in servers


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
    """Return :data:`CONFIG_BLOCK` re-terminated with ``newline``.

    ``CONFIG_BLOCK`` holds no carriage returns, so a plain replacement is exact.
    """
    if newline == DEFAULT_NEWLINE:
        return CONFIG_BLOCK
    return CONFIG_BLOCK.replace(DEFAULT_NEWLINE, newline)


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
        """Return the TOML block to append. Pure — touches no filesystem."""
        return CONFIG_BLOCK

    def apply(self, home: Path, cwd: Path, *, dry_run: bool = False) -> base.ApplyResult:
        """Append the localmem table unless the config already binds it.

        The file is parsed to decide and never rewritten from that parse: it is either
        left completely untouched, or extended by exactly :data:`CONFIG_BLOCK`
        re-terminated with its own line endings.
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
            return base.ApplyResult(
                self.name,
                target,
                None,
                None,
                "already_present",
                f"{target} already registers {SERVERS_TABLE}.{SERVER_KEY}",
            )
        if dry_run:
            return base.ApplyResult(
                self.name, target, None, None, "merged", f"would append localmem to {target}"
            )
        return self._append(target, raw)

    def _create(self, target: Path, *, dry_run: bool) -> base.ApplyResult:
        if dry_run:
            return base.ApplyResult(
                self.name, target, None, None, "written", f"would create {target}"
            )
        try:
            base.write_atomic(target, CONFIG_BLOCK.lstrip("\n"))
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
    re-parsed, and must both parse and bind ``mcp_servers.localmem``. Appending to a
    config that already bound the key under a spelling the decision missed shows up here
    as a parse error — TOML forbids declaring the same key twice — which is precisely
    what the old regex-based net could not see, because it shared its blind spot with
    the decision it was supposed to be checking.
    """
    try:
        written = base.read_text_verbatim(target)
    except (OSError, UnicodeDecodeError) as exc:
        return f"could not be read back ({exc})"
    try:
        present = declares_localmem(written)
    except tomllib.TOMLDecodeError as exc:
        return f"no longer parses as TOML ({exc})"
    if not present:
        return f"does not bind {SERVERS_TABLE}.{SERVER_KEY}"
    return None


WRITER = CodexWriter()
