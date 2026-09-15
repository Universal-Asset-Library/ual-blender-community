# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""Scene-level Sets mode RNA (Targets / Source lists + live controls).

Patterns inspired by MK Surface Blend UI — rewritten for UAL (no paste).
Internal RNA keeps ``ual_blend_set_a`` / ``ual_blend_set_b``; UI says Targets / Source.

Blender has no public API for Outliner → N-panel UIList drag-drop. Sets lists
are filled via: selection +, Object eyedropper/search, or Collection expand.
"""

from __future__ import annotations

import bpy
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)
from bpy.types import PropertyGroup, UIList

from . import constants as C
from .sets_list import collect_new_mesh_names, mesh_objects_in_collection

__all__ = (
    "collect_new_mesh_names",
    "mesh_objects_in_collection",
    "append_mesh_objects",
    "assign_meshes_from_selection",
    "resolve_set_meshes",
    "register",
    "unregister",
)


def _poll_mesh_object(_self, obj) -> bool:
    return obj is not None and getattr(obj, "type", "") == "MESH"


def append_mesh_objects(scene, coll_attr: str, objects) -> int:
    """Append unique mesh object names to a set list. Returns added count."""
    coll = getattr(scene, coll_attr, None)
    if coll is None:
        return 0
    existing = {item.name for item in coll if item.name}
    to_add = collect_new_mesh_names(existing, objects)
    for name in to_add:
        item = coll.add()
        item.name = name
    return len(to_add)


def _update_pick_obj_a(self, context) -> None:
    obj = getattr(self, "ual_blend_pick_obj_a", None)
    if obj is None:
        return
    append_mesh_objects(self, "ual_blend_set_a", [obj])
    self.ual_blend_pick_obj_a = None


def _update_pick_obj_b(self, context) -> None:
    obj = getattr(self, "ual_blend_pick_obj_b", None)
    if obj is None:
        return
    append_mesh_objects(self, "ual_blend_set_b", [obj])
    self.ual_blend_pick_obj_b = None


def _update_pick_coll_a(self, context) -> None:
    coll = getattr(self, "ual_blend_pick_coll_a", None)
    if coll is None:
        return
    append_mesh_objects(self, "ual_blend_set_a", mesh_objects_in_collection(coll))
    self.ual_blend_pick_coll_a = None


def _update_pick_coll_b(self, context) -> None:
    coll = getattr(self, "ual_blend_pick_coll_b", None)
    if coll is None:
        return
    append_mesh_objects(self, "ual_blend_set_b", mesh_objects_in_collection(coll))
    self.ual_blend_pick_coll_b = None


class UAL_PG_blend_set_item(PropertyGroup):
    name: StringProperty(name="Object", default="")


class UAL_UL_blend_set(UIList):
    bl_idname = "UAL_UL_blend_set"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        obj = bpy.data.objects.get(item.name) if item.name else None
        ic = "OUTLINER_OB_MESH" if obj and obj.type == "MESH" else "ERROR"
        layout.label(text=item.name or "(missing)", icon=ic)


def register() -> None:
    bpy.utils.register_class(UAL_PG_blend_set_item)
    bpy.utils.register_class(UAL_UL_blend_set)
    S = bpy.types.Scene
    S.ual_blend_mode = EnumProperty(
        name="Blend Mode",
        description="Object = active source + selected targets; Sets = named Targets / Source lists",
        items=(
            (C.MODE_OBJECT, "Object", "Active mesh = source; other selected = targets"),
            (C.MODE_SETS, "Sets", "Targets / Source lists; same proximity Max/Scale as Object"),
        ),
        default=C.MODE_OBJECT,
    )
    S.ual_blend_set_a = CollectionProperty(type=UAL_PG_blend_set_item)
    S.ual_blend_set_b = CollectionProperty(type=UAL_PG_blend_set_item)
    S.ual_blend_set_a_index = IntProperty(name="Targets Index", default=0)
    S.ual_blend_set_b_index = IntProperty(name="Source Index", default=0)
    S.ual_blend_pick_obj_a = PointerProperty(
        name="Pick Target",
        description="Eyedropper or search: append a mesh to Targets (then clears)",
        type=bpy.types.Object,
        poll=_poll_mesh_object,
        update=_update_pick_obj_a,
    )
    S.ual_blend_pick_obj_b = PointerProperty(
        name="Pick Source",
        description="Eyedropper or search: append a mesh to Source (then clears)",
        type=bpy.types.Object,
        poll=_poll_mesh_object,
        update=_update_pick_obj_b,
    )
    S.ual_blend_pick_coll_a = PointerProperty(
        name="Targets Collection",
        description="Append all meshes in this collection to Targets (then clears)",
        type=bpy.types.Collection,
        update=_update_pick_coll_a,
    )
    S.ual_blend_pick_coll_b = PointerProperty(
        name="Source Collection",
        description="Append all meshes in this collection to Source (then clears)",
        type=bpy.types.Collection,
        update=_update_pick_coll_b,
    )
    S.ual_blend_blend_normals = BoolProperty(
        name="Blend Normals",
        description=(
            "Transfer custom normals from source onto targets "
            "(proximity-weighted DATA_TRANSFER — Blendit-aligned)"
        ),
        default=True,
    )


def unregister() -> None:
    S = bpy.types.Scene
    for attr in (
        "ual_blend_mode",
        "ual_blend_set_a",
        "ual_blend_set_b",
        "ual_blend_set_a_index",
        "ual_blend_set_b_index",
        "ual_blend_pick_obj_a",
        "ual_blend_pick_obj_b",
        "ual_blend_pick_coll_a",
        "ual_blend_pick_coll_b",
        "ual_blend_live_strength",  # legacy Sets GN — delete if present
        "ual_blend_live_spread",
        "ual_blend_live_noise_scale",
        "ual_blend_live_noise_amount",
        "ual_blend_transfer_uv",  # legacy — delete if present
        "ual_blend_blend_normals",
    ):
        if hasattr(S, attr):
            delattr(S, attr)
    bpy.utils.unregister_class(UAL_UL_blend_set)
    bpy.utils.unregister_class(UAL_PG_blend_set_item)


def assign_meshes_from_selection(scene, coll_attr: str) -> int:
    """Append currently selected meshes to a set list (no duplicates). Returns added count."""
    ctx = bpy.context
    return append_mesh_objects(
        scene, coll_attr, getattr(ctx, "selected_objects", ()) or ()
    )


def resolve_set_meshes(scene, coll_attr: str) -> list:
    """Resolve name list → mesh objects (skip missing)."""
    coll = getattr(scene, coll_attr, None)
    out = []
    if coll is None:
        return out
    for item in coll:
        obj = bpy.data.objects.get(item.name)
        if obj is not None and getattr(obj, "type", "") == "MESH":
            out.append(obj)
    return out
