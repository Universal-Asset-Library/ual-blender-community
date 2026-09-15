# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""Stable asset selection keys (pure — no bpy).

Selection, hover targets, import, and drag must prefer these keys over list
indexes. Indexes are a view into the current CollectionProperty only.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from .config import norm_asset_path_key


def library_key(path: str) -> str:
    """Stable key for a local / library asset path."""
    p = norm_asset_path_key(path or "")
    return f"lib:{p}" if p else ""


def online_key(source_id: str, asset_id: str) -> str:
    """Stable key for an online search/result row."""
    sid = str(source_id or "").strip()
    aid = str(asset_id or "").strip()
    if not sid or not aid:
        return ""
    return f"online:{sid}:{aid}"


def key_for_library_item(item: Any) -> str:
    return library_key(getattr(item, "path", "") or "")


def key_for_online_item(item: Any) -> str:
    return online_key(
        getattr(item, "source_id", "") or "",
        getattr(item, "asset_id", "") or "",
    )


def index_for_library_key(assets: Iterable[Any], key: str) -> int:
    want = str(key or "")
    if not want:
        return -1
    for i, item in enumerate(assets):
        if key_for_library_item(item) == want:
            return int(i)
    return -1


def index_for_online_key(results: Iterable[Any], key: str) -> int:
    want = str(key or "")
    if not want:
        return -1
    for i, item in enumerate(results):
        if key_for_online_item(item) == want:
            return int(i)
    return -1


def sync_library_selection(ui: Any) -> int:
    """After ``ui.assets`` rebuild — restore index from ``selected_key``.

    Falls back to clamped ``selected_index`` when the key is missing/stale.
    Always refreshes ``selected_key`` to match the chosen row.
    """
    assets = getattr(ui, "assets", None)
    n = len(assets) if assets is not None else 0
    if n <= 0:
        try:
            ui.selected_index = 0
            ui.selected_key = ""
        except Exception:
            pass
        return 0
    key = str(getattr(ui, "selected_key", "") or "")
    idx = index_for_library_key(assets, key)
    if idx < 0:
        idx = max(0, min(int(getattr(ui, "selected_index", 0) or 0), n - 1))
    try:
        ui.selected_index = idx
        ui.selected_key = key_for_library_item(assets[idx])
    except Exception:
        pass
    return idx


def sync_online_selection(ui: Any, *, prefer_first_if_missing: bool = False) -> int:
    """After ``ui.online_results`` change — restore index from ``online_selected_key``."""
    results = getattr(ui, "online_results", None)
    n = len(results) if results is not None else 0
    if n <= 0:
        try:
            ui.online_selected_index = 0
            ui.online_selected_key = ""
        except Exception:
            pass
        return 0
    key = str(getattr(ui, "online_selected_key", "") or "")
    idx = index_for_online_key(results, key)
    if idx < 0:
        if prefer_first_if_missing:
            idx = 0
        else:
            idx = max(0, min(int(getattr(ui, "online_selected_index", 0) or 0), n - 1))
    try:
        ui.online_selected_index = idx
        ui.online_selected_key = key_for_online_item(results[idx])
    except Exception:
        pass
    return idx


def apply_library_index(ui: Any, index: int) -> Optional[str]:
    """Set library selection index + key. Returns the key or ``None``."""
    assets = getattr(ui, "assets", None)
    n = len(assets) if assets is not None else 0
    if n <= 0:
        try:
            ui.selected_index = 0
            ui.selected_key = ""
        except Exception:
            pass
        return None
    idx = max(0, min(int(index), n - 1))
    key = key_for_library_item(assets[idx])
    try:
        ui.selected_index = idx
        ui.selected_key = key
    except Exception:
        pass
    return key or None


def apply_online_index(ui: Any, index: int) -> Optional[str]:
    """Set online selection index + key. Returns the key or ``None``."""
    results = getattr(ui, "online_results", None)
    n = len(results) if results is not None else 0
    if n <= 0:
        try:
            ui.online_selected_index = 0
            ui.online_selected_key = ""
        except Exception:
            pass
        return None
    idx = max(0, min(int(index), n - 1))
    key = key_for_online_item(results[idx])
    try:
        ui.online_selected_index = idx
        ui.online_selected_key = key
    except Exception:
        pass
    return key or None


def resolve_library_target(
    ui: Any,
    *,
    target_key: str = "",
    target_index: int = -1,
) -> Optional[tuple]:
    """Resolve drag/import target by **key first**, then index.

    Returns ``(index, item, key)`` or ``None`` if the collection is empty /
    the explicit key is missing (never silently swap to a different asset).
    """
    assets = getattr(ui, "assets", None)
    n = len(assets) if assets is not None else 0
    if n <= 0:
        return None
    key = str(target_key or "").strip()
    if not key:
        key = str(getattr(ui, "selected_key", "") or "").strip()
    if key:
        idx = index_for_library_key(assets, key)
        if idx < 0:
            return None
        return (idx, assets[idx], key_for_library_item(assets[idx]))
    raw = int(target_index)
    if raw < 0:
        raw = int(getattr(ui, "selected_index", 0) or 0)
    idx = max(0, min(raw, n - 1))
    return (idx, assets[idx], key_for_library_item(assets[idx]))


def resolve_online_target(
    ui: Any,
    *,
    target_key: str = "",
    target_index: int = -1,
) -> Optional[tuple]:
    """Resolve online drag/download target by **key first**, then index."""
    results = getattr(ui, "online_results", None)
    n = len(results) if results is not None else 0
    if n <= 0:
        return None
    key = str(target_key or "").strip()
    if not key:
        key = str(getattr(ui, "online_selected_key", "") or "").strip()
    if key:
        idx = index_for_online_key(results, key)
        if idx < 0:
            return None
        return (idx, results[idx], key_for_online_item(results[idx]))
    raw = int(target_index)
    if raw < 0:
        raw = int(getattr(ui, "online_selected_index", 0) or 0)
    idx = max(0, min(raw, n - 1))
    return (idx, results[idx], key_for_online_item(results[idx]))
