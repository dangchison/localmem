"""Contract tests for the frozen MCP surface.

Two layers, per plan-m4 DD-14:

* direct calls into :mod:`localmem.mcp_server` — fast, and where the exact key sets,
  input validation and failure payloads are pinned;
* a real ``localmem serve`` subprocess driven by ``stdio_client`` — where §11's
  Definition of Done (initialize → list_tools → empty recall → add/recall round trip)
  is exercised over the wire.

The stdio layer uses ``anyio.run`` from ordinary synchronous test functions, so the
suite needs no async plugin and no dependency beyond what ``mcp`` already installs.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, TypeVar

import anyio
import pytest
from click.testing import CliRunner
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import CallToolResult, InitializeResult, ListToolsResult

from localmem import __version__, config, mcp_server
from localmem.cli import main

RESULT_KEYS = {
    "id",
    "content",
    "workspace",
    "kind",
    "source",
    "created_at",
    "score",
    "neighbors",
}
RECALL_KEYS = {"results", "core_memory", "message"}
ADD_KEYS = {"status", "id", "seen_count"}
CLI_COMMANDS = {"add", "search", "stats", "backfill", "dedupe", "gc", "serve"}

EMPTY_MESSAGE = "no memories yet — memories will accumulate as you work"
RFC3339_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

T = TypeVar("T")
Work = Callable[[ClientSession], Awaitable[T]]


# --------------------------------------------------------------------------- helpers


def over_stdio(db_path: Path, errlog: Path, work: Work[T]) -> T:
    """Spawn ``localmem serve`` as a real subprocess and run ``work`` against it.

    ``errlog`` receives the server's stderr; a test that fails mid-session can read it,
    and routing it to a file keeps pytest's captured stderr out of the child's stdio.
    """
    captured: list[T] = []

    async def session_body() -> None:
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "localmem.cli", "serve"],
            env={**os.environ, config.DB_PATH_ENV_VAR: str(db_path)},
        )
        with errlog.open("w", encoding="utf-8") as stream:
            async with stdio_client(parameters, errlog=stream) as (read, write):
                async with ClientSession(read, write) as session:
                    captured.append(await work(session))

    anyio.run(session_body)
    return captured[0]


def payload(result: CallToolResult) -> dict[str, Any]:
    """Return a tool result's JSON body, checking both encodings agree."""
    assert result.structured_content is not None
    text = "".join(block.text for block in result.content if block.type == "text")
    assert json.loads(text) == result.structured_content
    return result.structured_content


def seed(workspace: str, *contents: str) -> None:
    for content in contents:
        added = mcp_server.memory_add(content, workspace=workspace)
        assert added["status"] == "added", added


def row_count(db_path: Path) -> int:
    connection = sqlite3.connect(db_path)
    try:
        return int(connection.execute("SELECT COUNT(*) FROM memories").fetchone()[0])
    finally:
        connection.close()


# ------------------------------------------------------------------ AC4: command set


def test_cli_exposes_exactly_the_planned_commands() -> None:
    assert set(main.commands) == CLI_COMMANDS


def test_help_lists_serve() -> None:
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "serve" in result.output


# ------------------------------------------------------- AC5/AC6/AC7/AC10: real stdio


def test_stdio_initialize_and_list_tools(db_path: Path, tmp_path: Path) -> None:
    async def work(session: ClientSession) -> tuple[InitializeResult, ListToolsResult]:
        initialized = await session.initialize()
        return initialized, await session.list_tools()

    initialized, listed = over_stdio(db_path, tmp_path / "err.log", work)

    assert initialized.server_info.name == "localmem"
    assert initialized.server_info.version == __version__

    tools = {tool.name: tool for tool in listed.tools}
    assert set(tools) == {"memory_recall", "memory_add"}
    assert len(listed.tools) == 2
    assert tools["memory_recall"].description == (
        "Search the user's persistent cross-session memory. Call before answering "
        "questions about project history, past decisions, or user preferences."
    )
    assert tools["memory_add"].description == (
        "Save a durable fact, decision, or lesson to the user's persistent memory. "
        "Call when you learn something worth remembering across sessions."
    )


def test_stdio_recall_on_empty_database_is_friendly_not_an_error(
    db_path: Path, tmp_path: Path
) -> None:
    async def work(session: ClientSession) -> CallToolResult:
        await session.initialize()
        return await session.call_tool("memory_recall", {"query": "anything"})

    result = over_stdio(db_path, tmp_path / "err.log", work)

    assert result.is_error is False
    body = payload(result)
    assert set(body) == RECALL_KEYS
    assert body["results"] == []
    assert body["core_memory"] == ""
    assert body["message"] == EMPTY_MESSAGE


def test_stdio_add_then_recall_round_trips(db_path: Path, tmp_path: Path) -> None:
    async def work(session: ClientSession) -> tuple[CallToolResult, CallToolResult]:
        await session.initialize()
        added = await session.call_tool(
            "memory_add",
            {"content": "the deploy pipeline uses pnpm", "workspace": "roundtrip"},
        )
        recalled = await session.call_tool(
            "memory_recall", {"query": "deploy pipeline", "workspace": "roundtrip"}
        )
        return added, recalled

    added, recalled = over_stdio(db_path, tmp_path / "err.log", work)

    assert added.is_error is False
    assert set(payload(added)) == ADD_KEYS
    assert payload(added)["status"] == "added"

    assert recalled.is_error is False
    body = payload(recalled)
    assert [hit["content"] for hit in body["results"]] == ["the deploy pipeline uses pnpm"]
    assert body["message"] is None


def test_stdio_session_leaves_no_wal_sidecar(db_path: Path, tmp_path: Path) -> None:
    """DD-11/AC17: every tool call closes its connection, so the WAL checkpoints out."""

    async def work(session: ClientSession) -> None:
        await session.initialize()
        for index in range(5):
            await session.call_tool(
                "memory_add", {"content": f"fact number {index}", "workspace": "wal"}
            )
            await session.call_tool("memory_recall", {"query": "fact", "workspace": "wal"})

    over_stdio(db_path, tmp_path / "err.log", work)

    assert db_path.exists()
    assert not Path(f"{db_path}-wal").exists()
    assert not Path(f"{db_path}-shm").exists()


def test_importing_every_module_writes_nothing_to_stdout() -> None:
    """AC10: stdout is the protocol channel, so import time must not touch it."""
    probe = (
        "import importlib, pkgutil, localmem\n"
        "for module in pkgutil.iter_modules(localmem.__path__, 'localmem.'):\n"
        "    importlib.import_module(module.name)\n"
    )
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell, no external input
        [sys.executable, "-c", probe],
        capture_output=True,
        check=True,
    )
    assert completed.stdout == b""


# ------------------------------------------------------------- frozen payload shapes


def test_recall_payload_has_exactly_the_frozen_keys(db_path: Path) -> None:
    seed("shape", "the retriever fuses two views")
    body = mcp_server.memory_recall("retriever", workspace="shape")

    assert set(body) == RECALL_KEYS
    assert len(body["results"]) == 1
    hit = body["results"][0]
    assert set(hit) == RESULT_KEYS
    assert set(hit) & {"seen_count", "lexical_score", "relational_score", "session_id"} == set()
    assert all(set(neighbor) == {"id", "content"} for neighbor in hit["neighbors"])


def test_recall_payload_omits_core_memory_statistics(db_path: Path) -> None:
    seed("stats", "prefer pnpm over npm")
    body = mcp_server.memory_recall("pnpm", workspace="stats")

    assert "core_memory_tokens" not in body
    assert "core_memory_dropped" not in body


def test_core_memory_is_a_string_and_empty_when_there_is_none(db_path: Path) -> None:
    seed("core", "a plain note")
    assert mcp_server.memory_recall("note", workspace="core")["core_memory"] == ""

    mcp_server.memory_add("always use tabs", workspace="core", kind="core")
    assert "always use tabs" in mcp_server.memory_recall("note", workspace="core")["core_memory"]


def test_created_at_is_rfc3339_utc(db_path: Path) -> None:
    seed("clock", "an event happened")
    hit = mcp_server.memory_recall("event", workspace="clock")["results"][0]
    assert RFC3339_UTC.match(hit["created_at"]), hit["created_at"]


def test_unparsable_created_at_passes_through_instead_of_failing_the_recall(
    db_path: Path,
) -> None:
    seed("clock", "an event happened")
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("UPDATE memories SET created_at = 'not a timestamp'")
        connection.commit()
    finally:
        connection.close()

    hit = mcp_server.memory_recall("event", workspace="clock")["results"][0]
    assert hit["created_at"] == "not a timestamp"


def test_score_is_a_float_rounded_to_four_places(db_path: Path) -> None:
    seed("score", "alpha beta gamma", "beta gamma delta", "gamma delta epsilon")
    for hit in mcp_server.memory_recall("gamma", workspace="score", k=3)["results"]:
        assert isinstance(hit["score"], float)
        assert round(hit["score"], 4) == hit["score"]


def test_add_payload_has_exactly_the_frozen_keys(db_path: Path) -> None:
    body = mcp_server.memory_add("a durable fact", workspace="add")
    assert set(body) == ADD_KEYS
    assert body["status"] == "added"
    assert body["seen_count"] == 1
    assert isinstance(body["id"], int)


def test_adding_identical_content_twice_merges(db_path: Path) -> None:
    first = mcp_server.memory_add("use pnpm, not npm", workspace="dup")
    second = mcp_server.memory_add("use pnpm, not npm", workspace="dup")

    assert first["status"] == "added"
    assert second["status"] == "duplicate_merged"
    assert second["id"] == first["id"]
    assert second["seen_count"] == 2


# ---------------------------------------------------------------- workspace handling


def test_recall_with_workspace_all_spans_every_workspace(db_path: Path) -> None:
    seed("alpha", "the shared migration ran")
    seed("beta", "the shared migration was reverted")

    scoped = mcp_server.memory_recall("shared migration", workspace="alpha", k=20)
    every = mcp_server.memory_recall("shared migration", workspace="all", k=20)

    assert {hit["workspace"] for hit in scoped["results"]} == {"alpha"}
    assert {hit["workspace"] for hit in every["results"]} == {"alpha", "beta"}


def test_add_with_workspace_all_is_rejected_and_stores_nothing(db_path: Path) -> None:
    seed("real", "a memory that already exists")
    before = row_count(db_path)

    body = mcp_server.memory_add("this must not be stored", workspace="all")

    assert body == {
        "status": "error",
        "id": 0,
        "seen_count": 0,
        "message": body["message"],
    }
    assert body["message"].startswith(mcp_server.ERROR_PREFIX)
    assert "all" in body["message"]
    assert row_count(db_path) == before


def test_workspace_is_detected_at_call_time_not_at_startup(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "detect_workspace", lambda *_args, **_kwargs: "first")
    mcp_server.memory_add("a fact about the first project")
    monkeypatch.setattr(config, "detect_workspace", lambda *_args, **_kwargs: "second")
    mcp_server.memory_add("a fact about the second project")

    assert [hit["workspace"] for hit in mcp_server.memory_recall("fact", k=20)["results"]] == [
        "second"
    ]
    assert mcp_server.memory_recall("fact", workspace="all", k=20)["results"]
    assert {
        hit["workspace"]
        for hit in mcp_server.memory_recall("fact", workspace="all", k=20)["results"]
    } == {"first", "second"}


def test_blank_workspace_is_an_input_error(db_path: Path) -> None:
    assert mcp_server.memory_recall("q", workspace="   ")["message"].startswith(
        mcp_server.ERROR_PREFIX
    )
    assert mcp_server.memory_add("x", workspace="   ")["status"] == "error"


# ------------------------------------------------------------------ input validation


@pytest.mark.parametrize("kind", ["note", "trace", "core"])
def test_add_accepts_the_three_documented_kinds(db_path: Path, kind: str) -> None:
    body = mcp_server.memory_add(f"a {kind} memory", workspace="kinds", kind=kind)
    assert body["status"] == "added"


def test_add_rejects_the_imported_kind(db_path: Path) -> None:
    seed("kinds", "a memory that already exists")
    before = row_count(db_path)
    body = mcp_server.memory_add("smuggled in", workspace="kinds", kind="imported")

    assert body["status"] == "error"
    assert body["message"].startswith(mcp_server.ERROR_PREFIX)
    assert "imported" in body["message"]
    assert row_count(db_path) == before


def test_add_rejects_blank_content(db_path: Path) -> None:
    body = mcp_server.memory_add("   \n\t ", workspace="kinds")
    assert body["status"] == "error"
    assert body["message"].startswith(mcp_server.ERROR_PREFIX)


@pytest.mark.parametrize("k", [0, -1, 21, 100])
def test_out_of_range_k_returns_an_error_payload(db_path: Path, k: int) -> None:
    body = mcp_server.memory_recall("anything", workspace="range", k=k)

    assert set(body) == RECALL_KEYS
    assert body["results"] == []
    assert body["core_memory"] == ""
    assert body["message"].startswith(mcp_server.ERROR_PREFIX)


@pytest.mark.parametrize("k", [1, 20])
def test_k_boundaries_are_accepted(db_path: Path, k: int) -> None:
    seed("range", "a memory to find")
    assert mcp_server.memory_recall("memory", workspace="range", k=k)["message"] is None


# ------------------------------------------------------------------- never raise (DD-8)


@pytest.fixture
def corrupt_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "corrupt.db"
    path.write_bytes(b"this is not a SQLite database" * 64)
    monkeypatch.setenv(config.DB_PATH_ENV_VAR, str(path))
    return path


def test_recall_against_a_corrupt_database_degrades_instead_of_raising(
    corrupt_db: Path,
) -> None:
    body = mcp_server.memory_recall("anything")

    assert set(body) == RECALL_KEYS
    assert body["results"] == []
    assert body["core_memory"] == ""
    assert body["message"].startswith(mcp_server.ERROR_PREFIX)


def test_add_against_a_corrupt_database_degrades_instead_of_raising(corrupt_db: Path) -> None:
    body = mcp_server.memory_add("a fact", workspace="broken")

    assert body["status"] == "error"
    assert body["id"] == 0
    assert body["seen_count"] == 0
    assert body["message"].startswith(mcp_server.ERROR_PREFIX)


def test_stdio_reports_a_corrupt_database_without_a_protocol_error(
    corrupt_db: Path, tmp_path: Path
) -> None:
    async def work(session: ClientSession) -> tuple[CallToolResult, CallToolResult]:
        await session.initialize()
        recalled = await session.call_tool("memory_recall", {"query": "anything"})
        added = await session.call_tool("memory_add", {"content": "a fact", "workspace": "w"})
        return recalled, added

    recalled, added = over_stdio(corrupt_db, tmp_path / "err.log", work)

    assert recalled.is_error is False
    assert payload(recalled)["message"].startswith(mcp_server.ERROR_PREFIX)
    assert added.is_error is False
    assert payload(added)["status"] == "error"


def test_base_exception_still_propagates(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The tool boundary catches ``Exception``, never ``BaseException``."""

    def interrupt(*_args: object, **_kwargs: object) -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr(config, "detect_workspace", interrupt)
    with pytest.raises(KeyboardInterrupt):
        mcp_server.memory_recall("anything")
    with pytest.raises(KeyboardInterrupt):
        mcp_server.memory_add("anything")


# ------------------------------------------------------------------------ server wiring


def test_build_server_registers_both_tools_with_the_frozen_descriptions() -> None:
    server = mcp_server.build_server()
    tools = {tool.name: tool for tool in anyio.run(server.list_tools)}

    assert set(tools) == {"memory_recall", "memory_add"}
    assert tools["memory_recall"].description == mcp_server.RECALL_DESCRIPTION
    assert tools["memory_add"].description == mcp_server.ADD_DESCRIPTION
    assert server.name == "localmem"
    assert server.version == __version__


def test_serve_defaults_to_stdio(monkeypatch: pytest.MonkeyPatch) -> None:
    """DD-13: the transport is a parameter so HTTP stays a flag, not a rewrite."""
    seen: list[str] = []

    class Recorder:
        def run(self, transport: str) -> None:
            seen.append(transport)

    monkeypatch.setattr(mcp_server, "build_server", Recorder)
    mcp_server.serve()
    mcp_server.serve(transport="streamable-http")

    assert seen == ["stdio", "streamable-http"]


def test_logging_goes_to_stderr_and_is_configured_once() -> None:
    logger = logging.getLogger(mcp_server.__name__)
    original = list(logger.handlers)
    original_propagate = logger.propagate
    logger.handlers = []
    try:
        mcp_server._configure_logging()
        mcp_server._configure_logging()
        assert len(logger.handlers) == 1
        assert logger.handlers[0].stream is sys.stderr
        assert logger.propagate is False
    finally:
        logger.handlers = original
        logger.propagate = original_propagate
