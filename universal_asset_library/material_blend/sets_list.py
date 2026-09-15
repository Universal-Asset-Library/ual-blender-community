# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""Pure helpers for Sets Targets/Source name lists (no bpy)."""

from __future__ import annotations


def collect_new_mesh_names(existing: set, objects) -> list:
    """Ordered unique mesh names to append.

    ``objects`` may be mesh Objects or any object with ``.type`` / ``.name``.
    Non-MESH entries are skipped when ``.type`` is present.
    """
    out: list = []
    seen = set(existing or ())
    for obj in objects or ():
        if obj is None:
            continue
        typ = getattr(obj, "type", None)
        if typ is not None and typ != "MESH":
            continue
        name = getattr(obj, "name", None)
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(str(name))
    return out


def mesh_objects_in_collection(collection) -> list:
    """MESH objects in a Collection-like (``all_objects`` when available)."""
    if collection is None:
        return []
    objs = getattr(collection, "all_objects", None)
    if objs is None:
        objs = getattr(collection, "objects", None) or ()
    out = []
    for obj in objs:
        if obj is not None and getattr(obj, "type", "") == "MESH":
            out.append(obj)
    return out
