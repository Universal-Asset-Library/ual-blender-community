# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""Fast, token-aware search matching (pure — no bpy).

Used by Library SQLite, Online cache keys, and client-side catalogs
(Poly Haven, PBRPX). Empty query matches everything.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, List, Sequence

_SPLIT = re.compile(r"[\s,;/|]+")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize_search_query(text: str) -> str:
    """Strip and collapse whitespace — ``wood   floor`` → ``wood floor``."""
    return " ".join(str(text or "").split())


def cache_query_key(text: str) -> str:
    """Stable cache / fingerprint key (case-insensitive, collapsed space)."""
    return normalize_search_query(text).lower()


def query_tokens(text: str) -> List[str]:
    parts = _SPLIT.split(normalize_search_query(text))
    return [p for p in parts if p]


def compact_alnum(text: str) -> str:
    """``Wood_Floor-01`` → ``woodfloor01`` for glued-name matching."""
    return _NON_ALNUM.sub("", str(text or "").lower())


def escape_like_token(token: str) -> str:
    """Make a user token safe for SQL ``LIKE … ESCAPE '\\'``."""
    return (
        str(token or "")
        .replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )


def text_matches_query(haystack: str, query: str) -> bool:
    """True when every token appears, or compact names match (WoodFloor ↔ wood floor)."""
    q = normalize_search_query(query)
    if not q:
        return True
    hay = str(haystack or "")
    hay_l = hay.lower()
    tokens = query_tokens(q)
    if tokens and all(tok.lower() in hay_l for tok in tokens):
        return True
    compact_q = compact_alnum(q)
    return bool(compact_q) and compact_q in compact_alnum(hay)


def asset_matches_query(
    query: str,
    *,
    name: str = "",
    asset_id: str = "",
    tags: Sequence[str] = (),
    categories: Sequence[str] = (),
) -> bool:
    hay = " ".join(
        [str(name or ""), str(asset_id or "")]
        + [str(t) for t in (tags or ()) if t]
        + [str(c) for c in (categories or ()) if c]
    )
    return text_matches_query(hay, query)


def match_rank(haystack: str, query: str) -> int:
    """Lower is better. 999 = no match (callers still sort non-matches last)."""
    q = normalize_search_query(query).lower()
    if not q:
        return 50
    h = str(haystack or "").lower().strip()
    if h == q:
        return 0
    if h.startswith(q):
        return 1
    compact_h = compact_alnum(haystack)
    compact_q = compact_alnum(q)
    if compact_q:
        if compact_h == compact_q:
            return 2
        if compact_h.startswith(compact_q):
            return 3
    tokens = query_tokens(q)
    if tokens and all(tok.lower() in h for tok in tokens):
        if h.startswith(tokens[0].lower()):
            return 4
        return 5
    if compact_q and compact_q in compact_h:
        return 6
    return 999


def rank_search_hit(name: str, path_or_id: str, query: str) -> tuple:
    """Sort key: best of name / id, then name alphabetically."""
    return (
        min(match_rank(name, query), match_rank(path_or_id, query) + 1),
        str(name or "").lower(),
    )


def search_fingerprint(
    source_id: str,
    query: str,
    type_filter: str,
    *,
    page: int = 1,
) -> str:
    return "|".join(
        [
            str(source_id or "").strip().lower(),
            cache_query_key(query),
            str(type_filter or "ALL").strip().upper(),
            str(int(page or 1)),
        ]
    )


def sort_assets_by_query(items: Iterable[Any], query: str) -> List[Any]:
    """Stable relevance sort for client-side catalogs. Empty query keeps order."""
    seq = list(items)
    q = normalize_search_query(query)
    if not q:
        return seq
    seq.sort(
        key=lambda asset: rank_search_hit(
            str(getattr(asset, "name", "") or ""),
            str(getattr(asset, "asset_id", "") or ""),
            q,
        )
    )
    return seq
