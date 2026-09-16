# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""Drag-into-scene (BlenderKit-inspired) + file browser drop import.

BlenderKit: GPU thumb follows cursor, Esc cancels, release to drop.
UAL: modal Drop into Scene with square cursor ghost + status text; Esc cancels;
LMB **release** confirms import. Materials/textures assign to the raycast-hit
mesh; meshes place at the hit / ground point under the mouse — the scene
**3D cursor is never moved** during drop. Online assets reuse a matching library
copy when present (else download with the shared progress bar). Grid captions
also start this modal after a short drag threshold (see ``ual.select_*_index``).
"""

from __future__ import annotations

import os

import bpy
import gpu
from gpu_extras.batch import batch_for_shader

from .icons import ACTION_ICONS, MATERIAL_DROP_TYPES, canonical_import_type

from .. import download_progress
from .. import download_utils
from .. import import_routing
from .. import online_download
from .. import paths
from .. import tasks_queue
from .. import thumbnails
from ..indexer import classify_path
from ..sources import get_source
from .operators import (
    _cfg,
    _ensure_progress_timer,
    _report_error,
    _set_status,
    _ui,
    sync_download_progress_to_ui,
)
from ..ui_strings import MSG_ONLINE_ACCESS, friendly_error

try:
    import blf
except ImportError:  # pragma: no cover
    blf = None

# Active Drop modal (for cancel_all_interactions / unregister)
_active_drag_op = None
# UNIFORM_COLOR builtin is stable for the drag ghost — look up once, not per frame.
_ghost_shader = None


def force_cancel_active(context=None) -> None:
    """Idempotent cancel of the Drop-into-Scene modal if running."""
    global _active_drag_op
    op = _active_drag_op
    if op is None:
        return
    ctx = context
    if ctx is None:
        try:
            ctx = bpy.context
        except Exception:
            ctx = None
    try:
        op._cancelled = True
        if getattr(op, "_task_id", ""):
            download_progress.request_cancel(op._task_id)
        if ctx is not None:
            op._cleanup(ctx)
    except Exception:
        pass
    _active_drag_op = None


def _view3d_window_under_mouse(mouse_x: int, mouse_y: int):
    """VIEW_3D WINDOW region strictly under the cursor — no fallback to another area.

    ``Region.x`` / ``Region.y`` are window-relative — hit-test the WINDOW region
    itself (not merely the parent area, which also contains TOOLS/UI/HEADER).
    """
    context = bpy.context
    screen = getattr(context, "screen", None)
    if screen is None:
        return None, None
    for area in screen.areas:
        if area.type != "VIEW_3D":
            continue
        for region in area.regions:
            if region.type != "WINDOW":
                continue
            if (
                int(region.x) <= int(mouse_x) < int(region.x) + int(region.width)
                and int(region.y) <= int(mouse_y) < int(region.y) + int(region.height)
            ):
                return area, region
    return None, None


def mouse_over_eligible_drop_view3d(mouse_x: int, mouse_y: int) -> bool:
    """True when Drop release is over a VIEW_3D WINDOW (not Outliner / other editors)."""
    from ..drag_snapshot import drop_target_is_eligible_view3d

    area, region = _view3d_window_under_mouse(mouse_x, mouse_y)
    if area is None or region is None:
        return False
    return drop_target_is_eligible_view3d(
        area_type=area.type,
        region_type=region.type,
        mouse_x=mouse_x,
        mouse_y=mouse_y,
        area_x=area.x,
        area_y=area.y,
        area_width=area.width,
        area_height=area.height,
        region_x=region.x,
        region_y=region.y,
        region_width=region.width,
        region_height=region.height,
    )


def _place_at_cursor(objects) -> None:
    cursor = bpy.context.scene.cursor.location
    _place_at_location(objects, cursor)


def _place_at_location(objects, location) -> None:
    """Place imported mesh group at a world point (feet on the surface)."""
    if not objects or location is None:
        return
    from .. import geo_import

    geo_import.place_objects_at(objects, location, snap_base=True)


def _window_to_region(area, region, mouse_x: int, mouse_y: int):
    """Convert window pixel coords → VIEW_3D region-local coords.

    Blender ``Region.x`` / ``Region.y`` are **window-relative** (not area-relative).
    Subtracting ``area.x`` again pushed the drag ghost far from the cursor.
    """
    del area  # API uses window-relative region origin only
    return int(mouse_x) - int(region.x), int(mouse_y) - int(region.y)


def _view3d_space(area):
    for sp in area.spaces:
        if sp.type == "VIEW_3D":
            return sp
    return None


def raycast_viewport_object(context, mouse_x: int, mouse_y: int, *, area=None, region=None):
    """Raycast from the mouse into the 3D View (BlenderKit material-drop target).

    Returns ``{"object", "location", "normal", "face_index"}`` or ``None``.
    Pass ``area``/``region`` when the caller already resolved the VIEW_3D WINDOW
    under the cursor (avoids a second ``screen.areas`` scan per MOUSEMOVE).
    """
    try:
        from bpy_extras import view3d_utils
    except ImportError:
        return None
    if area is None or region is None:
        area, region = _view3d_window_under_mouse(mouse_x, mouse_y)
    if area is None or region is None:
        return None
    space = _view3d_space(area)
    if space is None or space.region_3d is None:
        return None
    rx, ry = _window_to_region(area, region, mouse_x, mouse_y)
    if rx < 0 or ry < 0 or rx >= region.width or ry >= region.height:
        return None
    coord = (float(rx), float(ry))
    rv3d = space.region_3d
    view_vector = view3d_utils.region_2d_to_vector_3d(region, rv3d, coord)
    ray_origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, coord)
    try:
        depsgraph = context.evaluated_depsgraph_get()
        result, location, normal, index, obj, _matrix = context.scene.ray_cast(
            depsgraph, ray_origin, view_vector
        )
    except Exception:
        return None
    if not result or obj is None:
        return None
    if getattr(obj, "type", "") != "MESH":
        return None
    return {
        "object": obj,
        "location": location,
        "normal": normal,
        "face_index": index,
    }


def _ground_plane_intersection(ray_origin, ray_direction, *, z: float = 0.0):
    """Intersect a view ray with the horizontal plane at ``z`` (world)."""
    from ..drop_placement import ground_plane_intersection

    hit = ground_plane_intersection(ray_origin, ray_direction, z=z)
    if hit is None:
        return None
    try:
        from mathutils import Vector

        return Vector(hit)
    except ImportError:
        return hit


def resolve_drop_location(context, mouse_x: int, mouse_y: int):
    """World position under the mouse for mesh placement.

    Prefer a stable geometry raycast hit. Grazing / near-parallel hits fall
    back to ground plane Z=0. Empty space uses ground, then a depth plane
    through the **existing** 3D cursor (read-only — never write ``scene.cursor``).
    Returns ``(location_vector_or_None, hit_dict_or_None)``.
    """
    try:
        from bpy_extras import view3d_utils
        from mathutils import Vector
    except ImportError:
        return None, None
    from ..drop_placement import is_grazing_hit

    area, region = _view3d_window_under_mouse(mouse_x, mouse_y)
    if area is None or region is None:
        return None, None
    space = _view3d_space(area)
    if space is None or space.region_3d is None:
        return None, None
    rx, ry = _window_to_region(area, region, mouse_x, mouse_y)
    if rx < 0 or ry < 0 or rx >= region.width or ry >= region.height:
        return None, None
    coord = (float(rx), float(ry))
    rv3d = space.region_3d
    view_vector = view3d_utils.region_2d_to_vector_3d(region, rv3d, coord)
    ray_origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, coord)

    hit = raycast_viewport_object(
        context, mouse_x, mouse_y, area=area, region=region
    )
    if hit and hit.get("location") is not None:
        normal = hit.get("normal")
        if normal is not None and is_grazing_hit(view_vector, normal):
            ground = _ground_plane_intersection(ray_origin, view_vector, z=0.0)
            if ground is not None:
                return ground, None
        else:
            return hit["location"], hit

    ground = _ground_plane_intersection(ray_origin, view_vector, z=0.0)
    if ground is not None:
        return ground, None
    # Read-only depth reference — never write scene.cursor during drop preview
    try:
        depth_loc = context.scene.cursor.location.copy()
    except Exception:
        depth_loc = Vector((0.0, 0.0, 0.0))
    loc = view3d_utils.region_2d_to_location_3d(region, rv3d, coord, depth_loc)
    return loc, None


def _draw_drag_ghost(op) -> None:
    """POST_PIXEL: square thumb ghost stuck to the cursor (BlenderKit-style)."""
    if getattr(op, "_state", "") not in {"ready", "downloading"}:
        return
    mx = int(getattr(op, "_mouse_x", 0) or 0)
    my = int(getattr(op, "_mouse_y", 0) or 0)

    # Draw only in the VIEW_3D WINDOW currently being painted *and* under the cursor
    # (SpaceView3D handlers fire for every 3D view).
    ctx_area = getattr(bpy.context, "area", None)
    ctx_region = getattr(bpy.context, "region", None)
    if (
        ctx_area is None
        or getattr(ctx_area, "type", "") != "VIEW_3D"
        or ctx_region is None
        or getattr(ctx_region, "type", "") != "WINDOW"
    ):
        return
    if not (
        int(ctx_region.x) <= mx < int(ctx_region.x) + int(ctx_region.width)
        and int(ctx_region.y) <= my < int(ctx_region.y) + int(ctx_region.height)
    ):
        return

    x = mx - int(ctx_region.x)
    y = my - int(ctx_region.y)
    size = int(getattr(op, "_ghost_size", 72) or 72)
    # Keep the thumb glued to the cursor tip (small pad so the tip stays visible).
    pad = 6
    ox, oy = x + pad, y - pad - size

    try:
        gpu.state.blend_set("ALPHA")
        global _ghost_shader
        shader = _ghost_shader
        if shader is None:
            shader = gpu.shader.from_builtin("UNIFORM_COLOR")
            _ghost_shader = shader
        # Vertex coords include ox/oy (POST_PIXEL pixel space). Caching the
        # 8-vertex batches would need gpu.matrix.translate every frame and is
        # not cheaper than rebaking; the hot cost was shader.from_builtin.
        coords = (
            (ox - 2, oy - 2),
            (ox + size + 2, oy - 2),
            (ox + size + 2, oy + size + 2),
            (ox - 2, oy + size + 2),
        )
        batch = batch_for_shader(shader, "TRI_FAN", {"pos": coords})
        shader.bind()
        accent = (0.89, 0.44, 0.12, 0.95) if op._state == "ready" else (0.35, 0.55, 0.9, 0.9)
        shader.uniform_float("color", accent)
        batch.draw(shader)
        inner = (
            (ox, oy),
            (ox + size, oy),
            (ox + size, oy + size),
            (ox, oy + size),
        )
        batch = batch_for_shader(shader, "TRI_FAN", {"pos": inner})
        shader.bind()
        shader.uniform_float("color", (0.12, 0.12, 0.13, 0.92))
        batch.draw(shader)
        gpu.state.blend_set("NONE")
    except Exception:
        pass

    if blf is None:
        return
    try:
        label = getattr(op, "_asset_name", "") or "Asset"
        hint = (
            "Release LMB to drop · Esc cancel"
            if op._state == "ready"
            else "Downloading… ghost ready · Esc cancels download"
        )
        font_id = 0
        blf.size(font_id, 12)
        blf.color(font_id, 0.95, 0.95, 0.95, 1.0)
        blf.position(font_id, ox, oy - 16, 0)
        blf.draw(font_id, label[:40])
        blf.size(font_id, 11)
        blf.color(font_id, 0.75, 0.75, 0.75, 1.0)
        blf.position(font_id, ox, oy - 32, 0)
        blf.draw(font_id, hint)
    except Exception:
        pass


class UAL_OT_drop_import(bpy.types.Operator):
    """Import filesystem path(s) via file browser."""

    bl_idname = "ual.drop_import"
    bl_label = "UAL Import From Disk"
    bl_options = {"REGISTER", "UNDO"}

    directory: bpy.props.StringProperty(subtype="DIR_PATH")
    files: bpy.props.CollectionProperty(type=bpy.types.OperatorFileListElement)
    filepath: bpy.props.StringProperty(subtype="FILE_PATH")

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        file_paths = []
        if self.files and self.directory:
            for entry in self.files:
                file_paths.append(os.path.join(self.directory, entry.name))
        elif self.filepath:
            file_paths.append(self.filepath)
        if not file_paths:
            return {"CANCELLED"}
        cfg = _cfg()
        for path in file_paths:
            try:
                result = import_routing.perform_typed_import(
                    path,
                    asset_type=classify_path(path) or None,
                    cfg=cfg,
                    place_at_origin=False,
                )
                _place_at_cursor(result.get("objects") or [])
            except Exception as exc:  # noqa: BLE001
                _report_error(self, exc)
                return {"CANCELLED"}
        return {"FINISHED"}


class UAL_OT_asset_drag_drop(bpy.types.Operator):
    """Modal drop-into-scene with BlenderKit-style cursor ghost.

    Drag from a grid tile or the toolbar drag icon, move in the 3D View,
    **release LMB** to import. Materials/textures assign to the mesh under the
    mouse (raycast). Meshes spawn at **world origin** with the base on Z=0
    (same as Import — always visible / aligned when the view looks at origin).
    The scene 3D cursor is **not** moved. Esc cancels.
    """

    bl_idname = "ual.asset_drag_drop"
    bl_label = "Drop into Scene"
    bl_description = (
        "Download (if online) and drop the asset. Materials apply to the object "
        "under the mouse; meshes spawn at world origin with base on the ground. "
        "Release LMB to drop, Esc to cancel."
    )
    bl_options = {"REGISTER", "UNDO"}

    target_index: bpy.props.IntProperty(default=-1, options={"SKIP_SAVE", "HIDDEN"})
    target_key: bpy.props.StringProperty(default="", options={"SKIP_SAVE", "HIDDEN"})

    _timer = None
    _draw_handle = None
    _task_id = ""
    _state = "idle"  # idle | downloading | ready | done
    _local_path = ""
    _asset_type = ""
    _asset_name = ""
    _error = ""
    _cancelled = False
    _header = ""
    _mouse_x = 0
    _mouse_y = 0
    _ghost_size = 72
    _icon_id = 0
    _skip_releases = 0
    _snap = None  # immutable drag_snapshot dict
    _drop_location = None
    _last_ghost_area = None

    def invoke(self, context, event):
        global _active_drag_op
        from .. import drag_snapshot

        ui = _ui(context)
        cfg = _cfg()
        self._state = "idle"
        self._local_path = ""
        self._error = ""
        self._cancelled = False
        self._snap = None
        self._mouse_x = event.mouse_x
        self._mouse_y = event.mouse_y
        self._ghost_size = max(56, min(int((cfg.get("ui") or {}).get("thumb_size", 88) or 88), 128))
        self._drop_location = None
        self._last_ghost_area = None
        # Toolbar button: invoke on LMB PRESS — ignore that click's RELEASE so we
        # do not instantly drop. Grid drag hands off on MOUSEMOVE — drop on RELEASE.
        self._skip_releases = (
            1 if event.type == "LEFTMOUSE" and event.value == "PRESS" else 0
        )

        if ui.scope == "online":
            if not getattr(bpy.app, "online_access", True):
                self.report({"ERROR"}, MSG_ONLINE_ACCESS)
                return {"CANCELLED"}
            if not ui.online_results:
                self.report({"ERROR"}, "Select an online asset first — click the name under the preview")
                return {"CANCELLED"}
            from .. import selection_keys

            resolved = selection_keys.resolve_online_target(
                ui,
                target_key=str(self.target_key or ""),
                target_index=int(self.target_index),
            )
            if resolved is None:
                self.report(
                    {"ERROR"},
                    "Selected online asset is no longer in results — select it again",
                )
                return {"CANCELLED"}
            _idx, item, _key = resolved
            del _idx, _key
            snap = drag_snapshot.snapshot_online_item(
                item, source_fallback=str(ui.online_source or "")
            )
            if not drag_snapshot.snapshot_is_complete(snap):
                self.report({"ERROR"}, "This result is missing download info. Search again.")
                return {"CANCELLED"}
            from ..ui_strings import source_auth_configured

            sid = (snap.get("source_id") or "").strip().lower()
            if sid in ("sketchfab", "polypizza", "fab") and not source_auth_configured(
                sid, cfg
            ):
                self.report(
                    {"WARNING"},
                    f"{sid}: sign-in required — Preferences → Online",
                )
                return {"CANCELLED"}
            self._snap = snap
            self._asset_type = snap["asset_type"]
            self._asset_name = snap["name"]
            self._icon_id = int(snap.get("icon_id") or 0) or thumbnails.placeholder_icon_id()

            # BlenderKit-style: reuse Online Downloads / cache before CDN
            from ..download_options import (
                preferred_format_from_ui,
                preferred_resolution_from_ui,
                resolve_download_resolution,
            )

            formats_snap = list(snap.get("formats") or [])
            preferred_res = preferred_resolution_from_ui(ui, cfg)
            preferred_fmt = preferred_format_from_ui(ui, formats_snap)
            try:
                src_probe = get_source(sid)
                from ..sources.base import AssetResult

                probe = AssetResult(
                    source_id=sid,
                    asset_id=str(snap.get("asset_id") or ""),
                    name=str(snap.get("name") or ""),
                    asset_type=str(snap.get("asset_type") or "mesh"),
                    formats=list(formats_snap),
                    resolutions=list(snap.get("resolutions") or []),
                    license_name=str(snap.get("license_name") or ""),
                )
                resolved_res = resolve_download_resolution(
                    sid,
                    probe,
                    preferred_res,
                    source=src_probe,
                    fmt=preferred_fmt,
                )
            except Exception:
                resolved_res = preferred_res
            reused_path, reused_origin = online_download.find_reusable_online_asset(
                cfg,
                source_id=sid,
                asset_id=str(snap.get("asset_id") or ""),
                asset_name=str(snap.get("name") or ""),
                asset_type=str(snap.get("asset_type") or "mesh"),
                resolution=str(resolved_res or ""),
                format=str(preferred_fmt or ""),
            )
            if reused_path:
                try:
                    path = online_download.prepare_reusable_asset_for_import(
                        reused_path,
                        reused_origin,
                        cfg,
                        source_id=sid,
                        asset_name=str(snap.get("name") or ""),
                        asset_type=str(snap.get("asset_type") or "mesh"),
                        asset_id=str(snap.get("asset_id") or ""),
                        thumb_url=str(snap.get("thumb_url") or ""),
                        resolution=str(resolved_res or ""),
                        format=str(preferred_fmt or ""),
                        preview_image=str(snap.get("thumb_path") or ""),
                    )
                except Exception as exc:  # noqa: BLE001
                    _report_error(self, exc)
                    return {"CANCELLED"}
                self._local_path = path
                self._state = "ready"
                where = "already in library" if reused_origin == "library" else "cached"
                self._header = (
                    f"UAL Drop: {snap['name']} ({where}) — release LMB to drop, Esc cancel"
                )
            else:
                if not download_progress.can_start_download():
                    self.report(
                        {"WARNING"},
                        "Too many downloads at once (max 3). Wait or cancel one.",
                    )
                    return {"CANCELLED"}
                self._header = f"UAL Drop: {snap['name']} (downloading… Esc cancel)"
                self._start_online_download(context, cfg, snap)
        else:
            if not ui.assets:
                self.report({"ERROR"}, "Select a library asset first — click the name under the preview")
                return {"CANCELLED"}
            from .. import selection_keys

            resolved = selection_keys.resolve_library_target(
                ui,
                target_key=str(self.target_key or ""),
                target_index=int(self.target_index),
            )
            if resolved is None:
                self.report(
                    {"ERROR"},
                    "Selected library asset is missing — select it again",
                )
                return {"CANCELLED"}
            _idx, item, _key = resolved
            del _idx, _key
            snap = drag_snapshot.snapshot_library_item(item)
            if not drag_snapshot.snapshot_is_complete(snap):
                self.report({"ERROR"}, "This asset has no file path. Select it again.")
                return {"CANCELLED"}
            if not drag_snapshot.library_path_ready_for_import(snap["path"]):
                self.report({"ERROR"}, "This file is missing. Rebuild Index or download it again.")
                return {"CANCELLED"}
            self._snap = snap
            self._local_path = snap["path"]
            self._asset_type = snap["asset_type"]
            self._asset_name = snap["name"]
            self._icon_id = int(snap.get("icon_id") or 0) or thumbnails.placeholder_icon_id()
            self._state = "ready"
            self._header = f"UAL Drop: {snap['name']} — release LMB to drop, Esc cancel"

        _active_drag_op = self
        context.window_manager.modal_handler_add(self)
        self._timer = context.window_manager.event_timer_add(0.1, window=context.window)
        try:
            from . import hover_preview

            hover_preview.set_drag_modal_active(True)
        except Exception:
            pass
        self._draw_handle = bpy.types.SpaceView3D.draw_handler_add(
            _draw_drag_ghost, (self,), "WINDOW", "POST_PIXEL"
        )
        context.workspace.status_text_set(self._header)
        # Force redraw so ghost appears immediately
        for area in context.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()
        return {"RUNNING_MODAL"}

    def _start_online_download(self, context, cfg, snap: dict) -> None:
        from ..online_auth import token_kwargs_for_source

        source_id = str(snap.get("source_id") or "")
        asset_id = str(snap.get("asset_id") or "")
        asset_name = str(snap.get("name") or asset_id or "Online asset")
        cache_dest = paths.source_cache_dir(cfg["cache_dir"], source_id, asset_id)
        paths.ensure_dir(cache_dest)

        token_kwargs = token_kwargs_for_source(source_id, cfg)

        task_id = download_progress.start_task(asset_name, phase="download")
        self._task_id = task_id
        self._state = "downloading"
        ui = _ui(context)
        ui.download_active = True
        ui.download_task_id = task_id
        _set_status(context, f"Downloading {asset_name}…", 0)
        _ensure_progress_timer()

        copy_to_library = bool((cfg.get("online") or {}).get("copy_to_library", True))
        holder = {"path": "", "error": "", "cancelled": False, "index_error": ""}
        # Snapshot N-panel overrides on the main thread (worker must not touch RNA)
        from ..download_options import (
            preferred_format_from_ui,
            preferred_resolution_from_ui,
            resolve_download_resolution,
        )

        formats_snap = list(snap.get("formats") or [])
        preferred_res = preferred_resolution_from_ui(ui, cfg)
        preferred_fmt = preferred_format_from_ui(ui, formats_snap)
        asset_type = str(snap.get("asset_type") or "mesh")
        license_name = str(snap.get("license_name") or "")
        thumb_url = str(snap.get("thumb_url") or "")
        thumb_path = str(snap.get("thumb_path") or "")
        resolutions_snap = list(snap.get("resolutions") or [])
        asset_key = str(snap.get("key") or "")

        def work():
            download_progress.set_active_task(task_id)
            try:
                from ..sources.base import AssetResult

                asset = AssetResult(
                    source_id=source_id,
                    asset_id=asset_id,
                    name=asset_name,
                    asset_type=asset_type,
                    formats=list(formats_snap),
                    resolutions=list(resolutions_snap),
                    license_name=license_name,
                )
                src = get_source(source_id)
                fmt = preferred_fmt
                res = resolve_download_resolution(
                    source_id,
                    asset,
                    preferred_res,
                    source=src,
                    fmt=fmt,
                )
                local = src.download(asset, res, fmt, cache_dest, **token_kwargs)
                if download_progress.is_cancelled(task_id):
                    holder["cancelled"] = True
                    return
                if copy_to_library and local:
                    download_progress.set_phase(task_id, "save", progress=90.0)
                    preview_src = ""
                    if thumb_url:
                        from .. import thumbnails as thumbs

                        preview_src = thumbs.fetch_online_thumb_to_disk(
                            cfg["cache_dir"],
                            source_id,
                            asset_id,
                            thumb_url,
                        ) or thumb_path or ""
                    local = online_download.register_and_stage_online_download(
                        local,
                        cfg,
                        source_id=source_id,
                        asset_name=asset_name,
                        asset_type=asset_type or "mesh",
                        preview_image=preview_src,
                        asset_id=asset_id,
                        thumb_url=thumb_url,
                        resolution=str(res or ""),
                        format=str(fmt or ""),
                    )
                holder["path"] = local
                if (cfg.get("online") or {}).get("index_downloads", True) and holder["path"]:
                    try:
                        from .. import indexer

                        indexer.scan_filesystem(
                            cfg["library_roots"],
                            cfg["cache_dir"],
                            force=True,
                            fetch_thumbs=False,
                        )
                    except Exception as exc:  # noqa: BLE001
                        # Worker-safe: do not touch bpy here. Surface on done().
                        holder["index_error"] = friendly_error(exc)
            except download_utils.DownloadCancelled:
                holder["cancelled"] = True
            except Exception as exc:  # noqa: BLE001
                holder["error"] = str(exc)
            finally:
                download_progress.set_active_task(None)

            def done():
                download_progress.finish_task(task_id)
                sync_download_progress_to_ui()
                if holder["cancelled"] or self._cancelled:
                    self._state = "done"
                    self._error = "cancelled"
                    return
                if holder["error"]:
                    self._state = "done"
                    self._error = holder["error"]
                    return
                # Reject if the user switched away from this online asset mid-download
                try:
                    live_ui = _ui(bpy.context)
                    from .. import drag_snapshot as ds

                    if live_ui is not None and asset_key and not ds.online_key_still_in_results(
                        live_ui.online_results, asset_key
                    ):
                        # Asset gone from results (new Search) — still allow drop of
                        # the downloaded file, but keep using the frozen snapshot path.
                        pass
                except Exception:
                    pass
                self._local_path = holder["path"] or ""
                self._state = "ready"
                self._header = f"UAL Drop: {asset_name} — release LMB to drop, Esc cancel"
                try:
                    bpy.context.workspace.status_text_set(self._header)
                except Exception:
                    pass
                index_msg = str(holder.get("index_error") or "").strip()
                if index_msg:
                    # Download+drop still proceed; Library grid may be stale until Rebuild Index.
                    note = "Downloaded, but the Library list didn't update. Rebuild Index."
                    try:
                        self.report({"WARNING"}, note)
                    except Exception:
                        pass
                    try:
                        _set_status(bpy.context, note, -1)
                    except Exception:
                        pass
                else:
                    try:
                        from .operators import _fill_library_list

                        _fill_library_list(bpy.context)
                    except Exception:
                        pass

            tasks_queue.add_task(done)

        tasks_queue.run_in_background(work)

    def _tag_ghost_areas(self, context) -> None:
        """Redraw only the 3D view under the cursor (and the previous one to clear)."""
        area, _region = _view3d_window_under_mouse(self._mouse_x, self._mouse_y)
        prev = self._last_ghost_area
        if prev is not None and prev != area:
            try:
                prev.tag_redraw()
            except Exception:
                pass
        if area is not None:
            try:
                area.tag_redraw()
            except Exception:
                pass
        self._last_ghost_area = area

    def modal(self, context, event):
        if event.type == "MOUSEMOVE":
            self._mouse_x = event.mouse_x
            self._mouse_y = event.mouse_y
            # Preview drop point via ghost only — do not move scene.cursor
            if self._state == "ready":
                loc, _hit = resolve_drop_location(context, self._mouse_x, self._mouse_y)
                self._drop_location = loc
            self._tag_ghost_areas(context)
            return {"RUNNING_MODAL"}

        if event.type == "ESC" and event.value == "PRESS":
            self._cancelled = True
            if self._task_id:
                download_progress.request_cancel(self._task_id)
            self._cleanup(context)
            self.report({"WARNING"}, "Drop cancelled")
            return {"CANCELLED"}

        if event.type == "TIMER":
            if self._state == "downloading":
                sync_download_progress_to_ui()
            if self._state == "done":
                self._cleanup(context)
                if self._error and self._error != "cancelled":
                    self.report({"ERROR"}, friendly_error(self._error))
                    return {"CANCELLED"}
                if self._error == "cancelled":
                    return {"CANCELLED"}
            return {"PASS_THROUGH"}

        # BlenderKit-style: drop on button release (works after drag-from-grid)
        if event.type == "LEFTMOUSE" and event.value == "RELEASE":
            if self._skip_releases > 0:
                self._skip_releases -= 1
                return {"RUNNING_MODAL"}
            if self._state == "ready":
                if not mouse_over_eligible_drop_view3d(self._mouse_x, self._mouse_y):
                    self._cancelled = True
                    if self._task_id:
                        download_progress.request_cancel(self._task_id)
                    self._cleanup(context)
                    self.report(
                        {"WARNING"},
                        "Drop cancelled — release over a 3D View (not Outliner/Properties)",
                    )
                    return {"CANCELLED"}
                return self._confirm_import(context)

        if self._state == "downloading":
            return {"RUNNING_MODAL"}

        return {"PASS_THROUGH"}

    def _confirm_import(self, context):
        from .. import drag_snapshot
        from .. import geo_import

        cfg = _cfg()
        snap = self._snap or {}
        path = self._local_path or str(snap.get("path") or "")
        if not path:
            self._cleanup(context)
            self.report({"ERROR"}, "Nothing to drop — select an asset first")
            return {"CANCELLED"}
        if not drag_snapshot.library_path_ready_for_import(path):
            self._cleanup(context)
            self.report({"ERROR"}, "This file is missing. Rebuild Index or download it again.")
            return {"CANCELLED"}
        # Online: allow drop even if a new Search cleared the grid — we already
        # downloaded using the frozen snapshot. Library keys must still match path.
        try:
            atype_hint = import_routing.resolve_effective_asset_type(
                path,
                self._asset_type or snap.get("asset_type") or "",
            )
            if not atype_hint:
                atype_hint = canonical_import_type(classify_path(path) or "")
            is_material = atype_hint in MATERIAL_DROP_TYPES
            want_origin = bool(
                (cfg.get("import") or {}).get("place_at_world_origin", True)
            )
            # Materials need the mesh under the cursor. Do not use
            # resolve_drop_location: grazing hits discard the object (mesh
            # placement fallback) and the texture never lands on the cube.
            if is_material:
                hit = raycast_viewport_object(context, self._mouse_x, self._mouse_y)
                target = hit.get("object") if hit else None
                face_index = hit.get("face_index") if hit else None
                hit_loc = hit.get("location") if hit else None
            else:
                drop_loc, hit = resolve_drop_location(
                    context, self._mouse_x, self._mouse_y
                )
                target = None
                face_index = None
                hit_loc = hit.get("location") if hit else drop_loc
            result = import_routing.perform_typed_import(
                path,
                asset_type=atype_hint or None,
                cfg=cfg,
                target_object=target if is_material else None,
                face_index=face_index if is_material else None,
                place_at_origin=(want_origin and not is_material),
                asset_name=str(
                    self._asset_name or snap.get("name") or ""
                ),
            )
            if not result.get("ok"):
                self._cleanup(context)
                err = result.get("error") or "Drop import failed"
                _set_status(context, err)
                self.report({"ERROR"}, err)
                return {"CANCELLED"}
            objs = result.get("objects") or []
            if objs and not is_material:
                from ..megascans_billboard import filter_alive_objects

                objs = filter_alive_objects(objs)
                if want_origin:
                    # Ensure base-on-ground at (0,0,0) even if import skipped
                    geo_import.place_at_world_origin(objs, snap_base=True)
                elif hit_loc is not None:
                    _place_at_location(objs, hit_loc)
                elif self._drop_location is not None:
                    _place_at_location(objs, self._drop_location)
            atype = canonical_import_type(
                result.get("asset_type") or atype_hint or self._asset_type
            )
            if atype in MATERIAL_DROP_TYPES:
                if result.get("material") is None:
                    _set_status(context, "No PBR maps found in this package", -1)
                    self.report(
                        {"WARNING"},
                        "No PBR maps found — check the download folder / source",
                    )
                elif target is not None:
                    _set_status(context, f"Material applied to {target.name}", -1)
                    self.report({"INFO"}, f"Material applied to {target.name}")
                else:
                    _set_status(context, "Material built (select a mesh to assign)", -1)
                    self.report({"INFO"}, "Material built — select a mesh or drop onto one")
            else:
                where = "at world origin" if want_origin else (
                    "at drop point" if hit_loc is not None else "in view"
                )
                _set_status(context, f"Drop import complete ({where})", -1)
                self.report({"INFO"}, f"Asset dropped into scene ({where})")
        except Exception as exc:  # noqa: BLE001
            self._cleanup(context)
            _report_error(self, exc)
            return {"CANCELLED"}
        self._cleanup(context)
        return {"FINISHED"}

    def _cleanup(self, context):
        global _active_drag_op
        if self._timer is not None:
            try:
                context.window_manager.event_timer_remove(self._timer)
            except Exception:
                pass
            self._timer = None
        if self._draw_handle is not None:
            try:
                bpy.types.SpaceView3D.draw_handler_remove(self._draw_handle, "WINDOW")
            except Exception:
                pass
            self._draw_handle = None
        try:
            from . import hover_preview

            # Always clear on teardown (cancel / success / error) — ending drag
            # must not unmask a selection-armed GPU tooltip over the viewport.
            hover_preview.set_drag_modal_active(False)
            hover_preview.clear_hover_target()
        except Exception:
            pass
        try:
            context.workspace.status_text_set(None)
        except Exception:
            pass
        try:
            for area in context.screen.areas:
                if area.type == "VIEW_3D":
                    area.tag_redraw()
        except Exception:
            pass
        if _active_drag_op is self:
            _active_drag_op = None


class UAL_MT_pie(bpy.types.Menu):
    bl_label = "UAL"
    bl_idname = "UAL_MT_pie"

    def draw(self, context):
        layout = self.layout
        pie = layout.menu_pie()
        pie.operator("ual.import_selected", icon=ACTION_ICONS["import"])
        pie.operator("ual.asset_drag_drop", icon=ACTION_ICONS["drop"])
        pie.operator("ual.material_blend_create", icon=ACTION_ICONS.get("material_blend", "NODE_MATERIAL"))
        pie.operator("ual.online_search", icon=ACTION_ICONS["search"])
        pie.operator("ual.drop_import", icon=ACTION_ICONS["drop_from_disk"])
        pie.operator("ual.download_cancel", icon=ACTION_ICONS["cancel"])


addon_keymaps = []


def register():
    bpy.utils.register_class(UAL_OT_drop_import)
    bpy.utils.register_class(UAL_OT_asset_drag_drop)
    bpy.utils.register_class(UAL_MT_pie)
    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon
    if kc:
        km = kc.keymaps.new(name="3D View", space_type="VIEW_3D")
        kmi = km.keymap_items.new("wm.call_menu_pie", "A", "PRESS", ctrl=True, shift=True)
        kmi.properties.name = "UAL_MT_pie"
        addon_keymaps.append((km, kmi))


def unregister():
    force_cancel_active(None)
    for km, kmi in addon_keymaps:
        km.keymap_items.remove(kmi)
    addon_keymaps.clear()
    bpy.utils.unregister_class(UAL_MT_pie)
    bpy.utils.unregister_class(UAL_OT_asset_drag_drop)
    bpy.utils.unregister_class(UAL_OT_drop_import)
