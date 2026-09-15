# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""Bounded OrderedDict LRU helpers (pure — no bpy)."""

from __future__ import annotations

from collections import OrderedDict
from typing import Any, MutableMapping, TypeVar

K = TypeVar("K")
V = TypeVar("V")

# Default soft cap for per-source enrich / detail dicts (session RAM guard)
SOURCE_DETAIL_CACHE_MAX = 64


def lru_get(cache: MutableMapping[K, V], key: K) -> Any:
    """Return value and mark as most-recently used, or None if missing."""
    if key not in cache:
        return None
    try:
        cache.move_to_end(key)  # type: ignore[attr-defined]
    except Exception:
        pass
    return cache[key]


def lru_put(cache: MutableMapping[K, V], key: K, value: V, maxsize: int = SOURCE_DETAIL_CACHE_MAX) -> None:
    """Insert/update and evict oldest entries beyond *maxsize*."""
    cache[key] = value
    try:
        cache.move_to_end(key)  # type: ignore[attr-defined]
    except Exception:
        pass
    limit = max(1, int(maxsize or 1))
    while len(cache) > limit:
        try:
            cache.popitem(last=False)  # type: ignore[attr-defined]
        except Exception:
            # Plain dict fallback — drop an arbitrary key
            try:
                cache.pop(next(iter(cache)))
            except Exception:
                break


def new_lru() -> "OrderedDict[Any, Any]":
    return OrderedDict()
