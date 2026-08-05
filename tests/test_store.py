"""Tests for the M1 core store: config, migrations, dedup, search and the CLI."""

from __future__ import annotations

import gc
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from localmem import config, db, store
from localmem.cli import main
from localmem.dedup import content_hash, normalize

VIETNAMESE = "Dùng pnpm thay vì npm cho dự án này"


# --- config -----------------------------------------------------------------


def test_resolve_db_path_uses_env(db_path: Path) -> None:
    assert config.resolve_db_path() == db_path


def test_resolve_db_path_expands_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(config.DB_PATH_ENV_VAR, "~/somewhere/memory.db")
    assert config.resolve_db_path() == Path.home() / "somewhere" / "memory.db"


def test_resolve_db_path_rejects_blank_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(config.DB_PATH_ENV_VAR, "   ")
    with pytest.raises(ValueError, match="set but empty"):
        config.resolve_db_path()


def test_detect_workspace_uses_git_root(tmp_path: Path) -> None:
    repo = tmp_path / "my-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    nested = repo / "src" / "pkg"
    nested.mkdir(parents=True)
    assert config.detect_workspace(nested) == "my-repo"


def test_detect_workspace_falls_back_to_directory_name(tmp_path: Path) -> None:
    plain = tmp_path / "plain-dir"
    plain.mkdir()
    assert config.detect_workspace(plain) == "plain-dir"


def test_detect_workspace_falls_back_to_global() -> None:
    assert config.detect_workspace(Path("/")) == config.FALLBACK_WORKSPACE


def test_validate_workspace_rejects_blank() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        config.validate_workspace("   ")


# --- dedup normalization ----------------------------------------------------


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("use pnpm not npm", "  USE   pnpm  not npm "),
        ("use pnpm not npm", "- Use pnpm not npm"),
        ("use pnpm not npm", "* use pnpm not npm"),
        ("use pnpm not npm", "1. use pnpm not npm"),
        ("use pnpm not npm", "2) use pnpm not npm"),
        ("line one\nline two", "line one    line two"),
        ("Dùng pnpm", "Dùng pnpm"),
    ],
)
def test_content_hash_collapses_equivalent_forms(left: str, right: str) -> None:
    assert content_hash(left) == content_hash(right)


def test_content_hash_separates_different_text() -> None:
    assert content_hash("use pnpm") != content_hash("use npm")


def test_normalize_keeps_inner_punctuation() -> None:
    # Punctuation is meaningful text, so it is preserved and therefore hashed:
    # only case, bullets and whitespace are normalized away.
    assert normalize("- Use PNPM, not npm.") == "use pnpm, not npm."
    assert content_hash("use pnpm not npm") != content_hash("- Use PNPM, not npm.")


# --- database setup and migrations -----------------------------------------


def test_journal_mode_is_wal(conn: sqlite3.Connection) -> None:
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_connection_pragmas(conn: sqlite3.Connection) -> None:
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_schema_creates_reserved_tables(conn: sqlite3.Connection) -> None:
    names = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }
    assert {"memories", "entities", "memory_entities", "dedup_queue", "meta"} <= names


def test_migration_is_idempotent(conn: sqlite3.Connection, db_path: Path) -> None:
    store.add_memory(conn, "keep me", "ws")
    assert db.schema_version(conn) == db.CURRENT_SCHEMA_VERSION
    conn.close()

    reopened = db.open_database(db_path)
    try:
        assert db.schema_version(reopened) == db.CURRENT_SCHEMA_VERSION
        assert reopened.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
    finally:
        reopened.close()


def _columns(connection: sqlite3.Connection) -> set[str]:
    return {row["name"] for row in connection.execute("PRAGMA table_info(memories)").fetchall()}


def _open_at_version_1(path: Path, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    """Open ``path`` with only the v1 migration registered — a v0.1.0 database."""
    monkeypatch.setattr(db, "_MIGRATIONS", db._MIGRATIONS[:1])
    return db.open_database(path)


def test_a_version_1_database_upgrades_with_its_data_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The forward-only mechanism built in M1, used for real for the first time."""
    path = tmp_path / "v1.db"
    old = _open_at_version_1(path, monkeypatch)
    try:
        store.add_memory(old, "use pnpm not npm", "ws")
        store.add_memory(old, "prefer small commits", "ws", "core")
        assert db.schema_version(old) == 1
        assert "recalled_count" not in _columns(old)
    finally:
        old.close()
    monkeypatch.undo()

    upgraded = db.open_database(path)
    try:
        assert db.schema_version(upgraded) == 2
        assert {"recalled_count", "last_recalled_at"} <= _columns(upgraded)
        rows = upgraded.execute(
            "SELECT content, recalled_count, last_recalled_at FROM memories ORDER BY id"
        ).fetchall()
        assert [row["content"] for row in rows] == ["use pnpm not npm", "prefer small commits"]
        # Every pre-existing row starts the counter at zero, not at NULL.
        assert [row["recalled_count"] for row in rows] == [0, 0]
        assert [row["last_recalled_at"] for row in rows] == [None, None]
        hits = store.search_memories(upgraded, "pnpm", "ws")
        assert [hit.content for hit in hits] == ["use pnpm not npm"]
    finally:
        upgraded.close()


def test_upgrading_a_version_1_database_twice_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "v1.db"
    old = _open_at_version_1(path, monkeypatch)
    try:
        store.add_memory(old, "use pnpm not npm", "ws")
    finally:
        old.close()
    monkeypatch.undo()

    first = db.open_database(path)
    first.close()
    second = db.open_database(path)
    try:
        assert db.migrate(second) == db.CURRENT_SCHEMA_VERSION
        assert second.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
    finally:
        second.close()


def test_open_database_creates_parent_directories(tmp_path: Path) -> None:
    nested = tmp_path / "deep" / "nested" / "memory.db"
    connection = db.open_database(nested)
    connection.close()
    assert nested.exists()


def test_opening_a_corrupt_database_does_not_leak_its_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M4 fix round 1: the half-built connection is closed, so no ResourceWarning.

    ``sqlite3.connect`` succeeds against any file; the first statement is what fails,
    and that statement is ``PRAGMA journal_mode=WAL``. ``connect`` used to drop the
    connection it had just opened, unreferenced and unclosed. The MCP server opens a
    connection per tool call, so against a corrupt database that was one leaked handle
    per call.

    Both halves are asserted, because either alone would pass vacuously: the connection
    object is closed (the mechanism), and nothing reaches ``sys.unraisablehook`` once
    the last reference is dropped (the symptom pytest reports as a ResourceWarning).
    The exception callers see is unchanged — ``cli._session`` and the MCP tool boundary
    both match on ``sqlite3.Error``.
    """
    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"this is not a SQLite database" * 64)

    opened: list[sqlite3.Connection] = []
    real_connect = sqlite3.connect

    def recording_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        connection = real_connect(*args, **kwargs)
        opened.append(connection)
        return connection

    unraisable: list[str] = []
    monkeypatch.setattr(sqlite3, "connect", recording_connect)
    monkeypatch.setattr(sys, "unraisablehook", lambda hook: unraisable.append(str(hook.exc_value)))

    with pytest.raises(sqlite3.Error):
        db.open_database(corrupt)

    assert opened, "open_database never reached sqlite3.connect"
    for connection in opened:
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            connection.execute("SELECT 1")

    opened.clear()
    gc.collect()
    assert unraisable == []


def test_schema_version_rejects_corrupt_value(conn: sqlite3.Connection) -> None:
    conn.execute("UPDATE meta SET value = 'not-a-number' WHERE key = ?", (db.SCHEMA_VERSION_KEY,))
    with pytest.raises(RuntimeError, match="corrupt"):
        db.schema_version(conn)


def test_transaction_rolls_back_on_error(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="boom"), db.transaction(conn):
        conn.execute(
            "INSERT INTO memories (content, content_hash, workspace) VALUES (?, ?, ?)",
            ("rolled back", "deadbeef", "ws"),
        )
        raise ValueError("boom")
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0


# --- add / dedup tier 1 -----------------------------------------------------


def test_add_then_duplicate_merges(conn: sqlite3.Connection) -> None:
    first = store.add_memory(conn, "use pnpm not npm", "ws")
    second = store.add_memory(conn, "use pnpm not npm", "ws")
    assert first == (store.STATUS_ADDED, first.id, 1)
    assert second == (store.STATUS_DUPLICATE_MERGED, first.id, 2)
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1


def test_duplicate_merge_keeps_created_at_and_raw_content(conn: sqlite3.Connection) -> None:
    added = store.add_memory(conn, "use pnpm not npm", "ws")
    original = conn.execute(
        "SELECT content, created_at FROM memories WHERE id = ?", (added.id,)
    ).fetchone()
    store.add_memory(conn, "- Use   PNPM not npm", "ws")
    merged = conn.execute(
        "SELECT content, created_at FROM memories WHERE id = ?", (added.id,)
    ).fetchone()
    assert merged["created_at"] == original["created_at"]
    assert merged["content"] == "use pnpm not npm"


def test_add_rejects_blank_content(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="content is empty"):
        store.add_memory(conn, "   \n ", "ws")


def test_add_rejects_blank_workspace(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="non-empty"):
        store.add_memory(conn, "something", "")


def test_add_stores_provenance(conn: sqlite3.Connection) -> None:
    added = store.add_memory(conn, "traced", "ws", "trace", "codex", "sess-1")
    row = conn.execute("SELECT * FROM memories WHERE id = ?", (added.id,)).fetchone()
    assert (row["kind"], row["source"], row["session_id"]) == ("trace", "codex", "sess-1")


def test_same_content_in_two_workspaces_is_two_rows(conn: sqlite3.Connection) -> None:
    left = store.add_memory(conn, "use pnpm not npm", "a")
    right = store.add_memory(conn, "use pnpm not npm", "b")
    assert left.status == store.STATUS_ADDED
    assert right.status == store.STATUS_ADDED
    assert left.id != right.id


# --- search -----------------------------------------------------------------


def test_search_round_trip(conn: sqlite3.Connection) -> None:
    store.add_memory(conn, "use pnpm not npm", "ws")
    hits = store.search_memories(conn, "pnpm", "ws")
    assert [hit.content for hit in hits] == ["use pnpm not npm"]
    assert hits[0].score > 0


def test_search_is_workspace_scoped(conn: sqlite3.Connection) -> None:
    store.add_memory(conn, "use pnpm not npm", "a")
    store.add_memory(conn, "use pnpm not npm", "b")
    hits = store.search_memories(conn, "pnpm", "a")
    assert [hit.workspace for hit in hits] == ["a"]
    assert len(store.search_memories(conn, "pnpm", None)) == 2


def test_search_matches_vietnamese_without_diacritics(conn: sqlite3.Connection) -> None:
    store.add_memory(conn, VIETNAMESE, "ws")
    hits = store.search_memories(conn, "dung pnpm", "ws")
    assert [hit.content for hit in hits] == [VIETNAMESE]


def test_search_ranks_better_matches_first(conn: sqlite3.Connection) -> None:
    store.add_memory(conn, "pnpm is the package manager", "ws")
    store.add_memory(conn, "pnpm pnpm pnpm workspace protocol", "ws")
    hits = store.search_memories(conn, "pnpm", "ws")
    assert hits[0].score >= hits[-1].score


def test_search_updates_index_after_merge(conn: sqlite3.Connection) -> None:
    store.add_memory(conn, "use pnpm not npm", "ws")
    store.add_memory(conn, "use pnpm not npm", "ws")
    assert len(store.search_memories(conn, "pnpm", "ws")) == 1


def test_search_on_empty_database_returns_nothing(conn: sqlite3.Connection) -> None:
    assert store.search_memories(conn, "anything", "ws") == []


@pytest.mark.parametrize(
    "query",
    [
        "npm AND (broken",
        "npm OR",
        "NEAR(",
        'quote " unbalanced',
        "star*",
        "col:on",
        "-dash",
        "^caret",
    ],
)
def test_search_survives_fts5_metacharacters(conn: sqlite3.Connection, query: str) -> None:
    store.add_memory(conn, "npm is broken on this machine", "ws")
    store.search_memories(conn, query, "ws")


def test_search_with_no_searchable_tokens_returns_empty(conn: sqlite3.Connection) -> None:
    store.add_memory(conn, "npm is broken", "ws")
    assert store.search_memories(conn, "*** ( ) ***", "ws") == []


def test_build_match_expression_quotes_tokens() -> None:
    assert store.build_match_expression("npm AND (broken") == '"npm" "AND" "broken"'


def test_build_match_expression_drops_empty_query() -> None:
    assert store.build_match_expression("  -- ") == ""


@pytest.mark.parametrize("limit", [0, 21, -1])
def test_search_rejects_out_of_range_limit(conn: sqlite3.Connection, limit: int) -> None:
    with pytest.raises(ValueError, match="k must be between"):
        store.search_memories(conn, "pnpm", "ws", limit)


def test_search_honours_limit(conn: sqlite3.Connection) -> None:
    for index in range(5):
        store.add_memory(conn, f"pnpm note number {index}", "ws")
    assert len(store.search_memories(conn, "pnpm", "ws", 2)) == 2


# --- stats ------------------------------------------------------------------


def test_stats_on_empty_database(conn: sqlite3.Connection, db_path: Path) -> None:
    summary = store.collect_stats(conn, db_path)
    assert summary.total == 0
    assert summary.per_workspace == ()
    assert summary.per_kind == ()
    assert summary.db_size_bytes > 0


def test_stats_counts_workspaces_and_kinds(conn: sqlite3.Connection, db_path: Path) -> None:
    store.add_memory(conn, "one", "a")
    store.add_memory(conn, "two", "a", "core")
    store.add_memory(conn, "three", "b")
    summary = store.collect_stats(conn, db_path)
    assert summary.total == 3
    assert dict(summary.per_workspace) == {"a": 2, "b": 1}
    assert dict(summary.per_kind) == {"note": 2, "core": 1}


def test_stats_totals_the_recorded_recalls(conn: sqlite3.Connection, db_path: Path) -> None:
    from localmem import retriever

    store.add_memory(conn, "use pnpm not npm", "a")
    store.add_memory(conn, "never looked at", "a")
    assert store.collect_stats(conn, db_path).total_recalled == 0

    retriever.retrieve(conn, "pnpm", "a")
    retriever.retrieve(conn, "pnpm", "a")

    # Recalls, not memories: one row returned twice counts twice.
    assert store.collect_stats(conn, db_path).total_recalled == 2


def test_cli_stats_reports_recalls(db_path: Path) -> None:
    runner = CliRunner()
    runner.invoke(main, ["add", "use pnpm not npm", "-w", "a"])
    runner.invoke(main, ["search", "pnpm", "-w", "a"])
    result = runner.invoke(main, ["stats"])
    assert result.exit_code == 0
    assert "recalls: 1 recorded across all memories" in result.output


# --- CLI --------------------------------------------------------------------


def test_cli_exposes_exactly_the_expected_commands() -> None:
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0
    # M3 added dedupe and gc, M4 added serve, M5 added init/import/agents/benchmark;
    # the assertion stays exact so a stray command is noticed.
    assert set(main.commands) == {
        "add",
        "agents",
        "audit",
        "backfill",
        "benchmark",
        "dedupe",
        "export",
        "gc",
        "import",
        "init",
        "restore",
        "search",
        "serve",
        "stats",
    }


def test_cli_add_round_trip() -> None:
    runner = CliRunner()
    first = runner.invoke(main, ["add", "use pnpm not npm", "-w", "proj"])
    second = runner.invoke(main, ["add", "- Use   PNPM not npm", "-w", "proj"])
    assert json.loads(first.output) == {"status": "added", "id": 1, "seen_count": 1}
    assert json.loads(second.output) == {"status": "duplicate_merged", "id": 1, "seen_count": 2}


def test_cli_add_rejects_blank_content() -> None:
    result = CliRunner().invoke(main, ["add", "   ", "-w", "proj"])
    assert result.exit_code != 0
    assert "content is empty" in result.output


def test_cli_add_rejects_blank_workspace() -> None:
    result = CliRunner().invoke(main, ["add", "text", "-w", " "])
    assert result.exit_code != 0
    assert "non-empty" in result.output


def test_cli_search_outputs_hits() -> None:
    runner = CliRunner()
    runner.invoke(main, ["add", VIETNAMESE, "-w", "proj"])
    result = runner.invoke(main, ["search", "dung pnpm", "-w", "proj"])
    assert result.exit_code == 0
    assert VIETNAMESE in result.output


def test_cli_search_all_workspaces() -> None:
    runner = CliRunner()
    runner.invoke(main, ["add", "use pnpm not npm", "-w", "a"])
    runner.invoke(main, ["add", "use pnpm not npm", "-w", "b"])
    scoped = runner.invoke(main, ["search", "pnpm", "-w", "a"])
    everywhere = runner.invoke(main, ["search", "pnpm", "--all"])
    assert scoped.output.count("workspace=") == 1
    assert everywhere.output.count("workspace=") == 2


def test_cli_search_reports_no_matches() -> None:
    result = CliRunner().invoke(main, ["search", "nothing here", "-w", "proj"])
    assert result.exit_code == 0
    assert "no memories matching" in result.output


def test_cli_search_rejects_out_of_range_k() -> None:
    result = CliRunner().invoke(main, ["search", "pnpm", "-k", "99"])
    assert result.exit_code != 0


def test_cli_stats_on_empty_database(db_path: Path) -> None:
    result = CliRunner().invoke(main, ["stats"])
    assert result.exit_code == 0
    assert str(db_path) in result.output
    assert "memories: 0" in result.output
    assert "(none)" in result.output


def test_cli_stats_reports_counts() -> None:
    runner = CliRunner()
    runner.invoke(main, ["add", "one", "-w", "a"])
    runner.invoke(main, ["add", "two", "-w", "b", "--kind", "core"])
    result = runner.invoke(main, ["stats"])
    assert "memories: 2" in result.output
    assert "by workspace" in result.output
    assert "by kind" in result.output


def test_cli_reports_unusable_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("", encoding="utf-8")
    monkeypatch.setenv(config.DB_PATH_ENV_VAR, str(blocker / "memory.db"))
    result = CliRunner().invoke(main, ["stats"])
    assert result.exit_code != 0
    assert "cannot open the localmem database" in result.output
