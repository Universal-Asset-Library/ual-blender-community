# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""In-memory LRU cache for Online search API results (fast second Search).

Thumbnails are cached separately on disk under ``{cache}/thumbs/online_*.png``.
This module only memoizes connector ``search()`` payloads for a short TTL so
repeating the same source/query does not wait on the network again.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

# Small LRU — enough for bouncing between a few sources/queries
_MAX_KEYS = 32
_TTL_SEC = 10 * 60  # 10 minutes (Houdini Fab aggregate parity)

# Default Online page size. GPUOpen MatLib is slow per request (~1.5s) but
# allows limit≤100 — fetch more per round-trip so Load More is rarer.
_DEFAULT_PAGE_SIZE = 40
_GPUOPEN_PAGE_SIZE = 80

_CACHE: "OrderedDict[str, Tuple[float, dict]]" = OrderedDict()


def online_search_page_size(source_id: str) -> int:
    """Connector page size for Search / Load More / prefetch / cache keys."""
    sid = str(source_id or "").strip().lower()
    if sid == "gpuopen":
        return int(_GPUOPEN_PAGE_SIZE)
    return int(_DEFAULT_PAGE_SIZE)


def _cache_key(
    source_id: str,
    query: str,
    page: int,
    page_size: int,
    type_filter: str,
) -> str:
    from .search_text import cache_query_key

    return "|".join(
        [
            str(source_id or "").strip().lower(),
            cache_query_key(query),
            str(int(page or 1)),
            str(int(page_size or 40)),
            str(type_filter or "ALL").strip().upper(),
            "v4",
        ]
    )


def clear_online_search_cache(source_id: Optional[str] = None) -> None:
    """Drop all entries, or only those for *source_id*."""
    sid = str(source_id or "").strip().lower()
    if not sid:
        _CACHE.clear()
        return
    dead = [k for k in _CACHE if k.startswith(sid + "|")]
    for key in dead:
        _CACHE.pop(key, None)


def get_cached_search(
    source_id: str,
    query: str,
    *,
    page: int = 1,
    page_size: int = 40,
    type_filter: str = "ALL",
) -> Optional[dict]:
    """Return ``{items, has_more, total_count}`` or None."""
    key = _cache_key(source_id, query, page, page_size, type_filter)
    entry = _CACHE.get(key)
    if not entry:
        return None
    stamped, payload = entry
    if (time.time() - stamped) > _TTL_SEC:
        _CACHE.pop(key, None)
        return None
    _CACHE.move_to_end(key)
    # Shallow copy list so callers can mutate safely
    items = list(payload.get("items") or [])
    return {
        "items": items,
        "has_more": bool(payload.get("has_more")),
        "total_count": int(payload.get("total_count") or 0),
    }


def store_cached_search(
    source_id: str,
    query: str,
    *,
    page: int = 1,
    page_size: int = 40,
    type_filter: str = "ALL",
    items: Optional[List[Any]] = None,
    has_more: bool = False,
    total_count: int = 0,
) -> None:
    key = _cache_key(source_id, query, page, page_size, type_filter)
    _CACHE[key] = (
        time.time(),
        {
            "items": list(items or []),
            "has_more": bool(has_more),
            "total_count": int(total_count or 0),
        },
    )
    _CACHE.move_to_end(key)
    while len(_CACHE) > _MAX_KEYS:
        _CACHE.popitem(last=False)
