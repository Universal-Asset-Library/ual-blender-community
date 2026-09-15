# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""Resolve a mesh file inside an online/library bundle folder (no bpy)."""

from __future__ import annotations

import os
from typing import List, Tuple

_MESH_EXTS = (
    ".glb",
    ".gltf",
    ".fbx",
    ".obj",
    ".stl",
    ".usd",
    ".usda",
    ".usdc",
    ".usdz",
    ".ply",
    ".abc",
)

_PRIORITY = {
    ".glb": 0,
    ".gltf": 1,
    ".fbx": 2,
    ".obj": 3,
    ".usd": 4,
    ".usda": 4,
    ".usdc": 4,
    ".usdz": 4,
    ".stl": 5,
    ".ply": 6,
    ".abc": 7,
}


def resolve_mesh_file(path: str) -> str:
    """Return a mesh filepath. Directories are scanned for the best candidate."""
    path = os.path.normpath(path or "")
    if os.path.isfile(path):
        return path
    if not os.path.isdir(path):
        raise FileNotFoundError(path)
    ranked: List[Tuple[int, int, int, str]] = []
    for root, _dirs, files in os.walk(path):
        depth = root[len(path) :].count(os.sep)
        for name in files:
            ext = os.path.splitext(name)[1].lower()
            if ext not in _MESH_EXTS:
                continue
            full = os.path.join(root, name)
            ranked.append((_PRIORITY.get(ext, 9), depth, len(name), full))
    if not ranked:
        raise FileNotFoundError(f"No mesh file in bundle: {path}")
    ranked.sort()
    return ranked[0][3]
