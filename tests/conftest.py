"""Shared fixtures. LOCALMEM_DB is redirected to tmp_path so the real
~/.localmem/ database is never touched by the test suite."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from localmem import config, db


@pytest.fixture(autouse=True)
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "memory.db"
    monkeypatch.setenv(config.DB_PATH_ENV_VAR, str(path))
    return path


@pytest.fixture
def conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    connection = db.open_database(db_path)
    try:
        yield connection
    finally:
        connection.close()
