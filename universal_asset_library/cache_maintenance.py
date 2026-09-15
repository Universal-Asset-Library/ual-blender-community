# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""Cache inventory + safe clear helpers for Preferences → Clear Cache.

Never deletes library-root asset files or ``assets.db`` (index) unless explicitly
requested in a future mode. Staging clears may remove the only copy when
Copy-to-library is off — callers must warn.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from . import paths
from .sources import list_source_ids

_CATALOG_PREFIXES = ("_catalog",)
_CATALOG_EXACT = frozenset(
    {
        "_catalog_threedscans_enriched_v4.json",
        "_catalog_threedscans_enriched_v5.json",
    }
)


@dataclass
class CacheClearResult:
    removed_files: int = 0
    removed_bytes: int = 0
    errors: List[str] = field(default_factory=list)
    notes: str = ""

    def merge(self, other: "CacheClearResult") -> "CacheClearResult":
        self.removed_files += int(other.removed_files or 0)
        self.removed_bytes += int(other.removed_bytes or 0)
        self.errors.extend(list(other.errors or []))
        if other.notes:
            self.notes = (
                "{}; {}".format(self.notes, other.notes).strip("; ")
                if self.notes
                else other.notes
            )
        return self


def format_bytes(num_bytes: int) -> str:
    value = float(max(0, int(num_bytes or 0)))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024.0 or unit == "TB":
            if unit == "B":
                return "{} {}".format(int(value), unit)
            return "{:.1f} {}".format(value, unit)
        value /= 1024.0
    return "{} B".format(int(num_bytes or 0))


def summarize_clear_result(result: CacheClearResult) -> str:
    base = "Removed {} files ({})".format(
        int(result.removed_files or 0),
        format_bytes(int(result.removed_bytes or 0)),
    )
    if result.notes:
        base = "{} — {}".format(base, result.notes)
    if result.errors:
        base = "{} · {} error(s)".format(base, len(result.errors))
    return base


def _safe_size(path: str) -> int:
    try:
        return int(os.path.getsize(path))
    except OSError:
        return 0


def walk_file_sizes(root: str) -> Tuple[int, int]:
    if not root or not os.path.isdir(root):
        return 0, 0
    count = 0
    total = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            path = os.path.join(dirpath, name)
            if not os.path.isfile(path):
                continue
            count += 1
            total += _safe_size(path)
    return count, total


def _is_online_browse_thumb(name: str) -> bool:
    lower = (name or "").lower()
    return lower.startswith("online_") and lower.endswith((".png", ".jpg", ".jpeg"))


def _is_catalog_cache_file(name: str) -> bool:
    if name in _CATALOG_EXACT:
        return True
    lower = (name or "").lower()
    return any(lower.startswith(p) and lower.endswith(".json") for p in _CATALOG_PREFIXES)


def sources_root(cache_dir: str) -> str:
    return os.path.normpath(os.path.join(cache_dir or "", "sources"))


def cache_usage_summary(cache_dir: str) -> Dict[str, Any]:
    cache = paths.repair_cache_dir(cache_dir or "", [])
    thumbs = paths.thumbs_dir(cache)
    db = paths.db_path(cache)
    staging_root = sources_root(cache)

    thumbs_files, thumbs_bytes = walk_file_sizes(thumbs)
    online_thumb_files = 0
    online_thumb_bytes = 0
    local_thumb_files = 0
    local_thumb_bytes = 0
    if os.path.isdir(thumbs):
        for name in os.listdir(thumbs):
            path = os.path.join(thumbs, name)
            if not os.path.isfile(path):
                continue
            size = _safe_size(path)
            if _is_online_browse_thumb(name):
                online_thumb_files += 1
                online_thumb_bytes += size
            else:
                local_thumb_files += 1
                local_thumb_bytes += size

    staging_files, staging_bytes = walk_file_sizes(staging_root)
    catalog_files = 0
    catalog_bytes = 0
    if os.path.isdir(staging_root):
        for dirpath, _dirnames, filenames in os.walk(staging_root):
            for name in filenames:
                if not _is_catalog_cache_file(name):
                    continue
                path = os.path.join(dirpath, name)
                if not os.path.isfile(path):
                    continue
                catalog_files += 1
                catalog_bytes += _safe_size(path)

    db_bytes = _safe_size(db) if os.path.isfile(db) else 0
    return {
        "cache_dir": cache,
        "thumbs_dir": thumbs,
        "db_path": db,
        "db_bytes": db_bytes,
        "thumbs_files": thumbs_files,
        "thumbs_bytes": thumbs_bytes,
        "local_thumb_files": local_thumb_files,
        "local_thumb_bytes": local_thumb_bytes,
        "online_thumb_files": online_thumb_files,
        "online_thumb_bytes": online_thumb_bytes,
        "staging_files": staging_files,
        "staging_bytes": staging_bytes,
        "catalog_files": catalog_files,
        "catalog_bytes": catalog_bytes,
        "regenerable_bytes": thumbs_bytes + catalog_bytes,
        "all_except_index_bytes": thumbs_bytes + staging_bytes,
    }


def _remove_file(path: str, result: CacheClearResult) -> None:
    try:
        size = _safe_size(path)
        os.remove(path)
        result.removed_files += 1
        result.removed_bytes += size
    except OSError as exc:
        result.errors.append("{} ({})".format(path, exc))


def _remove_empty_dirs(root: str) -> None:
    if not root or not os.path.isdir(root):
        return
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        if dirpath == root:
            continue
        if dirnames or filenames:
            continue
        try:
            os.rmdir(dirpath)
        except OSError:
            pass


def sync_index_after_thumb_clear(cache_dir: str) -> int:
    """Reattach/null ``assets.db`` thumb_path rows after thumbs/ disk clear."""
    if not cache_dir:
        return 0
    try:
        from . import db as ual_db

        conn = ual_db.connect(cache_dir)
        try:
            n = ual_db.sync_thumb_paths_after_cache_clear(
                conn, paths.thumbs_dir(cache_dir)
            )
            conn.commit()
            return int(n or 0)
        finally:
            conn.close()
    except Exception:
        return 0


def clear_memory_caches() -> CacheClearResult:
    """Drop PreviewCollection + in-memory search/source caches (no disk delete)."""
    result = CacheClearResult(notes="memory caches")
    try:
        from . import thumbnails

        thumbnails.clear_preview_cache()
    except Exception as exc:
        result.errors.append("preview: {}".format(exc))
    try:
        from . import online_search_cache

        online_search_cache.clear_online_search_cache()
    except Exception as exc:
        result.errors.append("search_cache: {}".format(exc))
    try:
        from .sources import clear_source_singletons

        clear_source_singletons()
    except Exception as exc:
        result.errors.append("sources: {}".format(exc))
    try:
        from . import online_health

        online_health.clear_cached_state()
    except Exception as exc:
        result.errors.append("online_health: {}".format(exc))
    try:
        from . import indexer

        indexer.clear_fingerprint_cache()
    except Exception as exc:
        result.errors.append("indexer: {}".format(exc))
    try:
        from .ui import hover_preview

        hover_preview.invalidate_assets_hit(clear_hover=True)
        hover_preview.clear_discover_cache()
    except Exception:
        pass
    try:
        from .ui import gpu_overlay_draw

        gpu_overlay_draw.clear_texture_cache()
    except Exception:
        try:
            from .ui.asset_bar import draw as asset_bar_draw

            asset_bar_draw.clear_texture_cache()
        except Exception:
            pass
    return result


def clear_online_browse_thumbs(cache_dir: str) -> CacheClearResult:
    result = CacheClearResult(notes="online browse thumbs")
    thumbs = paths.thumbs_dir(cache_dir)
    if not os.path.isdir(thumbs):
        return result
    for name in os.listdir(thumbs):
        if not _is_online_browse_thumb(name):
            continue
        _remove_file(os.path.join(thumbs, name), result)
    # Online browse thumbs are not stored as assets.db thumb_path usually, but
    # sync is cheap and heals any cache-path rows.
    synced = sync_index_after_thumb_clear(cache_dir)
    if synced:
        result.notes = "{}, {} index thumb(s) synced".format(result.notes, synced)
    return result


def clear_all_thumbnail_images(cache_dir: str) -> CacheClearResult:
    """Delete all PNGs under thumbs/ (local + online). Keeps index rows."""
    result = CacheClearResult(notes="all thumbnail images")
    thumbs = paths.thumbs_dir(cache_dir)
    if not os.path.isdir(thumbs):
        return result
    for name in os.listdir(thumbs):
        path = os.path.join(thumbs, name)
        if os.path.isfile(path):
            _remove_file(path, result)
    synced = sync_index_after_thumb_clear(cache_dir)
    if synced:
        result.notes = "{}, {} index thumb(s) synced".format(result.notes, synced)
    return result


def clear_online_catalog_caches(cache_dir: str) -> CacheClearResult:
    result = CacheClearResult(notes="online catalog JSON")
    staging = sources_root(cache_dir)
    if not os.path.isdir(staging):
        return result
    for dirpath, _dirnames, filenames in os.walk(staging):
        for name in filenames:
            if _is_catalog_cache_file(name):
                _remove_file(os.path.join(dirpath, name), result)
    return result


def clear_online_download_staging(cache_dir: str) -> CacheClearResult:
    """Delete ``sources/<provider>/…`` CDN staging (largest). Keeps assets.db."""
    result = CacheClearResult(notes="online download staging")
    staging = sources_root(cache_dir)
    if not os.path.isdir(staging):
        return result
    for name in list(os.listdir(staging)):
        path = os.path.join(staging, name)
        try:
            if os.path.isdir(path):
                # Sum sizes before rmtree for reporting
                files, nbytes = walk_file_sizes(path)
                shutil.rmtree(path)
                result.removed_files += files
                result.removed_bytes += nbytes
            elif os.path.isfile(path):
                _remove_file(path, result)
        except OSError as exc:
            result.errors.append("{} ({})".format(path, exc))
    _remove_empty_dirs(staging)
    # Known source ids — recreate empty dirs for next download
    for sid in list_source_ids():
        try:
            paths.ensure_dir(os.path.join(staging, sid))
        except OSError:
            pass
    return result


def clear_regenerable_cache(cache_dir: str) -> CacheClearResult:
    """Thumbs + catalog JSON + memory. Keeps index + download staging."""
    result = CacheClearResult(notes="regenerable cache")
    result.merge(clear_all_thumbnail_images(cache_dir))
    result.merge(clear_online_catalog_caches(cache_dir))
    result.merge(clear_memory_caches())
    return result


def clear_all_cache_keep_index(cache_dir: str) -> CacheClearResult:
    """Thumbs + staging + catalogs + memory. Keeps assets.db only."""
    result = CacheClearResult(notes="all cache (keep index)")
    result.merge(clear_all_thumbnail_images(cache_dir))
    result.merge(clear_online_download_staging(cache_dir))
    result.merge(clear_memory_caches())
    return result
