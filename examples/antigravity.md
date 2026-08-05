# Google Antigravity

Registering localmem as an MCP server for Google Antigravity.

## What localmem writes

`~/.gemini/config/mcp_config.json`, and only that file. Detection is simply "does `~/.gemini/`
exist"; the `config/` directory is created if it is missing.

## Setup

```bash
localmem agents                # see what was detected and where its config lives
localmem agents --install antigravity
```

Naming the agent in `--install` is the consent. Or let `localmem init` ask you, one agent at a
time, defaulting to no.

## The config it writes

Created from scratch when the file does not exist:

```json
{
  "mcpServers": {
    "localmem": {
      "command": "localmem",
      "args": [
        "serve"
      ]
    }
  }
}
```

If the file already exists, localmem **merges**: only `mcpServers.localmem` is touched,
`mcpServers` is created only if absent, and every other server and every other top-level key
survives. The merged document is written from the parsed JSON, so the file comes back
re-indented at two spaces — the *content* is preserved, the exact formatting is not. (Codex is
the only writer that preserves byte layout, because TOML carries comments that JSON does not.)

Before modifying an existing file the original is copied to `mcp_config.json.bak`, and the
write goes through a temp file plus `os.replace` so the config is never observed half-written.
If localmem is already registered with exactly this entry, nothing is written at all and the
result is `already_present`.

If the file exists but cannot be parsed — invalid JSON, empty, a top level that is not an
object, or an `mcpServers` value that is not an object — localmem **refuses**: nothing is
written, nothing is backed up, and the block above is printed for you to add by hand.
`localmem init` still exits 0.

That refusal is deliberate. The alternative — back the file up and write a fresh config
containing only localmem — silently drops every other MCP server from the *live* file, and a
`.bak` you have no reason to look for is not a remedy. You consented to adding localmem, not to
having your config replaced.

`command` is the bare `localmem` name, so it must be on the `PATH` Antigravity launches with.
If you installed into a venv that is not on your `PATH`, edit the entry to the absolute path of
`.venv/bin/localmem`.

## Permission-granular access

localmem exposes two tools deliberately split along read/write lines, which is what lets a
permission-granular client allow one and gate the other:

- `mcp(localmem/memory_recall)` — read only. Runs a query, never writes.
- `mcp(localmem/memory_add)` — the only tool that writes.

Allowing recall while gating adds is a reasonable posture: the agent can use everything you
have taught it, and every new memory passes through you.

## Verify

```bash
python3 -m json.tool ~/.gemini/config/mcp_config.json
```

Then restart Antigravity and confirm it lists `memory_recall` and `memory_add`.

## Tell Antigravity to use it

Add this to the instruction file Antigravity already loads. localmem prints it during `init`
and never writes it for you:

```markdown
## Memory

Before answering questions about project history, prior decisions, or user preferences, call the `memory_recall` tool. When you learn a durable fact or decision, save it with `memory_add`. Do not duplicate long-term memory in this file.
```

## Notes

- **Session provenance.** `memory_add` has no `session_id` parameter, so every memory
  Antigravity writes stores `session_id = NULL`. Only `localmem add --session-id …` populates
  it.
- **Workspace.** The server detects the workspace per call from its working directory's git
  repository root name, falling back to the directory name and then to `global`. Agents can
  override it with the `workspace` tool parameter, and pass `"all"` to `memory_recall` to search
  every workspace at once.
- **Removing it.** Delete the `localmem` entry from `mcpServers`.
