# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""Filesystem scan → SQLite index.

Library grid should look like Online: one tile per asset, with an albedo/preview
thumb — not every sidecar map as its own tile.
"""

from __future__ import annotations

import json
import os
import re
from typing import Callable, Dict, List, Optional, Set

from . import db
from . import paths
from . import thumbnails

MESH_EXT = {".fbx", ".obj", ".glb", ".gltf", ".stl", ".usd", ".usda", ".usdc", ".usdz", ".abc", ".ply"}
# Geo import formats only — ambientCG materials may ship a USD preview capsule
# that must not flip the tile to mesh (Metal 049 A + .usdc).
IMPORT_MESH_EXT = MESH_EXT - {".usd", ".usda", ".usdc", ".usdz"}
HDRI_EXT = {".hdr", ".exr"}
TEXTURE_EXT = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".tga", ".exr", ".webp", ".bmp"}
# Include Poly Haven / ambientCG abbreviations so mesh+map folders count as PBR sets
PBR_MARKERS = (
    "albedo", "basecolor", "base_color", "diffuse", "color", "diff", "col", "alb",
    "normal", "nor_gl", "nor_dx", "nor", "nrm",
    "roughness", "rough",
    "metalness", "metallic", "metal",
    "ao", "ambientocclusion", "occlusion",
    "displacement", "height", "disp",
    "orm", "arm", "ord", "ort",
    "explosive",  # Poly Haven multi-material stem (Barrel_01_explosive_diff_8k)
)
_TEXTURE_DIR_NAMES = {
    "textures", "texture", "tex", "maps", "map", "materials", "images", "img",
}
_NESTED_PBR_DIRS = _TEXTURE_DIR_NAMES | {"extracted"}
_RES_DIR = re.compile(r"^(1|2|4|6|8|16)k$", re.IGNORECASE)
_BUNDLE_NAME = ".assetlibrary.bundle.json"
# Path segments used by stage_to_library — never index loose maps under Models/
_ONLINE_MODEL_SEGMENTS = ("online downloads", "models")

_FINGERPRINT_CACHE: Dict[str, str] = {}


def clear_fingerprint_cache() -> None:
    """Drop scan short-circuit fingerprints (call after cache/index maintenance)."""
    _FINGERPRINT_CACHE.clear()


def file_extension(path: str) -> str:
    lower = path.lower()
    if lower.endswith(".bgeo.sc"):
        return ".bgeo.sc"
    return os.path.splitext(lower)[1]


def _pbr_file_hits(files: List[str]) -> int:
    hits = 0
    for name in files:
        if file_extension(name) not in TEXTURE_EXT:
            continue
        stem = os.path.splitext(name)[0].lower()
        if any(m in stem for m in PBR_MARKERS):
            hits += 1
            if hits >= 2:
                return hits
    return hits


def _looks_like_pbr_folder(folder: str, files: Optional[List[str]] = None) -> bool:
    """True when this folder (or ambientCG-style nested extract) is a map set.

    Only ``extracted/`` / ``textures/`` / ``1k``-style children count as nested
    maps. Named sibling packages (``Materials/Ailenstone01/``) must not make the
    parent folder look like a single material.
    """
    if files is None:
        try:
            files = [n for n in os.listdir(folder) if os.path.isfile(os.path.join(folder, n))]
        except OSError:
            return False
    if _pbr_file_hits(files) >= 2:
        return True
    try:
        entries = os.listdir(folder)
    except OSError:
        return False
    for name in entries:
        child = os.path.join(folder, name)
        if not os.path.isdir(child):
            continue
        lower = (name or "").lower()
        if not (lower in _NESTED_PBR_DIRS or _RES_DIR.match(lower)):
            continue
        try:
            child_names = os.listdir(child)
        except OSError:
            continue
        child_files = [n for n in child_names if os.path.isfile(os.path.join(child, n))]
        if _pbr_file_hits(child_files) >= 2:
            return True
        # extracted/Metal049A_2K-JPG/*.jpg — one extra nest for ZIP layouts
        for nested_name in child_names:
            nested = os.path.join(child, nested_name)
            if not os.path.isdir(nested):
                continue
            try:
                nested_files = os.listdir(nested)
            except OSError:
                continue
            if _pbr_file_hits(nested_files) >= 2:
                return True
    return False


def _dir_has_mesh(filenames: List[str]) -> bool:
    return any(file_extension(n) in MESH_EXT for n in filenames)


def _dir_has_import_mesh(filenames: List[str]) -> bool:
    return any(file_extension(n) in IMPORT_MESH_EXT for n in filenames)


def _is_texture_dir_name(name: str) -> bool:
    return (name or "").lower() in _TEXTURE_DIR_NAMES


def _path_parts(path: str) -> List[str]:
    return [p for p in os.path.normpath(path).replace("\\", "/").lower().split("/") if p]


_ONLINE_TYPE_SEGMENTS = ("models", "materials", "textures", "hdris", "volumes")


def _is_online_container_dir(path: str) -> bool:
    """True for ``Online Downloads``, ``…/<source>/``, or ``…/<source>/<Type>/``.

    Those folders must never become a single Library tile — packages live one
    level deeper (``…/Materials/Ailenstone01/``).
    """
    parts = _path_parts(path)
    try:
        i = parts.index("online downloads")
    except ValueError:
        return False
    after = parts[i + 1 :]
    if len(after) == 0:
        return True
    if len(after) == 1:
        return True
    if len(after) == 2 and after[1] in _ONLINE_TYPE_SEGMENTS:
        return True
    return False


def _under_online_models(path: str) -> bool:
    """True for ``…/Online Downloads/<source>/Models/…`` (staged mesh packages)."""
    parts = _path_parts(path)
    try:
        i = parts.index("online downloads")
    except ValueError:
        return False
    return "models" in parts[i + 1 :]


def _read_bundle_manifest(folder: str) -> Optional[Dict]:
    path = os.path.join(folder, _BUNDLE_NAME)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _ancestor_has_mesh_or_bundle(dirpath: str, *, max_up: int = 5) -> bool:
    """True when a parent folder is a mesh package (maps in textures/ sidecars)."""
    cur = os.path.normpath(dirpath)
    for _ in range(max_up):
        parent = os.path.dirname(cur)
        if not parent or parent == cur:
            break
        try:
            names = os.listdir(parent)
        except OSError:
            break
        if _read_bundle_manifest(parent) or _dir_has_mesh(names):
            return True
        cur = parent
    return False


def _is_mesh_package_folder(dirpath: str, filenames: List[str], dirnames: List[str]) -> bool:
    """One Library tile for a download folder (mesh + textures/ + optional bundle)."""
    if dirpath.endswith(os.sep) or not dirpath:
        return False
    bundle = _read_bundle_manifest(dirpath)
    has_mesh = _dir_has_mesh(filenames)
    has_tex_child = any(_is_texture_dir_name(d) for d in dirnames)
    if bundle and (has_mesh or has_tex_child or _looks_like_pbr_folder(dirpath, filenames)):
        return True
    if has_mesh and (bundle or has_tex_child):
        return True
    # Flat mesh + maps in same folder (no textures/ subdir)
    if has_mesh and _looks_like_pbr_folder(dirpath, filenames):
        return True
    # Staged Online Downloads Models/<Name>/ with only a mesh (ArmChair)
    if has_mesh and _under_online_models(dirpath):
        return True
    # Material packs accidentally under Models/ → still one folder tile (not each JPG)
    if _under_online_models(dirpath) and (
        bool(bundle) or has_tex_child or _looks_like_pbr_folder(dirpath, filenames)
    ):
        return True
    return False


def _path_has_mesh(path: str, *, max_depth: int = 3) -> bool:
    """True when path is a mesh file or a folder that contains one (shallow walk)."""
    if os.path.isfile(path):
        return file_extension(path) in MESH_EXT
    if not os.path.isdir(path):
        return False
    root = os.path.normpath(path)
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        if depth > max_depth:
            dirnames[:] = []
            continue
        if _dir_has_mesh(filenames):
            return True
    return False


def _path_has_import_mesh(path: str, *, max_depth: int = 3) -> bool:
    """Like ``_path_has_mesh`` but ignores USD preview files in material packs."""
    if os.path.isfile(path):
        return file_extension(path) in IMPORT_MESH_EXT
    if not os.path.isdir(path):
        return False
    root = os.path.normpath(path)
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        if depth > max_depth:
            dirnames[:] = []
            continue
        if _dir_has_import_mesh(filenames):
            return True
    return False


def resolve_asset_type(path: str, hint: Optional[str] = None) -> Optional[str]:
    """Canonical type for Drop/Import. Maps-only folders tagged ``mesh`` become materials."""
    from .ui.icons import canonical_import_type

    bundle = _read_bundle_manifest(path) if os.path.isdir(path) else None
    manifest = canonical_import_type(str((bundle or {}).get("asset_type") or ""))
    classified = classify_path(path) if path else None

    if manifest in {"material_set", "texture", "hdri"}:
        if manifest in {"material_set", "texture"} and _path_has_import_mesh(path):
            pass
        else:
            return manifest

    mapped = canonical_import_type(hint or "")
    if mapped == "mesh" and classified in {"material_set", "texture"}:
        if not _path_has_import_mesh(path):
            return classified
    if mapped:
        return mapped
    return classified


def classify_path(path: str, sibling_files: Optional[List[str]] = None) -> Optional[str]:
    ext = file_extension(path)
    if ext in MESH_EXT:
        return "mesh"
    if ext == ".mtlx":
        # GPUOpen MaterialX — treat as material package (maps live beside the graph)
        return "material_set"
    if ext in HDRI_EXT and "hdri" in path.lower().replace("\\", "/"):
        return "hdri"
    if ext in HDRI_EXT and any(k in os.path.basename(path).lower() for k in ("hdri", "env", "sky")):
        return "hdri"
    if os.path.isdir(path):
        try:
            names = os.listdir(path)
        except OSError:
            return None
        if any(str(n).lower().endswith(".mtlx") for n in names):
            return "material_set"
        file_names = [n for n in names if os.path.isfile(os.path.join(path, n))]
        # Mesh packages with sidecar maps must stay mesh (Drop imports geo, not paint)
        if _dir_has_import_mesh(file_names) or _path_has_import_mesh(path):
            return "mesh"
        if _looks_like_pbr_folder(path, file_names):
            return "material_set"
        bundle = _read_bundle_manifest(path)
        if bundle:
            from .ui.icons import canonical_import_type

            mapped = canonical_import_type(str(bundle.get("asset_type") or ""))
            return mapped or "mesh"
        return None
    if ext in TEXTURE_EXT:
        return "texture"
    return None


def _should_skip_texture(dirpath: str, filenames: List[str], name: str) -> bool:
    """Skip map files that belong to a mesh package or material set (Library clutter)."""
    if file_extension(name) not in TEXTURE_EXT:
        return False
    # Never show loose maps under Online Downloads/.../Models/...
    if _under_online_models(dirpath):
        return True
    # Material-set folder → one tile for the folder, not each map
    if _looks_like_pbr_folder(dirpath, filenames):
        return True
    # Maps sitting next to a mesh (Poly Haven glTF + _diff/_nor/_rough)
    if _dir_has_mesh(filenames):
        return True
    # textures/ maps/ under a parent that contains a mesh (Barrel_01/textures/*.jpg)
    base = os.path.basename(dirpath)
    if _is_texture_dir_name(base) or _ancestor_has_mesh_or_bundle(dirpath):
        return True
    return False


def _should_skip_mesh_file(dirpath: str, filenames: List[str], dirnames: List[str]) -> bool:
    """Skip individual mesh files when the parent folder is already the package tile."""
    return _is_mesh_package_folder(dirpath, filenames, dirnames)


def _thumb_hint(
    asset_path: str,
    asset_type: str,
    cache_dir: str = "",
    *,
    fetch: bool = True,
) -> str:
    """Prefer Online CDN embed; optional HTTP fetch when missing (worker-safe I/O)."""
    try:
        if cache_dir:
            online = thumbnails.ensure_online_style_preview(
                asset_path,
                cache_dir,
                asset_type=asset_type,
                fetch=bool(fetch),
            )
            if online:
                return online
        return thumbnails.discover_preview_image(asset_path, asset_type) or ""
    except Exception:
        return ""


def _package_record(
    *,
    dirpath: str,
    root: str,
    filenames: List[str],
    dirnames: Optional[List[str]] = None,
    bundle: Optional[Dict],
    cache_dir: str = "",
    fetch_thumbs: bool = True,
) -> Dict:
    rel_name = (bundle or {}).get("name") or os.path.basename(dirpath)
    from .ui.icons import canonical_import_type

    raw_type = str((bundle or {}).get("asset_type") or "")
    asset_type = canonical_import_type(raw_type) or "mesh"
    has_tex_child = any(_is_texture_dir_name(d) for d in (dirnames or []))
    if _dir_has_import_mesh(filenames) or _path_has_import_mesh(dirpath):
        asset_type = "mesh"
    elif _looks_like_pbr_folder(dirpath, filenames) or has_tex_child:
        asset_type = "material_set"
    elif not _dir_has_import_mesh(filenames):
        # Bundle/path said mesh but folder has no mesh file (texture/HDRI pack)
        if any(file_extension(n) in HDRI_EXT for n in filenames):
            asset_type = "hdri"
        elif any(file_extension(n) in TEXTURE_EXT for n in filenames):
            asset_type = "material_set"
    try:
        st = os.stat(dirpath)
        mtime, size = st.st_mtime, st.st_size
    except OSError:
        mtime, size = 0.0, 0
    path_norm = os.path.normpath(dirpath)
    return {
        "path": path_norm,
        "name": str(rel_name),
        "asset_type": asset_type,
        "root": root,
        "thumb_path": _thumb_hint(
            path_norm, asset_type, cache_dir, fetch=fetch_thumbs
        ),
        "mtime": mtime,
        "size": size,
        "source_id": str((bundle or {}).get("source_id") or "local"),
    }


def scan_filesystem(
    roots: List[str],
    cache_dir: str,
    *,
    progress: Optional[Callable[[str], None]] = None,
    force: bool = False,
    fetch_thumbs: bool = True,
) -> int:
    """Index library roots. Returns number of records written.

    ``fetch_thumbs=False`` skips CDN HTTP during index (use after Online Save —
    preview is already embedded; avoids main-thread stalls and worker imbuf).
    """
    conn = db.connect(cache_dir)
    total = 0
    try:
        for root in roots:
            root = os.path.normpath(root)
            if not os.path.isdir(root):
                if progress:
                    progress(f"Missing library root: {root}")
                continue
            fp = db.fingerprint_root(root)
            if not force and _FINGERPRINT_CACHE.get(root) == fp:
                if progress:
                    progress(f"Unchanged: {root}")
                continue
            # One commit per root — batched upserts (faster than per-row execute)
            db.clear_root(conn, root)
            batch: List[Dict] = []
            skip_dirs: Set[str] = set()

            def _flush_batch() -> None:
                nonlocal total
                if not batch:
                    return
                total += db.upsert_assets_batch(conn, batch)
                batch.clear()

            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [
                    d
                    for d in dirnames
                    if d not in {
                        ".git",
                        "__pycache__",
                        "UAL CACHE",
                        paths.PRODUCT_CACHE_BASENAME,
                        "UAL CACHE HOUDINI",
                    }
                    and d not in skip_dirs
                ]

                bundle = _read_bundle_manifest(dirpath)
                if _is_online_container_dir(dirpath):
                    continue
                is_package = (
                    dirpath != root
                    and _is_mesh_package_folder(dirpath, filenames, dirnames)
                )
                is_pbr_only = (
                    dirpath != root
                    and not is_package
                    and _looks_like_pbr_folder(dirpath, filenames)
                    and not _dir_has_mesh(filenames)
                    and not _under_online_models(dirpath)
                )

                # Mesh package (Poly Haven Barrel_01 + textures/) → ONE tile
                if is_package:
                    batch.append(
                        _package_record(
                            dirpath=dirpath,
                            root=root,
                            filenames=filenames,
                            dirnames=dirnames,
                            bundle=bundle,
                            cache_dir=cache_dir,
                            fetch_thumbs=fetch_thumbs,
                        )
                    )
                    if len(batch) >= 64:
                        _flush_batch()
                    dirnames[:] = []  # do not index textures/ or nested meshes
                    continue

                # Pure material folders (not under Models/)
                if is_pbr_only:
                    batch.append(
                        _package_record(
                            dirpath=dirpath,
                            root=root,
                            filenames=filenames,
                            dirnames=dirnames,
                            bundle=bundle,
                            cache_dir=cache_dir,
                            fetch_thumbs=fetch_thumbs,
                        )
                    )
                    if len(batch) >= 64:
                        _flush_batch()
                    dirnames[:] = []
                    continue

                # Never dive into texture sidecars next to a mesh
                if _dir_has_mesh(filenames) or bundle:
                    dirnames[:] = [d for d in dirnames if not _is_texture_dir_name(d)]

                for name in filenames:
                    if name.startswith(".") or name == _BUNDLE_NAME:
                        continue
                    if _should_skip_texture(dirpath, filenames, name):
                        continue
                    full = os.path.normpath(os.path.join(dirpath, name))
                    asset_type = classify_path(full)
                    if not asset_type:
                        continue
                    if asset_type == "texture":
                        # Absolute rule: no loose texture tiles under Models packages
                        if _under_online_models(full) or _ancestor_has_mesh_or_bundle(dirpath):
                            continue
                        if _should_skip_texture(dirpath, filenames, name):
                            continue
                    if asset_type == "mesh" and _should_skip_mesh_file(dirpath, filenames, dirnames):
                        continue
                    try:
                        st = os.stat(full)
                        mtime, size = st.st_mtime, st.st_size
                    except OSError:
                        mtime, size = 0.0, 0
                    batch.append(
                        {
                            "path": full,
                            "name": os.path.splitext(name)[0],
                            "asset_type": asset_type,
                            "root": root,
                            "thumb_path": _thumb_hint(
                                full,
                                asset_type,
                                cache_dir,
                                fetch=fetch_thumbs,
                            ),
                            "mtime": mtime,
                            "size": size,
                            "source_id": "local",
                        }
                    )
                    if len(batch) >= 64:
                        _flush_batch()
            _flush_batch()
            _FINGERPRINT_CACHE[root] = fp
            conn.commit()
            if progress:
                progress(f"Indexed {root}")
        conn.commit()
    finally:
        conn.close()
    return total


def list_assets(cache_dir: str, **kwargs) -> List[Dict]:
    conn = db.connect(cache_dir)
    try:
        return db.query_assets(conn, **kwargs)
    finally:
        conn.close()
