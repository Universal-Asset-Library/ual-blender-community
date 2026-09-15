# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""Proximity modifiers, color mask attribute, and drivers (Phase 1)."""

from __future__ import annotations

from typing import Iterable, Optional

import bpy

from . import constants as C


def ensure_vertex_group(obj: bpy.types.Object, name: str = C.VG_NAME) -> bpy.types.VertexGroup:
    mesh = obj.data
    vg = obj.vertex_groups.get(name)
    if vg is None:
        vg = obj.vertex_groups.new(name=name)
    idxs = [v.index for v in mesh.vertices]
    if idxs:
        vg.add(idxs, 1.0, "REPLACE")
    return vg


def ensure_mask_attribute(
    obj: bpy.types.Object,
    *,
    fill_white: bool,
    name: str = C.MASK_ATTR,
) -> None:
    """Corner color attribute used as the Mix Fac mask."""
    mesh = obj.data
    color = (1.0, 1.0, 1.0, 1.0) if fill_white else (0.0, 0.0, 0.0, 1.0)
    # Prefer color_attributes (3.2+)
    attrs = getattr(mesh, "color_attributes", None)
    if attrs is not None:
        attr = attrs.get(name)
        if attr is None:
            try:
                attr = attrs.new(name=name, type="BYTE_COLOR", domain="CORNER")
            except TypeError:
                attr = attrs.new(name=name, type="FLOAT_COLOR", domain="CORNER")
        try:
            data = attr.data
            for i in range(len(data)):
                data[i].color = color
        except Exception:
            pass
        return
    # Legacy vertex_colors
    vcols = getattr(mesh, "vertex_colors", None)
    if vcols is None:
        return
    layer = vcols.get(name)
    if layer is None:
        layer = vcols.new(name=name)
    for loop_col in layer.data:
        loop_col.color = color


def _remove_mod_if_present(obj: bpy.types.Object, name: str) -> None:
    mod = obj.modifiers.get(name)
    if mod is not None:
        obj.modifiers.remove(mod)


def _clear_proximity_drivers(obj: bpy.types.Object) -> None:
    ad = getattr(obj, "animation_data", None)
    if ad is None or not ad.drivers:
        return
    paths = (
        f'modifiers["{C.MOD_PROXIMITY}"].min_dist',
        f'modifiers["{C.MOD_PROXIMITY}"].max_dist',
    )
    for fc in list(ad.drivers):
        if fc.data_path in paths:
            try:
                obj.driver_remove(fc.data_path, -1)
            except TypeError:
                try:
                    obj.driver_remove(fc.data_path)
                except Exception:
                    pass


def remove_blend_uv_layer(obj: bpy.types.Object) -> None:
    """Strip legacy UAL_Blend_UV from older Transfer-UV blends."""
    mesh = getattr(obj, "data", None)
    layers = getattr(mesh, "uv_layers", None) if mesh is not None else None
    if not layers or C.UV_LAYER not in layers:
        return
    try:
        layers.remove(layers[C.UV_LAYER])
    except Exception:
        try:
            layers[C.UV_LAYER].active = True
            bpy.ops.mesh.uv_texture_remove()
        except Exception:
            pass


def create_normal_modifier(
    target: bpy.types.Object,
    source: bpy.types.Object,
    *,
    blend_normals: bool,
) -> None:
    """DATA_TRANSFER custom normals, weighted by the proximity vertex group."""
    _remove_mod_if_present(target, C.MOD_TRANSFER_NORM)
    st = target.ual_blend
    st.blend_normals_used = False
    if not blend_normals:
        return

    xfer = target.modifiers.new(name=C.MOD_TRANSFER_NORM, type="DATA_TRANSFER")
    xfer.object = source
    xfer.use_loop_data = True
    try:
        xfer.loop_mapping = "POLYINTERP_NEAREST"
    except Exception:
        pass
    try:
        xfer.data_types_loops = {"CUSTOM_NORMAL"}
    except Exception:
        _remove_mod_if_present(target, C.MOD_TRANSFER_NORM)
        return
    try:
        xfer.vertex_group = C.VG_NAME
    except Exception:
        pass
    try:
        xfer.mix_factor = 1.0
    except Exception:
        pass
    xfer.show_expanded = False
    st.blend_normals_used = True

    mesh = target.data
    if hasattr(mesh, "use_auto_smooth") and not bool(mesh.use_auto_smooth):
        try:
            mesh.use_auto_smooth = True
            if hasattr(mesh, "auto_smooth_angle"):
                mesh.auto_smooth_angle = 3.14159
        except Exception:
            pass


def create_proximity_stack(
    target: bpy.types.Object,
    source: bpy.types.Object,
) -> None:
    """VERTEX_WEIGHT_PROXIMITY + DATA_TRANSFER of mask onto target."""
    ensure_vertex_group(target)
    ensure_mask_attribute(target, fill_white=False)
    ensure_mask_attribute(source, fill_white=True)

    _remove_mod_if_present(target, C.MOD_PROXIMITY)
    _remove_mod_if_present(target, C.MOD_TRANSFER)

    prox = target.modifiers.new(name=C.MOD_PROXIMITY, type="VERTEX_WEIGHT_PROXIMITY")
    prox.vertex_group = C.VG_NAME
    prox.target = source
    prox.proximity_mode = "GEOMETRY"
    try:
        prox.proximity_geometry = {"FACE"}
    except Exception:
        pass
    prox.falloff_type = "SHARP"
    prox.show_expanded = False
    # Initial values; drivers overwrite live
    st_src = source.ual_blend
    st_tgt = target.ual_blend
    mn, mx = C.proximity_distances(
        st_src.blend_max, st_src.blend_min, st_tgt.blend_scale, simple=True
    )
    prox.min_dist = mx  # Blendit: min_dist = outer (weight starts falling)
    prox.max_dist = mn  # max_dist = inner (full weight)

    xfer = target.modifiers.new(name=C.MOD_TRANSFER, type="DATA_TRANSFER")
    xfer.object = source
    xfer.use_object_transform = False
    xfer.use_loop_data = True
    xfer.loop_mapping = "NEAREST_NORMAL"
    xfer.show_expanded = False
    xfer.vertex_group = C.VG_NAME
    # COLOR_CORNER on modern Blender
    try:
        xfer.data_types_loops = {"COLOR_CORNER"}
    except Exception:
        try:
            xfer.data_types_loops = {"VCOL"}
        except Exception:
            pass
    # Layer name pickers (3.2+ rename)
    for src_attr, dst_attr in (
        ("layers_vcol_loop_select_src", "layers_vcol_loop_select_dst"),
        ("layers_vcol_select_src", "layers_vcol_select_dst"),
    ):
        if hasattr(xfer, src_attr):
            try:
                setattr(xfer, src_attr, C.MASK_ATTR)
            except Exception:
                try:
                    setattr(xfer, src_attr, C.MASK_ATTR)
                except Exception:
                    pass
            break
    for dst_attr in ("layers_vcol_loop_select_dst", "layers_vcol_select_dst"):
        if hasattr(xfer, dst_attr):
            try:
                setattr(xfer, dst_attr, "NAME")
            except Exception:
                pass
            break

    _install_distance_drivers(target, source)


def _install_distance_drivers(target: bpy.types.Object, source: bpy.types.Object) -> None:
    _clear_proximity_drivers(target)
    prox = target.modifiers.get(C.MOD_PROXIMITY)
    if prox is None:
        return

    def _add_scripted(prop: str, expression: str, vars_spec: list) -> None:
        d = prox.driver_add(prop).driver
        d.type = "SCRIPTED"
        d.expression = expression
        for name, id_obj, path in vars_spec:
            var = d.variables.new()
            var.name = name
            var.type = "SINGLE_PROP"
            tar = var.targets[0]
            tar.id_type = "OBJECT"
            tar.id = id_obj
            tar.data_path = path

    # Outer = source.Max * target.Scale  (Blendit Simple)
    _add_scripted(
        "min_dist",
        "ual_max * ual_scale",
        [
            ("ual_max", source, "ual_blend.blend_max"),
            ("ual_scale", target, "ual_blend.blend_scale"),
        ],
    )
    # Inner = outer * 0.1
    _add_scripted(
        "max_dist",
        "ual_max * ual_scale * 0.1",
        [
            ("ual_max", source, "ual_blend.blend_max"),
            ("ual_scale", target, "ual_blend.blend_scale"),
        ],
    )
    # Immediate write so UI feels live even before depsgraph ticks
    push_proximity_distances(target)


def push_proximity_distances(target: bpy.types.Object) -> None:
    """Apply Max×Scale to VERTEX_WEIGHT_PROXIMITY (Blendit Simple mapping)."""
    st = getattr(target, "ual_blend", None)
    if st is None:
        return
    source = st.source_obj
    if source is None:
        return
    prox = target.modifiers.get(C.MOD_PROXIMITY)
    if prox is None:
        return
    mn, mx = C.proximity_distances(
        float(source.ual_blend.blend_max),
        float(source.ual_blend.blend_min),
        float(st.blend_scale),
        simple=True,
    )
    # Blendit: min_dist = outer, max_dist = inner
    prox.min_dist = mx
    prox.max_dist = mn
    try:
        target.update_tag()
    except Exception:
        pass


def push_proximity_for_source(source: bpy.types.Object) -> None:
    """When Source Max changes, refresh every target that points at it."""
    for obj in bpy.data.objects:
        if getattr(obj, "type", "") != "MESH":
            continue
        st = getattr(obj, "ual_blend", None)
        if st is None or not st.blended:
            continue
        if st.source_obj == source:
            push_proximity_distances(obj)


def apply_stack(obj: bpy.types.Object, context: Optional[bpy.types.Context] = None) -> bool:
    """Bake proximity modifiers; return True if anything applied."""
    if not getattr(obj.ual_blend, "blended", False):
        return False
    ctx = context or bpy.context
    view = getattr(ctx, "view_layer", None)
    if view is not None:
        view.objects.active = obj
    _clear_proximity_drivers(obj)
    applied_any = False
    for name in (C.MOD_PROXIMITY, C.MOD_TRANSFER, C.MOD_TRANSFER_NORM, C.MOD_TRANSFER_UV, C.MOD_UV_WARP):
        if name not in obj.modifiers:
            continue
        try:
            bpy.ops.object.modifier_apply(modifier=name)
            applied_any = True
        except Exception:
            _remove_mod_if_present(obj, name)
    obj.ual_blend.applied = True
    return applied_any


def remove_stack(obj: bpy.types.Object) -> None:
    _clear_proximity_drivers(obj)
    _remove_mod_if_present(obj, C.MOD_PROXIMITY)
    _remove_mod_if_present(obj, C.MOD_TRANSFER)
    _remove_mod_if_present(obj, C.MOD_TRANSFER_NORM)
    _remove_mod_if_present(obj, C.MOD_TRANSFER_UV)
    _remove_mod_if_present(obj, C.MOD_UV_WARP)
    remove_blend_uv_layer(obj)
    # Leave color attribute / VG for artist cleanup unless we wipe them
    vg = obj.vertex_groups.get(C.VG_NAME)
    if vg is not None:
        obj.vertex_groups.remove(vg)
    mesh = obj.data
    attrs = getattr(mesh, "color_attributes", None)
    if attrs is not None and C.MASK_ATTR in attrs:
        try:
            attrs.remove(attrs[C.MASK_ATTR])
        except Exception:
            pass
    elif hasattr(mesh, "vertex_colors") and C.MASK_ATTR in mesh.vertex_colors:
        try:
            mesh.vertex_colors.remove(mesh.vertex_colors[C.MASK_ATTR])
        except Exception:
            pass
    try:
        obj.ual_blend.blend_normals_used = False
    except Exception:
        pass


def iter_blend_targets(selected: Iterable[bpy.types.Object], active: bpy.types.Object):
    for obj in selected:
        if obj is None or obj == active:
            continue
        if getattr(obj, "type", "") != "MESH":
            continue
        yield obj
