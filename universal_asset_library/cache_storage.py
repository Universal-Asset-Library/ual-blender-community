# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""Product cache folder resolution and safe legacy UAL CACHE migration."""

from __future__ import annotations

import os
import sqlite3
from typing import List, Literal, Tuple

from . import paths

ProductId = Literal["houdini", "blender"]


def peek_assets_db_schema(db_path: str) -> str:
    """Return ``houdini``, ``blender``, ``missing``, or ``unknown``."""
    if not os.path.isfile(db_path):
        return "missing"
    try:
        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute("PRAGMA table_info(assets)").fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return "unknown"
    columns = {str(row[1]) for row in rows if row and row[1]}
    if "root_path" in columns:
        return "houdini"
    if "root" in columns:
        return "blender"
    return "unknown"


def cache_dir_is_empty(cache_dir: str) -> bool:
    if not os.path.isdir(cache_dir):
        return True
    try:
        with os.scandir(cache_dir) as scan:
            return not any(scan)
    except OSError:
        return True


def discover_legacy_cache_beside_library(library_root: str) -> str:
    """Return a legacy ``UAL CACHE`` folder on the library drive if it exists."""
    root = os.path.normpath(str(library_root or "").strip())
    if not root:
        return ""
    candidates: List[str] = []
    parent = os.path.dirname(root)
    if parent and parent != root:
        candidates.append(os.path.join(parent, paths.LEGACY_SHARED_CACHE_BASENAME))
    drive, _ = os.path.splitdrive(os.path.abspath(root))
    if drive:
        candidates.append(os.path.join(drive + os.sep, paths.LEGACY_SHARED_CACHE_BASENAME))
    seen = set()
    for cand in candidates:
        norm = os.path.normpath(cand)
        key = os.path.normcase(norm)
        if key in seen:
            continue
        seen.add(key)
        if os.path.isdir(norm) and paths.is_legacy_shared_cache_dir(norm):
            return norm
    return ""


def try_promote_legacy_cache(
    *,
    source_dir: str,
    target_dir: str,
    product: ProductId,
) -> Tuple[str, List[str]]:
    """Rename a compatible legacy cache folder to the product-specific name."""
    notes: List[str] = []
    source = os.path.normpath(str(source_dir or "").strip())
    target = os.path.normpath(str(target_dir or "").strip())
    if not source or not target:
        return target, notes
    if os.path.normcase(source) == os.path.normcase(target):
        return target, notes
    if not paths.is_legacy_shared_cache_dir(source):
        return target, notes
    if not os.path.isdir(source):
        return target, notes
    if os.path.isdir(target) and not cache_dir_is_empty(target):
        return target, notes
    if not paths.same_volume(source, target):
        notes.append(
            "Legacy UAL CACHE is on a different drive — a fresh "
            f"{paths.PRODUCT_CACHE_BASENAME} folder was created. "
            "Run Rebuild Index if local tiles look empty."
        )
        return target, notes

    schema = peek_assets_db_schema(os.path.join(source, "assets.db"))
    if schema not in ("missing", product):
        notes.append(
            "Legacy UAL CACHE contains another app's index — left unchanged and "
            f"created {paths.PRODUCT_CACHE_BASENAME}."
        )
        return target, notes

    try:
        if os.path.isdir(target) and cache_dir_is_empty(target):
            os.rmdir(target)
        os.rename(source, target)
        notes.append(
            f"Renamed legacy UAL CACHE to {paths.PRODUCT_CACHE_BASENAME} "
            "(index, thumbs, and staging preserved)."
        )
        return target, notes
    except OSError:
        notes.append(
            f"Could not rename legacy UAL CACHE — using {paths.PRODUCT_CACHE_BASENAME}. "
            "Run Rebuild Index if needed."
        )
        return target, notes


def _foreign_schema_note(product: ProductId) -> str:
    other = "Houdini" if product == "blender" else "Blender"
    return (
        f"Cache folder contains a {other} UAL assets.db — left that folder alone and "
        f"using {paths.PRODUCT_CACHE_BASENAME}. Share only the library (Online Downloads); "
        "never share cache between DCCs. Run Rebuild Index if local tiles look empty."
    )


def preferred_library_root_for_cache(library_roots: List[str]) -> str:
    """Pick the library root that should own the sibling product cache folder.

    Prefers a root that already has ``Online Downloads`` (typical shared Houdini/
    Blender library) over an empty default home folder when multiple roots exist.
    """
    roots = [os.path.normpath(r) for r in (library_roots or []) if (r or "").strip()]
    if not roots:
        return paths.default_library_root()
    existing = [r for r in roots if os.path.isdir(r)]
    pool = existing or roots
    for root in pool:
        if os.path.isdir(os.path.join(root, "Online Downloads")):
            return root
    return pool[0]


def resolve_product_cache_dir(
    *,
    cache_dir: str,
    library_roots: List[str],
    product: ProductId = "blender",
) -> Tuple[str, List[str]]:
    """Pick the product cache path and migrate legacy data when safe."""
    roots = [os.path.normpath(r) for r in (library_roots or []) if r]
    if not roots:
        cleaned = (cache_dir or "").strip()
        return (os.path.normpath(cleaned) if cleaned else paths.default_cache_dir()), []

    cache_raw = str(cache_dir or "").strip()
    target = paths.default_cache_dir(preferred_library_root_for_cache(roots))

    # Custom folder names (e.g. CACHE) are normally kept — unless assets.db is
    # the other DCC's schema. Sharing library is OK; sharing cache is not.
    if cache_raw and not paths.should_auto_relocate_cache_dir(cache_raw, roots):
        schema = peek_assets_db_schema(os.path.join(os.path.normpath(cache_raw), "assets.db"))
        if schema not in ("missing", "unknown", product):
            return paths.ensure_dir(target), [_foreign_schema_note(product)]
        return paths.ensure_dir(os.path.normpath(cache_raw)), []

    source = cache_raw
    if not source:
        source = discover_legacy_cache_beside_library(roots[0])
    if source and paths.is_legacy_shared_cache_dir(source):
        effective, notes = try_promote_legacy_cache(
            source_dir=source,
            target_dir=target,
            product=product,
        )
        return paths.ensure_dir(effective), notes
    # Foreign basename (UAL CACHE HOUDINI) or empty/colliding → product cache.
    if source and paths.is_foreign_dcc_cache_dir(source):
        return paths.ensure_dir(target), [_foreign_schema_note(product)]
    return paths.ensure_dir(target), []
