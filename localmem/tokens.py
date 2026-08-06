"""Character-based token estimation (the original spec §1).

Deliberately dependency-free: this module imports nothing from :mod:`localmem`, so
both the retriever's core-memory cap and the future ``benchmark`` command can rely on
one estimator without creating an import cycle.

Every number produced here is an approximation (±15%), never a real tokenizer count.
"""

from __future__ import annotations

import math

# Above this fraction of non-ASCII characters the text is treated as CJK/Vietnamese-heavy.
# The original spec §1 names the two regimes but not the boundary; 0.15 is chosen so that a mostly
# English sentence carrying a few accented words stays on the 4-chars-per-token estimate,
# while a genuinely Vietnamese sentence — which crosses 0.15 after only a handful of
# diacritics — moves to the denser one.
NON_ASCII_RATIO_CUTOFF = 0.15

CHARS_PER_TOKEN_LATIN = 4.0
CHARS_PER_TOKEN_DENSE = 2.5

_MAX_ASCII_CODEPOINT = 127


def estimate_tokens(text: str) -> int:
    """Return the estimated number of tokens in ``text``.

    Uses ``ceil(len/2.5)`` when more than :data:`NON_ASCII_RATIO_CUTOFF` of the
    characters are non-ASCII, and ``ceil(len/4)`` otherwise. The empty string is 0.
    """
    if not text:
        return 0
    non_ascii = sum(1 for character in text if ord(character) > _MAX_ASCII_CODEPOINT)
    dense = non_ascii / len(text) > NON_ASCII_RATIO_CUTOFF
    divisor = CHARS_PER_TOKEN_DENSE if dense else CHARS_PER_TOKEN_LATIN
    return math.ceil(len(text) / divisor)
