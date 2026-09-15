# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""Per-asset Online download option enrich (resolutions/formats) for the N-panel.

Search often leaves ``resolutions`` empty or incomplete. On selection we seed from
the result row, then optionally refresh via ``get_resolutions`` / ``get_formats``
off the UI thread (``tasks_queue.run_in_background``) and apply via a main-thread
timer (``tasks_queue.add_task`` → ``bpy.app.timers`` — never register timers from workers).
"""

from __future__ import annotations

import threading
from typing import Any, List, Optional, Tuple

from .download_options import (
    seed_download_option_caches,
    source_uses_download_resolution,
    split_option_csv,
)

_enrich_lock = threading.Lock()
_enrich_pending: Optional[Tuple[str, str, str]] = None  # source_id, asset_id, fmt
_enrich_result: Optional[dict] = None
_timer_registered = False


def _tag_redraw() -> None:
    try:
        import bpy

        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type in ("VIEW_3D", "PREFERENCES"):
                    area.tag_redraw()
    except Exception:
        pass


def _apply_enrich_on_main() -> Optional[float]:
    """Timer callback: write enriched CSV onto the selected Online result."""
    global _enrich_result, _timer_registered
    payload = None
    with _enrich_lock:
        payload = _enrich_result
        _enrich_result = None
    if not payload:
        _timer_registered = False
        return None
    try:
        from . import preferences

        ui = preferences.get_wm_ui()
        if ui is None:
            _timer_registered = False
            return None
        sid = str(payload.get("source_id") or "")
        aid = str(payload.get("asset_id") or "")
        idx = int(getattr(ui, "online_selected_index", 0) or 0)
        results = getattr(ui, "online_results", None)
        if not results or idx < 0 or idx >= len(results):
            _timer_registered = False
            return None
        item = results[idx]
        if str(item.source_id or "") != sid or str(item.asset_id or "") != aid:
            _timer_registered = False
            return None
        res_list: List[str] = list(payload.get("resolutions") or [])
        fmt_list: List[str] = list(payload.get("formats") or [])
        if res_list:
            item.resolutions = ",".join(res_list)
        if fmt_list:
            item.formats = ",".join(fmt_list)
        seed_download_option_caches(ui, item)
        _tag_redraw()
    except Exception as exc:  # noqa: BLE001 — never crash the timer
        print("[UAL] download-options apply failed: {}".format(exc))
    _timer_registered = False
    return None


def _ensure_timer() -> None:
    """Register the apply timer — must run on the Blender main thread only."""
    global _timer_registered
    if _timer_registered:
        return
    try:
        import bpy

        bpy.app.timers.register(_apply_enrich_on_main, first_interval=0.05)
        _timer_registered = True
    except Exception as exc:  # noqa: BLE001
        _timer_registered = False
        print("[UAL] download-options timer register failed: {}".format(exc))


def _schedule_apply_on_main() -> None:
    """Queue timer registration via tasks_queue (never call bpy from a worker)."""
    from . import tasks_queue

    tasks_queue.add_task(_ensure_timer)


def _needs_enrich(source_id: str, resolutions_csv: str, formats_csv: str) -> bool:
    sid = (source_id or "").strip().lower()
    res = split_option_csv(resolutions_csv)
    fmt = split_option_csv(formats_csv)
    if source_uses_download_resolution(sid) and len(res) < 2:
        return True
    if len(fmt) < 1 and sid in ("sketchfab", "fab", "polyhaven", "ambientcg"):
        return True
    # GPUOpen / ambientCG often ship a fake or empty list at search time
    if sid in ("gpuopen", "ambientcg", "lazytextures", "polyhaven") and not res:
        return True
    return False


def sync_download_options_for_selection(ui: Any) -> None:
    """Seed enum caches from the selected Online row; schedule enrich if needed."""
    if ui is None:
        return
    results = getattr(ui, "online_results", None)
    if not results:
        try:
            ui.online_dl_options_ready = False
            ui.online_dl_res_items = ""
            ui.online_dl_fmt_items = ""
        except Exception:
            pass
        return
    idx = int(getattr(ui, "online_selected_index", 0) or 0)
    idx = max(0, min(idx, len(results) - 1))
    item = results[idx]
    seed_download_option_caches(ui, item)
    source_id = str(getattr(item, "source_id", "") or "")
    asset_id = str(getattr(item, "asset_id", "") or "")
    if not _needs_enrich(source_id, item.resolutions, item.formats):
        return
    fmt_pick = str(getattr(ui, "online_dl_format", "") or "").strip()
    if fmt_pick.upper() in ("", "AUTO", "DEFAULT"):
        fmts = split_option_csv(item.formats)
        fmt_pick = fmts[0] if fmts else ""
    _schedule_enrich(source_id, asset_id, item.name, item.asset_type, item.formats, item.resolutions, fmt_pick)


def _schedule_enrich(
    source_id: str,
    asset_id: str,
    name: str,
    asset_type: str,
    formats_csv: str,
    resolutions_csv: str,
    fmt: str,
) -> None:
    global _enrich_pending
    key = (source_id, asset_id, fmt or "")
    with _enrich_lock:
        if _enrich_pending == key:
            return
        _enrich_pending = key

    def work() -> None:
        global _enrich_result, _enrich_pending
        try:
            from .sources import get_source
            from .sources.base import AssetResult

            src = get_source(source_id)
            asset = AssetResult(
                source_id=source_id,
                asset_id=asset_id,
                name=name or asset_id,
                asset_type=asset_type or "mesh",
                formats=split_option_csv(formats_csv),
                resolutions=split_option_csv(resolutions_csv),
            )
            formats = list(getattr(asset, "formats", None) or [])
            resolutions = list(getattr(asset, "resolutions", None) or [])
            if hasattr(src, "get_formats"):
                try:
                    lazy_fmt = src.get_formats(asset)
                    if lazy_fmt:
                        formats = [str(f) for f in lazy_fmt if f]
                except Exception as exc:  # noqa: BLE001
                    print("[UAL] get_formats({}): {}".format(source_id, exc))
            use_fmt = fmt or (formats[0] if formats else None)
            if hasattr(src, "get_resolutions"):
                try:
                    lazy_res = src.get_resolutions(asset, use_fmt)
                    if lazy_res:
                        resolutions = [str(r) for r in lazy_res if r]
                except TypeError:
                    try:
                        lazy_res = src.get_resolutions(asset)
                        if lazy_res:
                            resolutions = [str(r) for r in lazy_res if r]
                    except Exception as exc:  # noqa: BLE001
                        print("[UAL] get_resolutions({}): {}".format(source_id, exc))
                except Exception as exc:  # noqa: BLE001
                    print("[UAL] get_resolutions({}): {}".format(source_id, exc))
            with _enrich_lock:
                _enrich_result = {
                    "source_id": source_id,
                    "asset_id": asset_id,
                    "formats": formats,
                    "resolutions": resolutions,
                }
                if _enrich_pending == key:
                    _enrich_pending = None
        except Exception as exc:  # noqa: BLE001
            print("[UAL] download-options enrich failed: {}".format(exc))
            with _enrich_lock:
                if _enrich_pending == key:
                    _enrich_pending = None

    # Worker must not call bpy — schedule timer registration on the main thread.
    from . import tasks_queue

    tasks_queue.run_in_background(work, on_done=_schedule_apply_on_main)


def reset_download_options_after_search(ui: Any) -> None:
    """Call after Online search replaces ``online_results``."""
    sync_download_options_for_selection(ui)
