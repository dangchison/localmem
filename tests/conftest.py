"""Shared fixtures. LOCALMEM_DB is redirected to tmp_path so the real
~/.localmem/ database is never touched by the test suite, and HOME is redirected
so no test can reach the real ~/.claude/, ~/.codex/, ~/.gemini/ or ~/.kiro/."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from localmem import config, db

FIXTURES_DIR = Path(__file__).parent / "fixtures"
CONFIG_FIXTURES_DIR = FIXTURES_DIR / "configs"


@pytest.fixture(autouse=True)
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "memory.db"
    monkeypatch.setenv(config.DB_PATH_ENV_VAR, str(path))
    return path


@pytest.fixture(autouse=True)
def sandbox_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point HOME at an empty temporary directory for every test.

    The agent writers take ``home`` as a parameter and never resolve it themselves,
    so this is the second of two independent layers: even a bug that reached for
    ``Path.home()`` would land here rather than in the user's real config.
    """
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


@pytest.fixture
def conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    connection = db.open_database(db_path)
    try:
        yield connection
    finally:
        connection.close()
