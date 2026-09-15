# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""Per-object Material Blend RNA state."""

from __future__ import annotations

import bpy
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    FloatProperty,
    PointerProperty,
    StringProperty,
)
from bpy.types import PropertyGroup


class UAL_PG_blend_orig_mat(PropertyGroup):
    """One original material name per target slot (uses PropertyGroup.name)."""


def _update_distance(self, context) -> None:
    """Max / Scale → VERTEX_WEIGHT_PROXIMITY (Blendit Simple)."""
    obj = self.id_data
    try:
        from . import modifiers as mod_mod

        if getattr(self, "blend_source", False):
            mod_mod.push_proximity_for_source(obj)
        else:
            mod_mod.push_proximity_distances(obj)
    except Exception:
        pass


def _update_noise(self, context) -> None:
    """Noise / Amount → Mix Fac noise chain."""
    obj = self.id_data
    try:
        from . import materials as mat_mod

        mat_mod.push_noise_settings(obj)
    except Exception:
        pass


class UAL_PG_material_blend(PropertyGroup):
    """Live blend controls on each Object (update callbacks + drivers)."""

    blend_max: FloatProperty(
        name="Max Distance",
        description="Outer blending range on Source (world units). Outer = Max × Scale",
        subtype="DISTANCE",
        default=1.0,
        min=0.0,
        soft_max=20.0,
        update=_update_distance,
    )
    blend_min: FloatProperty(
        name="Min Distance",
        description="Inner blend start (Advanced; Simple uses 10% of Max×Scale)",
        subtype="DISTANCE",
        default=1.0,
        min=0.0,
        soft_max=2.0,
        update=_update_distance,
    )
    blend_scale: FloatProperty(
        name="Scale",
        description="Per-target multiplier on Max (Blendit blend_scale)",
        default=1.0,
        min=0.01,
        soft_max=10.0,
        update=_update_distance,
    )
    blend_noise_scale: FloatProperty(
        name="Noise Scale",
        description="Edge noise frequency (Blendit Frequency). Higher = finer breakup",
        default=1.0,
        min=0.0,
        soft_max=64.0,
        update=_update_noise,
    )
    blend_noise_amount: FloatProperty(
        name="Noise Amount",
        description="Edge noise strength (Blendit Amplitude). 0 = smooth proximity edge",
        default=1.0,
        min=0.0,
        soft_max=2.0,
        update=_update_noise,
    )
    blend_normals_used: BoolProperty(
        name="Blend Normals Used",
        description="Blend transferred custom normals from the source (proximity-weighted)",
        default=False,
    )
    blended: BoolProperty(
        name="Blended",
        description="Object has an active UAL Material Blend setup",
        default=False,
    )
    blend_source: BoolProperty(
        name="Is Source",
        description="This object was the active source when Create ran",
        default=False,
    )
    applied: BoolProperty(
        name="Applied",
        description="Modifiers were baked (Apply)",
        default=False,
    )
    source_obj: PointerProperty(
        name="Source Object",
        type=bpy.types.Object,
        description="Proximity source mesh",
    )
    original_material_name: StringProperty(
        name="Original Material",
        description="Legacy slot-0 material name to restore on Remove",
        default="",
    )
    original_materials: CollectionProperty(
        type=UAL_PG_blend_orig_mat,
        name="Original Materials",
        description="Per-slot material names to restore on Remove (multi-material targets)",
    )
    sets_blended: BoolProperty(
        name="Sets Blended",
        description="Object was blended via Sets mode (same proximity stack as Object mode)",
        default=False,
    )


def register() -> None:
    bpy.utils.register_class(UAL_PG_blend_orig_mat)
    bpy.utils.register_class(UAL_PG_material_blend)
    bpy.types.Object.ual_blend = PointerProperty(type=UAL_PG_material_blend)


def unregister() -> None:
    if hasattr(bpy.types.Object, "ual_blend"):
        del bpy.types.Object.ual_blend
    bpy.utils.unregister_class(UAL_PG_material_blend)
    bpy.utils.unregister_class(UAL_PG_blend_orig_mat)
