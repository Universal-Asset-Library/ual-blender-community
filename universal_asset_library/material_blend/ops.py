# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""Material Blend operators — Object mode + Sets mode."""

from __future__ import annotations

import bpy

from . import constants as C
from . import materials as mat_mod
from . import modifiers as mod_mod
from . import sets_state


def _slot0_material(obj: bpy.types.Object):
    if not obj.material_slots:
        return None
    return obj.material_slots[0].material


def _mesh_selected(context) -> list:
    return [
        o
        for o in (getattr(context, "selected_objects", None) or [])
        if o is not None and getattr(o, "type", "") == "MESH"
    ]


def _any_blended(context) -> bool:
    for o in _mesh_selected(context):
        st = getattr(o, "ual_blend", None)
        if st is None:
            continue
        if st.blended or st.blend_source or getattr(st, "sets_blended", False):
            return True
        from . import gn_mask

        if gn_mask.has_sets_modifier(o):
            return True
    return False


def create_selection_message(context) -> str:
    active = getattr(context, "object", None)
    meshes = _mesh_selected(context)
    if active is None or getattr(active, "type", "") != "MESH":
        return "Select a mesh as active (source)"
    has_mat = _slot0_material(active) is not None
    return C.selection_ready(len(meshes), has_mat)


def sets_blend_message(context) -> str:
    scene = context.scene
    receivers = sets_state.resolve_set_meshes(scene, "ual_blend_set_a")
    donors = sets_state.resolve_set_meshes(scene, "ual_blend_set_b")
    has_donor = False
    for d in donors:
        mat = _slot0_material(d) or getattr(d, "active_material", None)
        if mat is not None and getattr(mat, "use_nodes", False):
            has_donor = True
            break
    return C.sets_ready(len(receivers), len(donors), has_donor)


def _seed_source_distances(source: bpy.types.Object) -> None:
    try:
        from .. import preferences

        cfg = preferences.prefs_to_config()
        imp = cfg.get("import") or {}
        source.ual_blend.blend_max = float(
            imp.get("material_blend_max_distance", source.ual_blend.blend_max) or 1.0
        )
        source.ual_blend.blend_min = float(
            imp.get("material_blend_min_distance", source.ual_blend.blend_min) or 1.0
        )
    except Exception:
        pass


def _seed_target_noise(target: bpy.types.Object) -> None:
    """Apply prefs noise defaults only when Target still has factory values."""
    st = target.ual_blend
    at_factory = (
        abs(float(st.blend_noise_scale) - 1.0) < 1e-6
        and abs(float(st.blend_noise_amount) - 1.0) < 1e-6
    )
    if not at_factory:
        return
    try:
        from .. import preferences

        cfg = preferences.prefs_to_config()
        imp = cfg.get("import") or {}
        if "material_blend_live_noise_scale" in imp:
            st.blend_noise_scale = float(imp.get("material_blend_live_noise_scale") or 1.0)
        if "material_blend_live_noise_amount" in imp:
            st.blend_noise_amount = float(imp.get("material_blend_live_noise_amount") or 1.0)
    except Exception:
        pass


def _scene_blend_normals(context) -> bool:
    scene = getattr(context, "scene", None)
    if scene is not None and hasattr(scene, "ual_blend_blend_normals"):
        return bool(scene.ual_blend_blend_normals)
    try:
        from .. import preferences

        cfg = preferences.prefs_to_config()
        return bool((cfg.get("import") or {}).get("material_blend_blend_normals", True))
    except Exception:
        return True


def _blend_target_with_source(
    source: bpy.types.Object,
    target: bpy.types.Object,
    source_mat: bpy.types.Material,
    *,
    blend_normals: bool = True,
) -> bool:
    """Object-method proximity stack + Mix on every target material slot."""
    # Clear previous Object stack and/or legacy Sets GN before rebuilding
    from . import gn_mask

    if gn_mask.has_sets_modifier(target):
        gn_mask.remove_sets_from_object(target)
    if any(
        m.name.startswith(C.MOD_PROXIMITY)
        or m.name.startswith(C.MOD_TRANSFER)
        or m.name.startswith(C.MOD_UV_WARP)
        for m in target.modifiers
    ):
        mod_mod.remove_stack(target)
    if target.material_slots:
        for slot in target.material_slots:
            if slot.material:
                mat_mod.remove_sets_shader_blend(slot.material)

    # Snapshot originals before replacing (keeps prior snapshot when re-blending)
    mat_mod.snapshot_original_materials(target)

    if not target.material_slots:
        origin_mat = bpy.data.materials.new(name=f"UAL_Blend_Base_{target.name}")
        origin_mat.use_nodes = True
        target.data.materials.append(origin_mat)
        mat_mod.snapshot_original_materials(target)

    _seed_target_noise(target)

    blended_any = False
    for i, slot in enumerate(target.material_slots):
        origin_mat = mat_mod.resolve_slot_origin(target, i, slot.material)
        if origin_mat is None:
            continue
        if not origin_mat.use_nodes:
            continue
        blend_mat = mat_mod.build_simple_blend_material(
            source, target, source_mat, origin_mat, slot_index=i
        )
        if blend_mat is None:
            continue
        slot.material = blend_mat
        blended_any = True

    if not blended_any:
        if any(slot.material is not None for slot in target.material_slots):
            return False
        origin_mat = bpy.data.materials.new(name=f"UAL_Blend_Base_{target.name}")
        origin_mat.use_nodes = True
        if not target.material_slots:
            target.data.materials.append(origin_mat)
        else:
            target.material_slots[0].material = origin_mat
        target.ual_blend.original_materials.clear()
        item = target.ual_blend.original_materials.add()
        item.name = origin_mat.name
        target.ual_blend.original_material_name = origin_mat.name
        blend_mat = mat_mod.build_simple_blend_material(
            source, target, source_mat, origin_mat, slot_index=0
        )
        if blend_mat is None:
            return False
        target.material_slots[0].material = blend_mat
        blended_any = True

    mod_mod.create_proximity_stack(target, source)
    mod_mod.create_normal_modifier(target, source, blend_normals=blend_normals)
    target.ual_blend.blended = True
    target.ual_blend.applied = False
    target.ual_blend.blend_source = False
    target.ual_blend.source_obj = source
    return blended_any


class UAL_OT_material_blend_create(bpy.types.Operator):
    bl_idname = "ual.material_blend_create"
    bl_label = "Create Blend"
    bl_description = (
        "Proximity-blend materials: active mesh = source, other selected meshes = targets"
    )
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        if context.mode != "OBJECT":
            return False
        return create_selection_message(context) == ""

    def execute(self, context):
        source = context.object
        source_mat = _slot0_material(source)
        if source_mat is None:
            self.report({"ERROR"}, "Active mesh needs a material")
            return {"CANCELLED"}

        _seed_source_distances(source)

        source.ual_blend.blend_source = True
        source.ual_blend.blended = False
        source.ual_blend.source_obj = None
        source.ual_blend.sets_blended = False
        mod_mod.ensure_mask_attribute(source, fill_white=True)

        targets = list(mod_mod.iter_blend_targets(context.selected_objects, source))
        if not targets:
            self.report({"ERROR"}, "Select at least one other mesh as target")
            return {"CANCELLED"}

        blend_normals = _scene_blend_normals(context)
        ok = 0
        skipped = 0
        for target in targets:
            if _blend_target_with_source(
                source,
                target,
                source_mat,
                blend_normals=blend_normals,
            ):
                target.ual_blend.sets_blended = False
                ok += 1
            else:
                skipped += 1

        msg = f"Material Blend: {ok} target(s)"
        if skipped:
            msg += f" ({skipped} skipped — need node materials)"
        self.report({"INFO"}, msg)
        try:
            from ..ui import operators as ui_ops

            ui_ops._set_status(context, msg)
        except Exception:
            pass
        return {"FINISHED"}


class UAL_OT_material_blend_apply(bpy.types.Operator):
    bl_idname = "ual.material_blend_apply"
    bl_label = "Apply Blend"
    bl_description = "Bake proximity modifiers on blended selected meshes"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return context.mode == "OBJECT" and _any_blended(context)

    def execute(self, context):
        n = 0
        for obj in _mesh_selected(context):
            if getattr(obj.ual_blend, "blended", False):
                if mod_mod.apply_stack(obj, context):
                    n += 1
        msg = f"Applied blend on {n} object(s)"
        self.report({"INFO"}, msg)
        try:
            from ..ui import operators as ui_ops

            ui_ops._set_status(context, msg)
        except Exception:
            pass
        return {"FINISHED"}


class UAL_OT_material_blend_remove(bpy.types.Operator):
    bl_idname = "ual.material_blend_remove"
    bl_label = "Remove Blend"
    bl_description = "Remove Object or Sets blend from selected meshes"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return context.mode == "OBJECT" and _any_blended(context)

    def execute(self, context):
        from . import gn_mask

        n = 0
        for obj in _mesh_selected(context):
            st = obj.ual_blend
            did = False
            if getattr(st, "sets_blended", False) or gn_mask.has_sets_modifier(obj):
                if gn_mask.remove_sets_from_object(obj):
                    did = True
            has_obj_stack = any(
                m.name.startswith(C.MOD_PROXIMITY)
                or m.name.startswith(C.MOD_TRANSFER)
                or m.name.startswith(C.MOD_UV_WARP)
                for m in obj.modifiers
            )
            has_origins = bool(st.original_material_name) or len(st.original_materials) > 0
            if st.blend_source or has_obj_stack or (
                st.blended
                and has_origins
                and not getattr(st, "sets_blended", False)
            ):
                was_target = bool(st.blended) and not bool(st.blend_source)
                was_source = bool(st.blend_source)
                mod_mod.remove_stack(obj)
                if was_target or has_origins:
                    mat_mod.restore_original_material(obj)
                if was_source:
                    mod_mod.remove_blend_uv_layer(obj)
                st.blended = False
                st.blend_source = False
                st.sets_blended = False
                st.applied = False
                st.source_obj = None
                st.original_material_name = ""
                st.blend_normals_used = False
                try:
                    st.original_materials.clear()
                except Exception:
                    pass
                did = True
            if did:
                n += 1
        msg = f"Removed blend from {n} object(s)"
        self.report({"INFO"}, msg)
        try:
            from ..ui import operators as ui_ops

            ui_ops._set_status(context, msg)
        except Exception:
            pass
        return {"FINISHED"}


class UAL_OT_material_blend_set_assign_a(bpy.types.Operator):
    bl_idname = "ual.material_blend_set_assign_a"
    bl_label = "Add Targets"
    bl_description = (
        "Add selected meshes to Targets — viewport or Outliner selection "
        "(keeps existing; skips duplicates). Or use Pick / Collection below."
    )
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return context.mode == "OBJECT" and bool(_mesh_selected(context))

    def execute(self, context):
        n = sets_state.assign_meshes_from_selection(context.scene, "ual_blend_set_a")
        total = len(getattr(context.scene, "ual_blend_set_a", []) or [])
        if n:
            self.report({"INFO"}, f"Targets: added {n} (total {total})")
        else:
            self.report({"INFO"}, f"Targets: no new meshes (total {total})")
        return {"FINISHED"}


class UAL_OT_material_blend_set_assign_b(bpy.types.Operator):
    bl_idname = "ual.material_blend_set_assign_b"
    bl_label = "Add Source"
    bl_description = (
        "Add selected meshes to Source — viewport or Outliner selection "
        "(keeps existing; skips duplicates). Or use Pick / Collection below."
    )
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return context.mode == "OBJECT" and bool(_mesh_selected(context))

    def execute(self, context):
        n = sets_state.assign_meshes_from_selection(context.scene, "ual_blend_set_b")
        total = len(getattr(context.scene, "ual_blend_set_b", []) or [])
        if n:
            self.report({"INFO"}, f"Source: added {n} (total {total})")
        else:
            self.report({"INFO"}, f"Source: no new meshes (total {total})")
        return {"FINISHED"}


class UAL_OT_material_blend_set_clear_a(bpy.types.Operator):
    bl_idname = "ual.material_blend_set_clear_a"
    bl_label = "Clear Targets"
    bl_description = "Clear Targets list"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        context.scene.ual_blend_set_a.clear()
        context.scene.ual_blend_set_a_index = 0
        return {"FINISHED"}


class UAL_OT_material_blend_set_clear_b(bpy.types.Operator):
    bl_idname = "ual.material_blend_set_clear_b"
    bl_label = "Clear Source"
    bl_description = "Clear Source list"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        context.scene.ual_blend_set_b.clear()
        context.scene.ual_blend_set_b_index = 0
        return {"FINISHED"}


class UAL_OT_material_blend_sets_blend(bpy.types.Operator):
    bl_idname = "ual.material_blend_sets_blend"
    bl_label = "Blend"
    bl_description = (
        "Same proximity method as Object mode: Targets receive Source material "
        "(Max / Scale distance controls)"
    )
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        if context.mode != "OBJECT":
            return False
        return sets_blend_message(context) == ""

    def execute(self, context):
        scene = context.scene
        receivers = sets_state.resolve_set_meshes(scene, "ual_blend_set_a")
        donors = sets_state.resolve_set_meshes(scene, "ual_blend_set_b")
        msg = sets_blend_message(context)
        if msg:
            self.report({"ERROR"}, msg)
            return {"CANCELLED"}

        # Primary Source = first Source mesh with a slot-0 material (Object-method needs one)
        source = None
        source_mat = None
        for d in donors:
            mat = _slot0_material(d)
            if mat is not None:
                source = d
                source_mat = mat
                break
        if source is None or source_mat is None:
            self.report({"ERROR"}, "Source needs a material in slot 1")
            return {"CANCELLED"}

        _seed_source_distances(source)
        source.ual_blend.blend_source = True
        source.ual_blend.blended = False
        source.ual_blend.sets_blended = False
        source.ual_blend.source_obj = None
        mod_mod.ensure_mask_attribute(source, fill_white=True)

        donor_set = set(donors)
        blend_normals = _scene_blend_normals(context)
        ok = 0
        skipped = 0
        for target in receivers:
            if target in donor_set:
                continue
            if _blend_target_with_source(
                source,
                target,
                source_mat,
                blend_normals=blend_normals,
            ):
                target.ual_blend.sets_blended = True
                ok += 1
            else:
                skipped += 1

        if ok == 0:
            self.report({"ERROR"}, "Blend failed — Targets need node materials")
            return {"CANCELLED"}

        status = f"Blend: {ok} target(s)"
        if skipped:
            status += f" ({skipped} skipped)"
        if len(receivers) > C.SETS_WARN_COUNT:
            status += f" — many Targets ({len(receivers)})"
        try:
            from ..ui import operators as ui_ops

            ui_ops._set_status(context, status)
        except Exception:
            pass
        self.report({"INFO"}, status)
        return {"FINISHED"}


CLASSES = (
    UAL_OT_material_blend_create,
    UAL_OT_material_blend_apply,
    UAL_OT_material_blend_remove,
    UAL_OT_material_blend_set_assign_a,
    UAL_OT_material_blend_set_assign_b,
    UAL_OT_material_blend_set_clear_a,
    UAL_OT_material_blend_set_clear_b,
    UAL_OT_material_blend_sets_blend,
)


def register() -> None:
    for cls in CLASSES:
        bpy.utils.register_class(cls)


def unregister() -> None:
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
