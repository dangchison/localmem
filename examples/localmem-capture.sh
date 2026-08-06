#!/usr/bin/env bash
# localmem auto-capture — Claude Code Stop hook.
# Reads the hook payload on stdin and stores the final assistant message as a trace.
# Exits 0 no matter what: a memory-capture hook must never fail a session.
set -uo pipefail

# The summary is passed to `localmem add` as an exec argument, and an argument list has a
# hard kernel limit (ARG_MAX, 1048576 bytes on macOS). Past it exec fails with E2BIG, the
# `|| exit 0` below swallows it, and the hook exits 0 having stored NOTHING — silently, on
# exactly the input this script is most likely to see. Measured on this machine: a 900 KB
# summary stored fine, 1.1 MB and 1.5 MB stored nothing at all. 100 KB is far beyond any
# useful trace and an order of magnitude below the limit, whether ${#summary} is counting
# characters or bytes. A truncated trace that says so beats a session quietly lost.
readonly LOCALMEM_MAX_SUMMARY_CHARS=100000
readonly LOCALMEM_TRUNCATION_MARKER="…[truncated by capture hook]"

# The noise gate. A Stop hook fires on EVERY session, so without a floor here the store
# fills with "Done." and "The tests pass." — permanent rows that teach nothing. Length is
# the only signal available without a model, and it was measured before it was chosen
# (.corp/localmem-v1/gate-d-capture.md): over a fixture of 10 trivial summaries and 8 that
# recorded a real lesson, the noise tops out at 61 characters and the real traces start at
# 120. A floor of 80 drops 10/10 noise and loses 0/8 real traces, with 19 characters of
# margin below and 40 above. The 40 this replaced let 9 of those 10 through.
#
# The fixture is synthetic — the real database had one row in it when this was measured —
# so treat 80 as provisional. `localmem audit` reports what is actually being stored.
readonly LOCALMEM_MIN_SUMMARY_CHARS=80

command -v localmem >/dev/null 2>&1 || exit 0
command -v jq >/dev/null 2>&1 || exit 0

payload=$(cat)

# Claude Code has moved this field between versions; try the current shape, then the
# transcript fallback. `// empty` keeps jq quiet when neither is present.
summary=$(printf '%s' "$payload" | jq -r '
  (.last_assistant_message // .response // .message.content // empty)
  | if type == "array" then map(select(.type == "text") | .text) | join("\n") else . end
' 2>/dev/null)

# Cap FIRST, before anything else looks at the summary — see the ARG_MAX note above. The
# marker is appended later, after the blank and length tests have judged the real content:
# a summary that is nothing but whitespace must stay nothing, not become a marker.
truncated=no
if [ "${#summary}" -gt "$LOCALMEM_MAX_SUMMARY_CHARS" ]; then
    summary=${summary:0:$LOCALMEM_MAX_SUMMARY_CHARS}
    truncated=yes
fi

# Nothing worth storing, or a one-liner like "done" — stop here.
#
# The blank test MUST stay a `case`. The obvious `[ -z "${summary//[[:space:]]/}" ]` is
# quadratic in bash 3.2.57 — what /usr/bin/env bash is on a stock macOS — whenever the text
# is whitespace-heavy, and a session summary quoting a log or a stack trace is exactly that.
# Measured on 3.2.57: 50 KB of log-shaped text did not finish that expansion in 30 seconds.
case "$summary" in
    *[![:space:]]*) ;;
    *) exit 0 ;;
esac
[ "${#summary}" -lt "$LOCALMEM_MIN_SUMMARY_CHARS" ] && exit 0

# Say so in the record itself: a trace that was cut should admit it rather than read as a
# complete summary that happens to stop mid-sentence.
if [ "$truncated" = yes ]; then
    summary="${summary}${LOCALMEM_TRUNCATION_MARKER}"
fi

# Workspace: let localmem detect it from the repository the session ran in.
cd "$(printf '%s' "$payload" | jq -r '.cwd // "."')" 2>/dev/null || exit 0

# The redundancy gate, and the reason it lives inside `localmem` rather than here.
#
# Deciding "have I already recorded this session, in other words?" needs the summary
# tokenized and compared against what is stored. localmem/dedup.py already does exactly
# that — normalize, tokenize, Jaccard — and re-implementing any of it in shell would be a
# second copy free to drift from the one the rest of the system decides on. So the hook
# asks the question instead of answering it: --if-novel makes the write conditional and
# skips it when an existing memory in this workspace overlaps by >= 0.25 (measured; see
# dedup.CAPTURE_JACCARD_THRESHOLD for why it is not tier 2's 0.7).
#
# Nothing is deleted or edited by this: the redundant summary is simply not written. The
# hook stays silent either way — stdout is discarded, and a skip is a success, not an
# error, so `|| exit 0` is still only there for the E2BIG case above.
localmem add "$summary" --kind trace --source claude-code-hook --if-novel >/dev/null 2>&1 || exit 0
exit 0
