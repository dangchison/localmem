"""Tier-1 (exact) deduplication: content normalization and hashing.

Normalization only ever feeds the hash — the raw text is what gets stored.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

_BULLET_PREFIX_RE = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)")
_WHITESPACE_RUN_RE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Return the canonical form of ``text`` used for exact-duplicate detection.

    Applied in order: NFC normalization (so composed and decomposed Vietnamese
    hash identically), markdown bullet prefix removal per line, lowercasing,
    whitespace-run collapsing, and outer stripping.
    """
    composed = unicodedata.normalize("NFC", text)
    unbulleted = "\n".join(_BULLET_PREFIX_RE.sub("", line) for line in composed.splitlines())
    return _WHITESPACE_RUN_RE.sub(" ", unbulleted.lower()).strip()


def content_hash(text: str) -> str:
    """Return the sha256 hex digest of the normalized form of ``text``."""
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()
