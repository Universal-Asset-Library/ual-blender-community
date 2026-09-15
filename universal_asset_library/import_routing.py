# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""Central import dispatcher (mirrors Houdini import_routing).

Material assignment follows BlenderKit's selected / drop-target pattern: when
``import.apply_material_to_selected`` is on, textures/materials go onto
``target_object`` (viewport raycast on Drop) at the hit face's material slot,
or onto selected mesh objects (UV automap via ``material_import_automap``).

Online mesh packages (Poly Haven, Fab, …): after geo import, sidecar PBR maps
are detected and a Principled BSDF is built so Base Color + all maps connect.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import bpy

from . import collection_org
from . import geo_import
from . import material_builder
from .indexer import file_extension, resolve_asset_type
from .mesh_resolve import resolve_mesh_file
from .texture_match import (
    find_sidecar_texture_folders,
    prepare_import_texture_set,
    texture_set_ready_for_principled,
)


def resolve_effective_asset_type(path: str, hint: Optional[str] = None) -> str:
    """Canonical mesh / material_set / texture / hdri for import.

    Online connectors do not share one enum: ambientCG uses ``Material``,
    GPUOpen/Fab/LazyTextures use ``material``, Poly Haven uses ``texture``.
    Passing those raw hints used to miss the Principled branch and drop
    with a fake success (unsupported type, nothing assigned).

    Library tiles can also be stored as ``mesh`` when a material ZIP landed
    under Models/ — if the folder has maps and no mesh, treat it as a material.
    """
    resolved = resolve_asset_type(path, hint)
    if resolved:
        return resolved
    if os.path.isdir(path):
        return "material_set"
    ext = file_extension(path)
    if ext in {".hdr", ".exr"}:
        return "hdri"
    return "mesh"


def _selected_mesh_objects() -> List:
    out = []
    for obj in bpy.context.selected_objects or []:
        if getattr(obj, "type", "") == "MESH" or hasattr(getattr(obj, "data", None), "materials"):
            out.append(obj)
    return out


def _resolve_material_targets(apply_selected: bool, target_object=None) -> List:
    """Prefer drop-hit mesh, else current selection (BlenderKit-style)."""
    if not apply_selected:
        return []
    if target_object is not None:
        if getattr(target_object, "type", "") == "MESH" or hasattr(
            getattr(target_object, "data", None), "materials"
        ):
            return [target_object]
    return _selected_mesh_objects()


def _resolve_target_slot(target_object, face_index: Optional[int], target_slot: Optional[int]) -> int:
    if target_slot is not None:
        try:
            return max(0, int(target_slot))
        except (TypeError, ValueError):
            return 0
    return material_builder.material_slot_from_face(target_object, face_index)


def _naming_profile(cfg: Dict[str, Any]) -> Optional[str]:
    profile = str((cfg.get("import") or {}).get("texture_naming_profile") or "auto")
    return None if profile == "auto" else profile


def _build_sidecar_material(path: str, cfg: Dict[str, Any]):
    """Detect PBR maps beside an online/local mesh and build Principled."""
    try:
        mesh_file = resolve_mesh_file(path)
    except Exception:
        mesh_file = path if os.path.isfile(path) else path
    # Probe every likely textures folder (package root + parents)
    candidates: List[str] = []
    for base in (mesh_file, path):
        for folder in find_sidecar_texture_folders(base):
            if folder not in candidates:
                candidates.append(folder)
    profile = _naming_profile(cfg)
    normal_space = str((cfg.get("import") or {}).get("normal_map_space") or "OPENGL")
    best_maps = None
    best_audit = None
    for candidate in candidates:
        maps, audit = prepare_import_texture_set(
            candidate,
            profile=profile,
            # Do not stem-filter: Online Downloads packages are already per-asset folders.
            mesh_path=None,
            search_roots=candidates,
            normal_map_space=normal_space,
        )
        if texture_set_ready_for_principled(maps):
            # Prefer sets that include albedo (Base Color)
            if maps.get("albedo"):
                return maps, audit
            if best_maps is None:
                best_maps, best_audit = maps, audit
    if best_maps is not None:
        return best_maps, best_audit
    return None, None


def perform_typed_import(
    path: str,
    *,
    asset_type: Optional[str] = None,
    cfg: Optional[Dict[str, Any]] = None,
    target_object=None,
    face_index: Optional[int] = None,
    target_slot: Optional[int] = None,
    place_at_origin: bool = True,
    asset_name: str = "",
) -> Dict[str, Any]:
    """Import asset on the main thread. Returns a result dict.

    ``target_object`` / ``face_index`` — Drop raycast hit (material/texture assign).
    ``place_at_origin`` — meshes land at world origin with base on Z=0 (Import,
    Download & Import, and Drop when ``import.place_at_world_origin`` is on).
    ``asset_name`` — Outliner collection label (display name); path stem if empty.
    """
    cfg = cfg or {}
    path = os.path.normpath(path)
    atype = resolve_effective_asset_type(path, asset_type)
    result: Dict[str, Any] = {
        "ok": True,
        "asset_type": atype,
        "path": path,
        "objects": [],
        "material": None,
        "collection": None,
        "target_slot": 0,
        "maps": {},
    }
    apply_selected = bool((cfg.get("import") or {}).get("apply_material_to_selected", True))
    slot = _resolve_target_slot(target_object, face_index, target_slot)
    result["target_slot"] = slot

    if atype == "mesh":
        objects = geo_import.import_mesh(path)
        geo_import.maybe_apply_fab_scale(path, objects, cfg)
        # Must keep return value: strip deletes LOD2/billboard RNA (plant packs).
        objects = geo_import.strip_billboard_lods(objects, cfg, path)
        geo_import.apply_import_transforms(objects, cfg)
        # Panel Import / Download & Import / Drop (when enabled): world origin.
        if place_at_origin and bool((cfg.get("import") or {}).get("place_at_world_origin", True)):
            geo_import.place_at_world_origin(objects, snap_base=True)
        if bool((cfg.get("import") or {}).get("organize_into_collection", True)):
            coll_name = collection_org.resolve_asset_collection_name(asset_name, path)
            coll = collection_org.organize_imported_objects(objects, name=coll_name)
            result["collection"] = coll
        result["objects"] = objects
        maps, _audit = _build_sidecar_material(path, cfg)
        if maps:
            result["maps"] = {k: v for k, v in maps.items() if v}
            mat = material_builder.build_principled_material(
                os.path.splitext(os.path.basename(path))[0] or "UAL_Mesh",
                maps,
                cfg=cfg,
            )
            result["material"] = mat
            # Replace all slots on imported meshes so glTF embeds don't hide UAL maps
            material_builder.assign_material_to_objects(
                objects, mat, cfg=cfg, target_slot=0, all_slots=True
            )
            if apply_selected:
                extras = [
                    o
                    for o in _resolve_material_targets(True, target_object)
                    if o not in objects
                ]
                if extras:
                    material_builder.assign_material_to_objects(
                        extras, mat, cfg=cfg, target_slot=slot, all_slots=False
                    )
        return result

    if atype in ("material_set", "texture"):
        # .mtlx (GPUOpen) → scan the package folder for sibling image maps
        folder = path if os.path.isdir(path) else os.path.dirname(path)
        targets = _resolve_material_targets(apply_selected, target_object)
        mat = material_builder.build_from_folder(
            folder,
            assign_to=(targets if targets else None),
            target_slot=slot,
            cfg=cfg,
        )
        result["material"] = mat
        # Surface which maps were used (for status / debug)
        try:
            maps, _ = prepare_import_texture_set(folder, profile=_naming_profile(cfg))
            result["maps"] = {k: v for k, v in maps.items() if v}
        except Exception:
            pass
        return result

    if atype == "hdri":
        world = geo_import.import_hdri(path)
        result["world"] = world
        return result

    result["ok"] = False
    result["error"] = f"Unsupported asset type: {atype}"
    return result
