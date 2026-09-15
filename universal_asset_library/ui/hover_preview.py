# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""Houdini/BlenderKit-style thumbnail hover preview for the Blender N-panel.

Houdini uses Qt ``entered`` / mouse-move on the asset grid (not selection).
Blender's N-panel has no mouse-enter API, so UAL approximates that UX by:

1. Tracking mouse moves (pass-through modal) + tagging VIEW_3D to redraw
2. Hit-testing the cursor against the Assets grid/list layout during panel draw
3. Debouncing, then drawing a **GPU POST_PIXEL tooltip** over the 3D View
   (``ui/hover_tooltip_gpu.py``) — never an inline UILayout card (that pushed
   the grid and caused flicker)

Rules
-----
- Hover updates the floating tooltip only — **never** writes selection.
- Grid thumb is display-only (``template_icon``). Select / drag start from the
  **name bar** under each tile, list rows, Asset Bar, or toolbar Drop.
- Import / Drop / Favorite stay on the Assets toolbar (tooltip is preview + meta).
- Selection uses stable keys (``selection_keys``) with indexes as a view only.
- ``invoke_popup`` was retired for dwell hover — it stole focus.
"""

from __future__ import annotations

import os
import time
from collections import OrderedDict
from typing import Optional, Tuple

import bpy

from .. import paths
from .. import preferences
from .. import selection_keys
from .. import thumbnails
from ..hover_hit import (
    absolute_tile_index,
    apply_scroll_delta,
    calibrate_origin_from_click,
    layout_signature,
    should_ignore_hover_retarget,
)
from ..hover_preview_data import (
    build_library_hover_details,
    build_online_hover_details,
    hover_thumb_upgrade_roots,
    online_hover_upgrade_candidates,
    resolve_card_target_index,
    resolve_library_hover_thumb_path,
)
from ..ui_strings import (
    ADDON_VERSION,
    assets_toolbar_row_count,
    changelog_seen,
    item_is_auth_gated,
    onboarding_banner_should_show,
)
from .icons import icon_for_type

_timer_handle = None
_track_timer = None
_pinned = False
_last_token = ""
_select_hold_active = False
_drag_modal_active = False
_suppress_selection_hover = False
_discover_cache: OrderedDict = OrderedDict()
_preview_resolve_cache: OrderedDict = OrderedDict()
_DISCOVER_CACHE_MAX = 256
_PREVIEW_RESOLVE_CACHE_MAX = 64
_debug_last: dict = {}

# Hover target (pointer dwell). Index wins; key is the hovered row, not selection.
_hover_index = -1
_hover_key = ""
_hover_online = False
_hover_token = ""
_grid_hit: dict = {}
_hover_card_ready = False
_capturing_click = False
# What the visible card's buttons act on (may differ from WM selection)
_card_target_index = -1
_card_target_online = False
# Cursor snapshot when the card armed — ignore retargets until the mouse moves.
_hover_arm_xy: Tuple[float, float] = (-1.0, -1.0)
# Last time Assets panel called note_assets_layout (stale = panel hidden/collapsed)
_layout_drawn_mono = 0.0
_LAYOUT_FRESH_S = 0.4
# Accumulate TRACKPADPAN so one swipe does not jump many rows
_trackpad_pan_acc = 0
_TRACKPAD_ROW_PX = 28
# N-panel pointer dwell drives the GPU big preview (hover ≠ select).
# Hit-test uses the full cell so the name bar matches the thumb (icon-band-only
# left the preview stuck on the selected tile while the cursor sat on another).
_NPANEL_POINTER_HOVER = True
# Watchdog only — real hover is MOUSEMOVE on the pass-through modal.
_WATCHDOG_DISABLED_S = 0.5
_WATCHDOG_NO_UI_S = 1.0
_WATCHDOG_S = 1.5


def _prefs():
    try:
        pkg = __package__.rsplit(".", 1)[0]
        return bpy.context.preferences.addons[pkg].preferences
    except Exception:
        return None


def _hover_debug(where: str, exc: BaseException, *, interval_s: float = 8.0) -> None:
    """Rate-limited stderr so panel/timer excepts are not silent forever."""
    now = time.monotonic()
    last = float(_debug_last.get(where) or 0.0)
    if now - last < interval_s:
        return
    _debug_last[where] = now
    print(f"UAL hover ({where}): {type(exc).__name__}: {exc}")


def _delay_seconds() -> float:
    prefs = _prefs()
    ms = int(getattr(prefs, "hover_preview_delay_ms", 160) or 160) if prefs else 160
    return max(0.12, min(ms / 1000.0, 0.8))


def _preview_enabled() -> bool:
    prefs = _prefs()
    if prefs is None:
        return True
    return bool(getattr(prefs, "hover_preview_enabled", True))


def _pin_enabled() -> bool:
    prefs = _prefs()
    if prefs is None:
        return True
    return bool(getattr(prefs, "hover_preview_pin_enabled", True))


def _preview_size() -> int:
    prefs = _prefs()
    size = int(getattr(prefs, "hover_preview_size", 320) or 320) if prefs else 320
    return max(160, min(size, 512))


def _ui_unit(context) -> float:
    try:
        return 20.0 * float(context.preferences.system.ui_scale)
    except Exception:
        return 20.0


def _asset_bar_active() -> bool:
    """True when the GPU asset bar owns hover/click (skip N-panel tooltip)."""
    try:
        from . import asset_bar

        return bool(asset_bar.is_visible())
    except Exception:
        return False


def set_suppress_selection_hover(active: bool) -> None:
    """Block hover while Library lists rebuild or Online search fills results."""
    global _suppress_selection_hover
    _suppress_selection_hover = bool(active)
    if _suppress_selection_hover:
        cancel_scheduled()
        clear_hover_target()


def is_selection_hover_suppressed() -> bool:
    return bool(_suppress_selection_hover)


def set_select_hold_active(active: bool) -> None:
    global _select_hold_active
    _select_hold_active = bool(active)
    if _select_hold_active:
        cancel_scheduled()


def set_drag_modal_active(active: bool) -> None:
    global _drag_modal_active
    _drag_modal_active = bool(active)
    if _drag_modal_active:
        cancel_scheduled()
        set_pinned(False)
        clear_hover_target()


def is_drag_modal_active() -> bool:
    return bool(_drag_modal_active)


def cancel_scheduled() -> None:
    global _timer_handle
    if _timer_handle is not None and bpy.app.timers.is_registered(_timer_handle):
        bpy.app.timers.unregister(_timer_handle)
    _timer_handle = None


def clear_discover_cache() -> None:
    """Drop cached preview-path discoveries (call after thumb/disk clears)."""
    _discover_cache.clear()
    _preview_resolve_cache.clear()


def clear_hover_target() -> None:
    global _hover_index, _hover_key, _hover_token, _hover_card_ready
    global _card_target_index, _card_target_online, _hover_arm_xy
    was_ready = bool(_hover_card_ready)
    _hover_index = -1
    _hover_key = ""
    _hover_token = ""
    _hover_card_ready = False
    _card_target_index = -1
    _card_target_online = False
    _hover_arm_xy = (-1.0, -1.0)
    if was_ready:
        _tag_window_redraw()
        # Drop POST_PIXEL handler when tooltip is gone — avoids paying a
        # VIEW_3D redraw tax for the rest of the Blender session.
        if not _pinned:
            try:
                from . import hover_tooltip_gpu

                hover_tooltip_gpu.remove_handler()
            except Exception as exc:
                _hover_debug("remove_hover_handler", exc)


def _tag_window_redraw(context=None) -> None:
    """Redraw 3D View so the GPU tooltip appears/disappears."""
    try:
        from . import hover_tooltip_gpu

        hover_tooltip_gpu.tag_window_redraw(context)
    except Exception as exc:
        _hover_debug("tag_window_redraw", exc)


def _tag_ui_redraw(context=None) -> None:
    try:
        ctx = context or bpy.context
        for area in ctx.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()
    except Exception as exc:
        _hover_debug("tag_ui_redraw", exc)
    _tag_window_redraw(context)


def set_pinned(value: bool) -> None:
    global _pinned
    _pinned = bool(value) and _pin_enabled() and _preview_enabled()


def toggle_pin() -> bool:
    global _pinned
    if not _pin_enabled() or not _preview_enabled():
        _pinned = False
        return False
    _pinned = not _pinned
    return _pinned


def is_pinned() -> bool:
    return bool(_pinned)


def _banner_flags(ui=None, cfg=None) -> Tuple[bool, bool]:
    changelog_visible = False
    onboarding_visible = False
    try:
        if cfg is None:
            cfg = preferences.load_active_config()
        ui_cfg = cfg.get("ui") or {}
        changelog_visible = not changelog_seen(cfg, ADDON_VERSION)
        dismissed = bool(ui_cfg.get("onboarding_banner_dismissed", False))
        scope = "library"
        empty = False
        if ui is not None:
            try:
                scope = str(getattr(ui, "scope", "library") or "library")
            except Exception:
                scope = "library"
            try:
                empty = len(getattr(ui, "assets", None) or []) == 0
            except Exception:
                empty = False
        onboarding_visible = onboarding_banner_should_show(
            dismissed=dismissed, scope=scope, library_empty=empty
        )
    except Exception as exc:
        _hover_debug("banner_flags", exc)
    return changelog_visible, onboarding_visible


def _toolbar_rows_for_ui(context, ui) -> int:
    try:
        region = getattr(context, "region", None)
        width = float(getattr(region, "width", 320) or 320) if region else 320.0
        return assets_toolbar_row_count(getattr(ui, "scope", "library") or "library", width)
    except Exception:
        return 1


def _thumb_size_flag(context) -> int:
    prefs = _prefs()
    try:
        return int(getattr(prefs, "thumb_size", 112) or 112) if prefs else 112
    except Exception:
        return 88


def _region_scroll_y(region) -> float:
    """View2d Y at region bottom-left â€” changes when the N-panel scrolls."""
    if region is None:
        return 0.0
    try:
        _x, y0 = region.view2d.region_to_view(0, 0)
        return float(y0)
    except Exception:
        return 0.0


def _card_chrome_px(context) -> float:
    """Inline card removed — GPU tooltip does not affect N-panel chrome."""
    del context
    return 0.0


def _download_chrome_active(ui) -> bool:
    return bool(
        getattr(ui, "download_active", False)
        and int(getattr(ui, "status_progress", -1) or -1) >= 0
    )


def _download_option_rows(ui) -> int:
    """Size/Format row under Online Assets toolbar (0 or 1)."""
    if ui is None or (getattr(ui, "scope", "") or "") != "online":
        return 0
    try:
        results = getattr(ui, "online_results", None)
        if not results:
            return 0
        idx = int(getattr(ui, "online_selected_index", 0) or 0)
        if idx < 0 or idx >= len(results):
            return 0
        return 1
    except Exception:
        return 0


def _estimate_assets_content_top(context, ui, cfg=None) -> float:
    """Approx. pixels from top of UI region to Assets grid/list content.

    Must stay in sync with main filter chrome + Assets header/toolbar.
    Underestimating shifts hit boxes **above** the real tiles — hover feels
    like the trigger is "somewhere else".

    Large hover preview is a GPU overlay (not UILayout), so it does **not**
    participate in chrome height.
    """
    from ..ui_strings import estimate_assets_chrome_ui_units
    from .layout_breakpoints import region_width, show_capability_line

    changelog_visible, onboarding_visible = _banner_flags(ui, cfg=cfg)
    scope = getattr(ui, "scope", "library") or "library" if ui is not None else "library"
    toolbar_rows = _toolbar_rows_for_ui(context, ui)
    download_option_rows = _download_option_rows(ui)
    cap = 1 if scope == "online" and show_capability_line(region_width(context)) else 0
    units = estimate_assets_chrome_ui_units(
        scope,
        changelog_visible=changelog_visible,
        onboarding_visible=onboarding_visible,
        download_active=_download_chrome_active(ui),
        toolbar_rows=toolbar_rows,
        hover_card_visible=False,
        hover_preview_size=_preview_size(),
        download_option_rows=download_option_rows,
        capability_lines=cap,
    )
    top_px = units * _ui_unit(context)
    if scope == "online":
        try:
            from .chrome_debug import note_estimate_chrome

            region = getattr(context, "region", None)
            note_estimate_chrome(
                changelog_visible=bool(changelog_visible),
                onboarding_visible=bool(onboarding_visible),
                toolbar_rows=int(toolbar_rows),
                download_option_rows=int(download_option_rows),
                capability_lines=int(cap),
                units=float(units),
                top_px=float(top_px),
                region_width=float(getattr(region, "width", 0) or 0),
                first_row_top=(
                    float(region.height) - float(top_px) if region is not None else 0.0
                ),
            )
        except Exception:
            pass
    return top_px


def estimate_assets_content_top_px(context, ui, cfg=None) -> float:
    """Public chrome height for Assets viewport windowing (panels).

    Pass ``cfg`` to skip an extra ``load_active_config()`` when the caller
    already loaded settings for this draw.
    """
    return _estimate_assets_content_top(context, ui, cfg=cfg)


def invalidate_assets_hit(*, clear_hover: bool = True) -> None:
    """Drop cached hit geometry (call on grid↔list / scope / search rebuild).

    ``clear_hover=False`` keeps pin + hover target (e.g. Online Load More) and
    only discards geometry so the next Assets draw remeasures.

    Do **not** call this for virtual Assets window scroll — use
    ``on_assets_viewport_scrolled`` (updates ``index_offset`` and clears the
    hover card so a still cursor cannot keep a wrong absolute asset).
    """
    global _grid_hit, _capturing_click, _last_poll_xy, _layout_drawn_mono
    _grid_hit = {}
    _layout_drawn_mono = 0.0
    _capturing_click = False
    _last_poll_xy = (-1, -1)
    set_select_hold_active(False)
    cancel_scheduled()
    if clear_hover:
        set_pinned(False)
        clear_hover_target()
        _tag_ui_redraw()


def on_assets_viewport_scrolled(context, *, new_offset: int) -> None:
    """Virtual grid/list window scrolled — update offset and drop hover card.

    Screen cells stay put; absolute indices under the cursor change. Keeping the
    previous card would show the wrong asset — clear immediately; the next dwell
    after a fresh layout draw re-arms.
    """
    global _last_poll_xy
    offset = max(0, int(new_offset or 0))
    if _grid_hit:
        _grid_hit["index_offset"] = offset
    _last_poll_xy = (-1, -1)
    if not _pinned:
        clear_hover_target()
        cancel_scheduled()
    _tag_ui_redraw()
    _tag_window_redraw()


def assets_layout_is_fresh() -> bool:
    """True when Assets panel recently ran ``note_assets_layout`` (visible/drawing)."""
    if not _grid_hit:
        return False
    return (time.monotonic() - _layout_drawn_mono) <= _LAYOUT_FRESH_S


def assets_grid_hit_snapshot() -> dict:
    """Shallow copy of the last Assets layout hit metrics (for GPU overlays)."""
    return dict(_grid_hit) if _grid_hit else {}


def note_assets_layout(
    context,
    *,
    online: bool,
    mode: str,
    item_count: int,
    columns: int,
    cell_w: float,
    cell_h: float,
    index_offset: int = 0,
    icon_h: float = 0.0,
) -> None:
    """Record grid/list metrics while Assets panel draws (for mouse hit-test).

    Call **after** drawing the asset cells. Large hover preview is a GPU
    overlay and does not shift tile geometry.

    ``item_count`` is the **visible window** size; ``index_offset`` maps local
    hit indices to absolute asset indices.

    ``icon_h`` is the drawn thumb height (inset ``template_icon``), used when
    calibrating click → origin so hover stays on the same tile.
    """
    global _grid_hit, _layout_drawn_mono
    region = getattr(context, "region", None)
    window = getattr(context, "window", None)
    if region is None or window is None or item_count <= 0:
        _grid_hit = {}
        _layout_drawn_mono = 0.0
        return

    _layout_drawn_mono = time.monotonic()
    cols = max(1, int(columns))
    mode_key = mode or "grid"
    offset = max(0, int(index_offset or 0))
    icon_band = float(icon_h) if float(icon_h or 0) > 1.0 else float(cell_w)
    ui = preferences.get_wm_ui(context)
    changelog_visible, onboarding_visible = _banner_flags(ui)
    toolbar_rows = _toolbar_rows_for_ui(context, ui)
    download_active = _download_chrome_active(ui)
    download_option_rows = _download_option_rows(ui) if online else 0
    width_bucket = int(float(region.width) // 16)
    sig = layout_signature(
        online=bool(online),
        mode=mode_key,
        count=int(item_count),
        cols=cols,
        thumb_size=_thumb_size_flag(context),
        changelog_visible=changelog_visible,
        onboarding_visible=onboarding_visible,
        toolbar_rows=toolbar_rows,
        download_active=download_active,
        region_width_bucket=width_bucket,
        download_option_rows=download_option_rows,
    )
    # Do NOT fold index_offset into the signature — virtual scroll only changes
    # which assets sit in the same screen cells; click calibration stays valid.
    scroll_y = _region_scroll_y(region)

    # Keep click-calibrated origin when layout signature is unchanged; apply scroll
    if (
        _grid_hit.get("calibrated")
        and _grid_hit.get("region_ptr") == region.as_pointer()
        and _grid_hit.get("signature") == sig
    ):
        _grid_hit["cell_w"] = float(cell_w)
        _grid_hit["cell_h"] = float(cell_h)
        _grid_hit["icon_h"] = icon_band
        _grid_hit["count"] = int(item_count)
        _grid_hit["cols"] = cols
        _grid_hit["index_offset"] = offset
        _grid_hit["online"] = bool(online)
        _grid_hit["mode"] = mode_key
        _grid_hit["region_width"] = float(region.width)
        _grid_hit["region_height"] = float(region.height)
        base_top = float(_grid_hit.get("first_row_top_base") or _grid_hit.get("first_row_top") or 0)
        anchor = float(_grid_hit.get("scroll_y") or 0)
        # UILayout draw often reports region_to_view Y as 0 while the real
        # cur.ymin is non-zero. Applying (0 - anchor) yanks first_row_top.
        if abs(scroll_y) < 1.0 and abs(anchor) > 1.0:
            _grid_hit["first_row_top"] = base_top
        else:
            _grid_hit["first_row_top"] = apply_scroll_delta(base_top, scroll_y, anchor)
        # Always retarget while drawing — wrong-tile freezes came from skipping this.
        _hit_test_mouse(context)
        return

    # Chrome / mode / window size changed — drop stale hover before remeasure.
    if _grid_hit and _grid_hit.get("signature") != sig and not _pinned:
        clear_hover_target()
        cancel_scheduled()

    top = _estimate_assets_content_top(context, ui)
    # Panel content is left-aligned; tiny pad matches typical UILayout inset.
    try:
        origin_x = 2.0 * float(context.preferences.system.ui_scale)
    except Exception:
        origin_x = 2.0
    first_row_top = float(region.height) - top
    _grid_hit = {
        "region_ptr": region.as_pointer(),
        "origin_x": origin_x,
        "first_row_top": first_row_top,
        "first_row_top_base": first_row_top,
        "scroll_y": scroll_y,
        "cell_w": float(cell_w),
        "cell_h": float(cell_h),
        "icon_h": icon_band,
        "cols": cols,
        "count": int(item_count),
        "index_offset": offset,
        "mode": mode_key,
        "online": bool(online),
        "region_width": float(region.width),
        "region_height": float(region.height),
        "calibrated": False,
        "signature": sig,
        "card_offset_px": 0.0,
    }
    if online:
        try:
            from .chrome_debug import note_scroll_at_layout

            note_scroll_at_layout(
                scroll_y=float(scroll_y),
                region_h=float(region.height),
                first_row_top=float(first_row_top),
            )
        except Exception:
            pass
    _hit_test_mouse(context)


def calibrate_from_select(context, index: int, *, online: bool) -> None:
    """Snap hit-test origin to the tile the user just clicked (Houdini-accurate).

    Must use the VIEW_3D **UI** region under the cursor — not ``context.region``.
    The hover modal runs with the 3D WINDOW as context region; using that made
    ``_mouse_in_region`` miss the N-panel (calibration no-op) so the selection
    ring stayed on a wrong chrome estimate.
    """
    global _grid_hit
    if not _grid_hit or int(index) < 0:
        return
    region, mx, my = _find_ui_under_mouse(context)
    if region is None:
        return
    cell_w = float(_grid_hit.get("cell_w") or 1)
    cell_h = float(_grid_hit.get("cell_h") or 1)
    icon_h = float(_grid_hit.get("icon_h") or cell_w)
    cols = max(1, int(_grid_hit.get("cols") or 1))
    mode = _grid_hit.get("mode") or "grid"
    offset = max(0, int(_grid_hit.get("index_offset") or 0))
    local = int(index) - offset
    if local < 0 or local >= int(_grid_hit.get("count") or 0):
        return
    scroll_y = _region_scroll_y(region)
    # Prefer existing chrome / calibrated top so select-bar clicks (below the
    # thumb) do not yank first_row_top and break later hover hit-tests.
    prefer_top = None
    if _grid_hit.get("first_row_top") is not None:
        prefer_top = float(_grid_hit.get("first_row_top") or 0.0)
    origin_x, first_row_top = calibrate_origin_from_click(
        mx,
        my,
        local,
        cell_w=cell_w,
        cell_h=cell_h,
        cols=cols,
        mode=mode,
        icon_h=icon_h,
        prefer_first_row_top=prefer_top,
    )
    _grid_hit["online"] = bool(online)
    _grid_hit["calibrated"] = True
    _grid_hit["region_ptr"] = region.as_pointer()
    _grid_hit["origin_x"] = origin_x
    _grid_hit["first_row_top"] = first_row_top
    _grid_hit["first_row_top_base"] = first_row_top
    _grid_hit["scroll_y"] = scroll_y
    _grid_hit["card_offset_px"] = 0.0


_cursor_x = -1
_cursor_y = -1
_cursor_valid = False


def _remember_cursor_from_event(event) -> None:
    """Blender 5.x: mouse lives on ``event`` only — not ``Window.mouse_x``."""
    global _cursor_x, _cursor_y, _cursor_valid
    try:
        _cursor_x = int(event.mouse_x)
        _cursor_y = int(event.mouse_y)
        _cursor_valid = True
    except Exception:
        _cursor_valid = False


def _cursor_in_region(region) -> Optional[Tuple[float, float]]:
    """Region-local cursor (origin bottom-left, exclusive-end bounds)."""
    if not _cursor_valid or region is None:
        return None
    try:
        mx = float(_cursor_x - region.x)
        my = float(_cursor_y - region.y)
        width = float(region.width)
        height = float(region.height)
    except Exception:
        return None
    if 0.0 <= mx < width and 0.0 <= my < height:
        return mx, my
    return None


def _mouse_in_region(context) -> Optional[Tuple[float, float]]:
    """Region-local cursor for ``context.region`` (Assets draw-time)."""
    if context is None:
        return None
    return _cursor_in_region(getattr(context, "region", None))


def _index_at_mouse(mx: float, my: float, region=None) -> int:
    """Full-cell hit (thumb + name bar) plus live view2d scroll when ``region`` given."""
    scroll_now = None
    if region is not None:
        scroll_now = _region_scroll_y(region)
    return absolute_tile_index(
        mx, my, _grid_hit, full_cell=True, scroll_now=scroll_now
    )


def _key_for_hover_index(index: int, online: bool) -> str:
    """Stable key for the hovered row so preview cannot stick on the selection."""
    if int(index) < 0:
        return ""
    try:
        ui = preferences.get_wm_ui()
        if ui is None:
            return ""
        if online:
            items = ui.online_results
            if 0 <= int(index) < len(items):
                return selection_keys.key_for_online_item(items[int(index)]) or ""
        else:
            items = ui.assets
            if 0 <= int(index) < len(items):
                return selection_keys.key_for_library_item(items[int(index)]) or ""
    except Exception:
        return ""
    return ""


def _apply_hover_index(index: int, online: bool, *, leave_panel: bool = False) -> None:
    """Update hover target only — never writes WM selection.

    Fast mouse passes cancel the previous dwell timer before arming a new one
    so we do not flash multiple tooltips.

    ``index < 0`` is sticky unless ``leave_panel``: gaps between tiles must not
    clear an armed tooltip. Only leaving the N-panel UI region clears.

    Once a tooltip is already open, moving onto a **different** tile swaps the
    preview immediately (no second dwell) — fixes wrong-asset lag.
    """
    global _hover_index, _hover_key, _hover_online, _hover_token, _hover_card_ready
    global _hover_arm_xy, _last_token
    if not _preview_enabled() or _suppress_selection_hover:
        return
    if _select_hold_active or _drag_modal_active or _pinned:
        return
    if _capturing_click:
        return
    if index < 0:
        if leave_panel and (_hover_index >= 0 or _hover_card_ready):
            clear_hover_target()
            cancel_scheduled()
            _tag_ui_redraw()
        return
    arm_x, arm_y = _hover_arm_xy
    if should_ignore_hover_retarget(
        current_index=_hover_index,
        new_index=index,
        card_ready=_hover_card_ready,
        cursor_x=float(_cursor_x) if _cursor_valid else -1.0,
        cursor_y=float(_cursor_y) if _cursor_valid else -1.0,
        arm_x=arm_x,
        arm_y=arm_y,
    ):
        return
    if index == _hover_index and bool(online) == bool(_hover_online):
        return
    was_ready = bool(_hover_card_ready)
    cancel_scheduled()
    _hover_index = index
    _hover_online = online
    _hover_key = _key_for_hover_index(index, online)
    _hover_token = _hover_key or f"{'online' if online else 'lib'}:{index}"
    if online:
        try:
            from . import operators as ops  # lazy: operators imports this module inside functions

            ops.prioritize_online_thumb_fetch(bpy.context, index)
        except Exception:
            pass
    if was_ready:
        # Instant content swap — already showing a tooltip on another tile.
        _hover_card_ready = True
        _last_token = _hover_token
        if _cursor_valid:
            _hover_arm_xy = (float(_cursor_x), float(_cursor_y))
        else:
            _hover_arm_xy = (-1.0, -1.0)
        try:
            from . import hover_tooltip_gpu

            hover_tooltip_gpu.ensure_handler()
        except Exception:
            pass
        _tag_ui_redraw()
        return
    _hover_card_ready = False
    schedule_hover_preview(force=True)


def _hit_test_mouse(context) -> None:
    """Update hover target from cursor during Assets panel draw.

    Same UI-region lookup as mouse-move, but only if that region is the panel
    currently drawing — two 3D-view sidebars must not map the other panel's
    cursor into this grid's metrics.
    """
    try:
        if not _preview_enabled() or _suppress_selection_hover:
            return
        if not _NPANEL_POINTER_HOVER:
            return
        if _select_hold_active or _drag_modal_active or _pinned:
            return
        if _asset_bar_active():
            return
        if not _grid_hit or not _cursor_valid:
            return
        if not assets_layout_is_fresh():
            return
        draw_region = getattr(context, "region", None)
        found, mx, my = _find_ui_under_mouse(context)
        if found is None or draw_region is None:
            return
        try:
            if found.as_pointer() != draw_region.as_pointer():
                return
        except Exception:
            return
        _apply_hover_index(
            _index_at_mouse(mx, my, found), bool(_grid_hit.get("online"))
        )
    except Exception as exc:
        # Never crash panel draw (e.g. missing mouse attrs on some Blender builds)
        _hover_debug("hit_test_mouse", exc)
        return


def suppress_for_asset_bar() -> None:
    """Call when the GPU asset bar opens — clear N-panel hover state."""
    invalidate_assets_hit(clear_hover=True)


def _find_ui_under_mouse(context):
    """Return ``(region, mx, my)`` for the VIEW_3D UI region under the cursor."""
    if not _cursor_valid:
        return None, 0.0, 0.0
    screen = getattr(context, "screen", None)
    if screen is None:
        return None, 0.0, 0.0
    for area in screen.areas:
        if area.type != "VIEW_3D":
            continue
        for region in area.regions:
            if region.type != "UI":
                continue
            pos = _cursor_in_region(region)
            if pos is not None:
                return region, pos[0], pos[1]
    return None, 0.0, 0.0


def select_asset_at_index(
    context,
    index: int,
    *,
    online: bool,
    calibrate: bool = True,
) -> bool:
    """Set WM selection to ``index`` (and stable key). Optionally calibrate hover hit-test.

    Does **not** open the hover card by itself — callers ``arm_hover_for_selection``
    when needed. N-panel pointer dwell never writes selection.
    """
    global _hover_index, _hover_key, _hover_online, _hover_token
    try:
        ui = preferences.get_wm_ui(context)
    except Exception:
        return False
    if ui is None:
        return False
    if online:
        if not ui.online_results or index < 0 or index >= len(ui.online_results):
            return False
        key = selection_keys.apply_online_index(ui, index) or ""
    else:
        if not ui.assets or index < 0 or index >= len(ui.assets):
            return False
        key = selection_keys.apply_library_index(ui, index) or ""
    _hover_index = index
    _hover_key = key
    _hover_online = online
    _hover_token = key or f"{'online' if online else 'lib'}:{index}"
    if calibrate and _NPANEL_POINTER_HOVER:
        calibrate_from_select(context, index, online=online)
    try:
        from . import selection_overlay

        selection_overlay.ensure_handler()
        selection_overlay.tag_ui_redraw(context)
    except Exception:
        pass
    try:
        for area in context.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()
    except Exception:
        pass
    return True


def hover_card_is_ready() -> bool:
    """True when the GPU hover tooltip should draw over the 3D View."""
    if not _preview_enabled() or _asset_bar_active():
        return False
    if _drag_modal_active:
        return False
    if _pinned:
        return True
    if _select_hold_active:
        return False
    return bool(_hover_card_ready and (_hover_key or _hover_index >= 0))


def arm_hover_for_selection(
    context,
    *,
    index: int,
    online: bool,
    asset_key: str = "",
) -> None:
    """Arm GPU preview for the selected asset (key-stable; no pointer hit-test)."""
    global _hover_index, _hover_key, _hover_online, _hover_token
    try:
        ui = preferences.get_wm_ui(context)
    except Exception:
        ui = None
    key = str(asset_key or "").strip()
    idx = int(index)
    if ui is not None:
        if online:
            if key:
                found = selection_keys.index_for_online_key(ui.online_results, key)
                if found >= 0:
                    idx = found
            if not key and 0 <= idx < len(ui.online_results):
                key = selection_keys.key_for_online_item(ui.online_results[idx])
        else:
            if key:
                found = selection_keys.index_for_library_key(ui.assets, key)
                if found >= 0:
                    idx = found
            if not key and 0 <= idx < len(ui.assets):
                key = selection_keys.key_for_library_item(ui.assets[idx])
    if idx < 0:
        return
    _hover_key = key
    arm_hover_for_index(idx, online=online)


def arm_hover_for_index(index: int, *, online: bool) -> None:
    """Point hover card at ``index`` without changing WM selection.

    If that asset is already shown, keep the tooltip (no hide + re-dwell).
    """
    global _hover_index, _hover_key, _hover_online, _hover_token, _hover_card_ready
    global _last_token, _hover_arm_xy
    if index < 0:
        return
    same_ready = (
        int(index) == int(_hover_index)
        and bool(online) == bool(_hover_online)
        and bool(_hover_card_ready)
    )
    _hover_index = index
    _hover_online = bool(online)
    if not _hover_key:
        _hover_token = f"{'online' if online else 'lib'}:{index}"
    else:
        _hover_token = _hover_key
    if online:
        try:
            from . import operators as ops  # lazy: operators imports this module inside functions

            ops.prioritize_online_thumb_fetch(bpy.context, index)
        except Exception:
            pass
    if same_ready:
        _last_token = _hover_token
        if _cursor_valid:
            _hover_arm_xy = (float(_cursor_x), float(_cursor_y))
        else:
            _hover_arm_xy = (-1.0, -1.0)
        try:
            from . import hover_tooltip_gpu

            hover_tooltip_gpu.ensure_handler()
        except Exception:
            pass
        _tag_ui_redraw()
        return
    _hover_card_ready = False
    schedule_hover_preview(force=True)


def resolve_card_target(context) -> Tuple[int, bool]:
    """Return ``(index, online)`` for the asset the visible card represents.

    Pointer hover / dwell index wins. A leftover **selected_key** must not keep
    showing Metal 049 A while the cursor is on Barrel_01.
    """
    try:
        ui = preferences.get_wm_ui(context)
        scope = getattr(ui, "scope", "library") or "library"
        selected = int(getattr(ui, "selected_index", 0) or 0)
        online_selected = int(getattr(ui, "online_selected_index", 0) or 0)
    except Exception:
        ui = None
        scope, selected, online_selected = "library", 0, 0
    if int(_hover_index) >= 0 and (_hover_card_ready or _pinned):
        if ui is not None and _hover_key:
            if _hover_online:
                found = selection_keys.index_for_online_key(
                    ui.online_results, _hover_key
                )
            else:
                found = selection_keys.index_for_library_key(ui.assets, _hover_key)
            # Key is reorder-stable only when it still names this hovered cell.
            if found >= 0 and found == int(_hover_index):
                return int(found), bool(_hover_online)
        return int(_hover_index), bool(_hover_online)
    if ui is not None and _hover_key:
        if _hover_online:
            found = selection_keys.index_for_online_key(ui.online_results, _hover_key)
        else:
            found = selection_keys.index_for_library_key(ui.assets, _hover_key)
        if found >= 0:
            return int(found), bool(_hover_online)
    return resolve_card_target_index(
        hover_index=_hover_index,
        hover_online=_hover_online,
        hover_card_ready=_hover_card_ready,
        pinned=_pinned,
        scope=scope,
        selected_index=selected,
        online_selected_index=online_selected,
    )


def get_card_target() -> Tuple[int, bool]:
    """Last card target stored during draw (fallback if resolve unavailable)."""
    return int(_card_target_index), bool(_card_target_online)


def preview_status_label(context) -> str:
    """GPU tooltip caption: hover vs the name-bar selection."""
    if _pinned:
        return "Pinned · Esc to close"
    idx, online = resolve_card_target(context)
    try:
        ui = preferences.get_wm_ui(context)
        if ui is not None:
            sel = int(
                getattr(
                    ui,
                    "online_selected_index" if online else "selected_index",
                    -1,
                )
                or -1
            )
            if int(idx) == sel and sel >= 0:
                return "Selected · Esc to close"
    except Exception:
        pass
    return "Hover · click name to select"


def _tile_index_under_mouse(
    context, region=None, mx: float = 0.0, my: float = 0.0
) -> Tuple[int, bool]:
    """Return ``(index, online)`` or ``(-1, False)`` if not over a tile.

    Pass ``region``/``mx``/``my`` from a prior ``_find_ui_under_mouse`` so
    mouse-move does not scan ``screen.areas`` twice.
    """
    if not _grid_hit or _grid_hit.get("count", 0) <= 0:
        return -1, False
    if region is None:
        region, mx, my = _find_ui_under_mouse(context)
    if region is None:
        return -1, False
    # Allow hit-test even if region pointer changed after panel redraw
    _grid_hit["region_ptr"] = region.as_pointer()
    _grid_hit["region_width"] = float(region.width)
    _grid_hit["region_height"] = float(region.height)
    index = _index_at_mouse(mx, my, region)
    return index, bool(_grid_hit.get("online"))


_last_poll_xy = (-1, -1)


def on_mouse_move_redraw(context) -> None:
    """Mouse-move hover maintenance for the N-panel GPU tooltip.

    Pointer dwell hit-test is on — preview follows the tile under the cursor.
    Selection is unchanged (click the name bar). Leave-panel dismiss still runs.
    """
    global _last_poll_xy
    if not _preview_enabled() or _drag_modal_active or _select_hold_active:
        return
    if _asset_bar_active() or _capturing_click:
        return
    if not _cursor_valid:
        return
    region, mx, my = _find_ui_under_mouse(context)
    if region is None:
        # Selection-armed or draw-hit tooltip: dismiss when leaving N-panel UI.
        if (_hover_index >= 0 or _hover_card_ready) and not _pinned:
            _apply_hover_index(-1, False, leave_panel=True)
        return
    if not _NPANEL_POINTER_HOVER:
        return
    xy = (_cursor_x, _cursor_y)
    if not assets_layout_is_fresh():
        if xy != _last_poll_xy and _hover_index >= 0 and not _pinned:
            clear_hover_target()
            cancel_scheduled()
            _tag_ui_redraw()
        _last_poll_xy = xy
        return
    if xy == _last_poll_xy and _hover_index < 0:
        return
    if (
        _hover_card_ready
        and _hover_index >= 0
        and xy == _last_poll_xy
    ):
        return
    _last_poll_xy = xy
    index, online = _tile_index_under_mouse(context, region, mx, my)
    _apply_hover_index(index, online)


def handle_tracker_event(context, event) -> set:
    """Pass-through tracker. N-panel dwell updates the GPU preview; hover ≠ select."""
    global _capturing_click
    global _trackpad_pan_acc

    _remember_cursor_from_event(event)

    if _drag_modal_active or _asset_bar_active():
        _capturing_click = False
        return {"PASS_THROUGH"}

    if _capturing_click:
        _capturing_click = False
        set_select_hold_active(False)

    if event.type == "MOUSEMOVE":
        on_mouse_move_redraw(context)
        return {"PASS_THROUGH"}

    wheel_up = event.type in {"WHEELUPMOUSE", "WHEELINMOUSE"}
    wheel_down = event.type in {"WHEELDOWNMOUSE", "WHEELOUTMOUSE"}
    if (wheel_up or wheel_down) and event.value == "PRESS":
        # Height-capped Assets window: eat the wheel so Blender does not
        # scroll the whole N-panel (Search / toolbar would slide off).
        if assets_layout_is_fresh():
            region, _mx, _my = _find_ui_under_mouse(context)
            if region is not None:
                from .operators import assets_viewport_is_windowed, nudge_assets_view_scroll

                direction = "UP" if wheel_up else "DOWN"
                moved = nudge_assets_view_scroll(context, direction)
                if moved or assets_viewport_is_windowed(context):
                    return {"RUNNING_MODAL"}
        return {"PASS_THROUGH"}

    if event.type == "TRACKPADPAN":
        # Precision touchpads send TRACKPADPAN, not WHEEL* — same steal as wheel.
        if assets_layout_is_fresh():
            region, _mx, _my = _find_ui_under_mouse(context)
            if region is not None:
                from .operators import assets_viewport_is_windowed, nudge_assets_view_scroll

                if assets_viewport_is_windowed(context):
                    dy = int(getattr(event, "mouse_y", 0) or 0) - int(
                        getattr(event, "mouse_prev_y", 0) or 0
                    )
                    _trackpad_pan_acc += dy
                    while _trackpad_pan_acc >= _TRACKPAD_ROW_PX:
                        _trackpad_pan_acc -= _TRACKPAD_ROW_PX
                        nudge_assets_view_scroll(context, "UP")
                    while _trackpad_pan_acc <= -_TRACKPAD_ROW_PX:
                        _trackpad_pan_acc += _TRACKPAD_ROW_PX
                        nudge_assets_view_scroll(context, "DOWN")
                    return {"RUNNING_MODAL"}
        _trackpad_pan_acc = 0
        return {"PASS_THROUGH"}

    return {"PASS_THROUGH"}


class UAL_OT_hover_mouse_track(bpy.types.Operator):
    """Pass-through tracker: hover preview only (selection is native UILayout)."""

    bl_idname = "ual.hover_mouse_track"
    bl_label = "UAL Hover Mouse Track"
    bl_options = {"INTERNAL"}

    _running = False

    def modal(self, context, event):
        try:
            return handle_tracker_event(context, event)
        except Exception as exc:
            _hover_debug("hover_mouse_track", exc)
            return {"PASS_THROUGH"}

    def invoke(self, context, event):
        if UAL_OT_hover_mouse_track._running:
            return {"CANCELLED"}
        UAL_OT_hover_mouse_track._running = True
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        return self.invoke(context, None)


def schedule_hover_preview(*, force: bool = False) -> None:
    """Debounce then arm the GPU hover tooltip (never changes selection).

    Uses a 3D View POST_PIXEL overlay instead of UILayout / ``invoke_popup``
    so the N-panel grid never shifts under the cursor.
    """
    global _timer_handle, _last_token, _hover_card_ready
    if not _preview_enabled():
        cancel_scheduled()
        return
    if _asset_bar_active():
        cancel_scheduled()
        return
    if _suppress_selection_hover or _select_hold_active or _drag_modal_active:
        cancel_scheduled()
        return
    if _pinned and not force:
        return
    if _hover_index < 0 and not _pinned:
        cancel_scheduled()
        return
    token = _hover_token or f"pin:{_last_token}"
    if token == _last_token and not force and _hover_card_ready:
        return
    _last_token = token
    cancel_scheduled()

    def _fire():
        global _timer_handle, _hover_card_ready, _hover_arm_xy
        _timer_handle = None
        if not _preview_enabled() or _suppress_selection_hover:
            return None
        if _asset_bar_active() or _select_hold_active or _drag_modal_active:
            return None
        if _hover_index < 0 and not _pinned:
            return None
        # Do NOT write selected_index / online_selected_index — hover ≠ select
        _hover_card_ready = True
        # Snapshot cursor so races under a still mouse cannot retarget.
        if _cursor_valid:
            _hover_arm_xy = (float(_cursor_x), float(_cursor_y))
        else:
            _hover_arm_xy = (-1.0, -1.0)
        try:
            from . import hover_tooltip_gpu

            hover_tooltip_gpu.ensure_handler()
        except Exception as exc:
            _hover_debug("hover_fire_handler", exc)
        _tag_ui_redraw()
        return None

    _timer_handle = _fire
    bpy.app.timers.register(_fire, first_interval=_delay_seconds())


def draw_inline_hover_card(layout, context) -> bool:
    """Retired: large preview is GPU overlay (kept as no-op for callers)."""
    del layout, context
    return False


def reset_hover_state() -> None:
    global _last_token, _select_hold_active, _drag_modal_active, _suppress_selection_hover
    global _hover_card_ready, _capturing_click
    global _layout_drawn_mono
    cancel_scheduled()
    set_pinned(False)
    clear_hover_target()
    _last_token = ""
    _select_hold_active = False
    _drag_modal_active = False
    _suppress_selection_hover = False
    _hover_card_ready = False
    _capturing_click = False
    _layout_drawn_mono = 0.0
    _discover_cache.clear()
    _preview_resolve_cache.clear()
    _grid_hit.clear()
    try:
        from . import hover_tooltip_gpu

        hover_tooltip_gpu.remove_handler()
        hover_tooltip_gpu.tag_window_redraw()
    except Exception as exc:
        _hover_debug("reset_hover_state", exc)


def _lru_get(cache: OrderedDict, key):
    if key not in cache:
        return False, None
    cache.move_to_end(key)
    return True, cache[key]


def _lru_put(cache: OrderedDict, key, value, maxsize: int) -> None:
    if key in cache:
        cache[key] = value
        cache.move_to_end(key)
        return
    cache[key] = value
    while len(cache) > maxsize:
        cache.popitem(last=False)


def _cached_discover(path: str, asset_type: str) -> str:
    key = f"{path}|{asset_type or ''}"
    hit, cached = _lru_get(_discover_cache, key)
    if hit:
        return cached
    try:
        found = thumbnails.discover_preview_image(path, asset_type) or ""
    except Exception as exc:
        _hover_debug("discover_preview", exc)
        found = ""
    _lru_put(_discover_cache, key, found, _DISCOVER_CACHE_MAX)
    return found


def _preview_resolve_cache_key(idx: int, online: bool, item) -> tuple:
    token = _hover_token or f"{'online' if online else 'lib'}:{idx}"
    return (
        token,
        int(idx),
        bool(online),
        str(getattr(item, "thumb_path", "") or ""),
        int(getattr(item, "preview_icon_id", 0) or 0),
        str(getattr(item, "asset_id", "") or ""),
        str(getattr(item, "path", "") or ""),
        bool(getattr(item, "in_library", False)),
    )


def resolve_current_preview(context) -> Optional[dict]:
    """Build hover card from card target (hover / pinned fallback to selection).

    Called from GPU POST_PIXEL every redraw — cache the disk/discover outcome
    per hover token so we do not walk packages every frame (0.3.2 upgrade
    rules are unchanged; only the result is reused).
    """
    ui = preferences.get_wm_ui(context)
    if ui is None:
        return None
    idx, online = resolve_card_target(context)

    if online:
        if not ui.online_results:
            return None
        idx = max(0, min(idx, len(ui.online_results) - 1))
        item = ui.online_results[idx]
        cache_key = _preview_resolve_cache_key(idx, True, item)
        hit, cached = _lru_get(_preview_resolve_cache, cache_key)
        if hit:
            return cached
        details = build_online_hover_details(
            source_id=item.source_id,
            mapped_type=item.asset_type,
            license_name=item.license_name,
            in_library=bool(getattr(item, "in_library", False)),
            resolutions=getattr(item, "resolutions", "") or "",
            formats=getattr(item, "formats", "") or "",
        )
        auth_gated = False
        try:
            auth_gated = item_is_auth_gated(
                item.source_id or "", preferences.load_active_config()
            )
        except Exception as exc:
            _hover_debug("preview_auth_gate", exc)
            auth_gated = False
        icon_value = int(getattr(item, "preview_icon_id", 0) or 0)
        resolved_thumb = ""
        try:
            thumb_path = getattr(item, "thumb_path", "") or ""
            resolved_thumb = thumb_path
            if thumb_path:
                big = thumbnails.icon_id_for_file(thumb_path, kind="hover")
                if big:
                    icon_value = big
            # Prefer a better on-disk preview (per-asset package only — never
            # the shared thumbs/ pool, which made big previews look random).
            try:
                cache_dir = preferences.load_active_config().get("cache_dir") or ""
            except Exception:
                cache_dir = ""
            package_dir = ""
            if cache_dir and item.source_id and item.asset_id:
                try:
                    package_dir = paths.source_cache_dir(
                        cache_dir, item.source_id, item.asset_id
                    )
                except Exception as exc:
                    _hover_debug("preview_package_dir", exc)
                    package_dir = ""
            thumbs = paths.thumbs_dir(cache_dir) if cache_dir else ""
            upgrade_roots = hover_thumb_upgrade_roots(
                thumb_path=thumb_path,
                thumbs_dir=thumbs,
                source_package_dir=package_dir,
            )
            for discovered in online_hover_upgrade_candidates(
                thumb_path=thumb_path,
                asset_type=item.asset_type,
                upgrade_roots=upgrade_roots,
                discover_fn=thumbnails.discover_preview_image,
            ):
                big = thumbnails.icon_id_for_file(discovered, kind="hover")
                if big:
                    icon_value = big
                    resolved_thumb = discovered
                    break
            if not icon_value:
                icon_value = thumbnails.placeholder_icon_id()
        except Exception as exc:
            _hover_debug("preview_online_resolve", exc)
        result = {
            "name": item.name,
            "icon_value": icon_value,
            "type_icon": icon_for_type(item.asset_type),
            "details": details,
            "index": idx,
            "online": True,
            "auth_gated": auth_gated,
            "source_id": item.source_id or "",
            "thumb_path": resolved_thumb or "",
        }
        _lru_put(_preview_resolve_cache, cache_key, result, _PREVIEW_RESOLVE_CACHE_MAX)
        return result
    if not ui.assets:
        return None
    idx = max(0, min(idx, len(ui.assets) - 1))
    item = ui.assets[idx]
    cache_key = _preview_resolve_cache_key(idx, False, item)
    hit, cached = _lru_get(_preview_resolve_cache, cache_key)
    if hit:
        return cached
    size_label = ""
    try:
        path = getattr(item, "path", "") or ""
        if path and os.path.isfile(path):
            nbytes = os.path.getsize(path)
            if nbytes >= 1024 * 1024:
                size_label = f"{nbytes / (1024 * 1024):.1f} MB"
            elif nbytes >= 1024:
                size_label = f"{nbytes / 1024:.0f} KB"
            else:
                size_label = f"{nbytes} B"
    except OSError:
        pass
    details = build_library_hover_details(
        asset_type=item.asset_type,
        file_size_label=size_label,
        source_id=getattr(item, "source_id", "") or "",
    )
    icon_value = int(getattr(item, "preview_icon_id", 0) or 0)
    resolved_thumb = ""
    try:
        item_thumb = (getattr(item, "thumb_path", "") or "").strip()
        discovered = _cached_discover(item.path, item.asset_type)
        resolved_thumb = resolve_library_hover_thumb_path(
            item_thumb_path=item_thumb,
            discovered=discovered,
        )
        if resolved_thumb:
            big = thumbnails.icon_id_for_file(resolved_thumb, kind="hover")
            if big:
                icon_value = big
        if not icon_value:
            icon_value = thumbnails.placeholder_icon_id()
    except Exception as exc:
        _hover_debug("preview_library_resolve", exc)
    result = {
        "name": item.name or item.path,
        "icon_value": icon_value,
        "type_icon": icon_for_type(item.asset_type),
        "details": details,
        "index": idx,
        "online": False,
        "auth_gated": False,
        "thumb_path": resolved_thumb or "",
    }
    _lru_put(_preview_resolve_cache, cache_key, result, _PREVIEW_RESOLVE_CACHE_MAX)
    return result


class UAL_OT_hover_preview_pin(bpy.types.Operator):
    bl_idname = "ual.hover_preview_pin"
    bl_label = "Pin Hover Preview"
    bl_description = "Pin the hover preview to the current asset (Esc or press again to unpin)"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        if not _preview_enabled():
            self.report({"WARNING"}, "Hover preview is disabled in Preferences")
            return {"CANCELLED"}
        pinned = toggle_pin()
        if pinned:
            schedule_hover_preview(force=True)
            self.report({"INFO"}, "Hover preview pinned. Press Esc to unpin")
        else:
            cancel_scheduled()
            self.report({"INFO"}, "Hover preview unpinned")
        return {"FINISHED"}


class UAL_OT_hover_preview_unpin(bpy.types.Operator):
    bl_idname = "ual.hover_preview_unpin"
    bl_label = "Unpin Hover Preview"
    bl_options = {"INTERNAL"}

    @classmethod
    def poll(cls, context):
        # Esc is shared with the GPU asset bar — don't steal it while the bar is open.
        # Also dismiss an unpinned selection-armed card (otherwise Esc did nothing).
        if _asset_bar_active():
            return False
        return is_pinned() or hover_card_is_ready()

    def execute(self, context):
        set_pinned(False)
        cancel_scheduled()
        clear_hover_target()
        self.report({"INFO"}, "Hover preview closed")
        return {"FINISHED"}


def _view3d_ui_region_exists(context=None) -> bool:
    """Cheap: any VIEW_3D with an N-panel UI region (no hit-test)."""
    try:
        ctx = context or bpy.context
        screens = []
        wm = getattr(ctx, "window_manager", None)
        windows = getattr(wm, "windows", None) if wm is not None else None
        if windows:
            for win in windows:
                scr = getattr(win, "screen", None)
                if scr is not None:
                    screens.append(scr)
        if not screens:
            screen = getattr(ctx, "screen", None)
            if screen is not None:
                screens.append(screen)
        for screen in screens:
            for area in screen.areas:
                if area.type != "VIEW_3D":
                    continue
                for region in area.regions:
                    if region.type == "UI":
                        return True
    except Exception as exc:
        _hover_debug("view3d_ui_probe", exc)
        return True
    return False


def _ensure_mouse_track() -> None:
    if UAL_OT_hover_mouse_track._running:
        return
    try:
        bpy.ops.ual.hover_mouse_track("INVOKE_DEFAULT")
    except Exception as exc:
        _hover_debug("ensure_mouse_track", exc)


def _start_track_timer() -> None:
    global _track_timer

    def _tick():
        if not _preview_enabled():
            return _WATCHDOG_DISABLED_S
        try:
            has_ui = _view3d_ui_region_exists()
        except Exception as exc:
            _hover_debug("watchdog_visible", exc)
            has_ui = True
        if not has_ui:
            return _WATCHDOG_NO_UI_S
        try:
            _ensure_mouse_track()
        except Exception as exc:
            _hover_debug("watchdog_ensure", exc)
        return _WATCHDOG_S

    if _track_timer is not None and bpy.app.timers.is_registered(_track_timer):
        return
    _track_timer = _tick
    bpy.app.timers.register(_tick, first_interval=0.5)


_CLASSES = (
    UAL_OT_hover_preview_pin,
    UAL_OT_hover_preview_unpin,
    UAL_OT_hover_mouse_track,
)

_addon_keymaps = []


def register():
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon
    if kc:
        km = kc.keymaps.new(name="3D View", space_type="VIEW_3D")
        kmi = km.keymap_items.new(
            "ual.hover_preview_pin", "SPACE", "PRESS", ctrl=True, shift=True
        )
        _addon_keymaps.append((km, kmi))
        kmi_esc = km.keymap_items.new("ual.hover_preview_unpin", "ESC", "PRESS")
        _addon_keymaps.append((km, kmi_esc))
    try:
        from . import selection_overlay

        selection_overlay.ensure_handler()
    except Exception:
        pass
    try:
        from . import library_presence_overlay

        library_presence_overlay.ensure_handler()
    except Exception:
        pass
    _start_track_timer()


def unregister():
    global _track_timer
    try:
        from . import hover_tooltip_gpu

        hover_tooltip_gpu.remove_handler()
    except Exception:
        pass
    try:
        from . import library_presence_overlay

        library_presence_overlay.remove_handler()
    except Exception:
        pass
    try:
        from . import selection_overlay

        selection_overlay.remove_handler()
    except Exception:
        pass
    reset_hover_state()
    UAL_OT_hover_mouse_track._running = False
    if _track_timer is not None and bpy.app.timers.is_registered(_track_timer):
        bpy.app.timers.unregister(_track_timer)
    _track_timer = None
    for km, kmi in _addon_keymaps:
        try:
            km.keymap_items.remove(kmi)
        except Exception:
            pass
    _addon_keymaps.clear()
    for cls in reversed(_CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass
    try:
        from .. import register_utils

        for name in (
            "UAL_OT_hover_preview_pin",
            "UAL_OT_hover_preview_unpin",
            "UAL_OT_hover_mouse_track",
        ):
            register_utils.purge_named_types((name,))
    except Exception:
        pass
