# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""Namespaced IDs for Material Blend (Phase 1 Simple).

Core method mirrors the reference Blendit proximity→mask→Mix Shader flow,
rewritten for UAL — do not paste Blendit source.
"""

from __future__ import annotations

PREFIX = "UAL_Blend"
VG_NAME = PREFIX
MASK_ATTR = f"{PREFIX}_Mask"
# Sets mode (Geometry Nodes) stores a POINT float mask
SETS_MASK_ATTR = f"{PREFIX}_GroundMask"
MOD_PROXIMITY = f"{PREFIX}_Proximity"
MOD_TRANSFER = f"{PREFIX}_TransferMask"
MOD_TRANSFER_NORM = f"{PREFIX}_TransferNorm"
# Legacy names — still stripped on Remove from older blends
MOD_TRANSFER_UV = f"{PREFIX}_TransferUV"
MOD_UV_WARP = f"{PREFIX}_UVWarp"
MOD_GN = f"{PREFIX}_Ground"
NG_NAME = f"{PREFIX}_Ground"
SETS_SRC_COLL = f"{PREFIX}_Src"
SETS_MIX_NAME = f"{PREFIX}_SetsMix"
SETS_ATTR_NODE = f"{PREFIX}_SetsAttr"
SETS_SRC_PREFIX = f"{PREFIX}_SETS_SRC__"
ORIGIN_NODE_PREFIX = "UALOrigin_"
SOURCE_NODE_PREFIX = "UALSource_"
MIX_NODE_NAME = f"{PREFIX}_MixShader"
MASK_NODE_NAME = f"{PREFIX}_MaskAttr"
NOISE_TEX_NAME = f"{PREFIX}_Noise"
NOISE_CENTER_NAME = f"{PREFIX}_NoiseCenter"
NOISE_MUL_NAME = f"{PREFIX}_NoiseMul"
NOISE_ADD_NAME = f"{PREFIX}_NoiseAdd"
NOISE_CLAMP_NAME = f"{PREFIX}_NoiseClamp"
OUTPUT_NODE_NAME = f"{PREFIX}_Output"
UV_LAYER = f"{PREFIX}_UV"  # legacy layer name cleaned on Remove
MAT_NAME_PREFIX = f"{PREFIX}_S_"
SETS_WARN_COUNT = 50
# Stored on Object.ual_blend.original_material_name / original_materials when blended
STYLE_SIMPLE = 0  # Simple proximity; noise is optional edge breakup on Mix Fac

MODE_OBJECT = "OBJECT"
MODE_SETS = "SETS"


def blend_material_name(
    origin_mat_name: str,
    source_mat_name: str,
    target_obj_name: str = "",
    slot_index: int = 0,
) -> str:
    """Stable cache key / material name for a Simple blend pair (per target slot)."""
    o = (origin_mat_name or "Mat").replace(".", "_")[:28]
    s = (source_mat_name or "Src").replace(".", "_")[:28]
    t = (target_obj_name or "").replace(".", "_")[:16]
    slot = f"__s{max(0, int(slot_index))}"
    if t:
        name = f"{MAT_NAME_PREFIX}{o}__{s}__{t}{slot}"
    else:
        name = f"{MAT_NAME_PREFIX}{o}__{s}{slot}"
    return name[:63]


def proximity_distances(
    blend_max: float,
    blend_min: float,
    blend_scale: float,
    *,
    simple: bool = True,
) -> tuple[float, float]:
    """Return ``(min_dist, max_dist)`` for VERTEX_WEIGHT_PROXIMITY.

    Blendit Simple: max = blend_max * scale, min = blend_max * scale * 0.1.
    Advanced (Phase 2): min = blend_min * scale, max = blend_max * scale.
    """
    scale = max(0.0, float(blend_scale))
    mx = max(0.0, float(blend_max)) * scale
    if simple:
        return (mx * 0.1, mx)
    mn = max(0.0, float(blend_min)) * scale
    if mn > mx:
        mn, mx = mx, mn
    return (mn, mx)


def selection_ready(mesh_count: int, has_source_material: bool) -> str:
    """Empty string when Create is allowed; else short artist message."""
    if mesh_count < 2:
        return "Select source (active) + at least one target mesh"
    if not has_source_material:
        return "Active mesh needs a material in slot 1"
    return ""


def sets_ready(set_a_count: int, set_b_count: int, has_donor_material: bool) -> str:
    """Empty when Sets Blend is allowed."""
    if set_a_count < 1:
        return "Assign Targets (meshes that receive the blend)"
    if set_b_count < 1:
        return "Assign Source (surface material mesh)"
    if not has_donor_material:
        return "Source needs a material with nodes"
    return ""


def clamp_live(
    strength: float,
    spread: float,
    noise_scale: float,
    noise_amount: float,
) -> tuple[float, float, float, float]:
    """Pure clamps for Sets live controls (tests + prefs)."""
    s = max(0.0, min(float(strength), 1.0))
    sp = max(0.0, min(float(spread), 10.0))
    ns = max(0.0, float(noise_scale))
    na = max(0.0, min(float(noise_amount), 2.0))
    return (s, sp, ns, na)
