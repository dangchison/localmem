# Auto-recall with a Claude Code UserPromptSubmit hook

An opt-in example, and the mirror image of [the capture hook](claude_code_hook.md). That one
answers "the agent forgets to call `memory_add`". This one answers the other half: **the agent
forgets to call `memory_recall`.** A hook does not forget.

Nothing in localmem installs this. localmem never edits your agent settings — that rule holds
for hooks exactly as it holds for MCP registration. Copy the script, read it, and add the
block to your own `settings.json` yourself.

## What it does

Claude Code runs a **UserPromptSubmit hook** before the model sees your prompt and hands it a
JSON object on stdin. The script pulls `prompt` out of that object and runs:

```
localmem search "<your prompt>" --context -k 3
```

Whatever the script prints on stdout is injected into that prompt's context. `--context` is a
mode built for exactly this:

- **No match prints nothing at all** — not the "no memories matching…" line the plain command
  prints. A hook runs on every prompt, so a friendly message would become permanent noise.
- A match prints one header line and one line per memory, `- (workspace) content`, with the
  content collapsed onto a single line and truncated at 400 characters:

  ```
  Relevant memories (localmem):
  - (myrepo) file upload 413s behind nginx: client_max_body_size defaults to 1m…
  - (global) the deploy pipeline uses pnpm, not npm
  ```

- A memory longer than that is cut with `… (memory_recall id 42 for full text)`. A
  whole-file skill or checklist must not paste itself into every prompt you ever write; the
  agent is told the id and can recall the row properly when it turns out to matter.
- **Core memory is not included.** It comes back through an ordinary recall, where it is
  charged once. Injecting it per prompt would rebuild the fixed per-session cost localmem
  exists to remove. See `docs/design_decisions.md` §30.

## The script

It lives in this repository as [`localmem-auto-recall.sh`](localmem-auto-recall.sh); a test
asserts the copy below is byte-for-byte the same file, so the two cannot drift. Save it as
`~/.claude/hooks/localmem-auto-recall.sh` and `chmod +x` it.

```bash
#!/usr/bin/env bash
# localmem auto-recall — Claude Code UserPromptSubmit hook.
# Reads the hook payload on stdin and prints matching memories on stdout, which Claude
# Code injects into the context of that prompt.
# Exits 0 no matter what, and prints nothing when it has nothing: a recall hook runs on
# every prompt, so it must never block one and never add noise to one.
set -uo pipefail

# Seconds before the search is abandoned. `timeout` is coreutils; macOS ships it as
# `gtimeout` with `brew install coreutils`, and without either the search simply runs
# unguarded — localmem is a local SQLite read, not a network call.
readonly LOCALMEM_HOOK_TIMEOUT=5
readonly LOCALMEM_HOOK_RESULTS=3
# Prompts longer than this are searched by their first characters only. A pasted log or
# stack trace has no retrieval value past this point, and the cap bounds every cost after
# it in one move — see the quadratic note below.
readonly LOCALMEM_MAX_PROMPT_CHARS=4000

command -v localmem >/dev/null 2>&1 || exit 0
command -v jq >/dev/null 2>&1 || exit 0

payload=$(cat) || exit 0

prompt=$(printf '%s' "$payload" | jq -r '.prompt // empty' 2>/dev/null) || exit 0

# Cap FIRST, before anything looks at the prompt character by character.
if [ "${#prompt}" -gt "$LOCALMEM_MAX_PROMPT_CHARS" ]; then
    prompt=${prompt:0:$LOCALMEM_MAX_PROMPT_CHARS}
fi

# Empty, whitespace-only, or a payload that never had a `prompt` field: nothing to search.
#
# This MUST stay a `case`. The obvious `[ -z "${prompt//[[:space:]]/}" ]` is quadratic in
# bash 3.2.57 — the version /usr/bin/env bash resolves to on every stock macOS — whenever
# the text is whitespace-heavy, which a pasted log is. Measured on 3.2.57: 50 KB of
# log-shaped text spent **523 seconds** inside that one expansion, before the timeout
# guard below is ever reached. The `case` below is a single linear pattern match: 0.03 s
# on 1 MB, and 0.08 s on 1 MB of pure whitespace, which is its worst case.
case "$prompt" in
    *[![:space:]]*) ;;
    *) exit 0 ;;
esac

# Workspace: let localmem detect it from the repository the session is running in.
cd "$(printf '%s' "$payload" | jq -r '.cwd // "."')" 2>/dev/null || exit 0

if command -v timeout >/dev/null 2>&1; then
    timeout "$LOCALMEM_HOOK_TIMEOUT" \
        localmem search "$prompt" --context -k "$LOCALMEM_HOOK_RESULTS" 2>/dev/null || exit 0
elif command -v gtimeout >/dev/null 2>&1; then
    gtimeout "$LOCALMEM_HOOK_TIMEOUT" \
        localmem search "$prompt" --context -k "$LOCALMEM_HOOK_RESULTS" 2>/dev/null || exit 0
else
    localmem search "$prompt" --context -k "$LOCALMEM_HOOK_RESULTS" 2>/dev/null || exit 0
fi

exit 0
```

## Wiring it up

Add this to `~/.claude/settings.json` (or the project's `.claude/settings.json`). If the file
already has a `hooks` key, merge into it rather than replacing it — the capture hook from the
other example lives under `Stop` and the two coexist:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "~/.claude/hooks/localmem-auto-recall.sh"
          }
        ]
      }
    ]
  }
}
```

Check it worked before you trust it, by running the script the way the hook will:

```bash
echo '{"prompt":"upload 413","cwd":"'"$PWD"'"}' | ~/.claude/hooks/localmem-auto-recall.sh
echo "exit: $?"        # 0, always
```

An empty database prints nothing and still exits 0. That is the correct result, not a failure.

## Fail-safe, because this one runs on every prompt

The capture hook can fail quietly and you lose one trace. This hook sits between you and the
model, so its failure mode is worse — and every branch of it therefore ends in `exit 0`:

- `localmem` or `jq` not on the hook's `PATH` → exits 0, prints nothing. Claude Code hooks do
  not always inherit your interactive shell's `PATH`; this is the common case, not an exotic
  one, and it is silent by design.
- Malformed JSON, no `prompt` field, an empty or whitespace-only prompt → exits 0, prints
  nothing.
- A database error, a locked database, a corrupt file → stderr is discarded, exit 0.
- A search that somehow hangs → `timeout` kills it after 5 seconds, exit 0. On macOS install
  coreutils for `gtimeout`, or accept the unguarded call: the search is a local SQLite read.
- **A pasted log or stack trace** → capped at `LOCALMEM_MAX_PROMPT_CHARS` (4000) characters
  before anything else touches it. Measured end to end on bash 3.2.57, the version
  `/usr/bin/env bash` resolves to on a stock macOS: **1 MB of log-shaped text takes 0.19 s**,
  and 1 MB of pure whitespace 0.10 s.

  This is why the blank check is a `case` and not the obvious
  `[ -z "${prompt//[[:space:]]/}" ]`. That expansion is **quadratic** on bash 3.2 when the
  text is whitespace-heavy: 50 KB of the same log took **523 seconds** in that one line, and
  the `timeout` guard is downstream of it, so nothing would have saved the prompt. If you
  edit this script, keep the `case` and keep the cap ahead of it.

## Before you turn it on

- **It injects memory into every prompt.** Three results, 400 characters each, so the ceiling
  is roughly 300 estimated tokens per prompt — and only when something actually matches. Lower
  `LOCALMEM_HOOK_RESULTS` if that is too much for your context budget.
- **Recall is lexical.** A prompt that shares no words with a memory injects nothing. This
  hook widens *when* recall happens, not *what* recall can find. The match is conjunctive, so
  a very long prompt — a pasted log, say — matches nothing at all rather than matching
  everything. That is the safe direction to fail in, and it is why the 4000-character cap
  costs you nothing in practice.
- **What is injected is data, not instruction.** The pointer snippet in your instruction file
  is what tells the model so, and it should stay there even with this hook installed. See
  `docs/design_decisions.md` §23.
- **It writes to the database.** Recall bumps `recalled_count`, so with this hook every prompt
  performs one small write. Set `LOCALMEM_NO_TRACKING=1` in the hook's environment to turn
  that off and make recall strictly read-only.
- **`jq` is required by the script, not by localmem.** localmem itself has three runtime
  dependencies and `jq` is not one of them.
