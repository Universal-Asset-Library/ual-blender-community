# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""Outliner collection helpers for imported 3D assets (BlenderKit-style naming).

Pure naming helpers are bpy-free for unit tests. ``organize_imported_objects``
must run on the main Blender thread only.
"""

from __future__ import annotations

import os
import re
from typing import Any, List, Optional, Sequence

_INVALID_CHARS = re.compile(r'[\\/:\*\?"<>\|]')
_MULTI_SPACE = re.compile(r"\s+")


def sanitize_collection_name(name: str) -> str:
    """Make a Blender-safe collection label; fallback ``Asset`` when empty."""
    text = str(name or "").strip()
    text = os.path.basename(text.replace("\\", "/"))
    text = os.path.splitext(text)[0] if text else ""
    text = _INVALID_CHARS.sub(" ", text)
    text = _MULTI_SPACE.sub(" ", text).strip(" .")
    # Blender rejects empty names; keep short but readable
    if not text:
        return "Asset"
    return text[:63]


def resolve_asset_collection_name(asset_name: str = "", path: str = "") -> str:
    """Prefer display name; else package folder / file stem."""
    if (asset_name or "").strip():
        return sanitize_collection_name(asset_name)
    raw = (path or "").strip()
    if not raw:
        return "Asset"
    norm = os.path.normpath(raw)
    # Online Downloads/<source>/<Type>/<Name>/… → Name
    parts = [p for p in norm.replace("\\", "/").split("/") if p]
    lower = [p.lower() for p in parts]
    if "online downloads" in lower:
        try:
            idx = lower.index("online downloads")
            if len(parts) >= idx + 4:
                return sanitize_collection_name(parts[idx + 3])
        except ValueError:
            pass
    if os.path.isdir(norm):
        return sanitize_collection_name(os.path.basename(norm.rstrip("\\/")))
    stem = os.path.splitext(os.path.basename(norm))[0]
    parent = os.path.basename(os.path.dirname(norm))
    if parent and parent.lower() not in (
        "online downloads",
        "meshes",
        "models",
        "geo",
        "geometry",
        "fbx",
        "gltf",
        "glb",
    ):
        if stem.lower() in ("scene", "mesh", "model", "asset", "root") or stem.lower().startswith(
            "lod"
        ):
            return sanitize_collection_name(parent)
    return sanitize_collection_name(stem)


def _active_parent_collection():
    """View-layer active collection, else scene master collection."""
    import bpy

    try:
        layer = bpy.context.view_layer
        if layer is not None and layer.active_layer_collection is not None:
            return layer.active_layer_collection.collection
    except Exception:
        pass
    try:
        scene = bpy.context.scene
        if scene is not None:
            return scene.collection
    except Exception:
        pass
    return None


def organize_imported_objects(
    objects: Sequence[Any],
    *,
    name: str,
    parent_collection: Any = None,
) -> Optional[Any]:
    """Create a collection named ``name`` and move imported objects into it.

    Returns the collection, or None when there is nothing to organize.
    Main-thread only (touches ``bpy.data.collections``).
    """
    import bpy

    from .megascans_billboard import filter_alive_objects

    survivors: List[Any] = filter_alive_objects(list(objects or []))
    if not survivors:
        return None

    coll_name = sanitize_collection_name(name)
    coll = bpy.data.collections.new(coll_name)

    parent = parent_collection
    if parent is None:
        parent = _active_parent_collection()
    if parent is None:
        try:
            parent = bpy.context.scene.collection
        except Exception:
            parent = None
    if parent is not None:
        try:
            # Avoid linking under itself or double-linking
            if coll.name not in {c.name for c in parent.children}:
                parent.children.link(coll)
        except RuntimeError:
            try:
                bpy.context.scene.collection.children.link(coll)
            except Exception:
                pass

    for obj in survivors:
        try:
            for uc in list(obj.users_collection):
                try:
                    uc.objects.unlink(obj)
                except RuntimeError:
                    pass
            if obj.name not in coll.objects:
                coll.objects.link(obj)
        except Exception:
            continue
    return coll
