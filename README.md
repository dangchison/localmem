# localmem

Local-first, zero-token memory layer for AI coding agents. One SQLite database, raw
traces, structured retrieval — no LLM calls for any memory operation.

> Status: early development. Milestone M1 (core store) only: `add`, `search`, `stats`.
> The full README — quickstart per agent, benchmark methodology, migration guide and
> citation — lands in M6.

## Install (from a checkout)

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

## Usage

```bash
localmem add "use pnpm not npm"      # -> {"status": "added", "id": 1, "seen_count": 1}
localmem add "- Use   PNPM not npm"  # -> {"status": "duplicate_merged", "id": 1, "seen_count": 2}
localmem search "pnpm"               # ranked, workspace-scoped hits
localmem stats                       # row counts, database path and size
```

Duplicate detection normalizes markdown bullets, case and whitespace — the two `add`
calls above are the same memory. Punctuation is meaningful text and is kept, so
`"use pnpm not npm"` and `"use pnpm, not npm."` are stored as two separate memories.

The database lives at `~/.localmem/memory.db`, overridable with the `LOCALMEM_DB`
environment variable. Memories are partitioned by workspace, detected from the git
repository root name and overridable with `--workspace`.

## License

MIT — see [LICENSE](LICENSE).
