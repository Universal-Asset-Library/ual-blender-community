# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""Build Principled BSDF materials from PBR texture sets.

Wiring follows Blender 4.x Principled BSDF + Color Management docs:
https://docs.blender.org/manual/en/4.2/render/shader_nodes/shader/principled.html
https://docs.blender.org/manual/en/4.2/render/color_management.html

BlenderKit appends pre-built ``.blend`` shaders; UAL builds Principled from
folder maps. Assignment / UV automap / drop-slot mirror BlenderKit behavior
without copying GPL code.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

import bpy

from .principled_plan import PRINCIPLED_CHANNEL_PLAN, principled_wire_plan  # noqa: F401
from .texture_match import packed_channel_letter, prepare_import_texture_set

# Re-export plan helpers for callers that imported from material_builder
__all_plan__ = ("PRINCIPLED_CHANNEL_PLAN", "principled_wire_plan")


def _set_image_colorspace(img: bpy.types.Image, colorspace: str) -> None:
    """sRGB for Base Color / Emission; Non-Color + is_data for data maps (Blender docs)."""
    name = colorspace or "sRGB"
    try:
        settings = getattr(img, "colorspace_settings", None)
        if settings is None:
            return
        if name.lower() in {"non-color", "noncolor", "raw", "linear", "data"}:
            # BlenderKit image_utils: Non-Color → is_data=True
            if hasattr(settings, "is_data"):
                settings.is_data = True
            else:
                settings.name = "Non-Color"
            if hasattr(img, "is_data"):
                img.is_data = True
        else:
            if hasattr(settings, "is_data"):
                settings.is_data = False
            settings.name = "sRGB"
            if hasattr(img, "is_data"):
                img.is_data = False
    except Exception:
        try:
            img.colorspace_settings.name = "Non-Color" if "non" in name.lower() else "sRGB"
        except Exception:
            pass


def _load_image(path: str, *, colorspace: str = "sRGB") -> Optional[bpy.types.Image]:
    if not path or not os.path.isfile(path):
        return None
    abs_path = os.path.abspath(path)
    for img in bpy.data.images:
        try:
            fp = img.filepath_from_user() if hasattr(img, "filepath_from_user") else img.filepath
        except Exception:
            fp = img.filepath
        if os.path.normcase(os.path.normpath(fp or "")) == os.path.normcase(abs_path):
            _set_image_colorspace(img, colorspace)
            return img
    try:
        img = bpy.data.images.load(abs_path, check_existing=True)
    except RuntimeError:
        return None
    _set_image_colorspace(img, colorspace)
    return img


def _path_key(path: str) -> str:
    return os.path.normcase(os.path.normpath(path or ""))


def _sep_output_name(letter: str) -> str:
    return {"r": "Red", "g": "Green", "b": "Blue"}.get((letter or "r").lower(), "Red")


def _mix_color_sockets(mix):
    """Resolve Mix / MixRGB input/output sockets across Blender versions."""
    fac = mix.inputs.get("Factor") or mix.inputs.get("Fac") or mix.inputs[0]
    a = mix.inputs.get("A") or mix.inputs.get("Color1") or mix.inputs[1]
    b = mix.inputs.get("B") or mix.inputs.get("Color2") or mix.inputs[2]
    out = mix.outputs.get("Result") or mix.outputs.get("Color") or mix.outputs[0]
    return fac, a, b, out


def _ensure_mix_multiply(nodes, location=(-200, 200)):
    try:
        mix = nodes.new("ShaderNodeMix")
        mix.data_type = "RGBA"
        mix.blend_type = "MULTIPLY"
        mix.location = location
        fac, _, _, _ = _mix_color_sockets(mix)
        fac.default_value = 1.0
        return mix
    except Exception:
        mix = nodes.new("ShaderNodeMixRGB")
        mix.blend_type = "MULTIPLY"
        mix.location = location
        fac, _, _, _ = _mix_color_sockets(mix)
        fac.default_value = 1.0
        return mix


def _scalar_from_tex(nodes, links, tex, letter: Optional[str], location=(0, 0)):
    """Float socket source: packed channel or grayscale from Color."""
    if letter:
        sep = nodes.new("ShaderNodeSeparateColor")
        sep.location = location
        links.new(tex.outputs["Color"], sep.inputs["Color"])
        return sep.outputs[_sep_output_name(letter)]
    # Grayscale data map → use Red (same as G/B for typical grey maps)
    sep = nodes.new("ShaderNodeSeparateColor")
    sep.location = location
    links.new(tex.outputs["Color"], sep.inputs["Color"])
    return sep.outputs["Red"]


def _link_to_bsdf(links, socket_from, bsdf, socket_name: str) -> bool:
    if socket_name not in bsdf.inputs:
        return False
    links.new(socket_from, bsdf.inputs[socket_name])
    return True


def _wire_normal(nodes, links, bsdf, tex, *, normal_map_space: str = "OPENGL", location=(0, 0)) -> None:
    if "Normal" not in bsdf.inputs:
        return
    normal_map = nodes.new("ShaderNodeNormalMap")
    normal_map.location = location
    space = str(normal_map_space or "OPENGL").upper()
    if space == "DIRECTX":
        sep = nodes.new("ShaderNodeSeparateColor")
        comb = nodes.new("ShaderNodeCombineColor")
        inv = nodes.new("ShaderNodeMath")
        inv.operation = "SUBTRACT"
        inv.inputs[0].default_value = 1.0
        links.new(tex.outputs["Color"], sep.inputs["Color"])
        links.new(sep.outputs["Red"], comb.inputs["Red"])
        links.new(sep.outputs["Green"], inv.inputs[1])
        links.new(inv.outputs["Value"], comb.inputs["Green"])
        links.new(sep.outputs["Blue"], comb.inputs["Blue"])
        links.new(comb.outputs["Color"], normal_map.inputs["Color"])
    else:
        links.new(tex.outputs["Color"], normal_map.inputs["Color"])
    links.new(normal_map.outputs["Normal"], bsdf.inputs["Normal"])


def _wire_opacity(nodes, links, mat, bsdf, tex) -> None:
    """Opacity → Principled Alpha (Blender docs: usually Image Texture Alpha)."""
    if "Alpha" not in bsdf.inputs:
        return
    # Prefer Alpha output when present (RGBA masks); grayscale maps → Red (float).
    channels = int(getattr(getattr(tex, "image", None), "channels", 0) or 0)
    if "Alpha" in tex.outputs and channels == 4:
        links.new(tex.outputs["Alpha"], bsdf.inputs["Alpha"])
    elif "Color" in tex.outputs:
        sep = nodes.new("ShaderNodeSeparateColor")
        sep.location = (tex.location[0] + 200, tex.location[1])
        links.new(tex.outputs["Color"], sep.inputs["Color"])
        links.new(sep.outputs["Red"], bsdf.inputs["Alpha"])
    elif "Alpha" in tex.outputs:
        links.new(tex.outputs["Alpha"], bsdf.inputs["Alpha"])
    try:
        mat.blend_method = "HASHED"
    except Exception:
        pass
    try:
        if hasattr(mat, "surface_render_method"):
            mat.surface_render_method = "DITHERED"
    except Exception:
        pass


def build_principled_material(
    name: str,
    texture_set: Dict[str, str],
    *,
    assign_to=None,
    target_slot: int = 0,
    cfg: Optional[dict] = None,
) -> bpy.types.Material:
    """Create a Principled BSDF material with all detected PBR maps wired correctly.

    One Image Texture node per unique file (ORM/ARM share a single node).
    Base Color always gets albedo Color (sRGB); AO multiplies into Base Color
    (standard PBR / Blender tutorial practice — avoids relying on optional AO socket).
    """
    mat = bpy.data.materials.new(name=name[:60] or "UAL_Material")
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    nodes.clear()
    out = nodes.new("ShaderNodeOutputMaterial")
    bsdf = nodes.new("ShaderNodeBsdfPrincipled")
    out.location = (420, 0)
    bsdf.location = (120, 0)
    links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])

    expand = dict(texture_set or {})
    from .texture_match import expand_packed_maps_in_texture_set

    expand_packed_maps_in_texture_set(expand)
    normal_space = str(((cfg or {}).get("import") or {}).get("normal_map_space") or "OPENGL")

    # One Image Texture per filepath (shared ORM)
    tex_by_path: Dict[str, Any] = {}
    y = 360

    def tex_for(path: str, colorspace: str, label: str):
        nonlocal y
        key = _path_key(path)
        if key in tex_by_path:
            # Ensure colorspace matches strongest requirement (Non-Color wins if mixed)
            existing = tex_by_path[key]
            if colorspace == "Non-Color" and existing.image:
                _set_image_colorspace(existing.image, "Non-Color")
            return existing
        img = _load_image(path, colorspace=colorspace)
        if img is None:
            return None
        tex = nodes.new("ShaderNodeTexImage")
        tex.image = img
        tex.label = label
        tex.name = f"UAL_{label}"[:60]
        tex.location = (-520, y)
        y -= 300
        tex_by_path[key] = tex
        return tex

    albedo_tex = None
    ao_tex = None
    ao_letter = None

    # --- 1. Base Color (albedo) — sRGB Color → Base Color ---
    if expand.get("albedo"):
        albedo_tex = tex_for(expand["albedo"], "sRGB", "albedo")
        if albedo_tex is not None and "Base Color" in bsdf.inputs:
            links.new(albedo_tex.outputs["Color"], bsdf.inputs["Base Color"])
            # Cutout alpha baked into albedo when no separate opacity map
            if not expand.get("opacity") and "Alpha" in albedo_tex.outputs and "Alpha" in bsdf.inputs:
                if getattr(albedo_tex.image, "channels", 0) == 4:
                    links.new(albedo_tex.outputs["Alpha"], bsdf.inputs["Alpha"])
                    try:
                        mat.blend_method = "HASHED"
                    except Exception:
                        pass

    # --- 2. Roughness / Metallic / Transmission / Sheen (ORM may share one file) ---
    # translucency fills Transmission only when no dedicated transmission map
    if expand.get("translucency") and not expand.get("transmission"):
        expand["transmission"] = expand["translucency"]
    for channel, socket in (
        ("roughness", "Roughness"),
        ("metalness", "Metallic"),
        ("transmission", "Transmission Weight"),
        ("fuzz", "Sheen Weight"),
    ):
        path = expand.get(channel) or ""
        if not path or socket not in bsdf.inputs:
            continue
        tex = tex_for(path, "Non-Color", channel)
        if tex is None:
            continue
        letter = packed_channel_letter(expand, channel, path)
        src = _scalar_from_tex(nodes, links, tex, letter, location=(-260, tex.location[1]))
        _link_to_bsdf(links, src, bsdf, socket)
        if channel == "fuzz" and "Sheen Roughness" in bsdf.inputs:
            # Soft cloth default when only a weight map is present
            if not bsdf.inputs["Sheen Roughness"].is_linked:
                bsdf.inputs["Sheen Roughness"].default_value = 0.5

    # --- 3. Specular ---
    if expand.get("specular"):
        for socket in ("Specular IOR Level", "Specular"):
            if socket not in bsdf.inputs:
                continue
            tex = tex_for(expand["specular"], "Non-Color", "specular")
            if tex is None:
                break
            letter = packed_channel_letter(expand, "specular", expand["specular"])
            src = _scalar_from_tex(nodes, links, tex, letter, location=(-260, tex.location[1] - 40))
            _link_to_bsdf(links, src, bsdf, socket)
            break

    # --- 4. Normal (Non-Color → Normal Map → Normal) ---
    if expand.get("normal"):
        tex = tex_for(expand["normal"], "Non-Color", "normal")
        if tex is not None:
            _wire_normal(
                nodes,
                links,
                bsdf,
                tex,
                normal_map_space=normal_space,
                location=(-200, tex.location[1]),
            )

    # --- 5. AO → multiply into Base Color (do NOT also use AO socket — avoids double darkening) ---
    if expand.get("ao") and albedo_tex is not None and "Base Color" in bsdf.inputs:
        ao_tex = tex_for(expand["ao"], "Non-Color", "ao")
        if ao_tex is not None:
            ao_letter = packed_channel_letter(expand, "ao", expand["ao"])
            for link in list(bsdf.inputs["Base Color"].links):
                links.remove(link)
            mix = _ensure_mix_multiply(nodes, location=(-80, 200))
            _, sock_a, sock_b, sock_out = _mix_color_sockets(mix)
            links.new(albedo_tex.outputs["Color"], sock_a)
            if ao_letter:
                sep = nodes.new("ShaderNodeSeparateColor")
                comb = nodes.new("ShaderNodeCombineColor")
                links.new(ao_tex.outputs["Color"], sep.inputs["Color"])
                key = _sep_output_name(ao_letter)
                links.new(sep.outputs[key], comb.inputs["Red"])
                links.new(sep.outputs[key], comb.inputs["Green"])
                links.new(sep.outputs[key], comb.inputs["Blue"])
                links.new(comb.outputs["Color"], sock_b)
            else:
                links.new(ao_tex.outputs["Color"], sock_b)
            links.new(sock_out, bsdf.inputs["Base Color"])

    # --- 6. Opacity ---
    if expand.get("opacity"):
        tex = tex_for(expand["opacity"], "Non-Color", "opacity")
        if tex is not None:
            _wire_opacity(nodes, links, mat, bsdf, tex)

    # --- 7. Emission ---
    if expand.get("emissive"):
        tex = tex_for(expand["emissive"], "sRGB", "emissive")
        if tex is not None:
            if "Emission Color" in bsdf.inputs:
                links.new(tex.outputs["Color"], bsdf.inputs["Emission Color"])
            elif "Emission" in bsdf.inputs:
                links.new(tex.outputs["Color"], bsdf.inputs["Emission"])
            if "Emission Strength" in bsdf.inputs and not bsdf.inputs["Emission Strength"].is_linked:
                bsdf.inputs["Emission Strength"].default_value = max(
                    float(bsdf.inputs["Emission Strength"].default_value or 0.0), 1.0
                )

    # --- 8. Displacement → Material Output (not Principled) ---
    if expand.get("displacement") and "Displacement" in out.inputs:
        path = expand["displacement"]
        tex = tex_for(path, "Non-Color", "displacement")
        if tex is not None:
            disp = nodes.new("ShaderNodeDisplacement")
            disp.location = (120, -320)
            letter = packed_channel_letter(expand, "displacement", path)
            if letter:
                sep = nodes.new("ShaderNodeSeparateColor")
                links.new(tex.outputs["Color"], sep.inputs["Color"])
                links.new(sep.outputs[_sep_output_name(letter)], disp.inputs["Height"])
            else:
                links.new(tex.outputs["Color"], disp.inputs["Height"])
            if "Scale" in disp.inputs:
                disp.inputs["Scale"].default_value = 0.05
            if "Midlevel" in disp.inputs:
                disp.inputs["Midlevel"].default_value = 0.5
            links.new(disp.outputs["Displacement"], out.inputs["Displacement"])
            try:
                mat.displacement_method = "BOTH"
            except Exception:
                pass

    if assign_to is not None:
        if isinstance(assign_to, (list, tuple)):
            assign_material_to_objects(assign_to, mat, cfg=cfg, target_slot=target_slot)
        else:
            assign_material(assign_to, mat, cfg=cfg, target_slot=target_slot)
    return mat


def material_slot_from_face(obj, face_index: Optional[int]) -> int:
    """BlenderKit-style: evaluated polygon.material_index → object slot."""
    if obj is None or face_index is None or face_index < 0:
        return 0
    try:
        depsgraph = bpy.context.evaluated_depsgraph_get()
        eval_obj = obj.evaluated_get(depsgraph)
        mesh = eval_obj.to_mesh()
        try:
            if mesh and 0 <= face_index < len(mesh.polygons):
                return int(mesh.polygons[face_index].material_index)
        finally:
            eval_obj.to_mesh_clear()
    except Exception:
        pass
    try:
        mesh = getattr(obj, "data", None)
        if mesh and hasattr(mesh, "polygons") and 0 <= face_index < len(mesh.polygons):
            return int(mesh.polygons[face_index].material_index)
    except Exception:
        pass
    return 0


def assign_material(
    obj,
    mat: bpy.types.Material,
    *,
    cfg: Optional[dict] = None,
    target_slot: int = 0,
    all_slots: bool = False,
) -> None:
    if obj is None or not hasattr(obj, "data"):
        return
    data = obj.data
    if not hasattr(data, "materials"):
        return
    ensure_object_uvs(obj, cfg=cfg)
    if all_slots:
        if not data.materials:
            data.materials.append(mat)
        else:
            for index in range(len(data.materials)):
                data.materials[index] = mat
        try:
            if hasattr(obj, "active_material_index"):
                obj.active_material_index = 0
        except Exception:
            pass
        return
    slot = max(0, int(target_slot or 0))
    while len(data.materials) <= slot:
        data.materials.append(None)
    data.materials[slot] = mat
    try:
        if hasattr(obj, "active_material_index"):
            obj.active_material_index = slot
    except Exception:
        pass


def assign_material_to_objects(
    objects,
    mat: bpy.types.Material,
    *,
    cfg: Optional[dict] = None,
    target_slot: int = 0,
    all_slots: bool = False,
) -> None:
    from .megascans_billboard import filter_alive_objects, object_rna_alive

    for obj in filter_alive_objects(objects):
        if not object_rna_alive(obj):
            continue
        try:
            otype = getattr(obj, "type", "")
            has_mats = hasattr(getattr(obj, "data", None), "materials")
        except ReferenceError:
            continue
        if otype not in {"MESH", "CURVE", "SURFACE", "META", "FONT"} and not has_mats:
            continue
        try:
            assign_material(obj, mat, cfg=cfg, target_slot=target_slot, all_slots=all_slots)
        except ReferenceError:
            continue


def ensure_object_uvs(obj, *, cfg: Optional[dict] = None) -> None:
    """Create UVs when missing (``import.material_import_automap``)."""
    if obj is None or getattr(obj, "type", "") != "MESH":
        return
    if not bool((cfg or {}).get("import", {}).get("material_import_automap", True)):
        return
    mesh = getattr(obj, "data", None)
    if mesh is None or not hasattr(mesh, "uv_layers"):
        return
    if mesh.uv_layers and len(mesh.uv_layers) > 0:
        return
    try:
        prev_active = bpy.context.view_layer.objects.active
        prev_mode = getattr(obj, "mode", "OBJECT")
        bpy.ops.object.select_all(action="DESELECT")
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj
        if not mesh.uv_layers:
            mesh.uv_layers.new(name="automap")
        mesh.uv_layers.active = mesh.uv_layers[-1]
        bpy.ops.object.mode_set(mode="EDIT")
        bpy.ops.mesh.select_all(action="SELECT")
        try:
            bpy.ops.uv.smart_project(angle_limit=66.0, island_margin=0.02)
        except Exception:
            try:
                longest = max(float(d) for d in obj.dimensions) or 1.0
                bpy.ops.uv.cube_project(cube_size=max(longest, 0.01), correct_aspect=False)
            except Exception:
                pass
        bpy.ops.object.mode_set(mode="OBJECT")
        if prev_active is not None:
            bpy.context.view_layer.objects.active = prev_active
        if prev_mode and prev_mode != "OBJECT":
            try:
                bpy.ops.object.mode_set(mode=prev_mode)
            except Exception:
                pass
    except Exception:
        try:
            bpy.ops.object.mode_set(mode="OBJECT")
        except Exception:
            pass


def build_from_folder(
    folder: str,
    *,
    assign_to=None,
    target_slot: int = 0,
    profile: Optional[str] = None,
    cfg: Optional[dict] = None,
) -> bpy.types.Material:
    if profile is None and cfg:
        profile = str((cfg.get("import") or {}).get("texture_naming_profile") or "auto")
        if profile == "auto":
            profile = None
    normal_space = str(((cfg or {}).get("import") or {}).get("normal_map_space") or "OPENGL")
    tex_set, _audit = prepare_import_texture_set(
        folder,
        profile=profile,
        normal_map_space=normal_space,
    )
    name = os.path.basename(folder.rstrip("\\/")) or "UAL_Material"
    return build_principled_material(
        name,
        tex_set,
        assign_to=assign_to,
        target_slot=target_slot,
        cfg=cfg,
    )
