# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""Sets-mode Geometry Nodes proximity mask (UAL rewrite of MK-style GN).

Builds a shared node group that stores a POINT float attribute from world-space
proximity to Source geometry, with Strength / Spread / Noise live inputs.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

import bpy

from . import constants as C


def _set_mod_input(mod: bpy.types.Modifier, sock_name: str, value: float) -> None:
    ng = getattr(mod, "node_group", None)
    if not ng:
        return
    itf = getattr(ng, "interface", None)
    if itf is None:
        return
    for item in itf.items_tree:
        if getattr(item, "item_type", "") != "SOCKET":
            continue
        if item.in_out != "INPUT" or item.name != sock_name:
            continue
        try:
            mod[item.identifier] = value
        except Exception:
            pass
        return


def _refresh(obj: bpy.types.Object) -> None:
    try:
        obj.update_tag()
        if obj.data:
            obj.data.update_tag()
    except Exception:
        pass
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


def _mod_stores_attr(mod: bpy.types.Modifier, attr_name: str) -> bool:
    if mod.type != "NODES" or not mod.node_group:
        return False
    if not mod.node_group.name.startswith(C.NG_NAME):
        return False
    for nd in mod.node_group.nodes:
        if nd.bl_idname == "GeometryNodeStoreNamedAttribute":
            try:
                return nd.inputs["Name"].default_value == attr_name
            except Exception:
                return False
    return False


def has_sets_modifier(obj: bpy.types.Object) -> bool:
    return any(
        m.type == "NODES" and m.node_group and m.node_group.name.startswith(C.NG_NAME)
        for m in obj.modifiers
    )


def push_live_settings(context) -> None:
    """Push Scene live floats onto selected Sets-blended receivers only."""
    scene = context.scene
    settings = {
        "strength": float(getattr(scene, "ual_blend_live_strength", 1.0) or 1.0),
        "spread": float(getattr(scene, "ual_blend_live_spread", 0.12) or 0.12),
        "noise_scale": float(getattr(scene, "ual_blend_live_noise_scale", 6.0) or 6.0),
        "noise_amount": float(getattr(scene, "ual_blend_live_noise_amount", 0.15) or 0.15),
    }
    settings["strength"], settings["spread"], settings["noise_scale"], settings[
        "noise_amount"
    ] = C.clamp_live(
        settings["strength"],
        settings["spread"],
        settings["noise_scale"],
        settings["noise_amount"],
    )
    for obj in getattr(context, "selected_objects", ()) or ():
        if obj is None or getattr(obj, "type", "") != "MESH":
            continue
        touched = False
        for mod in obj.modifiers:
            if _mod_stores_attr(mod, C.SETS_MASK_ATTR):
                _set_mod_input(mod, "Spread", settings["spread"])
                _set_mod_input(mod, "Strength", settings["strength"])
                _set_mod_input(mod, "Noise Scale", settings["noise_scale"])
                _set_mod_input(mod, "Noise Amount", settings["noise_amount"])
                touched = True
        if touched:
            _refresh(obj)


def build_ground_node_group(
    source_coll: bpy.types.Collection,
    attr_name: str = C.SETS_MASK_ATTR,
) -> bpy.types.NodeTree:
    """Create a new Geometry Nodes group bound to ``source_coll``."""
    ng = bpy.data.node_groups.new(C.NG_NAME, "GeometryNodeTree")
    itf = ng.interface
    itf.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    s_spread = itf.new_socket("Spread", in_out="INPUT", socket_type="NodeSocketFloat")
    s_spread.default_value = 0.12
    s_spread.min_value = 0.0
    s_spread.max_value = 10.0
    s_str = itf.new_socket("Strength", in_out="INPUT", socket_type="NodeSocketFloat")
    s_str.default_value = 1.0
    s_str.min_value = 0.0
    s_str.max_value = 1.0
    s_nsc = itf.new_socket("Noise Scale", in_out="INPUT", socket_type="NodeSocketFloat")
    s_nsc.default_value = 6.0
    s_nsc.min_value = 0.0
    s_nam = itf.new_socket("Noise Amount", in_out="INPUT", socket_type="NodeSocketFloat")
    s_nam.default_value = 0.15
    s_nam.min_value = 0.0
    itf.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")

    n = ng.nodes
    L = ng.links
    gin = n.new("NodeGroupInput")
    gin.location = (-1300, 0)
    gout = n.new("NodeGroupOutput")
    gout.location = (600, 0)

    ci = n.new("GeometryNodeCollectionInfo")
    ci.location = (-1300, -320)
    ci.transform_space = "ORIGINAL"
    ci.inputs["Collection"].default_value = source_coll
    try:
        ci.inputs["Separate Children"].default_value = False
        ci.inputs["Reset Children"].default_value = False
    except Exception:
        pass

    real = n.new("GeometryNodeRealizeInstances")
    real.location = (-1080, -320)

    selfo = n.new("GeometryNodeSelfObject")
    selfo.location = (-1300, -480)
    selfoi = n.new("GeometryNodeObjectInfo")
    selfoi.location = (-1100, -480)
    selfoi.transform_space = "ORIGINAL"
    wpos = n.new("FunctionNodeTransformPoint")
    wpos.location = (-900, -480)

    prox = n.new("GeometryNodeProximity")
    prox.location = (-680, -260)
    prox.target_element = "FACES"

    pos = n.new("GeometryNodeInputPosition")
    pos.location = (-1300, -720)
    noise = n.new("ShaderNodeTexNoise")
    noise.location = (-880, -620)
    namt_neg = n.new("ShaderNodeMath")
    namt_neg.location = (-880, -820)
    namt_neg.operation = "MULTIPLY"
    namt_neg.inputs[1].default_value = -1.0
    nrange = n.new("ShaderNodeMapRange")
    nrange.location = (-660, -620)
    nrange.inputs["From Min"].default_value = 0.25
    nrange.inputs["From Max"].default_value = 0.75
    nrange.clamp = True

    add = n.new("ShaderNodeMath")
    add.location = (-440, -300)
    add.operation = "ADD"

    mr = n.new("ShaderNodeMapRange")
    mr.location = (-220, -180)
    mr.inputs["To Min"].default_value = 1.0
    mr.inputs["To Max"].default_value = 0.0
    mr.clamp = True

    mul = n.new("ShaderNodeMath")
    mul.location = (60, -120)
    mul.operation = "MULTIPLY"
    mul.use_clamp = True

    store = n.new("GeometryNodeStoreNamedAttribute")
    store.location = (320, 0)
    store.data_type = "FLOAT"
    store.domain = "POINT"
    store.inputs["Name"].default_value = attr_name

    L.new(gin.outputs["Geometry"], store.inputs["Geometry"])
    L.new(ci.outputs["Instances"], real.inputs["Geometry"])
    L.new(real.outputs["Geometry"], prox.inputs["Geometry"])

    L.new(selfo.outputs["Self Object"], selfoi.inputs["Object"])
    L.new(pos.outputs["Position"], wpos.inputs["Vector"])
    L.new(selfoi.outputs["Transform"], wpos.inputs["Transform"])
    L.new(wpos.outputs["Vector"], prox.inputs["Sample Position"])

    L.new(pos.outputs["Position"], noise.inputs["Vector"])
    L.new(gin.outputs["Noise Scale"], noise.inputs["Scale"])
    L.new(gin.outputs["Noise Amount"], nrange.inputs["To Max"])
    L.new(gin.outputs["Noise Amount"], namt_neg.inputs[0])
    L.new(namt_neg.outputs["Value"], nrange.inputs["To Min"])
    L.new(noise.outputs["Fac"], nrange.inputs["Value"])

    L.new(prox.outputs["Distance"], add.inputs[0])
    L.new(nrange.outputs["Result"], add.inputs[1])
    L.new(add.outputs["Value"], mr.inputs["Value"])
    L.new(gin.outputs["Spread"], mr.inputs["From Max"])

    L.new(mr.outputs["Result"], mul.inputs[0])
    L.new(gin.outputs["Strength"], mul.inputs[1])

    val_socket = next(s for s in store.inputs if s.name == "Value" and s.enabled)
    L.new(mul.outputs["Value"], val_socket)
    L.new(store.outputs["Geometry"], gout.inputs["Geometry"])
    return ng


def apply_sets_blend(
    receivers: Iterable[bpy.types.Object],
    donor_objs: List[bpy.types.Object],
    settings: Dict[str, float],
) -> int:
    """Attach GN mods + return how many receivers got a modifier."""
    from . import materials as mat_mod

    donor_mat = None
    for d in donor_objs:
        mat = getattr(d, "active_material", None) or (
            d.material_slots[0].material if d.material_slots else None
        )
        if mat is not None and mat.use_nodes:
            donor_mat = mat
            break
    if donor_mat is None:
        return 0

    coll = bpy.data.collections.new(C.SETS_SRC_COLL)
    for o in donor_objs:
        try:
            coll.objects.link(o)
        except Exception:
            pass
    ng = build_ground_node_group(coll, C.SETS_MASK_ATTR)
    strength, spread, nsc, nam = C.clamp_live(
        settings.get("strength", 1.0),
        settings.get("spread", 0.12),
        settings.get("noise_scale", 6.0),
        settings.get("noise_amount", 0.15),
    )
    cnt = 0
    donor_set = set(donor_objs)
    for obj in receivers:
        if obj in donor_set:
            continue
        mod = None
        for m in obj.modifiers:
            if m.type == "NODES" and m.node_group and m.node_group.name.startswith(C.NG_NAME):
                mod = m
                break
        if mod is None:
            mod = obj.modifiers.new(C.MOD_GN, "NODES")
        mod.node_group = ng
        _set_mod_input(mod, "Spread", spread)
        _set_mod_input(mod, "Strength", strength)
        _set_mod_input(mod, "Noise Scale", nsc)
        _set_mod_input(mod, "Noise Amount", nam)

        if not obj.material_slots:
            nm = bpy.data.materials.new(f"UAL_Blend_Base_{obj.name}")
            nm.use_nodes = True
            obj.data.materials.append(nm)

        ok_mat = False
        for slot in obj.material_slots:
            if slot.material and mat_mod.ensure_sets_shader_blend(
                slot.material, donor_mat, C.SETS_MASK_ATTR
            ):
                ok_mat = True
        if ok_mat:
            obj.ual_blend.sets_blended = True
            obj.ual_blend.blended = True
            cnt += 1
        else:
            obj.modifiers.remove(mod)
        _refresh(obj)
    return cnt

def _material_used_by_other_sets(mat: bpy.types.Material, skip: bpy.types.Object) -> bool:
    if mat is None:
        return False
    for o in bpy.data.objects:
        if o is skip or getattr(o, "type", "") != "MESH":
            continue
        if not getattr(getattr(o, "ual_blend", None), "sets_blended", False):
            continue
        for slot in o.material_slots:
            if slot.material == mat:
                return True
    return False


def remove_sets_from_object(obj: bpy.types.Object) -> bool:
    """Strip Sets GN modifiers and in-place shader mix. Returns True if changed."""
    from . import materials as mat_mod

    changed = False
    for mod in list(obj.modifiers):
        if mod.type == "NODES" and mod.node_group and mod.node_group.name.startswith(C.NG_NAME):
            ng = mod.node_group
            obj.modifiers.remove(mod)
            changed = True
            _cleanup_ng_and_coll(ng)
    for slot in obj.material_slots:
        mat = slot.material
        if mat is None:
            continue
        if _material_used_by_other_sets(mat, obj):
            continue
        if mat_mod.remove_sets_shader_blend(mat):
            changed = True
    if getattr(obj.ual_blend, "sets_blended", False):
        obj.ual_blend.sets_blended = False
        changed = True
    if getattr(obj.ual_blend, "blended", False) and not getattr(
        obj.ual_blend, "blend_source", False
    ):
        # Clear Object-mode flag only when Sets owned the blend
        if not any(
            m.name.startswith(C.MOD_PROXIMITY) or m.name.startswith(C.MOD_TRANSFER)
            for m in obj.modifiers
        ):
            obj.ual_blend.blended = False
            changed = True
    if changed:
        _refresh(obj)
    return changed


def _cleanup_ng_and_coll(ng: Optional[bpy.types.NodeTree]) -> None:
    if ng is None:
        return
    # Unlink temp collections referenced by CollectionInfo nodes
    colls = []
    for nd in ng.nodes:
        if nd.bl_idname == "GeometryNodeCollectionInfo":
            try:
                c = nd.inputs["Collection"].default_value
                if c is not None and c.name.startswith(C.SETS_SRC_COLL):
                    colls.append(c)
            except Exception:
                pass
    if ng.users <= 1:
        try:
            bpy.data.node_groups.remove(ng)
        except Exception:
            pass
    for c in colls:
        if c.users <= 1:
            try:
                bpy.data.collections.remove(c)
            except Exception:
                pass
