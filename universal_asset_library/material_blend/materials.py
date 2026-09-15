# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""Build Simple Mix Shader blend materials from source + target graphs."""

from __future__ import annotations

from typing import Dict, Optional, Tuple

import bpy
from mathutils import Vector

from . import constants as C

# Props safe to copy on most shader nodes
_SKIP_PROPS = frozenset(
    {
        "rna_type",
        "type",
        "dimensions",
        "width_hidden",
        "height",
        "inputs",
        "outputs",
        "internal_links",
        "select",
        "show_options",
        "show_preview",
        "show_texture",
        "width",
        "bl_idname",
        "bl_label",
        "bl_description",
        "bl_icon",
        "bl_static_type",
        "bl_width_default",
        "bl_width_min",
        "bl_width_max",
        "bl_height_default",
        "bl_height_min",
        "bl_height_max",
        "name",
        "label",
        "parent",
        "location",
    }
)


def _nodes_bounds(nodes) -> Tuple[Vector, Vector]:
    if not nodes:
        z = Vector((0.0, 0.0))
        return z, z
    xs, ys = [], []
    for n in nodes:
        loc = n.location
        xs.append(loc.x)
        ys.append(loc.y)
    return Vector((min(xs), min(ys))), Vector((max(xs), max(ys)))


def _copy_node(src, to_nodes):
    dst = to_nodes.new(src.bl_idname)
    for prop in src.bl_rna.properties:
        ident = prop.identifier
        if ident in _SKIP_PROPS or prop.is_readonly:
            continue
        try:
            setattr(dst, ident, getattr(src, ident))
        except Exception:
            pass
    # Color ramp elements
    if src.bl_idname == "ShaderNodeValToRGB" and hasattr(src, "color_ramp"):
        try:
            src_ramp = src.color_ramp
            dst_ramp = dst.color_ramp
            while len(dst_ramp.elements) > len(src_ramp.elements):
                dst_ramp.elements.remove(dst_ramp.elements[-1])
            while len(dst_ramp.elements) < len(src_ramp.elements):
                dst_ramp.elements.new(0.5)
            for i, el in enumerate(src_ramp.elements):
                dst_ramp.elements[i].position = el.position
                dst_ramp.elements[i].color = el.color[:]
        except Exception:
            pass
    dst.location = src.location.copy()
    return dst


def _clone_source_into(
    source_tree: bpy.types.NodeTree,
    dest_tree: bpy.types.NodeTree,
    offset: Vector,
    source_obj: bpy.types.Object,
) -> Optional[bpy.types.Node]:
    """Clone source material nodes into dest; return cloned Material Output (or None)."""
    mapping: Dict[str, bpy.types.Node] = {}
    out_clone = None
    for node in source_tree.nodes:
        cloned = _copy_node(node, dest_tree.nodes)
        cloned.name = C.SOURCE_NODE_PREFIX + node.name
        cloned.label = node.label or node.name
        if node.parent is None:
            cloned.location = node.location + offset
        mapping[node.name] = cloned
        if node.bl_idname == "ShaderNodeTexCoord":
            try:
                if getattr(cloned, "object", None) is None:
                    cloned.object = source_obj
            except Exception:
                pass
        if (
            node.bl_idname == "ShaderNodeOutputMaterial"
            and getattr(node, "is_active_output", False)
            and node.inputs
            and node.inputs[0].is_linked
        ):
            out_clone = cloned
            try:
                cloned.is_active_output = False
            except Exception:
                pass
    for link in source_tree.links:
        fn = mapping.get(link.from_node.name)
        tn = mapping.get(link.to_node.name)
        if fn is None or tn is None:
            continue
        try:
            from_sock = fn.outputs.get(link.from_socket.name)
            to_sock = tn.inputs.get(link.to_socket.name)
            if from_sock is None or to_sock is None:
                fi = list(link.from_node.outputs).index(link.from_socket)
                ti = list(link.to_node.inputs).index(link.to_socket)
                from_sock = fn.outputs[fi]
                to_sock = tn.inputs[ti]
            dest_tree.links.new(from_sock, to_sock)
        except Exception:
            pass

    return out_clone


def _find_active_surface_output(nodes) -> Optional[bpy.types.Node]:
    for node in nodes:
        if node.bl_idname != "ShaderNodeOutputMaterial":
            continue
        if getattr(node, "is_active_output", True) and node.inputs and node.inputs[0].is_linked:
            return node
    for node in nodes:
        if node.bl_idname == "ShaderNodeOutputMaterial" and node.inputs and node.inputs[0].is_linked:
            return node
    return None


def _ensure_mix_shader(nodes) -> bpy.types.Node:
    existing = nodes.get(C.MIX_NODE_NAME)
    if existing is not None:
        return existing
    mix = nodes.new("ShaderNodeMixShader")
    mix.name = C.MIX_NODE_NAME
    mix.label = "UAL Blend Mix"
    return mix


def _ensure_mask_reader(nodes) -> bpy.types.Node:
    existing = nodes.get(C.MASK_NODE_NAME)
    if existing is not None:
        return existing
    # Prefer Attribute (COLOR) for modern color attributes
    try:
        node = nodes.new("ShaderNodeAttribute")
        node.attribute_name = C.MASK_ATTR
        try:
            node.attribute_type = "GEOMETRY"
        except Exception:
            pass
        node.name = C.MASK_NODE_NAME
        node.label = "UAL Blend Mask"
        return node
    except Exception:
        pass
    node = nodes.new("ShaderNodeVertexColor")
    try:
        node.layer_name = C.MASK_ATTR
    except Exception:
        pass
    node.name = C.MASK_NODE_NAME
    node.label = "UAL Blend Mask"
    return node


def _mask_fac_socket(mask_node) -> Optional[bpy.types.NodeSocket]:
    if mask_node.bl_idname == "ShaderNodeAttribute":
        # Color → Fac via Fac output if present, else Color
        if "Fac" in mask_node.outputs:
            return mask_node.outputs["Fac"]
        return mask_node.outputs.get("Color") or (
            mask_node.outputs[0] if mask_node.outputs else None
        )
    return mask_node.outputs.get("Color") or (
        mask_node.outputs[0] if mask_node.outputs else None
    )


def _clear_driver(id_data, data_path: str, index: int = -1) -> None:
    try:
        if index >= 0:
            id_data.driver_remove(data_path, index)
        else:
            id_data.driver_remove(data_path)
    except Exception:
        pass


def _drive_socket_default(
    node: bpy.types.Node,
    input_name: str,
    target_obj: bpy.types.Object,
    rna_path: str,
) -> None:
    """Driver: node.inputs[input_name].default_value ← target_obj.ual_blend.<rna_path>."""
    path = f'nodes["{node.name}"].inputs["{input_name}"].default_value'
    tree = node.id_data
    _clear_driver(tree, path)
    try:
        fcurve = tree.driver_add(path)
    except Exception:
        return
    drv = fcurve.driver
    drv.type = "SCRIPTED"
    drv.expression = "ual"
    var = drv.variables.new()
    var.name = "ual"
    var.type = "SINGLE_PROP"
    tar = var.targets[0]
    tar.id_type = "OBJECT"
    tar.id = target_obj
    tar.data_path = f"ual_blend.{rna_path}"


def push_noise_settings(target_obj: bpy.types.Object) -> None:
    """Write Noise Scale / Amount into every UAL blend material on the object (live)."""
    st = getattr(target_obj, "ual_blend", None)
    if st is None or not target_obj.material_slots:
        return
    touched = False
    for slot in target_obj.material_slots:
        mat = slot.material
        if mat is None or not mat.use_nodes:
            continue
        if not mat.name.startswith(C.MAT_NAME_PREFIX):
            continue
        tree = mat.node_tree
        noise = tree.nodes.get(C.NOISE_TEX_NAME)
        mul = tree.nodes.get(C.NOISE_MUL_NAME)
        if noise is not None:
            try:
                noise.inputs["Scale"].default_value = float(st.blend_noise_scale)
            except Exception:
                pass
        if mul is not None:
            try:
                mul.inputs[1].default_value = float(st.blend_noise_amount)
            except Exception:
                pass
        try:
            mat.update_tag()
        except Exception:
            pass
        touched = True
    if touched:
        try:
            target_obj.update_tag()
        except Exception:
            pass


def _ensure_noise_fac_chain(
    tree: bpy.types.NodeTree,
    mask_node: bpy.types.Node,
    mix: bpy.types.Node,
    target_obj: bpy.types.Object,
) -> None:
    """Wire mask Fac through optional edge noise into Mix Fac.

    Fac = clamp(mask + (noise - 0.5) * amount). Amount 0 → proximity only.
    Noise Scale ≈ Blendit Frequency; Amount ≈ Blendit Amplitude (simplified).
    """
    nodes = tree.nodes
    links = tree.links
    fac_in = mix.inputs[0]
    for link in list(fac_in.links):
        links.remove(link)

    # Object / world position so noise moves with the mesh (Blendit uses Geometry)
    geo = nodes.get(f"{C.PREFIX}_NoiseGeo")
    if geo is None:
        geo = nodes.new("ShaderNodeNewGeometry")
        geo.name = f"{C.PREFIX}_NoiseGeo"
    geo.location = (mask_node.location.x - 200.0, mask_node.location.y - 220.0)

    noise = nodes.get(C.NOISE_TEX_NAME)
    if noise is None:
        noise = nodes.new("ShaderNodeTexNoise")
        noise.name = C.NOISE_TEX_NAME
        noise.label = "UAL Edge Noise"
        try:
            noise.noise_dimensions = "3D"
        except Exception:
            pass
    noise.location = (mask_node.location.x, mask_node.location.y - 220.0)
    try:
        links.new(geo.outputs["Position"], noise.inputs["Vector"])
    except Exception:
        pass
    try:
        noise.inputs["Scale"].default_value = float(
            getattr(target_obj.ual_blend, "blend_noise_scale", 1.0) or 1.0
        )
    except Exception:
        pass

    center = nodes.get(C.NOISE_CENTER_NAME)
    if center is None:
        center = nodes.new("ShaderNodeMath")
        center.name = C.NOISE_CENTER_NAME
        center.operation = "SUBTRACT"
        center.inputs[1].default_value = 0.5
    center.location = (noise.location.x + 200.0, noise.location.y)

    mul = nodes.get(C.NOISE_MUL_NAME)
    if mul is None:
        mul = nodes.new("ShaderNodeMath")
        mul.name = C.NOISE_MUL_NAME
        mul.operation = "MULTIPLY"
    mul.location = (center.location.x + 180.0, center.location.y)
    try:
        mul.inputs[1].default_value = float(
            getattr(target_obj.ual_blend, "blend_noise_amount", 1.0) or 1.0
        )
    except Exception:
        pass

    add = nodes.get(C.NOISE_ADD_NAME)
    if add is None:
        add = nodes.new("ShaderNodeMath")
        add.name = C.NOISE_ADD_NAME
        add.operation = "ADD"
    add.location = (mul.location.x + 180.0, mask_node.location.y - 40.0)

    clamp = nodes.get(C.NOISE_CLAMP_NAME)
    if clamp is None:
        clamp = nodes.new("ShaderNodeClamp")
        clamp.name = C.NOISE_CLAMP_NAME
        clamp.inputs["Min"].default_value = 0.0
        clamp.inputs["Max"].default_value = 1.0
    clamp.location = (add.location.x + 180.0, add.location.y)

    mask_sock = _mask_fac_socket(mask_node)
    if mask_sock is None:
        return

    try:
        links.new(noise.outputs["Fac"], center.inputs[0])
    except Exception:
        links.new(noise.outputs[0], center.inputs[0])
    links.new(center.outputs["Value"], mul.inputs[0])
    links.new(mask_sock, add.inputs[0])
    links.new(mul.outputs["Value"], add.inputs[1])
    links.new(add.outputs["Value"], clamp.inputs["Value"])
    try:
        links.new(clamp.outputs["Result"], fac_in)
    except Exception:
        links.new(clamp.outputs[0], fac_in)

    _drive_socket_default(noise, "Scale", target_obj, "blend_noise_scale")
    try:
        path = f'nodes["{mul.name}"].inputs[1].default_value'
        _clear_driver(tree, path)
        fcurve = tree.driver_add(path)
        drv = fcurve.driver
        drv.type = "SCRIPTED"
        drv.expression = "ual"
        var = drv.variables.new()
        var.name = "ual"
        var.type = "SINGLE_PROP"
        tar = var.targets[0]
        tar.id_type = "OBJECT"
        tar.id = target_obj
        tar.data_path = "ual_blend.blend_noise_amount"
    except Exception:
        pass
    push_noise_settings(target_obj)


def build_simple_blend_material(
    source_obj: bpy.types.Object,
    target_obj: bpy.types.Object,
    source_mat: bpy.types.Material,
    origin_mat: bpy.types.Material,
    slot_index: int = 0,
) -> Optional[bpy.types.Material]:
    """Return a Mix-Shader material blending origin (target slot) with source.

    Reuses an existing material with the same cache name when possible.
    Source textures keep their native UV map names (sampled on the target mesh).
    """
    if source_mat is None or origin_mat is None:
        return None
    if not source_mat.use_nodes or not origin_mat.use_nodes:
        return None

    mat_name = C.blend_material_name(
        origin_mat.name, source_mat.name, target_obj.name, slot_index=slot_index
    )
    existing = bpy.data.materials.get(mat_name)
    if existing is not None and existing.users > 0:
        # Re-hook noise drivers to this target (shared name = same target)
        try:
            tree = existing.node_tree
            mask = tree.nodes.get(C.MASK_NODE_NAME)
            mix = tree.nodes.get(C.MIX_NODE_NAME)
            if mask and mix:
                _ensure_noise_fac_chain(tree, mask, mix, target_obj)
        except Exception:
            pass
        return existing

    origin_mat.use_fake_user = True
    new_mat = origin_mat.copy()
    new_mat.name = mat_name
    new_mat.use_nodes = True
    tree = new_mat.node_tree

    # Prefix origin nodes
    for node in list(tree.nodes):
        if not node.name.startswith(C.ORIGIN_NODE_PREFIX):
            node.name = C.ORIGIN_NODE_PREFIX + node.name

    origin_out = _find_active_surface_output(tree.nodes)
    o_min, o_max = _nodes_bounds(tree.nodes)
    s_min, s_max = _nodes_bounds(source_mat.node_tree.nodes)
    offset = Vector((o_max.x - s_max.x, o_min.y - s_max.y - 500.0))

    source_out = _clone_source_into(
        source_mat.node_tree, tree, offset, source_obj
    )

    mask = _ensure_mask_reader(tree.nodes)
    mask.location = Vector((o_max.x + 100.0, o_min.y - 300.0))
    mix = _ensure_mix_shader(tree.nodes)
    mix.location = Vector((o_max.x + 550.0, o_min.y - 500.0))
    out = tree.nodes.get(C.OUTPUT_NODE_NAME)
    if out is None:
        out = tree.nodes.new("ShaderNodeOutputMaterial")
        out.name = C.OUTPUT_NODE_NAME
    out.location = Vector((o_max.x + 750.0, o_min.y - 500.0))
    out.is_active_output = True
    if origin_out is not None:
        try:
            origin_out.is_active_output = False
        except Exception:
            pass
    if source_out is not None:
        try:
            source_out.is_active_output = False
        except Exception:
            pass

    # Wire shaders into Mix: 1=origin/target, 2=source; Fac via noise chain
    if origin_out is not None and origin_out.inputs[0].is_linked:
        link = origin_out.inputs[0].links[0]
        try:
            tree.links.new(link.from_socket, mix.inputs[1])
        except Exception:
            pass
    if source_out is not None and source_out.inputs[0].is_linked:
        link = source_out.inputs[0].links[0]
        try:
            tree.links.new(link.from_socket, mix.inputs[2])
        except Exception:
            pass

    _ensure_noise_fac_chain(tree, mask, mix, target_obj)

    try:
        tree.links.new(mix.outputs[0], out.inputs[0])
    except Exception:
        pass

    return new_mat


def snapshot_original_materials(obj: bpy.types.Object) -> None:
    """Store per-slot material names once (skip if already snapshotted while blended)."""
    st = obj.ual_blend
    if st.blended and len(st.original_materials) > 0:
        return
    st.original_materials.clear()
    for i, slot in enumerate(obj.material_slots):
        item = st.original_materials.add()
        mat = slot.material
        if mat is None:
            item.name = ""
        elif mat.name.startswith(C.MAT_NAME_PREFIX):
            if i == 0 and st.original_material_name:
                item.name = st.original_material_name
            else:
                item.name = ""
        else:
            item.name = mat.name
    if st.original_materials:
        st.original_material_name = st.original_materials[0].name
    elif not st.original_material_name:
        st.original_material_name = ""


def resolve_slot_origin(
    obj: bpy.types.Object,
    slot_index: int,
    slot_mat: Optional[bpy.types.Material],
) -> Optional[bpy.types.Material]:
    """Origin material for a slot — prefer snapshotted name over a live blend mat."""
    st = obj.ual_blend
    if slot_index < len(st.original_materials):
        name = st.original_materials[slot_index].name or ""
        if name:
            mat = bpy.data.materials.get(name)
            if mat is not None and not mat.name.startswith(C.MAT_NAME_PREFIX):
                return mat
    if slot_mat is not None and not slot_mat.name.startswith(C.MAT_NAME_PREFIX):
        return slot_mat
    if (
        slot_index == 0
        and slot_mat is not None
        and slot_mat.name.startswith(C.MAT_NAME_PREFIX)
    ):
        legacy = st.original_material_name or ""
        if legacy:
            return bpy.data.materials.get(legacy)
    return None


def restore_original_material(obj: bpy.types.Object) -> None:
    st = obj.ual_blend
    if not obj.material_slots:
        return
    origins = list(st.original_materials)
    if origins:
        for i, slot in enumerate(obj.material_slots):
            name = origins[i].name if i < len(origins) else ""
            mat = bpy.data.materials.get(name) if name else None
            slot.material = mat
        return
    # Legacy: restore slot 0 only
    name = getattr(st, "original_material_name", "") or ""
    mat = bpy.data.materials.get(name) if name else None
    if mat is not None:
        obj.material_slots[0].material = mat
    # Drop blend mat if orphaned later — leave for orphans purge


_SETS_COPY_PROPS = (
    "operation",
    "use_clamp",
    "blend_type",
    "data_type",
    "domain",
    "interpolation",
    "extension",
    "attribute_type",
    "attribute_name",
    "noise_dimensions",
)


def _find_active_output(tree) -> Optional[bpy.types.Node]:
    outs = [nd for nd in tree.nodes if nd.type == "OUTPUT_MATERIAL"]
    if not outs:
        return None
    for o in outs:
        if getattr(o, "is_active_output", False):
            return o
    return outs[0]


def _copy_donor_surface(src_tree, dst_tree, prefix: str):
    """Copy donor shader subgraph into dst; return surface output socket or None."""
    src_out = _find_active_output(src_tree)
    if not src_out or not src_out.inputs["Surface"].links:
        return None
    surf_node = src_out.inputs["Surface"].links[0].from_node
    surf_sock = src_out.inputs["Surface"].links[0].from_socket.name
    # If donor already has Sets mix, take its base branch only
    if surf_node.name == C.SETS_MIX_NAME:
        base = surf_node.inputs[1].links
        if base:
            surf_node = base[0].from_node
            surf_sock = base[0].from_socket.name

    def _skip(nd) -> bool:
        return (
            nd.type == "OUTPUT_MATERIAL"
            or nd.name == C.SETS_MIX_NAME
            or nd.name == C.SETS_ATTR_NODE
            or nd.name.startswith(prefix)
            or nd.name.startswith(C.SETS_SRC_PREFIX)
        )

    old_to_new = {}
    for src in src_tree.nodes:
        if _skip(src):
            continue
        new = dst_tree.nodes.new(src.bl_idname)
        new.name = prefix + src.name
        new.label = src.label
        new.location = (src.location.x - 1400, src.location.y - 900)
        for p in _SETS_COPY_PROPS:
            if hasattr(src, p) and hasattr(new, p):
                try:
                    setattr(new, p, getattr(src, p))
                except Exception:
                    pass
        if src.type == "TEX_IMAGE":
            try:
                new.image = src.image
            except Exception:
                pass
        for i, sin in enumerate(src.inputs):
            try:
                new.inputs[i].default_value = sin.default_value
            except Exception:
                pass
        old_to_new[src] = new

    for link in src_tree.links:
        fn, tn = link.from_node, link.to_node
        if fn in old_to_new and tn in old_to_new:
            try:
                dst_tree.links.new(
                    old_to_new[fn].outputs[link.from_socket.name],
                    old_to_new[tn].inputs[link.to_socket.name],
                )
            except Exception:
                pass

    if surf_node not in old_to_new:
        return None
    return old_to_new[surf_node].outputs[surf_sock]


def ensure_sets_shader_blend(
    mat: bpy.types.Material,
    donor_mat: bpy.types.Material,
    attr_name: str = C.SETS_MASK_ATTR,
) -> bool:
    """In-place Mix Shader on receiver material (Sets mode)."""
    if mat is None or donor_mat is None or mat == donor_mat:
        return False
    if not mat.use_nodes:
        mat.use_nodes = True
    rt = mat.node_tree
    if rt.nodes.get(C.SETS_MIX_NAME):
        a = rt.nodes.get(C.SETS_ATTR_NODE)
        if a is not None:
            try:
                a.attribute_name = attr_name
            except Exception:
                pass
        mat["ual_sets_blend"] = True
        return True

    out = _find_active_output(rt)
    if out is None:
        return False

    donor_socket = _copy_donor_surface(donor_mat.node_tree, rt, C.SETS_SRC_PREFIX)
    if donor_socket is None:
        return False

    attr = rt.nodes.new("ShaderNodeAttribute")
    attr.name = C.SETS_ATTR_NODE
    try:
        attr.attribute_type = "GEOMETRY"
    except Exception:
        pass
    attr.attribute_name = attr_name
    attr.location = (out.location.x - 350, out.location.y + 300)

    mix = rt.nodes.new("ShaderNodeMixShader")
    mix.name = C.SETS_MIX_NAME
    mix.location = (out.location.x - 50, out.location.y + 120)

    surf_links = out.inputs["Surface"].links
    if surf_links:
        rt.links.new(surf_links[0].from_socket, mix.inputs[1])
    rt.links.new(donor_socket, mix.inputs[2])
    try:
        rt.links.new(attr.outputs["Fac"], mix.inputs["Fac"])
    except Exception:
        rt.links.new(attr.outputs[0], mix.inputs[0])
    out.location = (out.location.x + 250, out.location.y)
    rt.links.new(mix.outputs["Shader"], out.inputs["Surface"])
    mat["ual_sets_blend"] = True
    return True


def remove_sets_shader_blend(mat: bpy.types.Material) -> bool:
    """Remove Sets Mix / attr / donor copies. Returns True if changed."""
    if mat is None or not mat.use_nodes:
        return False
    rt = mat.node_tree
    mix = rt.nodes.get(C.SETS_MIX_NAME)
    out = _find_active_output(rt)
    changed = False
    if mix and out:
        orig_links = mix.inputs[1].links
        if orig_links:
            try:
                rt.links.new(orig_links[0].from_socket, out.inputs["Surface"])
                changed = True
            except Exception:
                pass
    for nd in list(rt.nodes):
        if (
            nd.name == C.SETS_MIX_NAME
            or nd.name == C.SETS_ATTR_NODE
            or nd.name.startswith(C.SETS_SRC_PREFIX)
        ):
            rt.nodes.remove(nd)
            changed = True
    if "ual_sets_blend" in mat:
        del mat["ual_sets_blend"]
        changed = True
    return changed
