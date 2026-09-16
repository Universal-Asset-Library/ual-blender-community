# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""Operators: index, import, search, download, favorites, health."""

from __future__ import annotations

import os
from typing import List, Optional, Tuple

import bpy

from .. import config as ual_config
from .. import download_progress
from .. import download_utils
from .. import indexer
from .. import import_routing
from .. import online_download
from .. import paths
from .. import preferences
from .. import tasks_queue
from .. import thumbnails
from ..sources import get_source
from ..ui_strings import (
    MSG_CACHE_MISSING,
    MSG_EMPTY_DOWNLOAD,
    MSG_FAB_SIGNIN,
    MSG_NO_ASSETS,
    MSG_ONLINE_ACCESS,
    MSG_SEARCH_FIRST,
    MSG_SELECT_ASSET,
    MSG_USE_DOWNLOAD,
    friendly_error,
    friendly_status,
)


def _ui(context):
    from .. import preferences

    return preferences.get_wm_ui(context)


def _set_status(context, message: str, progress: int = -1, *, ui=None) -> None:
    """Update N-panel status — skip while a download owns the progress chrome.

    Pass *ui* from timers / RNA updates — ``bpy.context`` is often restricted
    there, and ``_ui(context)`` would throw and abort the search.
    """
    if ui is None:
        try:
            ui = _ui(context)
        except Exception:
            ui = None
    if ui is None:
        return
    try:
        if download_progress.has_active_tasks() or bool(
            getattr(ui, "download_active", False)
        ):
            return
    except Exception:
        pass
    try:
        ui.status_message = friendly_status(message)
        ui.status_progress = progress
    except Exception:
        pass


def _report_error(op, exc_or_text) -> None:
    op.report({"ERROR"}, friendly_error(exc_or_text))


def _cfg():
    return preferences.prefs_to_config()


def _tag_redraw_all() -> None:
    try:
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == "VIEW_3D":
                    area.tag_redraw()
    except Exception:
        pass


def _library_selected_item(ui):
    """Resolve Library/Favorites selection by stable key (not a stale index)."""
    from .. import selection_keys

    resolved = selection_keys.resolve_library_target(ui)
    if resolved is None:
        return None
    _idx, item, _key = resolved
    return item


def sync_download_progress_to_ui() -> None:
    """Copy download_progress snapshot onto WindowManager UI props (main thread).

    The N-panel bar uses a lerped ``status_progress_display`` so fills animate
    instead of jumping 1% per redraw. Unknown-size downloads pulse (indeterminate).
    """
    try:
        ui = preferences.get_wm_ui()
    except Exception:
        return
    if ui is None:
        return
    tasks = download_progress.snapshot_tasks()
    display = float(getattr(ui, "status_progress_display", 0.0) or 0.0)
    if not tasks:
        ui.download_active = False
        ui.download_task_id = ""
        ui.download_label = ""
        # Ease the bar to empty, then hide (status_progress = -1).
        if display > 0.02:
            ui.status_progress_display = download_progress.lerp_display_factor(
                display, 0.0, rate=0.45
            )
            ui.status_progress = 0
        else:
            ui.status_progress_display = 0.0
            ui.status_progress = -1
        _tag_redraw_all()
        return
    task = tasks[0]
    ui.download_active = True
    ui.download_task_id = task["task_id"]
    ui.download_label = download_progress.progress_label(task)
    ui.status_message = ui.download_label
    target, indeterminate = download_progress.task_bar_target_factor(task)
    ui.status_progress = int(round(target * 100.0)) if not indeterminate else max(
        0, int(task.get("progress") or 0)
    )
    if indeterminate:
        ui.status_progress_display = target
    else:
        ui.status_progress_display = download_progress.lerp_display_factor(display, target)
    _tag_redraw_all()


def _progress_poll_timer() -> float | None:
    active = download_progress.has_active_tasks()
    sync_download_progress_to_ui()
    if not active:
        display = 0.0
        try:
            ui = preferences.get_wm_ui()
            display = float(getattr(ui, "status_progress_display", 0.0) or 0.0) if ui else 0.0
        except Exception:
            display = 0.0
        if display > 0.02:
            return 0.05  # finish ease-out
        return None
    return 0.05


def _ensure_progress_timer() -> None:
    if not bpy.app.timers.is_registered(_progress_poll_timer):
        bpy.app.timers.register(_progress_poll_timer, first_interval=0.1)


def _attach_library_thumbs(ui, cache_dir: str, *, limit: int = 250) -> None:
    """Main-thread: resolve preview files and load PreviewCollection icons."""
    for index, item in enumerate(ui.assets):
        if index >= limit:
            break
        thumb = thumbnails.resolve_local_thumb_file(
            cache_dir,
            item.path,
            item.asset_type,
            item.thumb_path,
        )
        if thumb:
            item.thumb_path = thumb
            item.preview_icon_id = thumbnails.icon_id_for_file(thumb, kind="local")
        else:
            # Grid uses square placeholder via resolve_grid_icon_id
            item.preview_icon_id = 0


_lib_search_timer = None
_LIB_SEARCH_DEBOUNCE_S = 0.22
_online_search_timer = None
_ONLINE_SEARCH_DEBOUNCE_S = 0.12
_online_auto_search_armed = False


def arm_online_auto_search() -> None:
    """Call after WM restore on enable so Type/Source/Enter may hit the network."""
    global _online_auto_search_armed
    _online_auto_search_armed = True


def disarm_online_auto_search() -> None:
    """Block auto-search during register/unregister; cancel a queued timer."""
    global _online_auto_search_armed, _online_search_timer
    _online_auto_search_armed = False
    if _online_search_timer is not None:
        try:
            if bpy.app.timers.is_registered(_online_search_timer):
                bpy.app.timers.unregister(_online_search_timer)
        except Exception:
            pass
        _online_search_timer = None


def _online_query_text(ui) -> str:
    from ..online_search_trigger import online_query_text

    return online_query_text(ui)


def schedule_online_search(context=None, ui=None, *, force: bool = False) -> bool:
    """Debounced Online search (Type/Source/Enter coalesce into one request).

    Returns True when a timer was queued. Pass the WM PropertyGroup as *ui*
    from RNA updates — ``bpy.context`` in a timer often cannot run ``bpy.ops``.
    ``force=True`` for user Source/Type switches (must populate even if unarmed).
    """
    global _online_search_timer
    from ..online_search_trigger import should_schedule_online_search

    if ui is None:
        try:
            ui = _ui(context) if context is not None else _ui(bpy.context)
        except Exception:
            ui = None
    scope = str(getattr(ui, "scope", "") or "") if ui is not None else ""
    if not should_schedule_online_search(scope, armed=_online_auto_search_armed, force=force):
        return False

    captured = ui

    def _run(live) -> None:
        try:
            ctx = context if context is not None else bpy.context
        except Exception:
            ctx = None
        try:
            run_online_search(ctx, live)
        except Exception as exc:
            print(f"[UAL] online search: {exc}")

    def _fire():
        global _online_search_timer
        _online_search_timer = None
        live = captured
        try:
            from .. import preferences as prefs_mod

            if live is None or not prefs_mod._ui_is_valid(live):
                try:
                    live = _ui(bpy.context)
                except Exception:
                    live = captured
        except Exception:
            pass
        if live is None or str(getattr(live, "scope", "") or "") != "online":
            return None
        _run(live)
        return None

    if _online_search_timer is not None and bpy.app.timers.is_registered(_online_search_timer):
        try:
            bpy.app.timers.unregister(_online_search_timer)
        except Exception:
            pass
    _online_search_timer = _fire
    try:
        bpy.app.timers.register(_fire, first_interval=_ONLINE_SEARCH_DEBOUNCE_S)
    except Exception:
        _online_search_timer = None
        _run(captured)
    return True


def _fill_library_list(context) -> None:
    from . import hover_preview
    from .. import db as ual_db

    ui = _ui(context)
    cfg = _cfg()
    cache = cfg["cache_dir"]
    query = ui.search_query or ""
    atype = ui.filter_type
    favs = None
    if ui.scope == "favorites":
        favs = list(cfg.get("favorites") or [])
    # Live search rebuilds this often — suppress hover spam / popup lag
    hover_preview.set_suppress_selection_hover(True)
    try:
        try:
            rows = indexer.list_assets(
                cache,
                query=query,
                asset_type=atype,
                favorites=favs,
            )
        except RuntimeError as exc:
            msg = str(exc).strip() or ual_db.SHARED_CACHE_SCHEMA_ERROR
            if "assets.db" in msg.lower() or "cache" in msg.lower():
                ui.assets.clear()
                _set_status(context, msg[:240], ui=ui)
                return
            raise
        ui.assets.clear()
        for row in rows:
            item = ui.assets.add()
            item.name = row.get("name") or ""
            item.path = row.get("path") or ""
            item.asset_type = row.get("asset_type") or "mesh"
            item.thumb_path = row.get("thumb_path") or ""
            item.source_id = row.get("source_id") or "local"
            item.preview_icon_id = 0
        if ui.assets:
            from .. import selection_keys

            selection_keys.sync_library_selection(ui)
        else:
            ui.selected_index = 0
            try:
                ui.selected_key = ""
            except Exception:
                pass
        _attach_library_thumbs(ui, cache)
        # Live count is folded into Assets header via assets_header_label — not status_message
    finally:
        hover_preview.set_suppress_selection_hover(False)
        reset_assets_view_scroll(context)
        # List geometry changed — drop stale hover hit-cache (keep hover target clear)
        hover_preview.invalidate_assets_hit(clear_hover=True)


def schedule_library_list_fill(context=None) -> None:
    """Debounce Library/Favorites live search so hover isn't cleared every keystroke."""
    global _lib_search_timer

    def _fire():
        global _lib_search_timer
        _lib_search_timer = None
        try:
            _fill_library_list(bpy.context)
        except Exception:
            pass
        return None

    if _lib_search_timer is not None and bpy.app.timers.is_registered(_lib_search_timer):
        try:
            bpy.app.timers.unregister(_lib_search_timer)
        except Exception:
            pass
    _lib_search_timer = _fire
    bpy.app.timers.register(_fire, first_interval=_LIB_SEARCH_DEBOUNCE_S)


def _resolve_lazy_online_thumb_url(source_id: str, asset_id: str, thumb_url: str = "") -> Tuple[str, List[str]]:
    """Return ``(primary_url, alt_urls)`` — Three D Scans enriches media lazily."""
    url = (thumb_url or "").strip()
    alts: List[str] = []
    sid = (source_id or "").strip().lower()
    if sid == "threedscans":
        try:
            from ..sources import get_source

            src = get_source("threedscans")
            if not url and hasattr(src, "resolve_thumb_url"):
                url = str(src.resolve_thumb_url(asset_id) or "").strip()
            if hasattr(src, "_enrich"):
                media = src._enrich(asset_id)  # noqa: SLF001 — shared enrich cache
                if isinstance(media, dict):
                    if not url:
                        url = str(media.get("thumb_url") or "").strip()
                    raw_alts = media.get("thumb_urls") or []
                    if isinstance(raw_alts, list):
                        alts = [str(u).strip() for u in raw_alts if str(u).strip()]
        except Exception:
            pass
    if url and url not in alts:
        alts = [url] + [u for u in alts if u != url]
    elif url:
        alts = [url]
    return url, alts


def _fetch_online_thumb_with_alts(
    cache: str,
    source_id: str,
    asset_id: str,
    primary: str,
    alts: Optional[List[str]] = None,
) -> Tuple[str, str]:
    """Try primary then alternate URLs. Returns ``(disk_path, used_url)``."""
    candidates: List[str] = []
    for u in [primary] + list(alts or []):
        u = (u or "").strip()
        if u and u not in candidates:
            candidates.append(u)
    # Always try once with empty URL so source-aware compact templates can run
    # (Poly Haven sized CDN) even when search omitted thumb_url.
    if not candidates:
        candidates = [""]
    for url in candidates:
        disk = thumbnails.fetch_online_thumb_to_disk(cache, source_id, asset_id, url)
        if disk:
            return disk, url or primary or ""
    return "", ""


_online_thumb_busy = False
_online_thumb_job = 0
# Ignore a second Load More click while the next site page is still fetching.
_online_load_more_busy = False
_online_load_more_gen = 0


def _paint_online_disk_hits(ui, cache: str) -> int:
    """Main-thread: load PreviewCollection for rows that already have disk thumbs."""
    painted = 0
    for item in ui.online_results:
        if int(getattr(item, "preview_icon_id", 0) or 0):
            continue
        sid = getattr(item, "source_id", "") or ""
        aid = getattr(item, "asset_id", "") or ""
        if not sid or not aid:
            continue
        disk = thumbnails.online_thumb_disk_path(cache, sid, aid)
        icon = thumbnails.icon_id_for_online(
            cache, sid, aid, getattr(item, "thumb_url", "") or ""
        )
        if icon:
            item.preview_icon_id = icon
            item.thumb_path = disk
            painted += 1
    return painted


def _schedule_online_thumb_fetch(context, *, priority_index: int = -1, ui=None) -> None:
    """Background fetch online thumbs → disk; main thread loads icons.

    Parallel workers (Houdini parity) paint each tile as soon as its download
    finishes — not after an entire serial batch. Prefer the **scrolled**
    viewport window, then selection proximity, then the rest.

    Three D Scans: search leaves ``thumb_url`` empty — lazy-enrich WP media here.
    """
    global _online_thumb_busy, _online_thumb_job

    if ui is None:
        try:
            ui = _ui(context)
        except Exception:
            ui = None
    if ui is None:
        return
    cfg = _cfg()
    cache = cfg["cache_dir"]
    search_gen = int(getattr(ui, "online_search_gen", 0) or 0)
    expected_source = str(getattr(ui, "online_source", "") or "")

    # Instant paint for anything already on disk (second Search / prior session)
    disk_painted = _paint_online_disk_hits(ui, cache)
    if disk_painted:
        _tag_redraw_all()

    snapshot = []
    for i, item in enumerate(ui.online_results):
        if int(getattr(item, "preview_icon_id", 0) or 0):
            continue
        url = (getattr(item, "thumb_url", "") or "").strip()
        sid = (item.source_id or "").strip().lower()
        if url or sid in {"threedscans", "polyhaven"}:
            snapshot.append((i, item.source_id, item.asset_id, url))
    if not snapshot:
        _set_status(context, "Ready", ui=ui)
        return

    selected = int(getattr(ui, "online_selected_index", 0) or 0)
    scroll = int(getattr(ui, "assets_view_scroll", 0) or 0)
    try:
        from . import asset_grid

        cols, _scale, _ui_x = asset_grid.grid_metrics(context)
        cols = max(1, int(cols or 4))
    except Exception:
        cols = 4
    visible_window = max(cols * 4, 24)
    prio = int(priority_index)
    batch_size = int(getattr(thumbnails, "ONLINE_THUMB_BATCH_SIZE", 12) or 12)
    workers = int(getattr(thumbnails, "ONLINE_THUMB_FETCH_WORKERS", 6) or 6)

    def _priority(entry):
        index = entry[0]
        if prio >= 0 and index == prio:
            return (-1, 0, index)
        # Visible viewport window (scroll-based), then near selection
        if scroll <= index < scroll + visible_window:
            return (0, index - scroll, index)
        dist_sel = abs(index - selected)
        if dist_sel <= visible_window:
            return (1, dist_sel, index)
        return (2, abs(index - scroll), index)

    snapshot.sort(key=_priority)
    batch = snapshot[: max(1, batch_size)]
    more_pending = len(snapshot) > len(batch)
    _online_thumb_job += 1
    job = _online_thumb_job
    _online_thumb_busy = True
    _set_status(context, "Loading previews…", ui=ui)

    def work():
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def fetch_one(entry):
            index, source_id, asset_id, url = entry
            primary, alts = _resolve_lazy_online_thumb_url(source_id, asset_id, url)
            disk, used = _fetch_online_thumb_with_alts(
                cache, source_id, asset_id, primary, alts
            )
            return index, disk, used, str(source_id), str(asset_id)

        def paint_one(index, disk, used, source_id, asset_id):
            if job != _online_thumb_job:
                return
            if int(getattr(ui, "online_search_gen", 0) or 0) != search_gen:
                return
            if expected_source and str(getattr(ui, "online_source", "") or "") != expected_source:
                return
            if index >= len(ui.online_results):
                return
            item = ui.online_results[index]
            if (
                str(item.source_id or "") != source_id
                or str(item.asset_id or "") != asset_id
            ):
                return
            if not disk:
                return
            item.thumb_path = disk
            if used and not (item.thumb_url or "").strip():
                item.thumb_url = used
            item.preview_icon_id = thumbnails.icon_id_for_file(disk, kind="online")
            _tag_redraw_all()

        try:
            with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
                futures = [pool.submit(fetch_one, entry) for entry in batch]
                for fut in as_completed(futures):
                    try:
                        index, disk, used, source_id, asset_id = fut.result()
                    except Exception:
                        continue
                    tasks_queue.add_task(
                        lambda i=index, d=disk, u=used, s=source_id, a=asset_id: paint_one(
                            i, d, u, s, a
                        )
                    )
        finally:

            def after_batch():
                global _online_thumb_busy
                if job != _online_thumb_job:
                    return
                _online_thumb_busy = False
                if int(getattr(ui, "online_search_gen", 0) or 0) != search_gen:
                    return
                if expected_source and str(
                    getattr(ui, "online_source", "") or ""
                ) != expected_source:
                    return
                if more_pending:
                    _set_status(context, "Loading previews…", ui=ui)
                    _tag_redraw_all()
                    _schedule_online_thumb_fetch(
                        context, priority_index=priority_index, ui=ui
                    )
                else:
                    _set_status(context, "Ready", ui=ui)
                    _tag_redraw_all()

            tasks_queue.add_task(after_batch)

    tasks_queue.run_in_background(work)


def prioritize_online_thumb_fetch(context, index: int) -> None:
    """Fetch / upgrade a single Online result thumb for hover dwell."""
    ui = _ui(context)
    idx = int(index)
    if idx < 0 or idx >= len(ui.online_results):
        return
    item = ui.online_results[idx]
    path = (getattr(item, "thumb_path", "") or "").strip()
    # Disk thumb already present — load hover-quality preview (grid may be small CDN).
    if path and os.path.isfile(path):
        try:
            hover_icon = thumbnails.icon_id_for_file(path, kind="hover")
            if hover_icon:
                item.preview_icon_id = hover_icon
                _tag_redraw_all()
        except Exception:
            pass
        return
    sid = (item.source_id or "").strip().lower()
    if not (getattr(item, "thumb_url", "") or "") and sid not in {
        "threedscans",
        "polyhaven",
    }:
        return
    cfg = _cfg()
    cache = cfg["cache_dir"]
    source_id = item.source_id
    asset_id = item.asset_id
    url = item.thumb_url or ""
    holder = {"disk": "", "url": ""}

    def work():
        primary, alts = _resolve_lazy_online_thumb_url(source_id, asset_id, url)
        holder["disk"], holder["url"] = _fetch_online_thumb_with_alts(
            cache, source_id, asset_id, primary, alts
        )

        def done():
            if idx >= len(ui.online_results):
                return
            cur = ui.online_results[idx]
            if holder["disk"]:
                cur.thumb_path = holder["disk"]
                if holder["url"] and not (cur.thumb_url or "").strip():
                    cur.thumb_url = holder["url"]
                cur.preview_icon_id = thumbnails.icon_id_for_file(
                    holder["disk"], kind="hover"
                ) or thumbnails.icon_id_for_file(holder["disk"], kind="online")
                _tag_redraw_all()

        tasks_queue.add_task(done)

    tasks_queue.run_in_background(work)


class UAL_OT_sync_prefs_to_config(bpy.types.Operator):
    bl_idname = "ual.sync_prefs_to_config"
    bl_label = "Force Sync Settings"
    bl_description = (
        "Write Preferences to settings.json immediately "
        "(usually unnecessary — options auto-save on change)"
    )
    bl_options = {"REGISTER"}

    def execute(self, context):
        preferences.ensure_storage_defaults()
        cfg = _cfg()
        ual_config.save_config(cfg, cfg["cache_dir"])
        self.report({"INFO"}, "UAL settings synced to disk")
        return {"FINISHED"}


class UAL_OT_open_preferences(bpy.types.Operator):
    """Jump to Preferences → Extensions → Universal Asset Library (all settings live there)."""

    bl_idname = "ual.open_preferences"
    bl_label = "Open Preferences"
    bl_description = "Open Universal Asset Library Preferences (library folders, cache, online keys)"
    bl_options = {"REGISTER"}

    def execute(self, context):
        module = preferences.__package__
        try:
            bpy.ops.preferences.addon_show(module=module)
        except Exception:
            bpy.ops.screen.userpref_show("INVOKE_DEFAULT")
            self.report(
                {"INFO"},
                "Open Get Extensions → Installed → Universal Asset Library to edit settings",
            )
            return {"FINISHED"}
        self.report({"INFO"}, "UAL settings are in Preferences (not the sidebar panel)")
        return {"FINISHED"}


class UAL_OT_auth_required(bpy.types.Operator):
    """Lock badge / empty-state CTA — opens Preferences on the Online tab."""

    bl_idname = "ual.auth_required"
    bl_label = "Sign in required"
    bl_description = (
        "This source needs sign-in. Opens Preferences → Online "
        "(Sketchfab token, Poly Pizza key, or Epic for Fab)."
    )
    bl_options = {"REGISTER"}

    source_id: bpy.props.StringProperty(default="", options={"SKIP_SAVE", "HIDDEN"})

    def execute(self, context):
        try:
            prefs = preferences._get_prefs()
            prefs.preferences_tab = "ONLINE"
        except Exception:
            pass
        sid = (self.source_id or "").strip().lower()
        if sid == "sketchfab":
            self.report(
                {"INFO"},
                "Sketchfab needs an API token. Preferences → Online → Get Sketchfab Token.",
            )
        elif sid == "polypizza":
            self.report(
                {"INFO"},
                "Poly Pizza needs an API key. Preferences → Online → Get Poly Pizza API Key.",
            )
        elif sid == "fab":
            self.report({"INFO"}, MSG_FAB_SIGNIN)
        return bpy.ops.ual.open_preferences()


class UAL_OT_clear_type_filter(bpy.types.Operator):
    bl_idname = "ual.clear_type_filter"
    bl_label = "Type = All"
    bl_description = "Clear the Type filter so this source can show every catalog it has"
    bl_options = {"REGISTER"}

    def execute(self, context):
        ui = _ui(context)
        if ui is None:
            return {"CANCELLED"}
        ui.filter_type = "ALL"
        return {"FINISHED"}


class UAL_OT_prefs_add_library_root(bpy.types.Operator):
    bl_idname = "ual.prefs_add_library_root"
    bl_label = "Add Library Root"
    bl_options = {"REGISTER"}

    directory: bpy.props.StringProperty(subtype="DIR_PATH")

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        prefs = preferences._get_prefs()
        path = (self.directory or "").strip()
        if not path:
            return {"CANCELLED"}
        item = prefs.library_roots.add()
        item.path = os.path.normpath(path)
        prefs.library_roots_index = len(prefs.library_roots) - 1
        if not (prefs.library_root or "").strip():
            prefs.library_root = item.path
        preferences.ensure_storage_defaults()
        preferences.save_prefs_config()
        return {"FINISHED"}


class UAL_OT_prefs_remove_library_root(bpy.types.Operator):
    bl_idname = "ual.prefs_remove_library_root"
    bl_label = "Remove Library Root"
    bl_options = {"REGISTER"}

    def execute(self, context):
        prefs = preferences._get_prefs()
        idx = prefs.library_roots_index
        if idx < 0 or idx >= len(prefs.library_roots):
            return {"CANCELLED"}
        prefs.library_roots.remove(idx)
        prefs.library_roots_index = max(0, min(idx, len(prefs.library_roots) - 1))
        preferences.ensure_storage_defaults()
        preferences.save_prefs_config()
        return {"FINISHED"}


class UAL_OT_open_cache_folder(bpy.types.Operator):
    bl_idname = "ual.open_cache_folder"
    bl_label = "Open Cache Folder"
    bl_options = {"REGISTER"}

    def execute(self, context):
        cfg = _cfg()
        path = cfg.get("cache_dir") or ""
        if not path or not os.path.isdir(path):
            self.report({"ERROR"}, MSG_CACHE_MISSING)
            return {"CANCELLED"}
        bpy.ops.wm.path_open(filepath=path)
        return {"FINISHED"}


class UAL_OT_show_cache_usage(bpy.types.Operator):
    """Report sizes for thumbs, online staging, catalogs, and the SQLite index."""

    bl_idname = "ual.show_cache_usage"
    bl_label = "Show Cache Usage"
    bl_options = {"REGISTER"}

    def execute(self, context):
        from .. import cache_maintenance as cm

        cfg = _cfg()
        cache = cfg.get("cache_dir") or ""
        usage = cm.cache_usage_summary(cache)
        lines = [
            "Cache: {}".format(usage.get("cache_dir") or "(unset)"),
            "Thumbs: {} ({}) — online browse {} · local {}".format(
                cm.format_bytes(int(usage.get("thumbs_bytes") or 0)),
                int(usage.get("thumbs_files") or 0),
                cm.format_bytes(int(usage.get("online_thumb_bytes") or 0)),
                cm.format_bytes(int(usage.get("local_thumb_bytes") or 0)),
            ),
            "Online staging: {} ({} files)".format(
                cm.format_bytes(int(usage.get("staging_bytes") or 0)),
                int(usage.get("staging_files") or 0),
            ),
            "Catalog JSON: {} ({} files)".format(
                cm.format_bytes(int(usage.get("catalog_bytes") or 0)),
                int(usage.get("catalog_files") or 0),
            ),
            "Index assets.db: {}".format(
                cm.format_bytes(int(usage.get("db_bytes") or 0))
            ),
        ]
        msg = " · ".join(lines)
        _set_status(context, msg[:240])
        self.report({"INFO"}, msg[:512])
        _tag_redraw_all()
        return {"FINISHED"}


def _cfg_copy_to_library(cfg: dict) -> bool:
    online = cfg.get("online") or {}
    if "copy_to_library" in online:
        return bool(online.get("copy_to_library"))
    return bool(cfg.get("copy_to_library", True))


class UAL_OT_clear_cache(bpy.types.Operator):
    """Clear regenerable cache data — never deletes library-root asset files or assets.db."""

    bl_idname = "ual.clear_cache"
    bl_label = "Clear Cache"
    bl_options = {"REGISTER"}

    mode: bpy.props.EnumProperty(
        name="What to clear",
        items=(
            (
                "MEMORY",
                "Memory Caches",
                "PreviewCollection, search LRU, source singletons, asset-bar textures — no disk delete",
            ),
            (
                "ONLINE_THUMBS",
                "Online Browse Previews",
                "Delete online_*.png under cache/thumbs — re-download when browsing Online",
            ),
            (
                "ALL_THUMBS",
                "All Preview Images",
                "Delete all files under cache/thumbs (local + online). Keeps the asset index",
            ),
            (
                "CATALOGS",
                "Online Catalog / Enrich Cache",
                "Clear search LRU + source enrich singletons + any _catalog*.json on disk",
            ),
            (
                "STAGING",
                "Online Download Staging",
                "Delete sources/<provider>/ CDN files + flush memory. WARNING if Copy to library is off",
            ),
            (
                "REGENERABLE",
                "Regenerable Cache",
                "Thumbs + catalog JSON + memory. Keeps index and download staging",
            ),
            (
                "ALL_KEEP_INDEX",
                "All Cache (Keep Index)",
                "Thumbs + staging + catalogs + memory. Keeps assets.db only",
            ),
        ),
        default="REGENERABLE",
    )
    confirm_destructive: bpy.props.BoolProperty(
        name="I understand staging may be my only copies",
        description="Required when Copy to library is OFF and clearing Staging / All Cache",
        default=False,
    )

    def invoke(self, context, event):
        self.confirm_destructive = False
        return context.window_manager.invoke_props_dialog(self, width=420)

    def draw(self, context):
        from .. import cache_maintenance as cm

        layout = self.layout
        layout.prop(self, "mode")
        cfg = _cfg()
        cache = cfg.get("cache_dir") or ""
        usage = cm.cache_usage_summary(cache)
        copy_on = _cfg_copy_to_library(cfg)
        col = layout.column(align=True)
        col.scale_y = 0.85
        mode = self.mode
        if mode == "MEMORY":
            col.label(text="No disk files will be deleted.")
        elif mode == "ONLINE_THUMBS":
            col.label(
                text="Will remove ~{} ({} files).".format(
                    cm.format_bytes(int(usage.get("online_thumb_bytes") or 0)),
                    int(usage.get("online_thumb_files") or 0),
                )
            )
        elif mode == "ALL_THUMBS":
            col.label(
                text="Will remove ~{} ({} files).".format(
                    cm.format_bytes(int(usage.get("thumbs_bytes") or 0)),
                    int(usage.get("thumbs_files") or 0),
                )
            )
            col.label(text="Index thumb paths reattach to library Preview/ when present.")
        elif mode == "CATALOGS":
            col.label(text="Clears search LRU + source enrich memory.")
            col.label(
                text="Disk catalogs: ~{} ({} files).".format(
                    cm.format_bytes(int(usage.get("catalog_bytes") or 0)),
                    int(usage.get("catalog_files") or 0),
                )
            )
        elif mode == "STAGING":
            col.label(
                text="Will remove ~{} ({} files).".format(
                    cm.format_bytes(int(usage.get("staging_bytes") or 0)),
                    int(usage.get("staging_files") or 0),
                ),
                icon="ERROR",
            )
            if not copy_on:
                col.label(
                    text="Copy to library is OFF — staging may be your only copies.",
                    icon="ERROR",
                )
                layout.prop(self, "confirm_destructive")
        elif mode == "REGENERABLE":
            col.label(
                text="Will remove ~{} regenerable data.".format(
                    cm.format_bytes(int(usage.get("regenerable_bytes") or 0))
                )
            )
        else:
            col.label(
                text="Will remove ~{} (keeps assets.db).".format(
                    cm.format_bytes(int(usage.get("all_except_index_bytes") or 0))
                ),
                icon="ERROR",
            )
            if not copy_on:
                col.label(
                    text="Copy to library is OFF — staging wipe is destructive.",
                    icon="ERROR",
                )
                layout.prop(self, "confirm_destructive")
        col.label(text="Library roots and Online Downloads are never deleted.")

    def execute(self, context):
        from .. import cache_maintenance as cm

        cfg = _cfg()
        cache = cfg.get("cache_dir") or ""
        if not cache:
            self.report({"ERROR"}, MSG_CACHE_MISSING)
            return {"CANCELLED"}
        mode = self.mode
        copy_on = _cfg_copy_to_library(cfg)
        if mode in {"STAGING", "ALL_KEEP_INDEX"} and not copy_on and not self.confirm_destructive:
            self.report(
                {"ERROR"},
                "Confirm the checkbox — Copy to library is OFF and staging may hold your only copies",
            )
            return {"CANCELLED"}
        if mode == "MEMORY":
            result = cm.clear_memory_caches()
        elif mode == "ONLINE_THUMBS":
            result = cm.clear_online_browse_thumbs(cache)
            result.merge(cm.clear_memory_caches())
        elif mode == "ALL_THUMBS":
            result = cm.clear_all_thumbnail_images(cache)
            result.merge(cm.clear_memory_caches())
        elif mode == "CATALOGS":
            # Blender connectors keep enrich in memory (singletons); disk _catalog*.json
            # is Houdini-parity / future — clear both so "Catalogs" is not a no-op.
            result = cm.clear_online_catalog_caches(cache)
            result.merge(cm.clear_memory_caches())
        elif mode == "STAGING":
            result = cm.clear_online_download_staging(cache)
            result.merge(cm.clear_memory_caches())
        elif mode == "REGENERABLE":
            result = cm.clear_regenerable_cache(cache)
        else:
            result = cm.clear_all_cache_keep_index(cache)

        msg = cm.summarize_clear_result(result)
        _set_status(context, msg[:240])
        level = {"WARNING"} if result.errors else {"INFO"}
        self.report(level, msg[:512])
        # Refresh Library grid so missing cache thumbs re-resolve from Preview/
        if mode in {"ALL_THUMBS", "REGENERABLE", "ALL_KEEP_INDEX", "ONLINE_THUMBS"}:
            try:
                bpy.ops.ual.refresh_list()
            except Exception:
                pass
        _tag_redraw_all()
        return {"FINISHED"}


class UAL_OT_open_settings_folder(bpy.types.Operator):
    bl_idname = "ual.open_settings_folder"
    bl_label = "Open Settings Folder"
    bl_options = {"REGISTER"}

    def execute(self, context):
        cfg = _cfg()
        cache = cfg.get("cache_dir") or ""
        conf = os.path.join(cache, "config") if cache else ""
        if not conf or not os.path.isdir(conf):
            self.report({"ERROR"}, "Can't find the settings folder. Run Health Check in Preferences.")
            return {"CANCELLED"}
        bpy.ops.wm.path_open(filepath=conf)
        return {"FINISHED"}


class UAL_OT_restore_default_prefs(bpy.types.Operator):
    """Reset Blender-relevant prefs to defaults (keeps roots, cache, tokens, favorites)."""

    bl_idname = "ual.restore_default_prefs"
    bl_label = "Restore Default Settings"
    bl_options = {"REGISTER"}

    def execute(self, context):
        prefs = preferences._get_prefs()
        # Preserve roots + secrets + cache (favorites stay via load_config merge)
        roots = [(item.path or "") for item in prefs.library_roots]
        secrets = {key: getattr(prefs, key) for key in preferences.RESTORE_PRESERVE_PREF_KEYS}
        prior = preferences.load_active_config()
        prior_favorites = list(prior.get("favorites") or [])
        prefs.copy_to_library = True
        prefs.keep_cache = False
        prefs.thumb_size = 112
        prefs.show_quick_setup = False
        prefs.preferences_tab = "LIBRARY"
        prefs.hover_preview_enabled = True
        prefs.hover_preview_size = 320
        prefs.hover_preview_delay_ms = 160
        prefs.hover_preview_pin_enabled = True
        prefs.index_downloads = True
        prefs.auto_search_on_open = False
        prefs.auto_open_asset_bar_on_search = False
        prefs.hide_asset_bar_when_ual_sidebar = True
        prefs.default_download_resolution = "2048"
        prefs.apply_material_to_selected = True
        prefs.material_import_automap = True
        prefs.material_blend_max_distance = 1.0
        prefs.material_blend_min_distance = 1.0
        prefs.material_blend_live_noise_scale = 1.0
        prefs.material_blend_live_noise_amount = 1.0
        prefs.material_blend_blend_normals = True
        prefs.strip_megascans_billboard_lods = True
        prefs.fab_unit_scale_mode = "cm_to_m"
        prefs.normal_map_space = "OPENGL"
        prefs.texture_naming_profile = "auto"
        prefs.normalize_scale = False
        prefs.pivot_to_base = False
        prefs.place_at_world_origin = True
        prefs.organize_into_collection = True
        prefs.unit_scale = 1.0
        prefs.match_size_on_import = False
        prefs.match_size_target = 1.0
        prefs.show_advanced_download = False
        prefs.show_advanced_import = False
        prefs.show_fab_cookie_fallback = False
        prefs.show_cache_clear_details = False
        prefs.show_online_diagnostics = False
        prefs.show_all_online_sources = True
        for sid in preferences.SOURCE_IDS:
            setattr(prefs, f"enable_{sid}", True)
        for key, value in secrets.items():
            setattr(prefs, key, value)
        if roots:
            prefs.library_roots.clear()
            for path in roots:
                if path:
                    item = prefs.library_roots.add()
                    item.path = path
        cfg = preferences.prefs_to_config()
        cfg["favorites"] = prior_favorites
        if isinstance(prior.get("epic_auth"), dict):
            cfg["epic_auth"] = prior["epic_auth"]
        preferences.save_prefs_config(cfg)
        self.report({"INFO"}, "UAL preferences restored (roots, cache, tokens & favorites kept)")
        return {"FINISHED"}


class UAL_OT_open_website(bpy.types.Operator):
    """Open a source / API / Epic sign-in page in the system browser."""

    bl_idname = "ual.open_website"
    bl_label = "Open Website"
    bl_description = "Open the selected online platform page in your default browser"
    bl_options = {"REGISTER"}

    url: bpy.props.StringProperty(name="URL", default="")
    link_id: bpy.props.StringProperty(
        name="Link ID",
        default="",
        description="Optional registry key: source id, sketchfab, polypizza, fab_site, epic_login",
    )

    def execute(self, context):
        from .. import online_links

        url = (self.url or "").strip()
        link_id = (self.link_id or "").strip().lower()
        if not url and link_id:
            if link_id == "epic_login":
                url = online_links.epic_authorize_login_url()
            elif link_id in online_links.SOURCE_WEBSITE_URLS:
                url = online_links.website_url(link_id)
            else:
                _label, auth_url = online_links.auth_link(link_id)
                url = auth_url
        if not online_links.is_http_url(url):
            self.report({"ERROR"}, "No website link for this source")
            return {"CANCELLED"}
        try:
            bpy.ops.wm.url_open(url=url)
        except Exception:  # noqa: BLE001
            self.report({"ERROR"}, "Couldn't open the browser. Copy the URL from Preferences.")
            return {"CANCELLED"}
        self.report({"INFO"}, "Opened in browser")
        return {"FINISHED"}


class UAL_OT_test_source_connection(bpy.types.Operator):
    """Probe a keyed online source (main-thread kick → background HTTP)."""

    bl_idname = "ual.test_source_connection"
    bl_label = "Test Source Connection"
    bl_options = {"REGISTER"}

    source_id: bpy.props.StringProperty(name="Source", default="sketchfab")

    def execute(self, context):
        if not getattr(bpy.app, "online_access", True):
            self.report({"ERROR"}, MSG_ONLINE_ACCESS)
            return {"CANCELLED"}
        source_id = (self.source_id or "").strip().lower()
        if source_id not in ("sketchfab", "polypizza", "fab", "polyhaven", "ambientcg"):
            self.report({"ERROR"}, "This online source isn't available")
            return {"CANCELLED"}
        cfg = _cfg()
        token_kwargs = _online_token_kwargs(source_id, cfg)
        if source_id == "sketchfab" and not token_kwargs.get("token"):
            self.report({"WARNING"}, "Sketchfab needs an API token. Preferences → Online → Get Sketchfab Token.")
            return {"CANCELLED"}
        if source_id == "polypizza" and not token_kwargs.get("api_key"):
            self.report({"WARNING"}, "Poly Pizza needs an API key. Preferences → Online → Get Poly Pizza API Key.")
            return {"CANCELLED"}
        if source_id == "fab":
            from ..ui_strings import source_auth_configured

            if not source_auth_configured("fab", cfg):
                self.report(
                    {"WARNING"},
                    "Fab: sign in with Epic in Preferences → Online, then test again",
                )
                return {"CANCELLED"}
        holder = {"ok": False, "message": ""}
        _set_status(context, f"Testing {source_id}…", 0)

        def work():
            try:
                src = get_source(source_id)
                from ..ui_strings import pluralize

                if source_id == "fab":
                    from .. import epic_oauth
                    from ..ui_strings import source_auth_configured

                    if not source_auth_configured("fab", cfg):
                        raise RuntimeError("Epic / Fab cookies not configured")
                    epic_raw = token_kwargs.get("epic_auth")
                    cookies = bool(token_kwargs.get("fab_sessionid"))
                    auth_bits = []
                    if isinstance(epic_raw, dict) and epic_raw:
                        session = epic_oauth.ensure_epic_session(
                            epic_oauth.EpicSession.from_dict(epic_raw)
                        )
                        # Persist on main thread only (no bpy prefs RNA in workers)
                        holder["epic_session"] = session.to_dict()
                        auth_bits.append(
                            "Epic ({})".format(session.display_name or "signed in")
                        )
                    if cookies:
                        auth_bits.append("browser cookies")
                    results = src.search("", page=1, page_size=1, **token_kwargs)
                    n = len(list(results.items or []))
                    holder["ok"] = True
                    holder["message"] = (
                        f"Fab is reachable ({pluralize(n, 'sample')}); "
                        f"download sign-in: {'+'.join(auth_bits) or 'none'}"
                    )
                elif source_id == "sketchfab":
                    # Validate token against /v3/me (Houdini health parity)
                    from ..sources import sketchfab as sk_mod

                    token = (token_kwargs.get("token") or "").strip()
                    me = sk_mod._http_json(f"{sk_mod.API_BASE}/me", token)
                    uname = ""
                    if isinstance(me, dict):
                        uname = str(
                            me.get("username") or me.get("displayName") or ""
                        ).strip()
                    results = src.search("", page=1, page_size=1, **token_kwargs)
                    n = len(list(results.items or []))
                    who = f" as {uname}" if uname else ""
                    holder["ok"] = True
                    holder["message"] = (
                        f"Sketchfab OK{who} ({pluralize(n, 'sample result')})"
                    )
                else:
                    results = src.search("", page=1, page_size=1, **token_kwargs)
                    n = len(list(results.items or []))
                    holder["ok"] = True
                    holder["message"] = f"{source_id}: OK ({pluralize(n, 'sample result')})"
            except Exception as exc:  # noqa: BLE001
                holder["ok"] = False
                holder["message"] = f"{source_id}: {friendly_error(exc)}"

            def done():
                epic_snap = holder.get("epic_session")
                if isinstance(epic_snap, dict) and epic_snap:
                    try:
                        from .. import epic_oauth
                        from .. import preferences as prefs_mod

                        full = prefs_mod.load_active_config()
                        full = epic_oauth.store_epic_session(
                            full, epic_oauth.EpicSession.from_dict(epic_snap)
                        )
                        prefs_mod.save_prefs_config(full)
                    except Exception:
                        pass
                _set_status(context, holder["message"])
                level = {"INFO"} if holder["ok"] else {"WARNING"}
                self.report(level, holder["message"])

            tasks_queue.add_task(done)

        tasks_queue.run_in_background(work)
        return {"FINISHED"}


class UAL_OT_dismiss_onboarding_banner(bpy.types.Operator):
    """Dismiss the first-run N-panel banner (persists in settings.json)."""

    bl_idname = "ual.dismiss_onboarding_banner"
    bl_label = "Dismiss Setup Banner"
    bl_description = "Hide the first-run setup tip on the UAL sidebar"
    bl_options = {"REGISTER"}

    def execute(self, context):
        from . import hover_preview

        cfg = _cfg()
        cfg.setdefault("ui", {})["onboarding_banner_dismissed"] = True
        ual_config.save_config(cfg, cfg["cache_dir"])
        hover_preview.invalidate_assets_hit(clear_hover=True)
        _set_status(context, "Setup banner dismissed")
        return {"FINISHED"}


class UAL_OT_dismiss_changelog(bpy.types.Operator):
    """Dismiss the per-version changelog nudge (persists in settings.json)."""

    bl_idname = "ual.dismiss_changelog"
    bl_label = "Dismiss Changelog"
    bl_description = "Hide this version's what's-new tip on the UAL sidebar"
    bl_options = {"REGISTER"}

    def execute(self, context):
        from ..ui_strings import ADDON_VERSION
        from . import hover_preview

        cfg = _cfg()
        cfg.setdefault("ui", {})["changelog_seen_version"] = ADDON_VERSION
        ual_config.save_config(cfg, cfg["cache_dir"])
        hover_preview.invalidate_assets_hit(clear_hover=True)
        _set_status(context, "Changelog dismissed")
        return {"FINISHED"}


class UAL_OT_rebuild_index(bpy.types.Operator):
    bl_idname = "ual.rebuild_index"
    bl_label = "Rebuild Library Index"
    bl_description = "Rescan library folders and rebuild the SQLite asset index"
    bl_options = {"REGISTER"}

    def execute(self, context):
        cfg = _cfg()
        roots = cfg["library_roots"]
        cache = cfg["cache_dir"]
        _set_status(context, "Indexing…", 0)

        def work():
            from .. import thumbnails

            count = indexer.scan_filesystem(roots, cache, force=True)
            thumbnails.soft_prune_thumbs(cache, force=True)

            def done():
                # Drop stale PreviewCollection entries so new albedo/CDN thumbs show
                thumbnails.clear_preview_cache()
                from ..ui_strings import pluralize

                _fill_library_list(context)
                _set_status(
                    context,
                    f"Indexed {pluralize(count, 'asset')} — thumbs refreshed",
                )

            tasks_queue.add_task(done)

        tasks_queue.run_in_background(work)
        return {"FINISHED"}


class UAL_OT_refresh_list(bpy.types.Operator):
    bl_idname = "ual.refresh_list"
    bl_label = "Refresh List"
    bl_description = "Reload the Library / Favorites list from the index (no full rescan)"
    bl_options = {"REGISTER"}

    def execute(self, context):
        _fill_library_list(context)
        return {"FINISHED"}


class UAL_OT_import_selected(bpy.types.Operator):
    bl_idname = "ual.import_selected"
    bl_label = "Import Selected"
    bl_description = (
        "Import the selected library asset at the world origin. Materials/textures "
        "apply to selected viewport meshes when Apply Materials is enabled in preferences."
    )
    bl_options = {"REGISTER", "UNDO"}

    target_index: bpy.props.IntProperty(default=-1, options={"SKIP_SAVE", "HIDDEN"})

    def execute(self, context):
        ui = _ui(context)
        if ui.scope == "online":
            self.report({"ERROR"}, MSG_USE_DOWNLOAD)
            return {"CANCELLED"}
        if not ui.assets:
            self.report({"ERROR"}, MSG_NO_ASSETS)
            return {"CANCELLED"}
        item = None
        if int(self.target_index) >= 0:
            idx = max(0, min(int(self.target_index), len(ui.assets) - 1))
            item = ui.assets[idx]
        else:
            item = _library_selected_item(ui)
        if item is None or not getattr(item, "path", ""):
            self.report({"WARNING"}, MSG_SELECT_ASSET)
            return {"CANCELLED"}
        idx = int(getattr(ui, "selected_index", 0) or 0)
        cfg = _cfg()
        try:
            result = import_routing.perform_typed_import(
                item.path,
                asset_type=item.asset_type,
                cfg=cfg,
                asset_name=str(getattr(item, "name", "") or ""),
            )
        except Exception as exc:  # noqa: BLE001
            _report_error(self, exc)
            return {"CANCELLED"}
        if not result.get("ok"):
            self.report({"ERROR"}, friendly_error(result.get("error") or "Import failed"))
            return {"CANCELLED"}
        # Keep selected_index so the outline stays on the imported asset
        ui.selected_index = idx
        try:
            from . import hover_preview

            hover_preview.clear_hover_target()
        except Exception:
            pass
        _set_status(context, f"Imported {item.name}")
        self.report({"INFO"}, f"Imported {item.name}")
        return {"FINISHED"}


class UAL_OT_toggle_favorite(bpy.types.Operator):
    bl_description = "Toggle favorite for the selected library asset"
    bl_idname = "ual.toggle_favorite"
    bl_label = "Toggle Favorite"
    bl_options = {"REGISTER"}

    target_index: bpy.props.IntProperty(default=-1, options={"SKIP_SAVE", "HIDDEN"})

    def execute(self, context):
        ui = _ui(context)
        if not ui.assets:
            return {"CANCELLED"}
        if int(self.target_index) >= 0:
            idx = max(0, min(int(self.target_index), len(ui.assets) - 1))
            path = ui.assets[idx].path
        else:
            item = _library_selected_item(ui)
            path = getattr(item, "path", "") if item is not None else ""
        if not path:
            self.report({"WARNING"}, MSG_SELECT_ASSET)
            return {"CANCELLED"}
        cfg = _cfg()
        now = ual_config.toggle_favorite(cfg, path)
        ual_config.save_config(cfg, cfg["cache_dir"])
        if ui.scope == "favorites":
            _fill_library_list(context)
        _set_status(context, "Favorited" if now else "Unfavorited")
        return {"FINISHED"}


class UAL_OT_delete_selected(bpy.types.Operator):
    """Delete the selected Library/Favorites asset (Houdini Delete from Disk parity)."""

    bl_idname = "ual.delete_selected"
    bl_label = "Delete from Disk"
    bl_description = (
        "Permanently delete the selected library asset from disk and remove it from "
        "the index. Online packages remove the full download folder. Cannot be undone."
    )
    bl_options = {"REGISTER"}

    confirm_message: bpy.props.StringProperty(default="")
    target_path: bpy.props.StringProperty(default="", options={"SKIP_SAVE", "HIDDEN"})

    def invoke(self, context, event):
        from .. import asset_delete

        ui = _ui(context)
        if ui.scope == "online":
            self.report({"WARNING"}, "Delete works in Library and Favorites, not while browsing Online")
            return {"CANCELLED"}
        if not ui.assets:
            self.report({"WARNING"}, MSG_NO_ASSETS)
            return {"CANCELLED"}
        item = _library_selected_item(ui)
        path = getattr(item, "path", "") if item is not None else ""
        if not path:
            self.report({"WARNING"}, MSG_SELECT_ASSET)
            return {"CANCELLED"}
        self.target_path = path
        cfg = _cfg()
        plan = asset_delete.plan_delete([path], asset_delete.DELETE_FILES, cfg=cfg)
        self.confirm_message = asset_delete.confirm_message(plan)
        if plan.warnings and not (plan.db_paths or plan.file_paths or plan.folder_paths):
            self.report({"WARNING"}, plan.warnings[0])
            return {"CANCELLED"}
        return context.window_manager.invoke_props_dialog(self, width=420)

    def draw(self, context):
        col = self.layout.column(align=True)
        for line in (self.confirm_message or "").split("\n"):
            col.label(text=line)

    def execute(self, context):
        from .. import asset_delete

        ui = _ui(context)
        if ui.scope == "online" or not ui.assets:
            return {"CANCELLED"}
        path = str(self.target_path or "")
        if not path:
            item = _library_selected_item(ui)
            path = getattr(item, "path", "") if item is not None else ""
        if not path:
            self.report({"WARNING"}, MSG_SELECT_ASSET)
            return {"CANCELLED"}
        cfg = _cfg()
        _plan, result = asset_delete.delete_asset_paths(
            [path], asset_delete.DELETE_FILES, cfg=cfg
        )
        if not result.success:
            self.report({"ERROR"}, friendly_error(result.message))
            _set_status(context, result.message)
            return {"CANCELLED"}
        try:
            from .. import thumbnails

            thumbnails.clear_preview_cache()
        except Exception:
            pass
        _fill_library_list(context)
        _refresh_online_in_library_flags(context)
        _set_status(context, result.message)
        self.report({"INFO"}, result.message)
        return {"FINISHED"}


class UAL_OT_hide_selected_from_index(bpy.types.Operator):
    """Remove from library index only — keep files on disk (Houdini Hide parity)."""

    bl_idname = "ual.hide_selected_from_index"
    bl_label = "Hide from Library"
    bl_description = (
        "Remove the selected asset from the library index only. Files stay on disk."
    )
    bl_options = {"REGISTER"}

    confirm_message: bpy.props.StringProperty(default="")
    target_path: bpy.props.StringProperty(default="", options={"SKIP_SAVE", "HIDDEN"})

    def invoke(self, context, event):
        from .. import asset_delete

        ui = _ui(context)
        if ui.scope == "online":
            self.report({"WARNING"}, "Hide works in Library and Favorites, not while browsing Online")
            return {"CANCELLED"}
        if not ui.assets:
            self.report({"WARNING"}, MSG_NO_ASSETS)
            return {"CANCELLED"}
        item = _library_selected_item(ui)
        path = getattr(item, "path", "") if item is not None else ""
        if not path:
            return {"CANCELLED"}
        self.target_path = path
        cfg = _cfg()
        plan = asset_delete.plan_delete([path], asset_delete.DELETE_INDEX_ONLY, cfg=cfg)
        self.confirm_message = asset_delete.confirm_message(plan)
        return context.window_manager.invoke_props_dialog(self, width=400)

    def draw(self, context):
        col = self.layout.column(align=True)
        for line in (self.confirm_message or "").split("\n"):
            col.label(text=line)

    def execute(self, context):
        from .. import asset_delete

        ui = _ui(context)
        if ui.scope == "online" or not ui.assets:
            return {"CANCELLED"}
        path = str(self.target_path or "")
        if not path:
            item = _library_selected_item(ui)
            path = getattr(item, "path", "") if item is not None else ""
        if not path:
            return {"CANCELLED"}
        cfg = _cfg()
        _plan, result = asset_delete.delete_asset_paths(
            [path], asset_delete.DELETE_INDEX_ONLY, cfg=cfg
        )
        if not result.success:
            self.report({"ERROR"}, friendly_error(result.message))
            return {"CANCELLED"}
        _fill_library_list(context)
        _set_status(context, result.message)
        self.report({"INFO"}, result.message)
        return {"FINISHED"}


def _online_token_kwargs(source_id: str, cfg_or_online: dict) -> dict:
    """Build source auth kwargs. Pass full config for Fab so ``epic_auth`` is included."""
    from ..online_auth import token_kwargs_for_source

    return token_kwargs_for_source(source_id, cfg_or_online)


def _require_search_auth(source_id: str, cfg: dict) -> str:
    """Return a warning if Search cannot run (Sketchfab/Poly Pizza need keys). Fab browse is public."""
    from ..ui_strings import source_auth_configured

    sid = (source_id or "").strip().lower()
    if sid == "sketchfab" and not source_auth_configured("sketchfab", cfg):
        return "Sketchfab needs an API token. Preferences → Online → Get Sketchfab Token."
    if sid == "polypizza" and not source_auth_configured("polypizza", cfg):
        return "Poly Pizza needs an API key. Preferences → Online → Get Poly Pizza API Key."
    return ""


def _require_download_auth(source_id: str, cfg: dict) -> str:
    """Return a warning if Download cannot run (includes Fab Epic/cookies)."""
    from ..ui_strings import source_auth_configured

    sid = (source_id or "").strip().lower()
    err = _require_search_auth(sid, cfg)
    if err:
        return err
    if sid == "fab" and not source_auth_configured("fab", cfg):
        return "Fab downloads need Epic sign-in. Preferences → Online → Sign In with Epic…"
    return ""


def _refresh_online_in_library_flags(context, ui=None) -> None:
    """Mark Online result rows that would skip CDN for current Size/Format."""
    try:
        from .. import online_library_presence

        target = ui if ui is not None else _ui(context)
        if target is None:
            return
        online_library_presence.refresh_online_result_in_library_flags(target, _cfg())
        try:
            from . import library_presence_overlay

            library_presence_overlay.tag_redraw(context)
        except Exception:
            pass
        try:
            from .asset_bar import operator as bar_op

            inst = getattr(bar_op, "_instance", None)
            if inst is not None:
                inst._items_stale = True
                if hasattr(inst, "_tag_redraw"):
                    inst._tag_redraw(context)
        except Exception:
            pass
        try:
            from . import selection_overlay

            selection_overlay.tag_ui_redraw(context)
        except Exception:
            pass
    except Exception:
        pass


def _append_online_result_item(ui, asset, cfg: dict) -> None:
    item = ui.online_results.add()
    item.name = asset.name
    item.asset_id = asset.asset_id
    item.source_id = asset.source_id
    item.asset_type = asset.asset_type
    item.thumb_url = asset.thumb_url
    item.license_name = asset.license_name
    item.formats = ",".join(asset.formats)
    item.resolutions = ",".join(asset.resolutions)
    item.preview_icon_id = 0
    item.thumb_path = ""
    try:
        item.in_library = False
    except Exception:
        pass
    disk = thumbnails.online_thumb_disk_path(
        cfg["cache_dir"], asset.source_id, asset.asset_id
    )
    cached = thumbnails.icon_id_for_online(
        cfg["cache_dir"], asset.source_id, asset.asset_id, asset.thumb_url
    )
    if cached:
        item.preview_icon_id = cached
        item.thumb_path = disk


def _apply_online_search_results(
    context,
    ui,
    *,
    source_id: str,
    search_gen: int,
    items,
    error: str,
    has_more: bool,
    total_count: int,
    fingerprint: str,
) -> None:
    """Paint Online grid on the main thread (cache hit or worker callback)."""
    global _online_load_more_busy, _online_load_more_gen
    from . import hover_preview
    from ..online_search_guard import online_search_still_current

    if not online_search_still_current(ui, search_gen, source_id):
        return

    hover_preview.set_suppress_selection_hover(True)
    try:
        cfg = _cfg()
        ui.online_results.clear()
        ui.online_did_search = True
        ui.online_page = 1
        if error:
            ui.online_has_more = False
            ui.online_total_count = 0
            try:
                ui.online_search_fp = ""
            except Exception:
                pass
            _set_status(context, error, ui=ui)
            return
        for asset in items or []:
            if str(getattr(asset, "source_id", "") or "") != str(source_id):
                continue
            _append_online_result_item(ui, asset, cfg)
        from .. import selection_keys

        if ui.online_results:
            selection_keys.apply_online_index(ui, 0)
        else:
            ui.online_selected_index = 0
            ui.online_selected_key = ""
        ui.online_has_more = bool(has_more)
        ui.online_total_count = int(total_count or 0)
        try:
            ui.online_search_fp = str(fingerprint or "")
        except Exception:
            pass
        try:
            from .. import online_dl_options

            online_dl_options.reset_download_options_after_search(ui)
        except Exception:
            pass
        _refresh_online_in_library_flags(context, ui)
        n = len(ui.online_results)
        if n:
            _set_status(context, "Loading previews…", ui=ui)
        else:
            _set_status(context, "Search finished", ui=ui)
        _schedule_online_thumb_fetch(context, ui=ui)
        try:
            prefs = preferences._get_prefs()
            if bool(getattr(prefs, "auto_open_asset_bar_on_search", False)) and n:
                from .. import pro_features
                from . import asset_bar

                if pro_features.asset_bar_enabled():
                    asset_bar.ensure_open(context)
        except Exception:
            pass
    finally:
        hover_preview.set_suppress_selection_hover(False)
        reset_assets_view_scroll(context)
        hover_preview.invalidate_assets_hit(clear_hover=True)
        _online_load_more_gen += 1
        _online_load_more_busy = False


def _fetch_online_search_page(
    source_id: str,
    query: str,
    *,
    page: int,
    page_size: int,
    type_filter: str,
    token_kwargs: dict,
):
    """Run connector search with that source's own type taxonomy (not a global enum)."""
    from ..online_type_filter import (
        apply_type_filter,
        search_category_for_filter,
        should_skip_search,
    )

    if should_skip_search(source_id, type_filter):
        return [], False, 0
    src = get_source(source_id)
    category = search_category_for_filter(source_id, type_filter)
    results = src.search(
        query,
        category=category,
        page=page,
        page_size=page_size,
        **token_kwargs,
    )
    items, has_more, total = apply_type_filter(
        list(results.items or []),
        type_filter,
        has_more=bool(getattr(results, "has_more", False)),
        api_total=int(getattr(results, "total_count", 0) or 0),
    )
    return items, has_more, total


def _prefetch_online_next_page(
    source_id: str,
    query: str,
    *,
    current_page: int,
    page_size: int,
    type_filter: str,
    token_kwargs: dict,
    has_more: bool,
) -> None:
    """Warm the search cache for page N+1 so Load More is instant when possible.

    GPUOpen MatLib is ~1.5s per request — prefetch hides that behind browsing.
    """
    if not has_more:
        return
    from .. import online_search_cache
    from ..ui_strings import ONLINE_RESULTS_SOFT_CAP

    next_page = int(current_page or 1) + 1
    if next_page < 2:
        return
    # Soft-cap: no point fetching past what the N-panel will show
    already = max(0, (int(current_page or 1) - 1) * int(page_size or 40))
    if already + int(page_size or 40) >= int(ONLINE_RESULTS_SOFT_CAP):
        return
    if online_search_cache.get_cached_search(
        source_id,
        query,
        page=next_page,
        page_size=page_size,
        type_filter=type_filter,
    ) is not None:
        return

    def work() -> None:
        try:
            if online_search_cache.get_cached_search(
                source_id,
                query,
                page=next_page,
                page_size=page_size,
                type_filter=type_filter,
            ) is not None:
                return
            items, more, total = _fetch_online_search_page(
                source_id,
                query,
                page=next_page,
                page_size=page_size,
                type_filter=type_filter,
                token_kwargs=token_kwargs,
            )
            online_search_cache.store_cached_search(
                source_id,
                query,
                page=next_page,
                page_size=page_size,
                type_filter=type_filter,
                items=items,
                has_more=more,
                total_count=total,
            )
        except Exception:
            pass

    try:
        tasks_queue.run_in_background(work)
    except Exception:
        pass


def run_online_search(context, ui=None, *, report=None) -> set:
    """Populate Online results for the current source.

    Safe from RNA updates and ``bpy.app.timers`` (no ``bpy.ops``). HTTP runs
    on the background pool; the grid is painted on the main thread.
    """
    if not getattr(bpy.app, "online_access", True):
        if report is not None:
            report({"ERROR"}, MSG_ONLINE_ACCESS)
        return {"CANCELLED"}
    if ui is None:
        ui = _ui(context)
    if ui is None:
        return {"CANCELLED"}
    from ..search_text import normalize_search_query, search_fingerprint

    source_id = ui.online_source
    query = normalize_search_query(_online_query_text(ui))
    cfg = _cfg()
    auth_err = _require_search_auth(source_id, cfg)
    if auth_err:
        if report is not None:
            report({"WARNING"}, auth_err)
        _set_status(context, auth_err, ui=ui)
        return {"CANCELLED"}
    type_filter = ui.filter_type or "ALL"
    fp = search_fingerprint(source_id, query, type_filter, page=1)
    if (
        bool(getattr(ui, "online_did_search", False))
        and str(getattr(ui, "online_search_fp", "") or "") == fp
    ):
        return {"FINISHED"}

    from .. import online_search_cache
    from ..online_type_filter import should_skip_search

    page_size = online_search_cache.online_search_page_size(source_id)
    cached = online_search_cache.get_cached_search(
        source_id,
        query,
        page=1,
        page_size=page_size,
        type_filter=type_filter,
    )
    skip_http = should_skip_search(source_id, type_filter)

    try:
        ui.online_search_gen = int(getattr(ui, "online_search_gen", 0) or 0) + 1
    except Exception:
        pass
    search_gen = int(getattr(ui, "online_search_gen", 0) or 0)

    if cached is not None or skip_http:
        items = [] if skip_http and cached is None else list((cached or {}).get("items") or [])
        has_more = False if skip_http and cached is None else bool((cached or {}).get("has_more"))
        total = 0 if skip_http and cached is None else int((cached or {}).get("total_count") or 0)
        if skip_http and cached is None:
            online_search_cache.store_cached_search(
                source_id,
                query,
                page=1,
                page_size=page_size,
                type_filter=type_filter,
                items=[],
                has_more=False,
                total_count=0,
            )
        _apply_online_search_results(
            context,
            ui,
            source_id=source_id,
            search_gen=search_gen,
            items=items,
            error="",
            has_more=has_more,
            total_count=total,
            fingerprint=fp,
        )
        if has_more and not skip_http:
            _prefetch_online_next_page(
                source_id,
                query,
                current_page=1,
                page_size=page_size,
                type_filter=type_filter,
                token_kwargs=_online_token_kwargs(source_id, cfg),
                has_more=True,
            )
        return {"FINISHED"}

    _set_status(context, f"Searching {source_id}…", 0, ui=ui)
    token_kwargs = _online_token_kwargs(source_id, cfg)
    results_holder = {
        "items": [],
        "error": "",
        "has_more": False,
        "total_count": 0,
    }

    def work():
        try:
            items, has_more, total = _fetch_online_search_page(
                source_id,
                query,
                page=1,
                page_size=page_size,
                type_filter=type_filter,
                token_kwargs=token_kwargs,
            )
            results_holder["items"] = items
            results_holder["has_more"] = has_more
            results_holder["total_count"] = total
            online_search_cache.store_cached_search(
                source_id,
                query,
                page=1,
                page_size=page_size,
                type_filter=type_filter,
                items=items,
                has_more=has_more,
                total_count=total,
            )
        except Exception as exc:  # noqa: BLE001
            results_holder["error"] = str(exc)

        def done():
            _apply_online_search_results(
                context,
                ui,
                source_id=source_id,
                search_gen=search_gen,
                items=results_holder["items"],
                error=results_holder["error"],
                has_more=results_holder["has_more"],
                total_count=results_holder["total_count"],
                fingerprint=fp,
            )
            if results_holder["has_more"] and not results_holder["error"]:
                _prefetch_online_next_page(
                    source_id,
                    query,
                    current_page=1,
                    page_size=page_size,
                    type_filter=type_filter,
                    token_kwargs=token_kwargs,
                    has_more=True,
                )

        tasks_queue.add_task(done)

    tasks_queue.run_in_background(work)
    return {"FINISHED"}


class UAL_OT_online_search(bpy.types.Operator):
    bl_idname = "ual.online_search"
    bl_label = "Search"
    bl_description = "Search the selected Online source (also runs on Enter, Type, and Source)"
    bl_options = {"REGISTER"}

    def execute(self, context):
        return run_online_search(context, _ui(context), report=self.report)


class UAL_OT_online_load_more(bpy.types.Operator):
    bl_idname = "ual.online_load_more"
    bl_label = "Load More"
    bl_description = (
        "Fetch the next page from this site. Use ▲▼ or the mouse wheel to browse "
        "tiles already loaded."
    )
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        del context
        return not _online_load_more_busy

    def execute(self, context):
        global _online_load_more_busy, _online_load_more_gen
        from ..ui_strings import ONLINE_RESULTS_SOFT_CAP

        if not getattr(bpy.app, "online_access", True):
            self.report({"ERROR"}, MSG_ONLINE_ACCESS)
            return {"CANCELLED"}
        ui = _ui(context)
        if _online_load_more_busy:
            return {"CANCELLED"}
        if not bool(getattr(ui, "online_has_more", False)):
            self.report({"INFO"}, "No more results")
            return {"CANCELLED"}
        if not bool(getattr(ui, "online_did_search", False)):
            self.report({"WARNING"}, MSG_SEARCH_FIRST)
            return {"CANCELLED"}
        if len(ui.online_results) >= ONLINE_RESULTS_SOFT_CAP:
            ui.online_has_more = False
            _set_status(
                context,
                f"Showing {ONLINE_RESULTS_SOFT_CAP} results — refine search to browse more",
            )
            self.report(
                {"INFO"},
                f"Panel cap ({ONLINE_RESULTS_SOFT_CAP}). Narrow the query or use the Asset Bar.",
            )
            return {"CANCELLED"}

        source_id = ui.online_source
        from ..search_text import normalize_search_query

        query = normalize_search_query(_online_query_text(ui))
        cfg = _cfg()
        auth_err = _require_search_auth(source_id, cfg)
        if auth_err:
            self.report({"WARNING"}, auth_err)
            _set_status(context, auth_err)
            return {"CANCELLED"}
        next_page = int(getattr(ui, "online_page", 1) or 1) + 1
        search_gen = int(getattr(ui, "online_search_gen", 0) or 0)
        type_filter = ui.filter_type or "ALL"
        from .. import online_search_cache

        page_size = online_search_cache.online_search_page_size(source_id)
        cached_more = online_search_cache.get_cached_search(
            source_id,
            query,
            page=next_page,
            page_size=page_size,
            type_filter=type_filter,
        )
        _set_status(context, f"Loading more from {source_id}…", 0)
        _online_load_more_gen += 1
        my_gen = _online_load_more_gen
        _online_load_more_busy = True

        token_kwargs = _online_token_kwargs(source_id, cfg)
        results_holder = {
            "items": [],
            "error": "",
            "has_more": False,
            "total_count": 0,
            "page": next_page,
        }

        def work():
            try:
                if cached_more is not None:
                    results_holder["items"] = list(cached_more.get("items") or [])
                    results_holder["has_more"] = bool(cached_more.get("has_more"))
                    results_holder["total_count"] = int(cached_more.get("total_count") or 0)
                else:
                    items, has_more, total = _fetch_online_search_page(
                        source_id,
                        query,
                        page=next_page,
                        page_size=page_size,
                        type_filter=type_filter,
                        token_kwargs=token_kwargs,
                    )
                    results_holder["items"] = items
                    results_holder["has_more"] = has_more
                    results_holder["total_count"] = total
                    online_search_cache.store_cached_search(
                        source_id,
                        query,
                        page=next_page,
                        page_size=page_size,
                        type_filter=type_filter,
                        items=items,
                        has_more=results_holder["has_more"],
                        total_count=results_holder["total_count"],
                    )
            except Exception as exc:  # noqa: BLE001
                results_holder["error"] = str(exc)

            def done():
                global _online_load_more_busy
                from . import hover_preview

                from ..online_search_guard import online_search_still_current

                try:
                    if my_gen != _online_load_more_gen:
                        return
                    if not online_search_still_current(ui, search_gen, source_id):
                        return

                    hover_preview.set_suppress_selection_hover(True)
                    try:
                        if results_holder["error"]:
                            _set_status(context, results_holder["error"])
                            return
                        from ..ui_strings import ONLINE_RESULTS_SOFT_CAP

                        room = max(0, ONLINE_RESULTS_SOFT_CAP - len(ui.online_results))
                        first_new = len(ui.online_results)
                        to_add = [
                            a
                            for a in list(results_holder["items"] or [])[:room]
                            if str(getattr(a, "source_id", "") or "") == str(source_id)
                        ]
                        for asset in to_add:
                            _append_online_result_item(ui, asset, cfg)
                        _refresh_online_in_library_flags(context, ui)
                        # Keep the same asset selected by stable key (index may stay put)
                        from .. import selection_keys

                        selection_keys.sync_online_selection(ui)
                        ui.online_page = int(results_holder["page"])
                        hit_cap = len(ui.online_results) >= ONLINE_RESULTS_SOFT_CAP
                        ui.online_has_more = bool(results_holder["has_more"]) and not hit_cap
                        if results_holder["total_count"]:
                            ui.online_total_count = int(results_holder["total_count"])
                        if to_add:
                            _set_status(context, "Loading previews…")
                            # Jump first so thumb fetch prioritizes the new window.
                            try:
                                ensure_assets_view_shows_index(
                                    context, first_new, prefer_top=True
                                )
                            except Exception:
                                pass
                            _schedule_online_thumb_fetch(context)
                        elif hit_cap:
                            _set_status(
                                context,
                                f"Showing {ONLINE_RESULTS_SOFT_CAP} results — refine search to browse more",
                            )
                        else:
                            _set_status(context, "Ready")
                        if ui.online_has_more and not results_holder["error"]:
                            _prefetch_online_next_page(
                                source_id,
                                query,
                                current_page=int(results_holder["page"]),
                                page_size=page_size,
                                type_filter=type_filter,
                                token_kwargs=token_kwargs,
                                has_more=True,
                            )
                    finally:
                        hover_preview.set_suppress_selection_hover(False)
                finally:
                    if my_gen == _online_load_more_gen:
                        _online_load_more_busy = False

            tasks_queue.add_task(done)

        tasks_queue.run_in_background(work)
        return {"FINISHED"}


class UAL_OT_online_download_import(bpy.types.Operator):
    bl_idname = "ual.online_download_import"
    bl_label = "Download & Import"
    bl_description = (
        "Download the selected online asset (skips CDN when already in Online Downloads), "
        "save if needed, then import. Materials apply to selected meshes; meshes at world origin."
    )
    bl_options = {"REGISTER", "UNDO"}

    target_index: bpy.props.IntProperty(default=-1, options={"SKIP_SAVE", "HIDDEN"})

    def execute(self, context):
        if not getattr(bpy.app, "online_access", True):
            self.report({"ERROR"}, MSG_ONLINE_ACCESS)
            return {"CANCELLED"}
        ui = _ui(context)
        if not ui.online_results:
            self.report({"ERROR"}, MSG_SEARCH_FIRST)
            return {"CANCELLED"}
        raw = (
            int(self.target_index)
            if int(self.target_index) >= 0
            else int(ui.online_selected_index)
        )
        idx = max(0, min(raw, len(ui.online_results) - 1))
        item = ui.online_results[idx]
        cfg = _cfg()
        source_id = item.source_id or ui.online_source
        auth_err = _require_download_auth(source_id, cfg)
        if auth_err:
            self.report({"WARNING"}, auth_err)
            _set_status(context, auth_err)
            return {"CANCELLED"}

        from ..download_options import (
            preferred_format_from_ui,
            preferred_resolution_from_ui,
            resolve_download_resolution,
        )

        formats_snap = [f for f in (item.formats or "").split(",") if f]
        preferred_res = preferred_resolution_from_ui(ui, cfg)
        preferred_fmt = preferred_format_from_ui(ui, formats_snap)
        # Resolve concrete resolution when possible (matches what CDN would fetch)
        try:
            src_probe = get_source(source_id)
            from ..sources.base import AssetResult

            probe_asset = AssetResult(
                source_id=source_id,
                asset_id=item.asset_id,
                name=item.name,
                asset_type=item.asset_type,
                formats=list(formats_snap),
                resolutions=[r for r in (item.resolutions or "").split(",") if r],
                license_name=item.license_name,
            )
            resolved_res = resolve_download_resolution(
                source_id,
                probe_asset,
                preferred_res,
                source=src_probe,
                fmt=preferred_fmt,
            )
        except Exception:
            resolved_res = preferred_res

        reused_path, reused_origin = online_download.find_reusable_online_asset(
            cfg,
            source_id=source_id,
            asset_id=item.asset_id,
            asset_name=item.name,
            asset_type=item.asset_type or "mesh",
            resolution=str(resolved_res or ""),
            format=str(preferred_fmt or ""),
        )
        if reused_path:
            try:
                path = online_download.prepare_reusable_asset_for_import(
                    reused_path,
                    reused_origin,
                    cfg,
                    source_id=source_id,
                    asset_name=item.name,
                    asset_type=item.asset_type or "mesh",
                    asset_id=item.asset_id,
                    thumb_url=item.thumb_url or "",
                    resolution=str(resolved_res or ""),
                    format=str(preferred_fmt or ""),
                    preview_image=item.thumb_path or "",
                )
                if (cfg.get("online") or {}).get("index_downloads", True) and reused_origin == "cache":
                    try:
                        from .. import indexer

                        indexer.scan_filesystem(
                            cfg["library_roots"],
                            cfg["cache_dir"],
                            force=True,
                            fetch_thumbs=False,
                        )
                        _fill_library_list(context)
                    except Exception:
                        pass
                import_routing.perform_typed_import(
                    path,
                    asset_type=item.asset_type,
                    cfg=cfg,
                    asset_name=str(item.name or ""),
                )
                if ui.online_results:
                    from .. import selection_keys

                    selection_keys.apply_online_index(
                        ui, max(0, min(idx, len(ui.online_results) - 1))
                    )
                where = "library" if reused_origin == "library" else "cache"
                if where == "library":
                    msg = f"Already in your library — imported {item.name}"
                else:
                    msg = f"Using a cached download — imported {item.name}"
                try:
                    item.in_library = True
                except Exception:
                    pass
                _refresh_online_in_library_flags(context, ui)
                _set_status(context, msg, -1)
                self.report({"INFO"}, msg)
            except Exception as exc:  # noqa: BLE001
                _set_status(context, str(exc))
                _report_error(self, exc)
                return {"CANCELLED"}
            return {"FINISHED"}

        if not download_progress.can_start_download():
            self.report(
                {"WARNING"},
                "Too many downloads at once (max 3). Wait or cancel one.",
            )
            return {"CANCELLED"}

        cache_dest = paths.source_cache_dir(cfg["cache_dir"], source_id, item.asset_id)
        paths.ensure_dir(cache_dest)

        token_kwargs = _online_token_kwargs(source_id, cfg)

        task_id = download_progress.start_task(item.name, phase="download")
        ui.download_active = True
        ui.download_task_id = task_id
        ui.download_label = f"Downloading {item.name}…"
        _set_status(context, ui.download_label, 0)
        _ensure_progress_timer()

        copy_to_library = bool((cfg.get("online") or {}).get("copy_to_library", True))

        holder = {"path": "", "error": "", "asset_type": item.asset_type, "cancelled": False}

        def work():
            download_progress.set_active_task(task_id)
            try:
                from ..sources.base import AssetResult

                asset = AssetResult(
                    source_id=source_id,
                    asset_id=item.asset_id,
                    name=item.name,
                    asset_type=item.asset_type,
                    formats=list(formats_snap),
                    resolutions=[r for r in (item.resolutions or "").split(",") if r],
                    license_name=item.license_name,
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
                    if item.thumb_url:
                        preview_src = thumbnails.fetch_online_thumb_to_disk(
                            cfg["cache_dir"],
                            source_id,
                            item.asset_id,
                            item.thumb_url,
                        ) or item.thumb_path or ""
                    local = online_download.register_and_stage_online_download(
                        local,
                        cfg,
                        source_id=source_id,
                        asset_name=item.name,
                        asset_type=item.asset_type or "mesh",
                        progress=lambda msg: download_progress.set_phase(task_id, "save", progress=92.0),
                        preview_image=preview_src,
                        asset_id=item.asset_id,
                        thumb_url=item.thumb_url or "",
                        resolution=str(res or ""),
                        format=str(fmt or ""),
                    )
                holder["path"] = local
                download_progress.set_phase(task_id, "import", progress=98.0)
            except download_utils.DownloadCancelled:
                holder["cancelled"] = True
            except Exception as exc:  # noqa: BLE001
                holder["error"] = str(exc)
            finally:
                download_progress.set_active_task(None)

            def done():
                download_progress.finish_task(task_id)
                sync_download_progress_to_ui()
                if holder["cancelled"]:
                    _set_status(context, "Download cancelled")
                    self.report({"WARNING"}, "Download cancelled")
                    return
                if holder["error"]:
                    _set_status(context, holder["error"])
                    _report_error(self, holder["error"])
                    return
                path = holder["path"]
                if not path:
                    _set_status(context, MSG_EMPTY_DOWNLOAD)
                    self.report({"ERROR"}, MSG_EMPTY_DOWNLOAD)
                    return
                try:
                    if (cfg.get("online") or {}).get("index_downloads", True):
                        try:
                            from .. import indexer

                            indexer.scan_filesystem(
                                cfg["library_roots"],
                                cfg["cache_dir"],
                                force=True,
                                fetch_thumbs=False,
                            )
                            _fill_library_list(context)
                        except Exception:
                            pass
                    import_routing.perform_typed_import(
                        path,
                        asset_type=holder["asset_type"],
                        cfg=cfg,
                        asset_name=str(item.name or ""),
                    )
                    # Keep outline on the online asset that was just imported
                    if ui.online_results:
                        from .. import selection_keys

                    selection_keys.apply_online_index(
                        ui, max(0, min(idx, len(ui.online_results) - 1))
                    )
                    try:
                        item.in_library = True
                    except Exception:
                        pass
                    _refresh_online_in_library_flags(context, ui)
                    _set_status(context, f"Imported {item.name}", -1)
                    self.report({"INFO"}, f"Imported {item.name}")
                except Exception as exc:  # noqa: BLE001
                    _set_status(context, str(exc))
                    _report_error(self, exc)

            tasks_queue.add_task(done)

        tasks_queue.run_in_background(work)
        return {"FINISHED"}


class UAL_OT_download_cancel(bpy.types.Operator):
    bl_idname = "ual.download_cancel"
    bl_label = "Cancel Download"
    bl_options = {"REGISTER"}

    task_id: bpy.props.StringProperty(default="")

    def execute(self, context):
        ui = _ui(context)
        tid = self.task_id or ui.download_task_id
        if not tid:
            tasks = download_progress.snapshot_tasks()
            tid = tasks[0]["task_id"] if tasks else ""
        if not tid:
            self.report({"WARNING"}, "No download in progress")
            return {"CANCELLED"}
        download_progress.request_cancel(tid)
        _set_status(context, "Cancelling download…")
        sync_download_progress_to_ui()
        return {"FINISHED"}


def _tag_redraw_preferences() -> None:
    try:
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == "PREFERENCES":
                    area.tag_redraw()
    except Exception:
        pass


def _set_backup_cloud_status(message: str) -> None:
    """Update Preferences cloud backup status line and redraw."""
    text = (message or "").strip() or "Cloud: not fetched"
    try:
        prefs = preferences._get_prefs()
        prefs.backup_cloud_status = text[:240]
    except Exception:
        return
    _tag_redraw_preferences()


def _run_platform_probe_background(context, *, report_op=None) -> None:
    """Kick HTTP platform probes off the UI thread; update Preferences indicators."""
    from .. import online_health
    from .. import tasks_queue

    if online_health.get_cached_state().get("checking"):
        if report_op is not None:
            report_op.report({"INFO"}, "Already checking online sites…")
        return
    if not getattr(bpy.app, "online_access", True):
        if report_op is not None:
            report_op.report({"WARNING"}, MSG_ONLINE_ACCESS)
        return

    cfg = preferences.prefs_to_config()
    enabled = preferences.enabled_source_ids()
    online_health.set_checking(True)
    _tag_redraw_preferences()
    _set_status(context, "Checking online sites…", 0)

    holder = {"rows": [], "error": ""}

    def work():
        try:
            holder["rows"] = online_health.probe_all_platforms(
                cfg,
                enabled_sources=enabled,
                online_access=True,
            )
        except Exception as exc:  # noqa: BLE001
            holder["error"] = str(exc)

        def done():
            if holder["error"]:
                online_health.set_checking(False)
                msg = friendly_error(holder["error"])
                _set_status(context, msg)
                if report_op is not None:
                    report_op.report({"WARNING"}, msg)
            else:
                rows = holder["rows"]
                summary = online_health.summarize_rows(rows)
                online_health.store_results(rows, summary)
                msg = f"Sites: {summary}"
                _set_status(context, msg)
                if report_op is not None:
                    fails = sum(1 for r in rows if r.status in ("fail", "need_auth", "warn"))
                    level = {"WARNING"} if fails else {"INFO"}
                    report_op.report(level, msg)
                try:
                    from .. import provider_sync

                    provider_sync.schedule_provider_sync(cfg)
                except Exception:
                    pass
            _tag_redraw_preferences()
            _tag_redraw_all()

        tasks_queue.add_task(done)

    tasks_queue.run_in_background(work)


class UAL_OT_check_online_platforms(bpy.types.Operator):
    """Probe all enabled online sources (connectivity + auth)."""

    bl_idname = "ual.check_online_platforms"
    bl_label = "Check online sources"
    bl_description = (
        "Test every enabled online site: reachable, and whether "
        "Sketchfab / Poly Pizza / Fab sign-in is set and accepted"
    )
    bl_options = {"REGISTER"}

    def execute(self, context):
        _run_platform_probe_background(context, report_op=self)
        return {"FINISHED"}


class UAL_OT_refresh_seat(bpy.types.Operator):
    """Ask ual-api which plan this account has and bind this Blender install as a device."""

    bl_idname = "ual.refresh_seat"
    bl_label = "Refresh seat"
    bl_description = (
        "Fetch plan and features for the connected UAL API key. Core library features stay on even offline."
    )
    bl_options = {"REGISTER"}

    def execute(self, context):
        from .. import entitlement_client
        from .. import tasks_queue
        from ..ui_strings import friendly_error

        if not getattr(bpy.app, "online_access", True):
            self.report({"WARNING"}, MSG_ONLINE_ACCESS)
            return {"CANCELLED"}

        cfg = preferences.prefs_to_config()
        if not entitlement_client.account_ready_for_api(cfg):
            self.report(
                {"WARNING"},
                "Set Updates API base URL and paste your UAL API key first (Account → Profile).",
            )
            return {"CANCELLED"}

        _set_status(context, "Refreshing UAL seat…", 0)
        holder = {"result": None, "error": ""}

        def work():
            try:
                holder["result"] = entitlement_client.refresh_seat(cfg)
            except Exception as exc:  # noqa: BLE001
                holder["error"] = str(exc)

            def done():
                if holder["error"]:
                    msg = entitlement_client.friendly_account_error(holder["error"])
                    _set_status(context, msg)
                    self.report({"WARNING"}, msg)
                else:
                    result = holder["result"]
                    if result.skipped:
                        msg = "Set Updates API base URL and UAL API key to refresh the seat."
                        self.report({"INFO"}, msg)
                        _set_status(context, msg)
                    elif not result.ok:
                        msg = entitlement_client.friendly_account_error(
                            result.error or "Seat refresh failed"
                        )
                        locked = entitlement_client.locked_seat_after_failed_refresh(
                            cfg,
                            result,
                        )
                        if locked:
                            try:
                                live = preferences.prefs_to_config()
                                live.setdefault("account", {})
                                live["account"]["seat"] = locked
                                live["account"]["last_seat_error"] = msg[:220]
                                preferences.save_prefs_config(live, skip_remote_sync=True)
                                offer_cfg = live
                            except Exception:
                                offer_cfg = {"account": {"seat": locked}}
                        self.report({"WARNING"}, msg)
                        _set_status(context, msg)
                    else:
                        offer_cfg = {"account": {"seat": result.seat}}
                        try:
                            live = preferences.prefs_to_config()
                            live.setdefault("account", {})
                            live["account"]["installation_id"] = (cfg.get("account") or {}).get(
                                "installation_id",
                            )
                            live["account"]["seat"] = result.seat
                            live["account"].pop("last_seat_error", None)
                            preferences.save_prefs_config(live, skip_remote_sync=True)
                            offer_cfg = live
                        except Exception:
                            pass
                        try:
                            from . import pro_operators

                            pro_operators.maybe_invoke_pro_package_install(offer_cfg)
                        except ImportError:
                            pass
                        seat = result.seat
                        msg = "Seat: {} ({})".format(
                            seat.get("planLabel") or "Community",
                            seat.get("status") or "unknown",
                        )
                        if result.device_error:
                            msg = "{} — {}".format(msg, result.device_error)
                        self.report({"INFO"} if not result.device_error else {"WARNING"}, msg[:250])
                        _set_status(context, msg)
                _tag_redraw_preferences()

            tasks_queue.add_task(done)

        tasks_queue.run_in_background(work)
        return {"FINISHED"}


class UAL_OT_connect_api_key(bpy.types.Operator):
    """Validate UAL API key from the website and connect this Blender install."""

    bl_idname = "ual.connect_api_key"
    bl_label = "Connect API key"
    bl_description = (
        "Validate the pasted UAL API key (ualak_*), resolve your account, and refresh seat."
    )

    bl_options = {"REGISTER"}

    def execute(self, context):
        from .. import entitlement_client
        from .. import tasks_queue

        if not getattr(bpy.app, "online_access", True):
            self.report({"WARNING"}, MSG_ONLINE_ACCESS)
            return {"CANCELLED"}

        prefs = preferences._get_prefs()
        api_key = ""
        if prefs:
            api_key = str(getattr(prefs, "account_addon_key", "") or "").strip()
        if not api_key:
            self.report({"WARNING"}, "Paste your UAL API key in Preferences first.")
            return {"CANCELLED"}

        cfg = preferences.prefs_to_config()
        if not entitlement_client.api_base_from_config(cfg):
            self.report(
                {"WARNING"},
                "Set Updates API base URL first (website origin, e.g. https://universalassetlibrary.com).",
            )
            return {"CANCELLED"}

        holder = {"result": None, "seat_result": None, "error": ""}

        def work():
            try:
                holder["result"] = entitlement_client.connect_with_api_key(cfg, api_key)
            except Exception as exc:  # noqa: BLE001
                holder["error"] = str(exc)
                holder["result"] = None
                tasks_queue.add_task(done)
                return

            result = holder["result"]
            if isinstance(result, dict) and result.get("success"):
                try:
                    # Same worker: validate key then bind device + pull Pro features.
                    holder["seat_result"] = entitlement_client.refresh_seat(cfg)
                except Exception as exc:  # noqa: BLE001
                    holder["error"] = str(exc)

            tasks_queue.add_task(done)

        def done():
            try:
                if holder["error"] and not (
                    isinstance(holder.get("result"), dict) and holder["result"].get("success")
                ):
                    msg = entitlement_client.friendly_account_error(
                        holder["error"] or "Connect failed"
                    )
                    _set_status(context, msg)
                    self.report({"WARNING"}, msg)
                    return

                result = holder["result"]
                if not isinstance(result, dict) or not result.get("success"):
                    err = None
                    if isinstance(result, dict):
                        err = result.get("error")
                    msg = entitlement_client.friendly_account_error(
                        err or holder["error"] or "Connect failed"
                    )
                    _set_status(context, msg)
                    self.report({"WARNING"}, msg)
                    return

                live = preferences.prefs_to_config()
                live.setdefault("account", {})
                live["account"]["user_id"] = str(result.get("userId") or "")
                live["account"]["addon_key"] = str(result.get("addonKey") or "")
                seat_result = holder.get("seat_result")
                if seat_result is not None and getattr(seat_result, "ok", False) and not getattr(
                    seat_result, "skipped", False
                ):
                    live["account"]["installation_id"] = (cfg.get("account") or {}).get(
                        "installation_id"
                    )
                    live["account"]["seat"] = seat_result.seat
                    live["account"].pop("last_seat_error", None)
                    preferences.save_prefs_config(live)
                    try:
                        from . import pro_operators

                        pro_operators.maybe_invoke_pro_package_install(live)
                    except ImportError:
                        pass
                    seat = seat_result.seat or {}
                    msg = "Connected — {} ({})".format(
                        seat.get("planLabel") or "Community",
                        seat.get("status") or "unknown",
                    )
                    if getattr(seat_result, "device_error", ""):
                        msg = "{} — {}".format(msg, seat_result.device_error)
                    self.report(
                        {"INFO"} if not getattr(seat_result, "device_error", "") else {"WARNING"},
                        msg[:250],
                    )
                    _set_status(context, msg)
                elif seat_result is not None and not getattr(seat_result, "ok", False):
                    locked = entitlement_client.locked_seat_after_failed_refresh(
                        cfg, seat_result
                    )
                    if locked:
                        live["account"]["seat"] = locked
                    err = entitlement_client.friendly_account_error(
                        getattr(seat_result, "error", "") or holder["error"] or "Seat refresh failed"
                    )
                    live["account"]["last_seat_error"] = err[:220]
                    preferences.save_prefs_config(live)
                    self.report({"WARNING"}, err)
                    _set_status(context, err)
                else:
                    preferences.save_prefs_config(live)
                    if holder["error"]:
                        msg = entitlement_client.friendly_account_error(holder["error"])
                        self.report({"WARNING"}, "Connected, but seat refresh failed: " + msg)
                        _set_status(context, msg)
                    else:
                        self.report({"INFO"}, "Connected — refreshing seat…")
                        _set_status(context, "Connected — refreshing seat…", 0)
                        try:
                            entitlement_client.schedule_seat_refresh(live)
                        except Exception:
                            pass

                live_prefs = preferences._get_prefs()
                if live_prefs:
                    live_prefs.account_user_id = live["account"]["user_id"]
                    live_prefs.account_addon_key = live["account"]["addon_key"]
            finally:
                _tag_redraw_preferences()

        tasks_queue.run_in_background(work)
        return {"FINISHED"}


class UAL_OT_disconnect_account(bpy.types.Operator):
    """Clear the stored UAL API key and seat cache on this Blender install."""

    bl_idname = "ual.disconnect_account"
    bl_label = "Disconnect UAL account"
    bl_description = "Remove the stored UAL API key and seat from this Blender install."
    bl_options = {"REGISTER"}

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        from .. import entitlement_client

        live = preferences.prefs_to_config()
        entitlement_client.clear_account(live)
        preferences.save_prefs_config(live, skip_remote_sync=True)
        prefs = preferences._get_prefs()
        if prefs:
            prefs.account_user_id = ""
            prefs.account_addon_key = ""
        self.report({"INFO"}, "UAL account disconnected.")
        _set_status(context, "UAL account disconnected.")
        _tag_redraw_preferences()
        return {"FINISHED"}


class UAL_OT_check_for_updates(bpy.types.Operator):
    """Compare installed addon version against the published release catalog."""

    bl_idname = "ual.check_for_updates"
    bl_label = "Check for Updates"
    bl_description = (
        "Ask ual-api / the UAL website whether a newer Blender package is published"
    )
    bl_options = {"REGISTER"}

    def execute(self, context):
        from .. import release_client
        from .. import tasks_queue
        from .. import update_manager
        from ..ui_strings import ADDON_VERSION, friendly_error

        if update_manager.get_cached_state().get("checking"):
            self.report({"INFO"}, "Already checking for updates…")
            return {"CANCELLED"}
        if update_manager.get_cached_state().get("installing"):
            self.report({"INFO"}, "Update install already in progress…")
            return {"CANCELLED"}
        if not getattr(bpy.app, "online_access", True):
            self.report({"WARNING"}, MSG_ONLINE_ACCESS)
            return {"CANCELLED"}

        prefs = preferences._get_prefs()
        api_base = str(getattr(prefs, "releases_api_base_url", "") or "").strip()
        channel = str(getattr(prefs, "release_channel", "stable") or "stable")
        update_manager.set_checking(True)
        _tag_redraw_preferences()
        _set_status(context, "Checking for updates…", 0)
        holder = {"result": None, "error": ""}

        def work():
            try:
                holder["result"] = release_client.check_for_update(
                    api_base, ADDON_VERSION, channel
                )
            except Exception as exc:  # noqa: BLE001
                holder["error"] = str(exc)

            def done():
                if holder["error"]:
                    update_manager.set_checking(False)
                    msg = friendly_error(holder["error"])
                    _set_status(context, msg)
                    self.report({"WARNING"}, msg)
                else:
                    result = holder["result"]
                    update_manager.store_result(result)
                    msg = result.message
                    level = {"INFO"}
                    if not result.ok:
                        level = {"WARNING"}
                    elif result.force_update or result.update_available:
                        level = {"WARNING"}
                    self.report(level, msg[:250])
                    _set_status(context, msg)
                    if (
                        result.ok
                        and result.update_available
                        and not result.artifact_url
                        and result.download_page_url
                    ):
                        try:
                            bpy.ops.ual.open_website(
                                "INVOKE_DEFAULT", url=result.download_page_url
                            )
                        except Exception:
                            pass
                _tag_redraw_preferences()
                _tag_redraw_all()

            tasks_queue.add_task(done)

        tasks_queue.run_in_background(work)
        return {"FINISHED"}


class UAL_OT_download_and_install_update(bpy.types.Operator):
    """Download the published ZIP, verify sha256, and overwrite the installed addon."""

    bl_idname = "ual.download_and_install_update"
    bl_label = "Download & Install Update"
    bl_description = (
        "Download the latest published Blender package, verify checksum, apply over "
        "this install, then restart Blender"
    )
    bl_options = {"REGISTER"}

    def execute(self, context):
        from .. import tasks_queue
        from .. import update_installer
        from .. import update_manager
        from ..ui_strings import friendly_error

        st = update_manager.get_cached_state()
        if st.get("checking") or st.get("installing"):
            self.report({"INFO"}, "Update already in progress…")
            return {"CANCELLED"}
        if not getattr(bpy.app, "online_access", True):
            self.report({"WARNING"}, MSG_ONLINE_ACCESS)
            return {"CANCELLED"}

        result = st.get("result") if isinstance(st.get("result"), dict) else None
        if not result or not result.get("ok") or not result.get("update_available"):
            self.report(
                {"WARNING"},
                "Run Check for Updates first when a newer version is published.",
            )
            return {"CANCELLED"}
        artifact_url = str(result.get("artifact_url") or "").strip()
        cfg = preferences.prefs_to_config()
        prefs = preferences._get_prefs()
        cache_dir = preferences.active_cache_dir(prefs)
        update_manager.set_installing(True)
        _tag_redraw_preferences()
        _set_status(context, "Downloading update…", 0)
        holder = {"result": None, "error": ""}

        def work():
            try:
                url = artifact_url
                sha = str(result.get("artifact_sha256") or "").strip() or None
                filename = str(result.get("artifact_filename") or "").strip() or None
                version = str(result.get("latest_version") or "").strip() or None
                if not url and entitlement_client.account_ready_for_api(cfg):
                    minted = entitlement_client.mint_signed_download(
                        cfg,
                        channel="stable",
                        version=version or "",
                    )
                    if not minted.get("success"):
                        holder["error"] = str(
                            minted.get("error") or "Could not mint update download",
                        )
                        return
                    url = str(minted.get("downloadUrl") or "").strip()
                    sha = str(minted.get("sha256") or "").strip() or sha
                    filename = str(minted.get("filename") or "").strip() or filename
                if not url:
                    page = result.get("download_page_url")
                    if page:
                        try:
                            bpy.ops.ual.open_website("INVOKE_DEFAULT", url=str(page))
                        except Exception:
                            pass
                    holder["error"] = (
                        "No direct artifact URL — opened the Download page instead."
                    )
                    return
                holder["result"] = update_installer.download_verify_and_apply(
                    artifact_url=url,
                    expected_sha256=sha,
                    filename=filename,
                    cache_dir=cache_dir,
                    version=version,
                )
            except Exception as exc:  # noqa: BLE001
                holder["error"] = str(exc)

            def done():
                update_manager.set_installing(False)
                if holder["error"]:
                    msg = friendly_error(holder["error"])
                    _set_status(context, msg)
                    self.report({"WARNING"}, msg)
                else:
                    install = holder["result"]
                    msg = install.message
                    level = {"INFO"} if install.ok else {"WARNING"}
                    self.report(level, msg[:250])
                    _set_status(context, msg)
                    if install.ok:
                        update_manager.set_summary(msg)
                _tag_redraw_preferences()
                _tag_redraw_all()

            tasks_queue.add_task(done)

        tasks_queue.run_in_background(work)
        return {"FINISHED"}


class UAL_OT_health_check(bpy.types.Operator):
    bl_idname = "ual.health_check"
    bl_label = "Health Check"
    bl_description = (
        "Check library and cache folders, move the cache if it sits inside the library, "
        "then check whether online sites are reachable"
    )
    bl_options = {"REGISTER"}

    def execute(self, context):
        from .. import health_check
        from . import hover_preview

        prefs = preferences._get_prefs()
        # CRITICAL: use raw prefs.cache_dir — prefs_to_config() already "repairs"
        # in memory and would hide empty/colliding values from this operator.
        roots = [
            os.path.normpath(item.path)
            for item in prefs.library_roots
            if (item.path or "").strip()
        ]
        if not roots and (prefs.library_root or "").strip():
            roots = [os.path.normpath(prefs.library_root.strip())]
        cache_raw = (prefs.cache_dir or "").strip()
        online_ok = bool(getattr(bpy.app, "online_access", True))

        diag = health_check.diagnose_local_health(
            library_roots=roots,
            cache_dir=cache_raw,
            library_root=prefs.library_root or "",
            online_access=online_ok,
            copy_to_library=bool(getattr(prefs, "copy_to_library", True)),
            index_downloads=bool(getattr(prefs, "index_downloads", True)),
            keep_cache=bool(getattr(prefs, "keep_cache", False)),
        )
        issues = list(diag.issues)
        fixed = []

        # Shared-library safety: Copy OFF leaves downloads invisible to Houdini.
        if not bool(getattr(prefs, "copy_to_library", True)):
            try:
                prefs.copy_to_library = True
                prefs.keep_cache = False
                fixed.append("Turned Copy Downloads to Library ON (required for shared Houdini library)")
                issues = [
                    i
                    for i in issues
                    if "Copy Downloads to Library is OFF" not in i
                ]
            except Exception:
                pass
        if not bool(getattr(prefs, "index_downloads", True)):
            try:
                prefs.index_downloads = True
                fixed.append("Turned Index Downloads ON so My Library can list saved assets")
                issues = [i for i in issues if "Index Downloads is OFF" not in i]
            except Exception:
                pass

        cache_use = diag.cache_effective
        if diag.needs_cache_repair and diag.roots:
            try:
                cache_use = health_check.apply_cache_repair(diag.cache_effective)
                prefs.cache_dir = cache_use
                cfg = preferences.prefs_to_config()
                cfg["cache_dir"] = cache_use
                ual_config.save_config(cfg, cache_use)
                fixed.append("Moved cache folder to UAL CACHE BLENDER (outside the library)")
            except Exception:  # noqa: BLE001
                issues.append(
                    "Couldn't move the cache folder. Check folder permissions."
                )
                cache_use = cache_raw or diag.cache_effective

        write_err = health_check.probe_cache_writable(cache_use)
        if write_err:
            issues.append(write_err)

        pruned = 0
        if cache_use and os.path.isdir(cache_use):
            try:
                pruned = int(thumbnails.soft_prune_thumbs(cache_use, force=True) or 0)
            except Exception:
                pruned = 0
        if pruned:
            fixed.append(f"Cleared {pruned} old thumbnails")

        # Auth readiness (local — use Preferences → Test for live HTTP probes)
        try:
            from ..online_auth import auth_health_notes

            cfg_auth = preferences.prefs_to_config()
            auth_notes = auth_health_notes(
                cfg_auth,
                enabled_sources=preferences.enabled_source_ids(prefs),
            )
            diag.notes.extend(auth_notes)
        except Exception:
            pass

        # Ensure library roots exist as dirs when configured (create missing optional)
        for root in diag.roots:
            if root and not os.path.isdir(root):
                # Do not auto-create missing libraries — artist must pick a folder
                pass

        hover_preview.invalidate_assets_hit(clear_hover=True)
        _tag_redraw_all()

        try:
            health_check.set_last_local_diagnostics(
                health_check.HealthDiagnosis(
                    issues=list(issues),
                    fixed=list(fixed),
                    notes=list(diag.notes),
                    roots=list(diag.roots),
                    cache_raw=str(diag.cache_raw),
                    cache_effective=str(cache_use or diag.cache_effective),
                    needs_cache_repair=False,
                    ok=not issues,
                )
            )
        except Exception:
            pass

        level, msg = health_check.format_health_toast(
            issues, fixed, list(diag.notes)
        )
        _set_status(context, msg)
        self.report({level}, msg)

        # Always follow local health with platform connectivity (Houdini parity)
        _run_platform_probe_background(context, report_op=self)
        return {"FINISHED"}


class UAL_OT_send_diagnostics_report(bpy.types.Operator):
    bl_idname = "ual.send_diagnostics_report"
    bl_label = "Send Diagnostics Report"
    bl_description = (
        "Opt-in: send redacted Health Check diagnostics to UAL Support. "
        "Same report is merged into the existing ticket (no spam). Never runs automatically."
    )
    bl_options = {"REGISTER"}
    _busy = False

    def _redact(self, text: str) -> str:
        from ..diagnostics_redact import redact_support_text

        return redact_support_text(text)

    def invoke(self, context, event):
        try:
            return context.window_manager.invoke_confirm(self, event)
        except Exception:
            return self.execute(context)

    def execute(self, context):
        from .. import health_check, online_health
        from .. import entitlement_client
        from ..ui_strings import ADDON_VERSION
        import platform

        if UAL_OT_send_diagnostics_report._busy:
            self.report({"INFO"}, "Diagnostics send already in progress.")
            return {"CANCELLED"}

        diag = health_check.get_last_local_diagnostics()
        if diag is None:
            self.report({"ERROR"}, "Run Health Check first to capture diagnostics.")
            return {"CANCELLED"}

        cfg = preferences.prefs_to_config()
        if not entitlement_client.account_ready_for_api(cfg):
            self.report(
                {"WARNING"},
                "Set Updates API base URL and UAL API key first (Account → Profile).",
            )
            return {"CANCELLED"}

        platform_state = online_health.get_cached_state()
        if platform_state.get("checking"):
            self.report(
                {"INFO"},
                "Platform check still running — online rows in the report may be incomplete.",
            )
        from ..diagnostics_redact import redact_diagnostics_value

        diagnostics_json = redact_diagnostics_value(
            {
                "product": "blender",
                "ualVersion": ADDON_VERSION,
                "os": platform.platform(),
                "results": {
                    "issues": [self._redact(i) for i in diag.issues],
                    "fixed": [self._redact(i) for i in diag.fixed],
                    "notes": [self._redact(n) for n in diag.notes],
                    "cacheEffective": self._redact(diag.cache_effective),
                },
                "online": {
                    "summary": platform_state.get("summary"),
                    "rows": platform_state.get("rows"),
                    "checking": bool(platform_state.get("checking")),
                },
            }
        )

        subject = "UAL Diagnostics (Blender)"
        body = "Opt-in diagnostics from Blender Health Check."

        holder = {"result": None}
        UAL_OT_send_diagnostics_report._busy = True

        def work():
            holder["result"] = entitlement_client.create_support_ticket(
                cfg,
                subject=subject,
                body=body,
                diagnostics_json=diagnostics_json,
                timeout=12.0,
            )

        def done():
            UAL_OT_send_diagnostics_report._busy = False
            res = holder.get("result") or {}
            if isinstance(res, dict) and res.get("success"):
                if res.get("duplicate"):
                    self.report(
                        {"INFO"},
                        "Same diagnostics already on your Support ticket.",
                    )
                else:
                    self.report({"INFO"}, "Diagnostics sent to Support.")
            else:
                err = res.get("error") or "unknown error"
                self.report({"WARNING"}, f"Send failed: {self._redact(err)[:140]}")

        tasks_queue.run_in_background(work, on_done=done)
        return {"FINISHED"}


class UAL_OT_import_drop_path(bpy.types.Operator):
    bl_idname = "ual.import_drop_path"
    bl_label = "Import Dropped Asset"
    bl_options = {"REGISTER", "UNDO"}

    filepath: bpy.props.StringProperty(name="File Path", default="")
    asset_type: bpy.props.StringProperty(name="Type", default="")

    def execute(self, context):
        if not self.filepath:
            return {"CANCELLED"}
        cfg = _cfg()
        try:
            import_routing.perform_typed_import(
                self.filepath, asset_type=self.asset_type or None, cfg=cfg
            )
        except Exception as exc:  # noqa: BLE001
            _report_error(self, exc)
            return {"CANCELLED"}
        return {"FINISHED"}


# BlenderKit-style drag threshold (pixels) before handing off to drop modal
_SELECT_DRAG_THRESHOLD = 12


def _select_drag_modal(op, context, event):
    """Select / list modal: click = select done; drag past threshold = Drop into Scene.

    Grid tiles and list rows are native operators; hover tracker does not select.
    """
    from . import hover_preview

    if event.type == "MOUSEMOVE":
        dx = abs(event.mouse_x - op._start_x)
        dy = abs(event.mouse_y - op._start_y)
        if dx > _SELECT_DRAG_THRESHOLD or dy > _SELECT_DRAG_THRESHOLD:
            hover_preview.set_select_hold_active(False)
            hover_preview.cancel_scheduled()
            # Dismiss before handoff — drag invoke may CANCEL before it sets
            # drag_modal (auth / missing path), which would leave a stuck card.
            hover_preview.clear_hover_target()
            try:
                from .. import selection_keys

                ui = _ui(context)
                online = ui.scope == "online"
                key = str(getattr(op, "asset_key", "") or "").strip()
                idx = int(getattr(op, "index", -1))
                if not key:
                    if online and 0 <= idx < len(ui.online_results):
                        key = selection_keys.key_for_online_item(ui.online_results[idx])
                    elif (not online) and 0 <= idx < len(ui.assets):
                        key = selection_keys.key_for_library_item(ui.assets[idx])
                    else:
                        key = str(
                            getattr(ui, "online_selected_key" if online else "selected_key", "")
                            or ""
                        )
                kwargs = {}
                if key:
                    kwargs["target_key"] = key
                if idx >= 0:
                    kwargs["target_index"] = idx
                bpy.ops.ual.asset_drag_drop("INVOKE_DEFAULT", **kwargs)
            except Exception:
                pass
            return {"FINISHED"}
        return {"RUNNING_MODAL"}
    if event.type in {"LEFTMOUSE", "RIGHTMOUSE"} and event.value == "RELEASE":
        hover_preview.set_select_hold_active(False)
        # Selection settled → show GPU preview for the *selected* asset (no pointer hover).
        try:
            from . import asset_bar

            bar_on = asset_bar.is_visible()
        except Exception:
            bar_on = False
        if not hover_preview.is_pinned() and not bar_on:
            ui = _ui(context)
            online = ui.scope == "online"
            key = str(getattr(op, "asset_key", "") or "").strip()
            idx = int(getattr(op, "index", -1))
            if idx < 0:
                idx = ui.online_selected_index if online else ui.selected_index
            hover_preview.arm_hover_for_selection(
                context, index=idx, online=online, asset_key=key
            )
        return {"FINISHED"}
    if event.type == "ESC":
        hover_preview.set_select_hold_active(False)
        return {"CANCELLED"}
    return {"RUNNING_MODAL"}


class UAL_OT_select_library_index(bpy.types.Operator):
    bl_idname = "ual.select_library_index"
    bl_label = "Select Library Asset"
    bl_description = (
        "Click this name to select (the picture is a preview). "
        "Drag to Drop into Scene."
    )
    bl_options = {"INTERNAL"}

    index: bpy.props.IntProperty(default=0)
    asset_key: bpy.props.StringProperty(default="", options={"SKIP_SAVE", "HIDDEN"})

    def _resolve_index(self, ui) -> int:
        from .. import selection_keys

        key = str(self.asset_key or "").strip()
        if key:
            found = selection_keys.index_for_library_key(ui.assets, key)
            if found >= 0:
                return found
        return int(self.index)

    def invoke(self, context, event):
        from . import hover_preview

        ui = _ui(context)
        if not ui.assets:
            return {"CANCELLED"}
        idx = self._resolve_index(ui)
        self.index = idx
        hover_preview._remember_cursor_from_event(event)
        hover_preview.set_select_hold_active(True)
        hover_preview.cancel_scheduled()
        hover_preview.select_asset_at_index(
            context, idx, online=False, calibrate=False
        )
        self._start_x = event.mouse_x
        self._start_y = event.mouse_y
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        return _select_drag_modal(self, context, event)

    def execute(self, context):
        from . import hover_preview

        ui = _ui(context)
        if not ui.assets:
            return {"CANCELLED"}
        idx = self._resolve_index(ui)
        hover_preview.set_select_hold_active(False)
        hover_preview.select_asset_at_index(
            context, idx, online=False, calibrate=False
        )
        if not hover_preview.is_pinned():
            hover_preview.arm_hover_for_selection(
                context, index=idx, online=False, asset_key=str(self.asset_key or "")
            )
        return {"FINISHED"}


class UAL_OT_select_online_index(bpy.types.Operator):
    bl_idname = "ual.select_online_index"
    bl_label = "Select Online Asset"
    bl_description = (
        "Click this name to select (the picture is a preview). "
        "Drag to Drop into Scene."
    )
    bl_options = {"INTERNAL"}

    index: bpy.props.IntProperty(default=0)
    asset_key: bpy.props.StringProperty(default="", options={"SKIP_SAVE", "HIDDEN"})

    def _resolve_index(self, ui) -> int:
        from .. import selection_keys

        key = str(self.asset_key or "").strip()
        if key:
            found = selection_keys.index_for_online_key(ui.online_results, key)
            if found >= 0:
                return found
        return int(self.index)

    def invoke(self, context, event):
        from . import hover_preview

        ui = _ui(context)
        if not ui.online_results:
            return {"CANCELLED"}
        idx = self._resolve_index(ui)
        self.index = idx
        hover_preview._remember_cursor_from_event(event)
        hover_preview.set_select_hold_active(True)
        hover_preview.cancel_scheduled()
        hover_preview.select_asset_at_index(
            context, idx, online=True, calibrate=False
        )
        self._start_x = event.mouse_x
        self._start_y = event.mouse_y
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        return _select_drag_modal(self, context, event)

    def execute(self, context):
        from . import hover_preview

        ui = _ui(context)
        if not ui.online_results:
            return {"CANCELLED"}
        idx = self._resolve_index(ui)
        hover_preview.set_select_hold_active(False)
        hover_preview.select_asset_at_index(
            context, idx, online=True, calibrate=False
        )
        if not hover_preview.is_pinned():
            hover_preview.arm_hover_for_selection(
                context, index=idx, online=True, asset_key=str(self.asset_key or "")
            )
        return {"FINISHED"}


class UAL_OT_persist_view_prefs(bpy.types.Operator):
    bl_idname = "ual.persist_view_prefs"
    bl_label = "Save View Mode"
    bl_options = {"REGISTER"}

    def execute(self, context):
        preferences._persist_view_modes_from_wm(context)
        self.report({"INFO"}, "View preferences saved")
        return {"FINISHED"}


class UAL_OT_toggle_view_mode(bpy.types.Operator):
    """Houdini ViewModeToggle parity — one icon button flips grid ↔ list."""

    bl_idname = "ual.toggle_view_mode"
    bl_label = "Toggle Grid / List"
    bl_description = (
        "Toggle preview grid ↔ compact list (same as Houdini ViewModeToggle)"
    )
    bl_options = {"REGISTER"}

    def execute(self, context):
        from . import hover_preview

        ui = _ui(context)
        if ui.scope == "online":
            ui.online_view_mode = "list" if (ui.online_view_mode or "grid") == "grid" else "grid"
            mode = ui.online_view_mode
        else:
            ui.view_mode = "list" if (ui.view_mode or "grid") == "grid" else "grid"
            mode = ui.view_mode
        # Property update calls invalidate; reinforce in case update was skipped
        hover_preview.invalidate_assets_hit(clear_hover=True)
        label = "List" if mode == "list" else "Grid"
        self.report({"INFO"}, f"{label} view")
        return {"FINISHED"}


class UAL_OT_refresh_online_thumbs(bpy.types.Operator):
    bl_idname = "ual.refresh_online_thumbs"
    bl_label = "Refresh Previews"
    bl_description = "Re-download missing Online search previews for the current results"
    bl_options = {"REGISTER"}

    def execute(self, context):
        if not getattr(bpy.app, "online_access", True):
            self.report({"ERROR"}, MSG_ONLINE_ACCESS)
            return {"CANCELLED"}
        ui = _ui(context)
        if not ui.online_results:
            self.report({"WARNING"}, "No online results — press Enter or Search first")
            return {"CANCELLED"}
        _set_status(context, "Refreshing previews…")
        _schedule_online_thumb_fetch(context)
        return {"FINISHED"}


def reset_assets_view_scroll(context=None) -> None:
    """Jump Assets viewport back to the top (search / scope / source changes)."""
    try:
        ui = _ui(context or bpy.context)
        ui.assets_view_scroll = 0
    except Exception:
        pass


def nudge_assets_view_scroll(context, direction: str) -> bool:
    """Move the height-capped Assets window by one row (keeps chrome on-screen)."""
    from . import asset_grid
    from . import hover_preview

    ui = _ui(context)
    if ui is None:
        return False
    online = str(getattr(ui, "scope", "") or "") == "online"
    if online:
        mode = str(getattr(ui, "online_view_mode", "grid") or "grid")
    else:
        mode = str(getattr(ui, "view_mode", "grid") or "grid")
    try:
        vp, _cw, _ch, _ih = asset_grid.viewport_from_context(
            context, ui, online=online, mode=mode
        )
    except Exception:
        return False
    if int(vp.max_scroll) <= 0:
        return False
    step = max(1, int(vp.cols))
    start = int(vp.start)
    if str(direction or "").upper() == "DOWN":
        nxt = min(int(vp.max_scroll), start + step)
    else:
        nxt = max(0, start - step)
    if nxt == start:
        return False
    ui.assets_view_scroll = int(nxt)
    hover_preview.on_assets_viewport_scrolled(context, new_offset=int(nxt))
    _tag_redraw_all()
    return True


def assets_viewport_is_windowed(context) -> bool:
    """True when Assets is height-capped (wheel should not scroll the N-panel)."""
    from . import asset_grid

    ui = _ui(context)
    if ui is None:
        return False
    online = str(getattr(ui, "scope", "") or "") == "online"
    if online:
        mode = str(getattr(ui, "online_view_mode", "grid") or "grid")
    else:
        mode = str(getattr(ui, "view_mode", "grid") or "grid")
    try:
        vp, _cw, _ch, _ih = asset_grid.viewport_from_context(
            context, ui, online=online, mode=mode
        )
    except Exception:
        return False
    return int(vp.max_scroll) > 0


class UAL_OT_assets_scroll(bpy.types.Operator):
    """Scroll the height-capped Assets viewport (keeps Search and toolbar on-screen)."""

    bl_idname = "ual.assets_scroll"
    bl_label = "Scroll Assets"
    bl_description = "Scroll the asset list. Search, Type, Source, and toolbar stay put."
    bl_options = {"INTERNAL"}

    direction: bpy.props.EnumProperty(
        items=(
            ("UP", "Up", "Show previous rows"),
            ("DOWN", "Down", "Show next rows"),
        ),
        default="DOWN",
    )

    def execute(self, context):
        if nudge_assets_view_scroll(context, self.direction):
            return {"FINISHED"}
        return {"CANCELLED"}


class UAL_OT_nudge_selection(bpy.types.Operator):
    """Arrow-key grid/list selection nudge when the cursor is over the UAL N-panel."""

    bl_idname = "ual.nudge_selection"
    bl_label = "Nudge UAL Selection"
    bl_options = {"INTERNAL"}

    direction: bpy.props.EnumProperty(
        items=(
            ("LEFT", "Left", ""),
            ("RIGHT", "Right", ""),
            ("UP", "Up", ""),
            ("DOWN", "Down", ""),
        ),
        default="RIGHT",
    )

    def invoke(self, context, event):
        from . import hover_preview
        from . import asset_grid

        # Only when cursor is over a VIEW_3D UI region (N-panel)
        hover_preview._remember_cursor_from_event(event)
        region, _mx, _my = hover_preview._find_ui_under_mouse(context)
        if region is None:
            return {"PASS_THROUGH"}

        ui = _ui(context)
        online = ui.scope == "online"
        items = ui.online_results if online else ui.assets
        n = len(items)
        if n <= 0:
            return {"CANCELLED"}

        idx = int(ui.online_selected_index if online else ui.selected_index)
        mode = (
            (ui.online_view_mode if online else ui.view_mode) or "grid"
        )
        cols = 1
        if mode == "grid":
            try:
                cols = max(1, asset_grid.grid_metrics(context)[0])
            except Exception:
                cols = 2

        d = self.direction
        if d == "LEFT":
            idx = max(0, idx - 1)
        elif d == "RIGHT":
            idx = min(n - 1, idx + 1)
        elif d == "UP":
            idx = max(0, idx - cols)
        else:
            idx = min(n - 1, idx + cols)

        if online:
            hover_preview.select_asset_at_index(
                context, idx, online=True, calibrate=False
            )
        else:
            hover_preview.select_asset_at_index(
                context, idx, online=False, calibrate=False
            )
        # Keep selection inside the height-capped viewport
        try:
            ensure_assets_view_shows_index(context, idx)
        except Exception:
            pass
        # Do NOT calibrate_from_select here — cursor may still sit on another
        # tile; snapping hit origin to the keyboard target poisons hover.
        # Do NOT arm_hover — selection ≠ hover preview (mouse owns the tooltip).
        return {"FINISHED"}

    def execute(self, context):
        return self.invoke(context, None)


def ensure_assets_view_shows_index(
    context, index: int, *, prefer_top: bool = False
) -> None:
    """Keep ``index`` inside the height-capped Assets window.

    Arrow keys keep the target in view (usually last row). Load More passes
    ``prefer_top=True`` so the new site page starts at the top of the window.
    """
    from . import asset_grid
    from . import hover_preview

    ui = _ui(context)
    if ui is None:
        return
    idx = max(0, int(index or 0))
    online = str(getattr(ui, "scope", "") or "") == "online"
    if online:
        mode = str(getattr(ui, "online_view_mode", "grid") or "grid")
    else:
        mode = str(getattr(ui, "view_mode", "grid") or "grid")
    try:
        vp, _cw, _ch, _ih = asset_grid.viewport_from_context(
            context, ui, online=online, mode=mode
        )
    except Exception:
        return
    cols = max(1, int(vp.cols))
    start = int(vp.start)
    end = start + int(vp.count)
    if (not prefer_top) and idx >= start and idx < end:
        return
    if idx < start or prefer_top:
        nxt = (idx // cols) * cols
    else:
        last_row = max(0, int(vp.rows_visible) - 1)
        nxt = max(0, ((idx // cols) * cols) - (last_row * cols))
    nxt = max(0, min(int(vp.max_scroll), nxt))
    if nxt == start:
        return
    ui.assets_view_scroll = int(nxt)
    hover_preview.on_assets_viewport_scrolled(context, new_offset=int(nxt))
    _tag_redraw_all()


_CLASSES = (
    UAL_OT_sync_prefs_to_config,
    UAL_OT_open_preferences,
    UAL_OT_auth_required,
    UAL_OT_clear_type_filter,
    UAL_OT_prefs_add_library_root,
    UAL_OT_prefs_remove_library_root,
    UAL_OT_open_cache_folder,
    UAL_OT_show_cache_usage,
    UAL_OT_clear_cache,
    UAL_OT_open_settings_folder,
    UAL_OT_restore_default_prefs,
    UAL_OT_open_website,
    UAL_OT_test_source_connection,
    UAL_OT_dismiss_onboarding_banner,
    UAL_OT_dismiss_changelog,
    UAL_OT_rebuild_index,
    UAL_OT_refresh_list,
    UAL_OT_import_selected,
    UAL_OT_toggle_favorite,
    UAL_OT_delete_selected,
    UAL_OT_hide_selected_from_index,
    UAL_OT_online_search,
    UAL_OT_online_load_more,
    UAL_OT_online_download_import,
    UAL_OT_download_cancel,
    UAL_OT_check_online_platforms,
    UAL_OT_refresh_seat,
    UAL_OT_connect_api_key,
    UAL_OT_disconnect_account,
    UAL_OT_check_for_updates,
    UAL_OT_download_and_install_update,
    UAL_OT_health_check,
    UAL_OT_send_diagnostics_report,
    UAL_OT_import_drop_path,
    UAL_OT_select_library_index,
    UAL_OT_select_online_index,
    UAL_OT_persist_view_prefs,
    UAL_OT_toggle_view_mode,
    UAL_OT_refresh_online_thumbs,
    UAL_OT_assets_scroll,
    UAL_OT_nudge_selection,
)

_addon_keymaps = []


def _pro_operator_classes():
    """Pro operators live in ui.pro_operators (omitted from Community export)."""
    try:
        from . import pro_operators

        return tuple(pro_operators.CLASSES)
    except ImportError:
        return ()


def _on_seat_saved(cfg=None) -> None:
    """After seat persist: wire Pro UI RNA if needed, then Community→Pro prompt."""
    try:
        from .. import ensure_pro_ui_registered

        ensure_pro_ui_registered()
    except Exception:
        pass
    try:
        from . import pro_operators

        pro_operators.maybe_invoke_pro_package_install(cfg)
    except ImportError:
        pass


def register():
    disarm_online_auto_search()
    from .. import entitlement_client

    entitlement_client.set_seat_saved_hook(_on_seat_saved)
    for cls in _CLASSES + _pro_operator_classes():
        bpy.utils.register_class(cls)
    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon
    if kc:
        km = kc.keymaps.new(name="3D View", space_type="VIEW_3D")
        for key, direction in (
            ("LEFT_ARROW", "LEFT"),
            ("RIGHT_ARROW", "RIGHT"),
            ("UP_ARROW", "UP"),
            ("DOWN_ARROW", "DOWN"),
        ):
            kmi = km.keymap_items.new("ual.nudge_selection", key, "PRESS")
            kmi.properties.direction = direction
            _addon_keymaps.append((km, kmi))


def unregister():
    disarm_online_auto_search()
    try:
        from .. import entitlement_client

        entitlement_client.set_seat_saved_hook(None)
    except Exception:
        pass
    for km, kmi in _addon_keymaps:
        try:
            km.keymap_items.remove(kmi)
        except Exception:
            pass
    _addon_keymaps.clear()
    for cls in reversed(_CLASSES + _pro_operator_classes()):
        bpy.utils.unregister_class(cls)
