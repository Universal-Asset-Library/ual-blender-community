# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""Megascans / Fab plant-pack junk naming (pure — no bpy).

Plant / vegetation packs ship multi-variant meshes plus LOD2 impostor planes,
UE collision/proxy hulls, and far LODs. ``geo_import.strip_billboard_lods``
removes those (Fab-gated), then must drop deleted Object RNA refs so
place/assign never hit ``StructRNA of type Object has been removed``.

Parity with Houdini ``megascans_mesh_lod`` default strip (billboard + collision),
without the full Import LOD / plant-variant UI.
"""

from __future__ import annotations

import re
from typing import Iterable, List, Optional, Sequence, Set

_BILLBOARD_OBJECT_RE = re.compile(
    r"(billboard|impostor|_lod2\b|lod2_|_lod_2\b|lod_2\b)",
    re.IGNORECASE,
)

# UE collision hull prefixes + common proxy / convex collision labels.
_COLLISION_OBJECT_RE = re.compile(
    r"(^ucx_|^ubx_|^ucp_|^usp_|collision|convex|_proxy\b|\bproxy_|\bproxy\b)",
    re.IGNORECASE,
)

# Far distance LODs (LOD3+) — ultra-low / impostor tiers unused in Blender.
_FAR_LOD_OBJECT_RE = re.compile(
    r"(_lod[3-9]\b|lod[3-9]_|_lod_[3-9]\b)",
    re.IGNORECASE,
)

_LOD_SUFFIX_RE = re.compile(r"^(?P<base>.+)_LOD\d+$", re.IGNORECASE)


def is_megascans_billboard_object_name(name: str) -> bool:
    """True when an object name looks like a Megascans LOD2 / billboard impostor."""
    return bool(_BILLBOARD_OBJECT_RE.search(str(name or "")))


def is_megascans_collision_proxy_object_name(name: str) -> bool:
    """True for UE collision hulls / convex / proxy meshes (never used in Blender)."""
    return bool(_COLLISION_OBJECT_RE.search(str(name or "")))


def is_megascans_far_lod_object_name(name: str) -> bool:
    """True for LOD3+ distance meshes (strip with default plant cleanup)."""
    return bool(_FAR_LOD_OBJECT_RE.search(str(name or "")))


def is_megascans_billboard_material_name(name: str) -> bool:
    return "billboard" in str(name or "").strip().lower()


def find_unsuffixed_lod_companion_names(names: Iterable[str]) -> Set[str]:
    """Names with no ``_LODn`` suffix that also have a ``_LODn`` sibling.

    Fab plant glTFs often ship ``SM_…_VarA`` (collision/proxy) beside
    ``SM_…_VarA_LOD0`` / ``_LOD1``. The unsuffixed mesh is game collision.
    """
    cleaned = [str(n or "").strip() for n in names if str(n or "").strip()]
    if not cleaned:
        return set()
    lod_bases: Set[str] = set()
    for label in cleaned:
        match = _LOD_SUFFIX_RE.match(label)
        if match:
            lod_bases.add(match.group("base"))
    companions: Set[str] = set()
    for label in cleaned:
        if _LOD_SUFFIX_RE.match(label):
            continue
        if label in lod_bases:
            companions.add(label)
    return companions


def should_strip_megascans_plant_object(
    name: str,
    *,
    companion_names: Optional[Set[str]] = None,
) -> bool:
    """True when this object should be deleted on Fab plant import."""
    label = str(name or "").strip()
    if not label:
        return False
    if is_megascans_billboard_object_name(label):
        return True
    if is_megascans_collision_proxy_object_name(label):
        return True
    if is_megascans_far_lod_object_name(label):
        return True
    if companion_names and label in companion_names:
        return True
    return False


def object_rna_alive(obj) -> bool:
    """False when ``obj`` is None or Blender has already deleted the StructRNA."""
    if obj is None:
        return False
    try:
        # Any Blender RNA property access raises ReferenceError once removed.
        _ = obj.name
        return True
    except ReferenceError:
        return False
    except AttributeError:
        return False


def filter_alive_objects(objects: Optional[Sequence]) -> List:
    """Drop deleted Object refs (safe after ``bpy.data.objects.remove``)."""
    return [o for o in (objects or []) if object_rna_alive(o)]
