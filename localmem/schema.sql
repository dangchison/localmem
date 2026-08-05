-- Canonical localmem schema (version 1).
-- Connection-level PRAGMAs (WAL, busy_timeout, foreign_keys) are applied by db.connect();
-- this file holds DDL only so it can be replayed inside a transaction.
-- Tables entities, memory_entities and dedup_queue are created here for schema stability
-- but are only populated from milestone M2 onwards.

CREATE TABLE IF NOT EXISTS memories (
    id            INTEGER PRIMARY KEY,
    content       TEXT    NOT NULL,
    content_hash  TEXT    NOT NULL,          -- sha256 of normalized content
    workspace     TEXT    NOT NULL DEFAULT 'global',
    kind          TEXT    NOT NULL DEFAULT 'note',
                  -- 'note' | 'trace' | 'imported' | 'core'
    source        TEXT,                      -- e.g. 'claude-code', 'codex', 'import:CLAUDE.md'
    session_id    TEXT,
    seen_count    INTEGER NOT NULL DEFAULT 1,
    superseded_by INTEGER REFERENCES memories(id),  -- reserved; logic lands in v0.2
    created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (workspace, content_hash)         -- dedup is per workspace; see docs/design_decisions.md
);

CREATE INDEX IF NOT EXISTS idx_mem_workspace ON memories(workspace);
CREATE INDEX IF NOT EXISTS idx_mem_created   ON memories(created_at);

-- Full-text index kept in sync by triggers.
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    content,
    content='memories',
    content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'   -- Vietnamese-friendly
);

CREATE TRIGGER IF NOT EXISTS mem_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
END;

CREATE TRIGGER IF NOT EXISTS mem_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content) VALUES ('delete', old.id, old.content);
END;

CREATE TRIGGER IF NOT EXISTS mem_au AFTER UPDATE OF content ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content) VALUES ('delete', old.id, old.content);
    INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
END;

CREATE TABLE IF NOT EXISTS entities (
    id        INTEGER PRIMARY KEY,
    name      TEXT NOT NULL,
    norm_name TEXT NOT NULL,                 -- lowercased/normalized
    UNIQUE (norm_name)
);

CREATE TABLE IF NOT EXISTS memory_entities (
    memory_id INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    weight    REAL    NOT NULL DEFAULT 1.0,  -- normalized occurrence weight
    PRIMARY KEY (memory_id, entity_id)
);

CREATE TABLE IF NOT EXISTS dedup_queue (     -- tier-2 near-duplicate candidates
    id           INTEGER PRIMARY KEY,
    memory_id    INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    candidate_id INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    score        REAL NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',  -- 'pending' | 'merged' | 'kept_both'
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS meta (            -- schema_version, install info
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
