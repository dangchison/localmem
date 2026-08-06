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

from localmem import cli, config, core_memory, db, store
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


def _open_at_version_2(path: Path, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    """Open ``path`` with only the v1 and v2 migrations registered — a v0.2.x database."""
    monkeypatch.setattr(db, "_MIGRATIONS", db._MIGRATIONS[:2])
    return db.open_database(path)


def _insert_at_old_version(
    connection: sqlite3.Connection, content: str, workspace: str, kind: str = "note"
) -> None:
    """Write one row the way the localmem of that schema version would have.

    ``store.add_memory`` cannot be used here: it writes today's column list, and the whole
    point of these fixtures is a database that does not have those columns yet.
    """
    connection.execute(
        "INSERT INTO memories (content, content_hash, workspace, kind) VALUES (?, ?, ?, ?)",
        (content, content_hash(content), workspace, kind),
    )


def test_a_version_1_database_upgrades_with_its_data_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The forward-only mechanism built in M1, used for real for the first time."""
    path = tmp_path / "v1.db"
    old = _open_at_version_1(path, monkeypatch)
    try:
        _insert_at_old_version(old, "use pnpm not npm", "ws")
        _insert_at_old_version(old, "prefer small commits", "ws", "core")
        assert db.schema_version(old) == 1
        assert "recalled_count" not in _columns(old)
    finally:
        old.close()
    monkeypatch.undo()

    upgraded = db.open_database(path)
    try:
        assert db.schema_version(upgraded) == db.CURRENT_SCHEMA_VERSION
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


def test_a_version_2_database_upgrades_with_its_data_and_index_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v3 drops and rebuilds ``memories_fts``, which is the risky part of the migration.

    A dropped FTS index that is not correctly rebuilt fails *silently* — the table is
    still there and queries still run, they just return nothing. So this asserts search
    actually works afterwards, on both an ASCII and a Vietnamese row.
    """
    path = tmp_path / "v2.db"
    old = _open_at_version_2(path, monkeypatch)
    try:
        _insert_at_old_version(old, "use pnpm not npm", "ws")
        _insert_at_old_version(old, VIETNAMESE, "ws")
        assert db.schema_version(old) == 2
        assert "keywords" not in _columns(old)
    finally:
        old.close()
    monkeypatch.undo()

    upgraded = db.open_database(path)
    try:
        assert db.schema_version(upgraded) == 3
        assert "keywords" in _columns(upgraded)
        # Pre-existing rows have no keywords, and NULL is the value that keeps them
        # ranking exactly as they did before the upgrade.
        assert [row["keywords"] for row in upgraded.execute("SELECT keywords FROM memories")] == [
            None,
            None,
        ]
        assert [hit.content for hit in store.search_memories(upgraded, "pnpm", "ws")] == [
            "use pnpm not npm",
            VIETNAMESE,
        ]
        # The rebuilt index keeps the tokenizer, so diacritic folding still works.
        assert [hit.content for hit in store.search_memories(upgraded, "dung pnpm", "ws")] == [
            VIETNAMESE
        ]
        # And the recreated triggers still maintain it for new writes.
        store.add_memory(upgraded, "a fresh row about caching", "ws", keywords=["cache"])
        assert len(store.search_memories(upgraded, "cache", "ws")) == 1
    finally:
        upgraded.close()


def test_a_keyword_free_database_ranks_exactly_as_version_2_did(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression that matters most: almost every existing row has no keywords.

    The same corpus is built twice — once on the v2 single-column index, once on today's
    two-column one — and the same query is run against both. Ids *and* scores must match
    exactly. bm25 divides by total document length across all columns, so an empty
    ``keywords`` column adds nothing to the denominator and the second column is
    arithmetically invisible; this pins that, rather than trusting it.
    """
    corpus = [
        "pnpm is the package manager",
        "pnpm pnpm pnpm workspace protocol",
        "use pnpm not npm for this project",
        "totally unrelated weather notes",
    ]

    legacy_path = tmp_path / "v2.db"
    legacy = _open_at_version_2(legacy_path, monkeypatch)
    try:
        for content in corpus:
            _insert_at_old_version(legacy, content, "ws")
        legacy_ranked = legacy.execute(
            "SELECT m.id AS id, -bm25(memories_fts) AS score "
            "  FROM memories_fts JOIN memories AS m ON m.id = memories_fts.rowid "
            " WHERE memories_fts MATCH ? AND m.workspace = ? "
            " ORDER BY score DESC, m.created_at DESC",
            ('"pnpm"', "ws"),
        ).fetchall()
    finally:
        legacy.close()
    monkeypatch.undo()

    current = db.open_database(tmp_path / "v3.db")
    try:
        for content in corpus:
            store.add_memory(current, content, "ws")
        current_hits = store.search_memories(current, "pnpm", "ws")
    finally:
        current.close()

    assert [row["id"] for row in legacy_ranked] == [hit.id for hit in current_hits]
    assert [row["score"] for row in legacy_ranked] == [hit.score for hit in current_hits]


def test_upgrading_a_version_1_database_twice_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "v1.db"
    old = _open_at_version_1(path, monkeypatch)
    try:
        _insert_at_old_version(old, "use pnpm not npm", "ws")
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


# --- file permissions -------------------------------------------------------


def mode_of(path: Path) -> int:
    return path.stat().st_mode & 0o777


def test_a_new_database_file_is_owner_only(tmp_path: Path) -> None:
    """v0.2.1 item 5: another account on the machine must not be able to read memories."""
    target = tmp_path / "fresh" / "memory.db"
    connection = db.open_database(target)
    try:
        store.add_memory(connection, "a memory worth protecting", "proj")
    finally:
        connection.close()
    assert mode_of(target) == db.DB_FILE_MODE


def test_a_new_database_directory_is_owner_only(tmp_path: Path) -> None:
    target = tmp_path / "fresh" / "memory.db"
    db.open_database(target).close()
    assert mode_of(target.parent) == config.DB_DIRECTORY_MODE


def test_the_wal_sidecars_are_owner_only_too(tmp_path: Path) -> None:
    """SQLite copies the database file's mode onto ``-wal``/``-shm`` at first write.

    Measured on this machine at umask 022: with no chmod all three files land at 644,
    and tightening the database file *after* the first write leaves the sidecars at 644.
    ``open_database`` therefore tightens before :func:`db.migrate` — which is the first
    write there is — so this asserts the ordering, not just the final mode.
    """
    target = tmp_path / "fresh" / "memory.db"
    connection = db.open_database(target)
    try:
        store.add_memory(connection, "a memory worth protecting", "proj")
        sidecars = [target.with_name(target.name + suffix) for suffix in ("-wal", "-shm")]
        assert [side.exists() for side in sidecars] == [True, True]
        assert [mode_of(side) for side in sidecars] == [db.DB_FILE_MODE, db.DB_FILE_MODE]
    finally:
        connection.close()


def test_a_stale_sidecar_left_by_a_crash_is_tightened(tmp_path: Path) -> None:
    """The one case ordering cannot cover: a ``-wal`` outliving its database file.

    SQLite adopts the file that is already there rather than recreating it, so its mode
    survives both the connect and the first write; the sweep is what closes it. The
    assertion runs while the connection is open, because a clean close deletes the file.
    """
    target = tmp_path / "memory.db"
    stale = target.with_name(target.name + "-wal")
    stale.write_bytes(b"")
    stale.chmod(0o644)

    connection = db.open_database(target)
    try:
        store.add_memory(connection, "written after the crash", "proj")
        assert mode_of(stale) == db.DB_FILE_MODE
    finally:
        connection.close()


def test_an_existing_database_file_keeps_its_permissions(tmp_path: Path) -> None:
    """A custom ``$LOCALMEM_DB`` the user chmodded deliberately is not "repaired"."""
    target = tmp_path / "memory.db"
    db.open_database(target).close()
    target.chmod(0o644)

    db.open_database(target).close()

    assert mode_of(target) == 0o644


def test_an_existing_directory_keeps_its_permissions(tmp_path: Path) -> None:
    parent = tmp_path / "shared"
    parent.mkdir(mode=0o755)
    parent.chmod(0o755)

    db.open_database(parent / "memory.db").close()

    assert mode_of(parent) == 0o755


def test_a_filesystem_without_modes_does_not_break_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Permissions are hardening; a chmod that cannot work must not fail the open."""

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise OSError("chmod is not supported here")

    monkeypatch.setattr(Path, "chmod", refuse)
    connection = db.open_database(tmp_path / "fresh" / "memory.db")
    try:
        assert db.schema_version(connection) == db.CURRENT_SCHEMA_VERSION
    finally:
        connection.close()


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


def test_build_or_match_expression_joins_the_same_tokens_disjunctively() -> None:
    assert store.build_or_match_expression("npm AND (broken") == '"npm" OR "AND" OR "broken"'


def test_build_or_match_expression_drops_empty_query() -> None:
    assert store.build_or_match_expression("  -- ") == ""


# --- keywords ---------------------------------------------------------------


def test_a_memory_is_found_by_a_keyword_that_is_not_in_its_content(
    conn: sqlite3.Connection,
) -> None:
    """The whole point of the feature: the query shares no token with the content."""
    store.add_memory(
        conn,
        "client_max_body_size mặc định 1m trong nginx",
        "ws",
        keywords=["413", "upload", "tải lên"],
    )
    assert "413" not in "client_max_body_size mặc định 1m trong nginx"

    hits = store.search_memories(conn, "413 upload", "ws")

    assert [hit.content for hit in hits] == ["client_max_body_size mặc định 1m trong nginx"]


def test_keywords_are_lowercased_deduplicated_and_capped() -> None:
    assert store.normalize_keywords(["Upload", "upload", "  TẢI LÊN  "]) == "upload tải lên"
    assert store.normalize_keywords(None) is None
    assert store.normalize_keywords([]) is None
    assert store.normalize_keywords(["   ", ""]) is None
    assert store.normalize_keywords(["x" * 200]) == "x" * store.MAX_KEYWORD_CHARS
    many = store.normalize_keywords([f"kw{index}" for index in range(50)])
    assert many is not None
    assert len(many.split()) == store.MAX_KEYWORDS


def test_a_memory_without_keywords_stores_null_not_empty_string(
    conn: sqlite3.Connection,
) -> None:
    """NULL is what makes a keyword-free row indistinguishable from a v0.2.2 one."""
    added = store.add_memory(conn, "no keywords here", "ws")
    stored = conn.execute("SELECT keywords FROM memories WHERE id = ?", (added.id,)).fetchone()
    assert stored["keywords"] is None


def test_merging_a_duplicate_unions_its_keywords(conn: sqlite3.Connection) -> None:
    """The only route by which an already-stored memory ever gains keywords."""
    first = store.add_memory(conn, "use pnpm not npm", "ws", keywords=["package manager"])
    second = store.add_memory(conn, "use pnpm not npm", "ws", keywords=["trình quản lý gói"])

    assert second.status == store.STATUS_DUPLICATE_MERGED
    assert second.id == first.id
    assert second.seen_count == 2
    stored = conn.execute("SELECT keywords FROM memories WHERE id = ?", (first.id,)).fetchone()
    assert stored["keywords"] == "package manager trình quản lý gói"
    # The union is indexed, not just stored: the new wording finds the row.
    assert [hit.id for hit in store.search_memories(conn, "trình quản lý gói", "ws")] == [first.id]


def test_merging_enriches_a_memory_that_had_no_keywords_at_all(
    conn: sqlite3.Connection,
) -> None:
    """Covers the pre-existing rows a migration cannot backfill without a model."""
    first = store.add_memory(conn, "deploy needs the VPN", "ws")
    assert store.search_memories(conn, "mạng riêng ảo", "ws") == []

    store.add_memory(conn, "deploy needs the VPN", "ws", keywords=["mạng riêng ảo"])

    assert [hit.id for hit in store.search_memories(conn, "mạng riêng ảo", "ws")] == [first.id]


def test_merging_without_keywords_leaves_the_stored_ones_alone(
    conn: sqlite3.Connection,
) -> None:
    first = store.add_memory(conn, "use pnpm not npm", "ws", keywords=["package manager"])
    store.add_memory(conn, "use pnpm not npm", "ws")
    stored = conn.execute("SELECT keywords FROM memories WHERE id = ?", (first.id,)).fetchone()
    assert stored["keywords"] == "package manager"


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


# --- promote ----------------------------------------------------------------


def _row(conn: sqlite3.Connection, memory_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
    assert row is not None
    return row


def test_promote_changes_the_kind_by_id(conn: sqlite3.Connection) -> None:
    """v0.4.0 B3: a direct UPDATE, which is what sidesteps the content-hash merge."""
    added = store.add_memory(conn, "413 came from nginx, not the app", "ws", keywords=["upload"])
    before = _row(conn, added.id)

    result = store.promote_memory(conn, added.id, "lesson")

    assert result == (store.STATUS_PROMOTED, added.id, "ws", "lesson", "note")
    after = _row(conn, added.id)
    assert after["kind"] == "lesson"
    # Only the kind moves. seen_count, created_at, content and keywords are history.
    assert (after["content"], after["keywords"]) == (before["content"], before["keywords"])
    assert (after["seen_count"], after["created_at"]) == (
        before["seen_count"],
        before["created_at"],
    )


def test_re_adding_with_another_kind_still_does_not_promote(conn: sqlite3.Connection) -> None:
    """The premise of the whole command, asserted rather than assumed."""
    added = store.add_memory(conn, "413 came from nginx, not the app", "ws")
    merged = store.add_memory(conn, "413 came from nginx, not the app", "ws", kind="lesson")

    assert merged.status == store.STATUS_DUPLICATE_MERGED
    assert merged.id == added.id
    assert _row(conn, added.id)["kind"] == "note"


def test_promote_is_idempotent(conn: sqlite3.Connection) -> None:
    added = store.add_memory(conn, "a note that becomes a lesson", "ws")
    store.promote_memory(conn, added.id, "lesson")
    updated_at = _row(conn, added.id)["updated_at"]

    again = store.promote_memory(conn, added.id, "lesson")

    assert again == (store.STATUS_UNCHANGED, added.id, "ws", "lesson", "lesson")
    assert _row(conn, added.id)["updated_at"] == updated_at


def test_promote_rejects_an_unknown_id(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="no memory with id 404"):
        store.promote_memory(conn, 404, "lesson")


def test_promote_leaves_the_full_text_index_and_entity_links_alone(
    conn: sqlite3.Connection,
) -> None:
    """`kind` is not an FTS column and `mem_au` fires only on content/keywords.

    Verified, not assumed: FTS5's own integrity-check fails loudly on an external-content
    index that has drifted from its table, and the promoted row must still be findable
    by both of its indexed columns.
    """
    added = store.add_memory(conn, "load src/main.py with MyClass", "ws", keywords=["boot"])
    links = conn.execute(
        "SELECT COUNT(*) AS n FROM memory_entities WHERE memory_id = ?", (added.id,)
    ).fetchone()["n"]
    assert links > 0

    store.promote_memory(conn, added.id, "lesson")

    conn.execute("INSERT INTO memories_fts(memories_fts) VALUES('integrity-check')")
    assert [hit.id for hit in store.search_memories(conn, "MyClass", "ws")] == [added.id]
    assert [hit.id for hit in store.search_memories(conn, "boot", "ws")] == [added.id]
    assert (
        conn.execute(
            "SELECT COUNT(*) AS n FROM memory_entities WHERE memory_id = ?", (added.id,)
        ).fetchone()["n"]
        == links
    )


# --- supersede --------------------------------------------------------------

WRONG = "the leak is in the image resizer — it holds every buffer it allocates"
RIGHT = "not a leak at all: the connection pool was exhausted, max=5 in config/db.yml"


def test_supersede_points_the_old_row_at_the_new_one(conn: sqlite3.Connection) -> None:
    """v0.4.0 C1: the column M1 reserved finally carries a value."""
    wrong = store.add_memory(conn, WRONG, "ws")

    right = store.add_memory(conn, RIGHT, "ws", supersedes=[wrong.id])

    assert _row(conn, wrong.id)["superseded_by"] == right.id
    # The correction itself is current: nothing points at it, and it points at nothing.
    assert _row(conn, right.id)["superseded_by"] is None


def test_supersede_accepts_several_ids_at_once(conn: sqlite3.Connection) -> None:
    first = store.add_memory(conn, "the first wrong guess", "ws")
    second = store.add_memory(conn, "the second wrong guess", "ws")

    right = store.add_memory(conn, RIGHT, "ws", supersedes=[first.id, second.id, first.id])

    assert _row(conn, first.id)["superseded_by"] == right.id
    assert _row(conn, second.id)["superseded_by"] == right.id


def test_supersede_rejects_an_unknown_id_and_stores_nothing(conn: sqlite3.Connection) -> None:
    """An unknown id is an error, never a silent no-op — and it rolls the write back."""
    with pytest.raises(ValueError, match="no memory with id 404"):
        store.add_memory(conn, RIGHT, "ws", supersedes=[404])

    assert conn.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"] == 0


def test_supersede_rolls_back_the_whole_add(conn: sqlite3.Connection) -> None:
    """The valid half of a half-valid list must not survive either."""
    wrong = store.add_memory(conn, WRONG, "ws")

    with pytest.raises(ValueError, match="no memory with id 404"):
        store.add_memory(conn, RIGHT, "ws", supersedes=[wrong.id, 404])

    assert _row(conn, wrong.id)["superseded_by"] is None
    assert conn.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"] == 1


def test_a_memory_cannot_supersede_itself(conn: sqlite3.Connection) -> None:
    """Reachable only through the merge path, where the resolved id is an existing row."""
    added = store.add_memory(conn, WRONG, "ws")

    with pytest.raises(ValueError, match="cannot supersede itself"):
        store.add_memory(conn, WRONG, "ws", supersedes=[added.id])

    assert _row(conn, added.id)["superseded_by"] is None
    # The rollback also undid the duplicate merge's seen_count bump.
    assert _row(conn, added.id)["seen_count"] == 1


def test_an_already_superseded_row_may_be_superseded_again(conn: sqlite3.Connection) -> None:
    """Corrections get corrected; the link moves to the newest one."""
    wrong = store.add_memory(conn, WRONG, "ws")
    better = store.add_memory(conn, RIGHT, "ws", supersedes=[wrong.id])

    best = store.add_memory(
        conn, "final word: the retry loop never released a connection", "ws", supersedes=[wrong.id]
    )

    assert _row(conn, wrong.id)["superseded_by"] == best.id
    assert _row(conn, better.id)["superseded_by"] is None


def test_supersede_refuses_a_cycle(conn: sqlite3.Connection) -> None:
    """A supersede cycle would leave neither row as the current answer.

    An insert cannot make one — a new row has no incoming links — but the duplicate
    merge can, because there the resolved id is an existing, possibly superseded row.
    """
    first = store.add_memory(conn, WRONG, "ws")
    second = store.add_memory(conn, RIGHT, "ws", supersedes=[first.id])

    with pytest.raises(ValueError, match="already supersedes"):
        store.add_memory(conn, WRONG, "ws", supersedes=[second.id])

    assert _row(conn, first.id)["superseded_by"] == second.id
    assert _row(conn, second.id)["superseded_by"] is None


def test_supersede_refuses_a_longer_cycle(conn: sqlite3.Connection) -> None:
    first = store.add_memory(conn, "guess one", "ws")
    second = store.add_memory(conn, "guess two", "ws", supersedes=[first.id])
    third = store.add_memory(conn, "guess three", "ws", supersedes=[second.id])

    with pytest.raises(ValueError, match="already supersedes"):
        store.add_memory(conn, "guess one", "ws", supersedes=[third.id])


def test_a_global_memory_may_supersede_a_repo_one(conn: sqlite3.Connection) -> None:
    """The direction that is allowed: the shared tier is readable from the repo."""
    wrong = store.add_memory(conn, WRONG, "proj")

    right = store.add_memory(conn, RIGHT, core_memory.GLOBAL_WORKSPACE, supersedes=[wrong.id])

    assert _row(conn, wrong.id)["superseded_by"] == right.id


def test_a_repo_memory_may_not_supersede_another_workspace(conn: sqlite3.Connection) -> None:
    """The questionable direction, refused: `other` cannot even see `proj`."""
    wrong = store.add_memory(conn, WRONG, "proj")

    with pytest.raises(ValueError, match="cannot supersede memory"):
        store.add_memory(conn, RIGHT, "other", supersedes=[wrong.id])

    assert _row(conn, wrong.id)["superseded_by"] is None


def test_a_repo_memory_may_not_supersede_a_global_one(conn: sqlite3.Connection) -> None:
    """One repo must not retract knowledge every other repo relies on."""
    wrong = store.add_memory(conn, WRONG, core_memory.GLOBAL_WORKSPACE)

    with pytest.raises(ValueError, match="cannot supersede memory"):
        store.add_memory(conn, RIGHT, "proj", supersedes=[wrong.id])


def test_supersede_leaves_the_full_text_index_intact(conn: sqlite3.Connection) -> None:
    """`mem_au` fires on content/keywords only, so this UPDATE costs no index work."""
    wrong = store.add_memory(conn, WRONG, "ws")
    store.add_memory(conn, RIGHT, "ws", supersedes=[wrong.id])

    conn.execute("INSERT INTO memories_fts(memories_fts) VALUES('integrity-check')")
    # Superseded is not deleted: plain search still finds it.
    assert wrong.id in [hit.id for hit in store.search_memories(conn, "resizer", "ws")]


def test_cli_add_supersedes_is_repeatable(db_path: Path) -> None:
    runner = CliRunner()
    first = runner.invoke(main, ["add", "guess one", "-w", "proj"])
    second = runner.invoke(main, ["add", "guess two", "-w", "proj"])
    correction = runner.invoke(
        main,
        ["add", RIGHT, "-w", "proj", "--kind", "lesson", "--supersedes", "1", "--supersedes", "2"],
    )

    assert json.loads(first.output)["id"] == 1
    assert json.loads(second.output)["id"] == 2
    assert json.loads(correction.output) == {"status": "added", "id": 3, "seen_count": 1}

    connection = db.open_database(db_path)
    try:
        rows = connection.execute("SELECT id, superseded_by FROM memories ORDER BY id").fetchall()
    finally:
        connection.close()
    assert [(row["id"], row["superseded_by"]) for row in rows] == [(1, 3), (2, 3), (3, None)]


def test_cli_add_reports_an_unknown_supersedes_id(db_path: Path) -> None:
    runner = CliRunner()
    runner.invoke(main, ["add", "guess one", "-w", "proj"])

    result = runner.invoke(main, ["add", RIGHT, "-w", "proj", "--supersedes", "404"])

    assert result.exit_code != 0
    assert "no memory with id 404" in result.output


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
    # M3 added dedupe and gc, M4 added serve, M5 added init/import/agents/benchmark,
    # v0.4.0 added promote, v0.5.1 added forget; the assertion stays exact so a stray
    # command is noticed. It is a NAME SET, not a count: adding a name is the change.
    assert set(main.commands) == {
        "add",
        "agents",
        "audit",
        "backfill",
        "benchmark",
        "dedupe",
        "eval",
        "export",
        "forget",
        "gc",
        "import",
        "init",
        "promote",
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


def test_cli_add_accepts_the_lesson_kind() -> None:
    """v0.4.0 B1: `lesson` is writable from the CLI as well as over MCP."""
    runner = CliRunner()
    added = runner.invoke(
        main, ["add", "413 was nginx all along", "-w", "proj", "--kind", "lesson"]
    )
    assert added.exit_code == 0, added.output
    assert json.loads(added.stdout)["status"] == "added"

    stats = runner.invoke(main, ["stats"])
    assert "by kind:\n  lesson  1" in stats.output


def test_cli_promote_turns_a_note_into_a_lesson() -> None:
    runner = CliRunner()
    runner.invoke(main, ["add", "upload 413 was the proxy, not the app", "-w", "proj"])

    promoted = runner.invoke(main, ["promote", "1"])

    assert promoted.exit_code == 0, promoted.output
    assert json.loads(promoted.stdout) == {
        "status": "promoted",
        "id": 1,
        "workspace": "proj",
        "kind": "lesson",
        "previous_kind": "note",
    }
    assert "kind=lesson" in runner.invoke(main, ["search", "upload 413", "-w", "proj"]).output
    assert "by kind:\n  lesson  1" in runner.invoke(main, ["stats"]).output


def test_cli_promote_defaults_to_lesson_and_takes_any_other_kind() -> None:
    runner = CliRunner()
    runner.invoke(main, ["add", "a trace of what the agent did", "-w", "proj"])
    promoted = runner.invoke(main, ["promote", "1", "--kind", "trace"])
    assert json.loads(promoted.stdout)["kind"] == "trace"
    assert "--kind" in runner.invoke(main, ["promote", "--help"]).output


def test_cli_promote_reports_an_unknown_id() -> None:
    result = CliRunner().invoke(main, ["promote", "404"])
    assert result.exit_code != 0
    assert "no memory with id 404" in result.output


def test_cli_promote_rejects_a_kind_nobody_may_write() -> None:
    """`imported` is the importer's stamp; promote must not hand it out."""
    runner = CliRunner()
    runner.invoke(main, ["add", "a note", "-w", "proj"])
    result = runner.invoke(main, ["promote", "1", "--kind", "imported"])
    assert result.exit_code != 0


def test_cli_promote_is_idempotent() -> None:
    runner = CliRunner()
    runner.invoke(main, ["add", "a note that becomes a lesson", "-w", "proj"])
    runner.invoke(main, ["promote", "1"])

    again = runner.invoke(main, ["promote", "1"])

    assert again.exit_code == 0, again.output
    assert json.loads(again.stdout) == {
        "status": "unchanged",
        "id": 1,
        "workspace": "proj",
        "kind": "lesson",
        "previous_kind": "lesson",
    }


def test_cli_promote_to_core_warns_when_the_cap_starts_hiding_rows() -> None:
    """The warning is on stderr, so stdout stays one parseable JSON object."""
    runner = CliRunner()
    filler = "x" * (core_memory.CORE_MEMORY_TOKEN_CAP * 4)
    runner.invoke(main, ["add", filler, "-w", "proj", "--kind", "core"])
    runner.invoke(main, ["add", "one more rule that will not fit", "-w", "proj"])

    result = runner.invoke(main, ["promote", "2", "--kind", "core"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["kind"] == "core"
    assert f"{core_memory.CORE_MEMORY_TOKEN_CAP}-token cap" in result.stderr
    assert "are hidden" in result.stderr
    assert "will not be loaded on recall" in result.stderr


def test_cli_promote_to_core_is_quiet_under_the_cap() -> None:
    runner = CliRunner()
    runner.invoke(main, ["add", "prefer small commits", "-w", "proj"])
    result = runner.invoke(main, ["promote", "1", "--kind", "core"])
    assert result.exit_code == 0, result.output
    assert result.stderr == ""


def test_cli_promote_to_a_non_core_kind_never_warns() -> None:
    runner = CliRunner()
    filler = "x" * (core_memory.CORE_MEMORY_TOKEN_CAP * 4)
    runner.invoke(main, ["add", filler, "-w", "proj", "--kind", "core"])
    runner.invoke(main, ["add", "a note that stays out of the core tier", "-w", "proj"])
    result = runner.invoke(main, ["promote", "2"])
    assert result.stderr == ""


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


# --- v0.5.1: forget ---------------------------------------------------------

#: Content that yields entity links under every extractor class that matters here — a
#: file path, a snake_case identifier and an acronym. `forget` has to take all of them
#: with it, because an identifier the extractor pulled out of a secret *is* the secret.
SENSITIVE_CONTENT = "the deploy key lives in config/secrets.env as deploy_token for the API"

#: One entity the two memories below deliberately share, so a delete can be shown to
#: remove what is now unused without touching what is still in use.
SHARED_CONTENT = "config/secrets.env is read by the boot script"


def fts_integrity(conn: sqlite3.Connection) -> None:
    """Assert the FTS5 index is internally consistent.

    This is the only check that sees the damage a hand-written
    ``DELETE FROM memories_fts`` does. An external-content index cannot verify a delete
    row, so a second one — on top of the `mem_ad` trigger's — corrupts it silently: every
    later query still runs, and simply returns the wrong rows. Asserted after every test
    here that deletes anything (``docs/design_decisions.md`` §52).
    """
    conn.execute("INSERT INTO memories_fts(memories_fts) VALUES('integrity-check')")


def foreign_keys_clean(conn: sqlite3.Connection) -> None:
    """Assert no row references a memory that is no longer there."""
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def entity_names(conn: sqlite3.Connection) -> set[str]:
    return {row["norm_name"] for row in conn.execute("SELECT norm_name FROM entities")}


def test_forget_removes_the_row_from_every_table(conn: sqlite3.Connection) -> None:
    """AC2.1 — memories, the FTS index, the entity links and the queue all lose it."""
    doomed = store.add_memory(conn, SENSITIVE_CONTENT, "ws").id
    keeper = store.add_memory(conn, "an unrelated memory about pnpm", "ws").id
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM memory_entities WHERE memory_id = ?", (doomed,)
    ).fetchone()["n"]

    result = store.forget_memory(conn, doomed)

    assert result.id == doomed
    assert {row["id"] for row in conn.execute("SELECT id FROM memories")} == {keeper}
    assert (
        conn.execute(
            "SELECT COUNT(*) AS n FROM memory_entities WHERE memory_id = ?", (doomed,)
        ).fetchone()["n"]
        == 0
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) AS n FROM dedup_queue WHERE memory_id = ? OR candidate_id = ?",
            (doomed, doomed),
        ).fetchone()["n"]
        == 0
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) AS n FROM memories_fts WHERE memories_fts MATCH ?", ('"deploy"',)
        ).fetchone()["n"]
        == 0
    )
    foreign_keys_clean(conn)
    fts_integrity(conn)


def test_forget_leaves_the_fts_index_intact_for_every_other_row(
    conn: sqlite3.Connection,
) -> None:
    """AC2.1 — the trigger does the FTS delete, and nothing else may.

    A second delete against an external-content index does not raise; it silently
    desynchronizes it. The surviving row still being findable is what proves the index
    was not damaged in passing, and `integrity-check` is what proves it structurally.
    """
    doomed = store.add_memory(conn, "use pnpm not npm for this project", "ws").id
    store.add_memory(conn, "use pnpm workspaces for the monorepo", "ws")

    store.forget_memory(conn, doomed)

    fts_integrity(conn)
    hits = store.search_memories(conn, "pnpm", "ws")
    assert [hit.id for hit in hits] and doomed not in {hit.id for hit in hits}


def test_forget_removes_the_memory_from_search(conn: sqlite3.Connection) -> None:
    """AC2.2 — the point of the feature, stated as the user would check it."""
    doomed = store.add_memory(conn, SENSITIVE_CONTENT, "ws").id
    assert [hit.id for hit in store.search_memories(conn, "deploy token", "ws")] == [doomed]

    store.forget_memory(conn, doomed)

    assert store.search_memories(conn, "deploy token", "ws") == []
    fts_integrity(conn)


def test_forget_sweeps_orphaned_entities_and_keeps_shared_ones(
    conn: sqlite3.Connection,
) -> None:
    """AC2.3 — a privacy control, not tidiness.

    ``deploy_token`` was extracted out of the deleted memory and nothing else links it,
    so it goes. ``config/secrets.env`` is still named by another memory, so it stays —
    deleting it would break that memory's relational view.
    """
    doomed = store.add_memory(conn, SENSITIVE_CONTENT, "ws").id
    store.add_memory(conn, SHARED_CONTENT, "ws")
    before = entity_names(conn)
    assert "deploy_token" in before
    assert "config/secrets.env" in before

    result = store.forget_memory(conn, doomed)

    after = entity_names(conn)
    assert "deploy_token" not in after, "an identifier extracted from the deleted memory survived"
    assert "config/secrets.env" in after, "an entity another memory still uses was deleted"
    assert result.entities_removed == len(before - after)
    foreign_keys_clean(conn)
    fts_integrity(conn)


def test_forget_rejects_an_unknown_id_and_changes_nothing(conn: sqlite3.Connection) -> None:
    """AC2.4."""
    kept = store.add_memory(conn, SENSITIVE_CONTENT, "ws").id
    entities_before = entity_names(conn)

    with pytest.raises(ValueError, match="no memory with id 999"):
        store.forget_memory(conn, 999)

    assert {row["id"] for row in conn.execute("SELECT id FROM memories")} == {kept}
    assert entity_names(conn) == entities_before
    fts_integrity(conn)


def test_forget_refuses_a_memory_that_other_memories_name_as_their_replacement(
    conn: sqlite3.Connection,
) -> None:
    """AC2.5 — refused by default, with the dependents listed by id and content.

    ``superseded_by`` has no ``ON DELETE`` clause, so an unguarded delete raises
    ``FOREIGN KEY constraint failed`` and rolls the statement back while the command can
    still report success — measured in milestone C. The guard is a lookup, not a caught
    exception, so the user gets the list rather than an FK error string.
    """
    stale = store.add_memory(conn, "the old advice: use npm", "ws").id
    correction = store.add_memory(conn, "actually use pnpm", "ws", supersedes=[stale]).id

    with pytest.raises(ValueError) as caught:
        store.forget_memory(conn, correction)

    message = str(caught.value)
    assert f"id={stale}" in message
    assert "use npm" in message
    assert "--force" in message
    assert "full rank" in message
    assert {row["id"] for row in conn.execute("SELECT id FROM memories")} == {stale, correction}
    assert (
        conn.execute("SELECT superseded_by FROM memories WHERE id = ?", (stale,)).fetchone()[
            "superseded_by"
        ]
        == correction
    )
    foreign_keys_clean(conn)
    fts_integrity(conn)


def test_forget_force_clears_the_dangling_references(conn: sqlite3.Connection) -> None:
    """AC2.5 — `--force` deletes it and un-retracts what it corrected.

    Refusing outright would leave a memory holding a secret permanently undeletable,
    which defeats the point of the command; the price is that the correction goes away
    and the thing it corrected comes back at full rank.
    """
    stale = store.add_memory(conn, "the old advice: use npm", "ws").id
    also_stale = store.add_memory(conn, "another old note about npm caching", "ws").id
    correction = store.add_memory(
        conn, "actually use pnpm", "ws", supersedes=[stale, also_stale]
    ).id

    result = store.forget_memory(conn, correction, force=True)

    assert set(result.restored) == {stale, also_stale}
    assert {row["id"] for row in conn.execute("SELECT id FROM memories")} == {stale, also_stale}
    assert [row["superseded_by"] for row in conn.execute("SELECT superseded_by FROM memories")] == [
        None,
        None,
    ]
    foreign_keys_clean(conn)
    fts_integrity(conn)


def test_describe_forget_reports_the_row_and_what_goes_with_it(
    conn: sqlite3.Connection,
) -> None:
    """The read-only half: it is what the prompt and `--dry-run` both print."""
    stale = store.add_memory(conn, "the old advice: use npm", "ws").id
    correction = store.add_memory(
        conn, SENSITIVE_CONTENT, "ws", source="claude-code", supersedes=[stale]
    ).id

    target = store.describe_forget(conn, correction)

    assert target.id == correction
    assert target.workspace == "ws"
    assert target.kind == "note"
    assert target.source == "claude-code"
    assert target.seen_count == 1
    assert target.recalled_count == 0
    assert target.entity_links > 0
    assert [row.id for row in target.supersedes] == [stale]
    assert {row["id"] for row in conn.execute("SELECT id FROM memories")} == {stale, correction}


def test_prune_traces_no_longer_leaves_orphaned_entities(conn: sqlite3.Connection) -> None:
    """AC2.8 — the same leak, closed on the other deletion path by the same helper."""
    trace = store.add_memory(conn, SENSITIVE_CONTENT, "ws", kind="trace").id
    store.add_memory(conn, SHARED_CONTENT, "ws")
    conn.execute(
        "UPDATE memories SET created_at = datetime('now', '-90 days') WHERE id = ?", (trace,)
    )
    conn.commit()

    outcome = store.prune_traces(conn, 30)

    assert outcome.traces == 1
    assert outcome.entities > 0
    assert "deploy_token" not in entity_names(conn)
    assert "config/secrets.env" in entity_names(conn)
    foreign_keys_clean(conn)
    fts_integrity(conn)


def test_delete_orphaned_entities_sweeps_what_earlier_versions_left_behind(
    conn: sqlite3.Connection,
) -> None:
    """The sweep is global, so a database that leaked before v0.5.1 is cleaned too."""
    conn.execute("INSERT INTO entities (name, norm_name) VALUES (?, ?)", ("LEFTOVER", "leftover"))
    conn.commit()
    assert "leftover" in entity_names(conn)

    with db.transaction(conn):
        removed = store.delete_orphaned_entities(conn)

    assert removed == 1
    assert "leftover" not in entity_names(conn)


# --- v0.5.1: the forget command ---------------------------------------------


def test_cli_forget_dry_run_prints_the_row_and_writes_nothing() -> None:
    """AC2.6 — `--dry-run` shows exactly what a real run would take, and writes nothing."""
    runner = CliRunner()
    runner.invoke(main, ["add", SENSITIVE_CONTENT, "-w", "ws"])

    result = runner.invoke(main, ["forget", "1", "--dry-run"])

    assert result.exit_code == 0
    assert "deploy_token" in result.output
    assert "nothing was written" in result.output
    assert "memories: 1" in runner.invoke(main, ["stats"]).output


def test_cli_forget_without_a_terminal_and_without_yes_fails() -> None:
    """AC2.6 — never a silent delete, and never a silent skip either.

    CliRunner's stdin is not a tty, which is exactly the condition being tested.
    """
    runner = CliRunner()
    runner.invoke(main, ["add", SENSITIVE_CONTENT, "-w", "ws"])

    result = runner.invoke(main, ["forget", "1"])

    assert result.exit_code != 0
    assert "--yes" in result.output
    assert "memories: 1" in runner.invoke(main, ["stats"]).output


def test_cli_forget_declined_at_the_prompt_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC2.6 — answering no keeps the memory.

    CliRunner's stdin is never a tty, so the terminal is faked the way every other
    prompting test in this suite fakes it; the answer itself is real input.
    """
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    runner = CliRunner()
    runner.invoke(main, ["add", SENSITIVE_CONTENT, "-w", "ws"])

    result = runner.invoke(main, ["forget", "1"], input="n\n")

    assert result.exit_code == 0
    assert "was kept" in result.output
    assert "memories: 1" in runner.invoke(main, ["stats"]).output


def test_cli_forget_with_yes_deletes_and_reports_the_entity_sweep() -> None:
    """AC2.1 through AC2.3 through the command the user actually types."""
    runner = CliRunner()
    runner.invoke(main, ["add", SENSITIVE_CONTENT, "-w", "ws"])
    runner.invoke(main, ["add", SHARED_CONTENT, "-w", "ws"])

    result = runner.invoke(main, ["forget", "1", "--yes"])

    assert result.exit_code == 0
    assert "deleted memory 1" in result.output
    assert "entities no other memory still uses" in result.output
    assert "memories: 1" in runner.invoke(main, ["stats"]).output
    assert (
        "no memories matching" in runner.invoke(main, ["search", "deploy token", "-w", "ws"]).output
    )


def test_cli_forget_refuses_a_replacement_then_accepts_force() -> None:
    """AC2.5 end to end, including the warning that must be said in terms."""
    runner = CliRunner()
    runner.invoke(main, ["add", "the old advice: use npm", "-w", "ws"])
    runner.invoke(main, ["add", "actually use pnpm", "-w", "ws", "--supersedes", "1"])

    refused = runner.invoke(main, ["forget", "2", "--yes"])

    assert refused.exit_code != 0
    assert "--force" in refused.output
    assert "memories: 2" in runner.invoke(main, ["stats"]).output

    forced = runner.invoke(main, ["forget", "2", "--yes", "--force"])

    assert forced.exit_code == 0
    assert "full rank" in forced.output
    assert "cleared superseded_by" in forced.output
    assert "memories: 1" in runner.invoke(main, ["stats"]).output


def test_cli_forget_unknown_id_fails_cleanly() -> None:
    """AC2.4 through the command."""
    result = CliRunner().invoke(main, ["forget", "999", "--yes"])
    assert result.exit_code != 0
    assert "no memory with id 999" in result.output
