# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""Mesh / HDRI import via Blender operators (main thread only)."""

from __future__ import annotations

import os
from typing import List, Optional

import bpy

from . import online_identity
from .indexer import file_extension
from .megascans_billboard import (
    filter_alive_objects,
    find_unsuffixed_lod_companion_names,
    is_megascans_billboard_material_name,
    is_megascans_billboard_object_name,
    object_rna_alive,
    should_strip_megascans_plant_object,
)
from .mesh_resolve import resolve_mesh_file

# Re-export pure helpers for callers that imported them from geo_import.
__all__ = [
    "filter_alive_objects",
    "import_hdri",
    "import_mesh",
    "is_megascans_billboard_material_name",
    "is_megascans_billboard_object_name",
    "maybe_apply_fab_scale",
    "object_rna_alive",
    "place_at_world_origin",
    "place_objects_at",
    "strip_billboard_lods",
]


def _selected_objects_before() -> set:
    return set(bpy.context.selected_objects)


def import_mesh(path: str, *, scale: float = 1.0) -> List[bpy.types.Object]:
    path = resolve_mesh_file(path)
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    before = _selected_objects_before()
    ext = file_extension(path)
    kwargs = {"filepath": path}
    if ext == ".fbx":
        bpy.ops.import_scene.fbx(**kwargs, global_scale=scale)
    elif ext in {".glb", ".gltf"}:
        bpy.ops.import_scene.gltf(**kwargs)
    elif ext == ".obj":
        if hasattr(bpy.ops.wm, "obj_import"):
            bpy.ops.wm.obj_import(**kwargs)
        else:
            bpy.ops.import_scene.obj(**kwargs)
    elif ext == ".stl":
        if hasattr(bpy.ops.wm, "stl_import"):
            bpy.ops.wm.stl_import(**kwargs)
        else:
            bpy.ops.import_mesh.stl(**kwargs)
    elif ext in {".usd", ".usda", ".usdc", ".usdz"}:
        bpy.ops.wm.usd_import(**kwargs)
    elif ext == ".ply":
        if hasattr(bpy.ops.wm, "ply_import"):
            bpy.ops.wm.ply_import(**kwargs)
        else:
            bpy.ops.import_mesh.ply(**kwargs)
    elif ext == ".abc":
        bpy.ops.wm.alembic_import(**kwargs)
    else:
        raise ValueError(f"Unsupported mesh format: {ext}")

    imported = [o for o in bpy.context.selected_objects if o not in before]
    if not imported:
        # Fallback: newly selected mesh objects
        imported = [o for o in bpy.context.selected_objects if o.type == "MESH"]
    if scale != 1.0 and ext not in {".fbx"}:
        for obj in imported:
            if not object_rna_alive(obj):
                continue
            obj.scale = (obj.scale[0] * scale, obj.scale[1] * scale, obj.scale[2] * scale)
    return imported


def maybe_apply_fab_scale(path: str, objects: List[bpy.types.Object], cfg: dict) -> None:
    if not online_identity.is_fab_or_megascans_asset(path, cfg):
        return
    mode = str((cfg.get("import") or {}).get("fab_unit_scale_mode") or "cm_to_m").lower()
    if mode == "off":
        return
    if mode == "auto":
        # Quixel glTF is already meters; FBX/USD are typically centimeters.
        ext = file_extension(path)
        if ext in {".glb", ".gltf"}:
            return
    # cm_to_m and auto (non-glTF)
    for obj in filter_alive_objects(objects):
        obj.scale = (obj.scale[0] * 0.01, obj.scale[1] * 0.01, obj.scale[2] * 0.01)


def strip_billboard_lods(
    objects: List[bpy.types.Object],
    cfg: dict,
    path: str,
) -> List[bpy.types.Object]:
    """Remove Fab/Megascans plant junk: billboard/LOD2+, collision/proxy, far LODs.

    Returns the **surviving** objects. Callers must use the return value (the
    list is also mutated in place to the same survivors) — deleted RNA refs must
    not be passed to place / material assign.
    """
    if not objects:
        return []
    if not (cfg.get("import") or {}).get("strip_megascans_billboard_lods", True):
        survivors = filter_alive_objects(objects)
        objects[:] = survivors
        return survivors
    if not online_identity.is_fab_or_megascans_asset(path, cfg):
        survivors = filter_alive_objects(objects)
        objects[:] = survivors
        return survivors

    alive = filter_alive_objects(objects)
    names: List[str] = []
    for obj in alive:
        try:
            names.append(obj.name or "")
        except ReferenceError:
            continue
    companions = find_unsuffixed_lod_companion_names(names)

    to_remove: List[bpy.types.Object] = []
    for obj in alive:
        try:
            name = obj.name or ""
        except ReferenceError:
            continue
        if should_strip_megascans_plant_object(name, companion_names=companions):
            to_remove.append(obj)
            continue
        # Clear billboard material slots on kept meshes (do not remove the object).
        try:
            data = getattr(obj, "data", None)
            if data is not None and hasattr(data, "materials"):
                for i, slot in enumerate(list(data.materials)):
                    if slot and is_megascans_billboard_material_name(slot.name):
                        data.materials[i] = None
        except ReferenceError:
            continue

    for obj in to_remove:
        try:
            bpy.data.objects.remove(obj, do_unlink=True)
        except (ReferenceError, RuntimeError):
            pass

    survivors = filter_alive_objects(alive)
    # Mutate caller's list in place so older callers that ignore the return
    # value still avoid dangling refs.
    objects[:] = survivors
    return survivors


def apply_import_transforms(objects: List[bpy.types.Object], cfg: dict) -> None:
    """Optional normalize-scale / match-size / unit-scale / pivot-to-base."""
    imp = cfg.get("import") or {}
    objects = filter_alive_objects(objects)
    unit = float(imp.get("unit_scale") or 1.0)
    if abs(unit - 1.0) > 1e-9:
        for obj in objects:
            obj.scale = (obj.scale[0] * unit, obj.scale[1] * unit, obj.scale[2] * unit)
    if imp.get("normalize_scale"):
        for obj in objects:
            if obj.type != "MESH" or obj.data is None:
                continue
            dims = obj.dimensions
            longest = max(float(dims[0]), float(dims[1]), float(dims[2]), 1e-6)
            factor = 1.0 / longest
            obj.scale = (obj.scale[0] * factor, obj.scale[1] * factor, obj.scale[2] * factor)
    if imp.get("match_size_on_import"):
        target = max(0.01, float(imp.get("match_size_target") or 1.0))
        for obj in objects:
            if obj.type != "MESH" or obj.data is None:
                continue
            dims = obj.dimensions
            longest = max(float(dims[0]), float(dims[1]), float(dims[2]), 1e-6)
            factor = target / longest
            obj.scale = (obj.scale[0] * factor, obj.scale[1] * factor, obj.scale[2] * factor)
    # Legacy pref: snap floor to Z=0 without XY centering (full origin placement is separate)
    if imp.get("pivot_to_base") and not imp.get("place_at_world_origin", True):
        for obj in objects:
            if obj.type != "MESH":
                continue
            bbox = [obj.matrix_world @ v.co for v in obj.data.vertices] if obj.data and obj.data.vertices else []
            if not bbox:
                continue
            min_z = min(v.z for v in bbox)
            obj.location.z -= min_z


def _import_roots(objects: List[bpy.types.Object]) -> List[bpy.types.Object]:
    """Top-level objects in the imported set (parents outside the set count as roots)."""
    objects = filter_alive_objects(objects)
    obj_set = set(objects)
    roots: List[bpy.types.Object] = []
    for o in objects:
        try:
            parent = o.parent
        except ReferenceError:
            roots.append(o)
            continue
        if parent is None or not object_rna_alive(parent) or parent not in obj_set:
            roots.append(o)
    return roots or list(objects)


def world_bbox_corners(objects: List[bpy.types.Object]):
    """World-space bound_box corners for mesh objects (empty list if none)."""
    from mathutils import Vector

    corners = []
    for obj in filter_alive_objects(objects):
        if getattr(obj, "type", "") != "MESH":
            continue
        try:
            for corner in obj.bound_box:
                corners.append(obj.matrix_world @ Vector(corner))
        except Exception:
            continue
    return corners


def placement_anchor(objects: List[bpy.types.Object], *, snap_base: bool = True):
    """Point used to align the import group (floor center, or first root location)."""
    from mathutils import Vector

    objects = filter_alive_objects(objects)
    corners = world_bbox_corners(objects)
    if corners:
        min_x = min(v.x for v in corners)
        max_x = max(v.x for v in corners)
        min_y = min(v.y for v in corners)
        max_y = max(v.y for v in corners)
        min_z = min(v.z for v in corners)
        max_z = max(v.z for v in corners)
        z = min_z if snap_base else (min_z + max_z) * 0.5
        return Vector(((min_x + max_x) * 0.5, (min_y + max_y) * 0.5, z))
    roots = _import_roots(objects)
    if not roots:
        return Vector((0.0, 0.0, 0.0))
    return roots[0].matrix_world.translation.copy()


def place_objects_at(
    objects: List[bpy.types.Object],
    location=(0.0, 0.0, 0.0),
    *,
    snap_base: bool = True,
) -> None:
    """Move the imported group so its anchor lands at ``location`` (world space).

    Default snap_base=True puts the lowest point on the target Z (sits on the
    ground plane at world origin for Import / Download & Import).
    """
    from mathutils import Vector

    objects = filter_alive_objects(objects)
    if not objects:
        return
    target = Vector(location)
    anchor = placement_anchor(objects, snap_base=snap_base)
    delta = target - anchor
    if abs(delta.x) < 1e-9 and abs(delta.y) < 1e-9 and abs(delta.z) < 1e-9:
        return
    for root in _import_roots(objects):
        try:
            root.matrix_world.translation = root.matrix_world.translation + delta
        except Exception:
            try:
                root.location = root.location + delta
            except Exception:
                pass


def place_at_world_origin(objects: List[bpy.types.Object], *, snap_base: bool = True) -> None:
    """Place imported meshes properly at the world origin (0, 0, 0)."""
    place_objects_at(objects, (0.0, 0.0, 0.0), snap_base=snap_base)


def import_hdri(path: str) -> Optional[bpy.types.World]:
    """Assign a World HDRI with the Node Wrangler Ctrl+T chain (built-in nodes).

    Texture Coordinate (Generated) → Mapping → Environment Texture
    → Background → World Output.

    Node Wrangler is **not** required and is never invoked.
    """
    from .hdri_world import (
        HDRI_LINKS,
        HDRI_LOCATIONS,
        HDRI_NODE_TYPES,
        pick_hdri_colorspace,
        resolve_hdri_file,
    )

    path = resolve_hdri_file(path)
    if not os.path.isfile(path):
        raise FileNotFoundError(path)

    world = bpy.data.worlds.new(os.path.splitext(os.path.basename(path))[0][:60])
    world.use_nodes = True
    tree = world.node_tree
    if tree is None:
        bpy.data.worlds.remove(world)
        raise RuntimeError("World node tree was not created")
    nodes = tree.nodes
    links = tree.links
    nodes.clear()

    created = {}
    try:
        for key, bl_idname in HDRI_NODE_TYPES.items():
            node = nodes.new(bl_idname)
            loc = HDRI_LOCATIONS.get(key) or (0.0, 0.0)
            node.location = loc
            created[key] = node

        env = created["env"]
        mapping = created["mapping"]
        bg = created["bg"]
        output = created["output"]

        try:
            mapping.vector_type = "POINT"
        except Exception:
            pass
        try:
            env.projection = "EQUIRECTANGULAR"
        except Exception:
            pass
        try:
            env.interpolation = "Linear"
        except Exception:
            pass
        try:
            bg.inputs["Strength"].default_value = 1.0
        except Exception:
            pass
        try:
            output.target = "ALL"
        except Exception:
            pass

        try:
            image = bpy.data.images.load(path, check_existing=True)
        except RuntimeError as exc:
            raise RuntimeError(f"Failed to load HDRI: {exc}") from exc
        env.image = image
        try:
            image.alpha_mode = "PREMUL"
        except Exception:
            pass
        _apply_hdri_colorspace(image)

        for src_key, src_sock, dst_key, dst_sock in HDRI_LINKS:
            src = created[src_key].outputs.get(src_sock)
            dst = created[dst_key].inputs.get(dst_sock)
            if src is None or dst is None:
                raise RuntimeError(
                    f"HDRI socket missing: {src_key}.{src_sock} → {dst_key}.{dst_sock}"
                )
            links.new(src, dst)
    except Exception:
        try:
            bpy.data.worlds.remove(world)
        except Exception:
            pass
        raise

    scene = getattr(bpy.context, "scene", None)
    if scene is not None:
        scene.world = world
    _enable_viewport_scene_world()
    return world


def _apply_hdri_colorspace(image) -> str:
    from .hdri_world import pick_hdri_colorspace

    names: List[str] = []
    try:
        prop = image.colorspace_settings.bl_rna.properties["name"]
        names = [item.identifier for item in prop.enum_items]
    except Exception:
        names = []
    chosen = pick_hdri_colorspace(names)
    try:
        image.colorspace_settings.name = chosen
    except Exception:
        for fallback in ("Linear Rec.709", "Linear", "Non-Color"):
            try:
                image.colorspace_settings.name = fallback
                return fallback
            except Exception:
                continue
    return chosen


def _enable_viewport_scene_world() -> None:
    """Show the new World in Material/Rendered 3D Views (do not change Solid)."""
    wm = getattr(bpy.context, "window_manager", None)
    if wm is None:
        return
    for window in wm.windows:
        screen = getattr(window, "screen", None)
        if screen is None:
            continue
        for area in screen.areas:
            if area.type != "VIEW_3D":
                continue
            for space in area.spaces:
                if space.type != "VIEW_3D":
                    continue
                shading = getattr(space, "shading", None)
                if shading is None:
                    continue
                if getattr(shading, "type", "") in {"MATERIAL", "RENDERED"}:
                    try:
                        shading.use_scene_world = True
                    except Exception:
                        pass
