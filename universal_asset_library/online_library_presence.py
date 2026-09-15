# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""Batch “already in library” flags for Online browse tiles (pure helpers).

Green borders / list ticks use the same reuse rules as
``find_reusable_online_asset`` so the indicator matches skip-CDN import.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional, Sequence, Set, Tuple

from . import online_download
from . import paths
from .selection_keys import online_key


def _norm_opt(value: str) -> str:
    v = str(value or "").strip().lower()
    if v in ("", "default", "auto"):
        return ""
    return v


def index_source_packages(cfg: Dict[str, Any], source_id: str) -> Dict[str, Tuple[str, dict]]:
    """Map ``asset_id.lower()`` → ``(package_path, manifest)`` under Online Downloads."""
    sid = str(source_id or "").strip()
    out: Dict[str, Tuple[str, dict]] = {}
    if not sid:
        return out
    roots: Sequence[str] = list(cfg.get("library_roots") or [])
    if not roots:
        roots = [paths.default_library_root()]
    for root in roots:
        if not root:
            continue
        source_root = os.path.join(paths.online_downloads_dir(root), sid)
        if not os.path.isdir(source_root):
            continue
        try:
            type_dirs = os.listdir(source_root)
        except OSError:
            continue
        for type_name in type_dirs:
            type_path = os.path.join(source_root, type_name)
            if not os.path.isdir(type_path):
                continue
            try:
                packages = os.listdir(type_path)
            except OSError:
                continue
            for pkg_name in packages:
                pkg = os.path.join(type_path, pkg_name)
                if not os.path.isdir(pkg):
                    continue
                manifest = online_download.read_bundle_manifest(pkg)
                aid = str(manifest.get("asset_id") or "").strip()
                if not aid:
                    continue
                atype = str(manifest.get("asset_type") or "")
                if not online_download.package_has_content(pkg, atype):
                    continue
                key = aid.lower()
                # First hit wins (same as find_existing_library_package)
                if key not in out:
                    out[key] = (os.path.normpath(pkg), manifest)
    return out


def index_cache_asset_ids(cfg: Dict[str, Any], source_id: str) -> Set[str]:
    """Lowercase asset ids with usable cache staging under ``sources/{sid}/``."""
    sid = str(source_id or "").strip()
    cache_dir = str(cfg.get("cache_dir") or "").strip()
    found: Set[str] = set()
    if not sid or not cache_dir:
        return found
    base = os.path.join(cache_dir, "sources", sid)
    if not os.path.isdir(base):
        return found
    try:
        names = os.listdir(base)
    except OSError:
        return found
    for aid in names:
        if not aid or aid.startswith("."):
            continue
        path = online_download.find_existing_cache_package(
            cfg, source_id=sid, asset_id=aid, asset_type="mesh"
        )
        if path:
            found.add(str(aid).strip().lower())
    return found


def build_online_library_key_set(
    cfg: Dict[str, Any],
    source_id: str,
    *,
    resolution: str = "",
    format: str = "",
) -> Set[str]:
    """Keys ``online:{sid}:{aid}`` that would skip CDN for the given options."""
    sid = str(source_id or "").strip()
    keys: Set[str] = set()
    if not sid:
        return keys
    want_res = _norm_opt(resolution)
    want_fmt = _norm_opt(format)
    for aid_l, (_path, manifest) in index_source_packages(cfg, sid).items():
        if not online_download.package_options_compatible(
            manifest, resolution=resolution, format=format
        ):
            continue
        aid = str(manifest.get("asset_id") or aid_l).strip()
        k = online_key(sid, aid)
        if k:
            keys.add(k)
    # Cache reuse only when resolution/format are unset (same as find_reusable)
    if not want_res and not want_fmt:
        for aid_l in index_cache_asset_ids(cfg, sid):
            k = online_key(sid, aid_l)
            if k:
                keys.add(k)
    return keys


def item_would_reuse(
    cfg: Dict[str, Any],
    *,
    source_id: str,
    asset_id: str,
    asset_name: str,
    asset_type: str,
    resolution: str = "",
    format: str = "",
    package_index: Optional[Dict[str, Tuple[str, dict]]] = None,
    cache_ids: Optional[Set[str]] = None,
) -> bool:
    """True when ``find_reusable_online_asset`` would return a path."""
    sid = str(source_id or "").strip()
    aid = str(asset_id or "").strip()
    if not sid:
        return False
    index = package_index if package_index is not None else index_source_packages(cfg, sid)
    if aid:
        hit = index.get(aid.lower())
        if hit is not None:
            _path, manifest = hit
            if online_download.package_options_compatible(
                manifest, resolution=resolution, format=format
            ):
                return True
    # Name / cache path — same as import reuse (covers missing manifest asset_id)
    path, _origin = online_download.find_reusable_online_asset(
        cfg,
        source_id=sid,
        asset_id=aid,
        asset_name=asset_name,
        asset_type=asset_type or "mesh",
        resolution=resolution,
        format=format,
    )
    if path:
        return True
    if cache_ids is not None and aid and not _norm_opt(resolution) and not _norm_opt(format):
        return aid.lower() in cache_ids
    return False


def resolve_ui_download_options(ui: Any, cfg: Dict[str, Any], item: Any = None) -> Tuple[str, str]:
    """Current Size/Format pickers as strings for reuse matching.

    Never call connector ``get_resolutions`` / ``get_formats`` here — those hit
    the network. Search-result paint used to freeze Blender for tens of HTTP
    round-trips on the UI thread (ambientCG / GPUOpen / LazyTextures).
    """
    from .download_options import (
        preferred_format_from_ui,
        preferred_resolution_from_ui,
    )

    formats_snap = []
    if item is not None:
        formats_snap = [f for f in (getattr(item, "formats", "") or "").split(",") if f]
    preferred_res = preferred_resolution_from_ui(ui, cfg)
    preferred_fmt = preferred_format_from_ui(ui, formats_snap) or ""
    return str(preferred_res or ""), str(preferred_fmt or "")


def refresh_online_result_in_library_flags(ui: Any, cfg: Optional[Dict[str, Any]] = None) -> int:
    """Set ``in_library`` on each ``ui.online_results`` row. Returns how many are True."""
    if ui is None:
        return 0
    results = getattr(ui, "online_results", None)
    if results is None:
        return 0
    if cfg is None:
        try:
            from . import preferences

            cfg = preferences.prefs_to_config()
        except Exception:
            cfg = {}
    sid = str(getattr(ui, "online_source", "") or "").strip()
    if not sid and len(results):
        sid = str(getattr(results[0], "source_id", "") or "").strip()
    package_index = index_source_packages(cfg, sid) if sid else {}
    # One picker snapshot for the whole grid — do not resolve per row (no HTTP).
    base_res, base_fmt = resolve_ui_download_options(ui, cfg, results[0] if results else None)
    want_res = _norm_opt(base_res)
    want_fmt = _norm_opt(base_fmt)
    cache_ids = (
        index_cache_asset_ids(cfg, sid) if sid and not want_res and not want_fmt else set()
    )
    marked = 0
    for item in results:
        item_sid = str(getattr(item, "source_id", "") or sid).strip()
        res, fmt = base_res, base_fmt
        # Rebuild index when source differs (rare mixed results)
        idx = package_index if item_sid == sid else index_source_packages(cfg, item_sid)
        ok = item_would_reuse(
            cfg,
            source_id=item_sid,
            asset_id=str(getattr(item, "asset_id", "") or ""),
            asset_name=str(getattr(item, "name", "") or ""),
            asset_type=str(getattr(item, "asset_type", "") or "mesh"),
            resolution=res,
            format=fmt,
            package_index=idx,
            cache_ids=cache_ids if item_sid == sid else None,
        )
        try:
            item.in_library = bool(ok)
        except Exception:
            pass
        if ok:
            marked += 1
    return marked
