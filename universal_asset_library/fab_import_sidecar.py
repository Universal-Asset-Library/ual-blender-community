# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""Minimal Fab fab_import.json writer for shared-library parity with Houdini UAL."""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Sequence

FAB_IMPORT_SIDECAR = "fab_import.json"
MESH_EXTENSIONS = (".fbx", ".obj", ".abc", ".glb", ".gltf", ".usd", ".usda", ".usdc", ".usdz")
QUALITY_TO_LOD = {"raw": "raw", "high": "high", "mid": "med", "low": "low"}


def quality_to_active_lod(quality: str) -> str:
    return QUALITY_TO_LOD.get(str(quality or "").strip().lower(), "high")


def _collect_files(root: str) -> List[str]:
    paths: List[str] = []
    if not root or not os.path.isdir(root):
        return paths
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            paths.append(os.path.normpath(os.path.join(dirpath, name)))
    return paths


def _pick_mesh_files(paths: Sequence[str], quality: str) -> List[str]:
    meshes = [path for path in paths if os.path.splitext(path)[1].lower() in MESH_EXTENSIONS]
    if not meshes:
        return []
    quality_l = str(quality or "").lower()
    hints = {
        "raw": ("raw", "8k"),
        "high": ("high", "4k"),
        "mid": ("mid", "medium", "2k"),
        "low": ("low", "1k"),
    }.get(quality_l, (quality_l,))
    preferred = [
        mesh
        for mesh in meshes
        if any(hint and hint in os.path.basename(mesh).lower() for hint in hints)
    ]
    chosen = preferred or meshes
    chosen.sort(key=lambda item: os.path.basename(item).lower())
    return chosen


def _relative_bundle_path(bundle_folder: str, path: str) -> str:
    abs_path = os.path.normpath(str(path or ""))
    root = os.path.normpath(str(bundle_folder or ""))
    if not abs_path:
        return ""
    if not root:
        return abs_path.replace("\\", "/")
    try:
        if os.path.commonpath([abs_path, root]) == root:
            return os.path.relpath(abs_path, root).replace("\\", "/")
    except ValueError:
        pass
    return abs_path.replace("\\", "/")


def build_fab_import_manifest(
    *,
    name: str,
    asset_id: str,
    listing_type: str = "3d-model",
    root: str,
    bundle_folder: str = "",
    quality: str = "high",
    fmt: str = "",
    mesh_lod: str = "",
) -> Dict[str, Any]:
    store_root = os.path.normpath(bundle_folder or root or "")
    paths = _collect_files(root)
    mesh_paths = _pick_mesh_files(paths, quality)
    meshes = [
        {
            "name": os.path.basename(mesh),
            "path": _relative_bundle_path(store_root, mesh),
        }
        for mesh in mesh_paths
    ]
    return {
        "source": "fab",
        "listing_id": asset_id,
        "title": name,
        "listing_type": str(listing_type or "3d-model").strip().lower() or "3d-model",
        "quality": quality,
        "quality_lod": quality_to_active_lod(quality),
        "mesh_lod": str(mesh_lod or "").strip().lower(),
        "format": fmt,
        "naming_profile": "megascans",
        "tags": [],
        "categories": [],
        "textures": [],
        "meshes": meshes,
    }


def write_fab_import_sidecar(bundle_folder: str, payload: Dict[str, Any]) -> str:
    os.makedirs(bundle_folder, exist_ok=True)
    import_path = os.path.join(bundle_folder, FAB_IMPORT_SIDECAR)
    with open(import_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
    return import_path


def write_fab_sidecar_for_bundle(
    bundle_folder: str,
    *,
    asset_name: str,
    asset_id: str,
    asset_type: str = "mesh",
    quality: str = "",
    fmt: str = "",
    mesh_lod: str = "",
    listing_type: str = "",
) -> Optional[str]:
    if not bundle_folder or not os.path.isdir(bundle_folder):
        return None
    scan_root = bundle_folder
    extracted = os.path.join(bundle_folder, "extracted")
    if os.path.isdir(extracted):
        scan_root = extracted
    listing = listing_type or ("3d-model" if str(asset_type or "").lower() == "mesh" else "surface")
    payload = build_fab_import_manifest(
        name=asset_name or asset_id,
        asset_id=asset_id,
        listing_type=listing,
        root=scan_root,
        bundle_folder=bundle_folder,
        quality=quality or "high",
        fmt=fmt,
        mesh_lod=mesh_lod,
    )
    return write_fab_import_sidecar(bundle_folder, payload)
