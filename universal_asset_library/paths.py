# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""Cache vs library path helpers (mirrors Houdini UAL two-store rule)."""

from __future__ import annotations

import os
import sys
from typing import List, Optional, Tuple

# DCC-specific cache folder on the library drive (never share with Houdini UAL).
PRODUCT_CACHE_BASENAME = "UAL CACHE BLENDER"
LEGACY_SHARED_CACHE_BASENAME = "UAL CACHE"
FOREIGN_DCC_CACHE_BASENAMES = frozenset({"UAL CACHE HOUDINI"})


def _home() -> str:
    return os.path.expanduser("~")


def default_library_root() -> str:
    return os.path.normpath(os.path.join(_home(), "UAL Library"))


def cache_dir_basename(path: str) -> str:
    raw = str(path or "").strip()
    if not raw:
        return ""
    # Treat Windows-style separators so unit tests and mixed paths resolve on Linux CI.
    normalized = raw.replace("\\", "/").rstrip("/")
    return normalized.split("/")[-1]


def is_legacy_shared_cache_dir(path: str) -> bool:
    """True when cache uses the old generic name shared with Houdini UAL."""
    return cache_dir_basename(path).upper() == LEGACY_SHARED_CACHE_BASENAME.upper()


def is_foreign_dcc_cache_dir(path: str) -> bool:
    """True when cache folder belongs to another DCC product."""
    return cache_dir_basename(path).upper() in {
        name.upper() for name in FOREIGN_DCC_CACHE_BASENAMES
    }


def cache_collides_with_library_roots(cache_dir: str, library_roots: List[str]) -> bool:
    """True when cache is a library root or lives inside one."""
    cache_norm = os.path.normpath(str(cache_dir or "").strip())
    if not cache_norm:
        return False
    cache_key = os.path.normcase(cache_norm)
    for root in library_roots or []:
        if not root:
            continue
        root_norm = os.path.normpath(root)
        root_key = os.path.normcase(root_norm)
        if cache_key == root_key:
            return True
        if cache_key.startswith(root_key + os.sep):
            return True
    return False


def should_auto_relocate_cache_dir(cache_dir: str, library_roots: List[str]) -> bool:
    """Empty, library-colliding, legacy shared, or foreign-DCC cache paths."""
    cleaned = str(cache_dir or "").strip()
    if not cleaned:
        return True
    if cache_collides_with_library_roots(cleaned, library_roots):
        return True
    if is_legacy_shared_cache_dir(cleaned):
        return True
    if is_foreign_dcc_cache_dir(cleaned):
        return True
    return False


def default_cache_dir(library_root: Optional[str] = None) -> str:
    """Prefer a sibling product cache folder on the same drive as the library."""
    root = library_root or default_library_root()
    drive, _ = os.path.splitdrive(os.path.abspath(root))
    if sys.platform == "win32" and drive:
        return os.path.normpath(os.path.join(drive + os.sep, PRODUCT_CACHE_BASENAME))
    parent = os.path.dirname(os.path.abspath(root)) or _home()
    return os.path.normpath(os.path.join(parent, PRODUCT_CACHE_BASENAME))


def online_downloads_dir(library_root: str) -> str:
    return os.path.normpath(os.path.join(library_root, "Online Downloads"))


def source_cache_dir(cache_dir: str, source_id: str, asset_id: str = "") -> str:
    base = os.path.join(cache_dir, "sources", source_id)
    if asset_id:
        # LazyTextures ids are ``materials/Name`` — flatten so staging is one folder.
        safe_id = str(asset_id).replace("/", "_").replace("\\", "_").strip()
        base = os.path.join(base, safe_id)
    return os.path.normpath(base)


def thumbs_dir(cache_dir: str) -> str:
    return os.path.normpath(os.path.join(cache_dir, "thumbs"))


def db_path(cache_dir: str) -> str:
    return os.path.normpath(os.path.join(cache_dir, "assets.db"))


def ensure_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


def repair_cache_dir_with_notes(cache_dir: str, library_roots: List[str]) -> Tuple[str, List[str]]:
    """Empty, colliding, legacy shared, or foreign-DCC cache → UAL CACHE BLENDER."""
    from . import cache_storage

    return cache_storage.resolve_product_cache_dir(
        cache_dir=cache_dir,
        library_roots=library_roots,
        product="blender",
    )


def repair_cache_dir(cache_dir: str, library_roots: List[str]) -> str:
    effective, _notes = repair_cache_dir_with_notes(cache_dir, library_roots)
    return effective


def same_volume(path_a: str, path_b: str) -> bool:
    a = os.path.abspath(path_a)
    b = os.path.abspath(path_b)
    if sys.platform == "win32":
        return os.path.splitdrive(a)[0].lower() == os.path.splitdrive(b)[0].lower()
    return os.stat(a).st_dev == os.stat(b).st_dev if os.path.exists(a) and os.path.exists(b) else True


def path_warnings(
    library_roots: List[str],
    cache_dir: str,
    *,
    library_root: str = "",
) -> List[str]:
    """Inline path warnings (missing roots / cache↔library collide). Pure — no bpy."""
    warnings: List[str] = []
    roots = [os.path.normpath(r) for r in (library_roots or []) if (r or "").strip()]
    if not roots and (library_root or "").strip():
        roots = [os.path.normpath(library_root)]
    if not roots:
        warnings.append("No library folder set — pick one in Preferences → Library")
    for root in roots:
        if root and not os.path.isdir(root):
            warnings.append(f"Library folder not found: {root}")
    cache = (cache_dir or "").strip()
    if cache:
        repaired = repair_cache_dir(cache, roots or [default_library_root()])
        if os.path.normcase(repaired) != os.path.normcase(os.path.normpath(cache)):
            if is_legacy_shared_cache_dir(cache) or is_foreign_dcc_cache_dir(cache):
                warnings.append(
                    "Cache folder is shared with Houdini UAL — Health Check will move it"
                )
            else:
                # Includes custom folders (e.g. …/CACHE) that hold Houdini assets.db
                from . import cache_storage

                schema = cache_storage.peek_assets_db_schema(
                    os.path.join(os.path.normpath(cache), "assets.db")
                )
                if schema == "houdini":
                    warnings.append(
                        "This cache has Houdini's assets.db — Blender will use "
                        f"{PRODUCT_CACHE_BASENAME} instead (share library only)"
                    )
                else:
                    warnings.append(
                        "Cache folder is inside the library — Health Check will move it"
                    )
        for root in roots:
            if os.path.normcase(os.path.normpath(cache)) == os.path.normcase(os.path.normpath(root)):
                warnings.append("Cache and library can't be the same folder")
    return warnings
