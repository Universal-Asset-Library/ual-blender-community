# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""GPU floating hover tooltip for N-panel browse (BlenderKit-style).

Draws over the 3D View WINDOW region via ``POST_PIXEL`` — never inserts
UILayout chrome into the N-panel (no grid shift / flicker).
"""

from __future__ import annotations

import os
from typing import Optional, Tuple

import bpy

from ..hover_hit import npanel_tooltip_rect

_draw_handle = None


def _tooltip_size_px() -> float:
    try:
        from . import hover_preview

        return float(max(160, min(int(hover_preview._preview_size()), 400)))
    except Exception:
        return 320.0


def _tile_center_y_ui(hover_index: int, hit: dict) -> Optional[float]:
    """Return hovered tile centre Y in UI-region coords, or None."""
    if not hit or int(hover_index) < 0:
        return None
    offset = max(0, int(hit.get("index_offset") or 0))
    local = int(hover_index) - offset
    count = int(hit.get("count") or 0)
    if local < 0 or local >= count:
        return None
    cols = max(1, int(hit.get("cols") or 1))
    cell_h = float(hit.get("cell_h") or 1.0)
    top = float(hit.get("first_row_top") or 0.0)
    mode = str(hit.get("mode") or "grid")
    if mode == "list":
        return top - (local + 0.5) * cell_h
    row = local // cols
    return top - (row + 0.5) * cell_h


def _find_view3d_regions(context) -> Tuple[Optional[object], Optional[object]]:
    """Return ``(window_region, ui_region)`` for the VIEW_3D under the cursor.

    Falls back to the first VIEW_3D when mouse position is unavailable.
    """
    screen = getattr(context, "screen", None)
    if screen is None:
        return None, None

    mouse_x = mouse_y = None
    try:
        from . import hover_preview

        if getattr(hover_preview, "_cursor_valid", False):
            mouse_x = int(getattr(hover_preview, "_cursor_x", 0) or 0)
            mouse_y = int(getattr(hover_preview, "_cursor_y", 0) or 0)
    except Exception:
        mouse_x = mouse_y = None

    first: Tuple[Optional[object], Optional[object]] = (None, None)
    for area in screen.areas:
        if area.type != "VIEW_3D":
            continue
        window_region = None
        ui_region = None
        for region in area.regions:
            if region.type == "WINDOW":
                window_region = region
            elif region.type == "UI":
                ui_region = region
        if window_region is None:
            continue
        if first[0] is None:
            first = (window_region, ui_region)
        if mouse_x is None or mouse_y is None:
            continue
        if (
            int(window_region.x) <= mouse_x < int(window_region.x) + int(window_region.width)
            and int(window_region.y) <= mouse_y < int(window_region.y) + int(window_region.height)
        ):
            return window_region, ui_region
        # Cursor may sit in the N-panel UI strip of this area
        if ui_region is not None and (
            int(ui_region.x) <= mouse_x < int(ui_region.x) + int(ui_region.width)
            and int(ui_region.y) <= mouse_y < int(ui_region.y) + int(ui_region.height)
        ):
            return window_region, ui_region
    return first


def _placeholder_path() -> str:
    try:
        from .. import thumbnails

        return os.path.join(
            os.path.dirname(thumbnails.__file__), "data", "placeholder_thumb.png"
        )
    except Exception:
        return ""


def _draw_callback():
    """POST_PIXEL: floating preview when N-panel hover is armed."""
    try:
        from . import hover_preview
        from .asset_bar import draw as bar_draw
        from ..hover_preview_data import hover_tooltip_chrome_height
    except Exception:
        return

    if not hover_preview.hover_card_is_ready():
        return
    try:
        context = bpy.context
    except Exception:
        return

    window_region, ui_region = _find_view3d_regions(context)
    if window_region is None:
        return
    # Only draw in the WINDOW region pass
    if getattr(context, "region", None) is not None and context.region.type != "WINDOW":
        return

    data = hover_preview.resolve_current_preview(context)
    if data is None:
        return

    hit = getattr(hover_preview, "_grid_hit", None) or {}
    hover_index = int(getattr(hover_preview, "_hover_index", -1) or -1)
    if hover_index < 0:
        hover_index = int(data.get("index", -1) or -1)
    tile_cy = _tile_center_y_ui(hover_index, hit)
    if tile_cy is None:
        # Hovered absolute index is outside the visible window (e.g. mid-scroll
        # before remap) — do not draw a floating preview for a ghost tile.
        if hit:
            return
        tile_cy = float(ui_region.height) * 0.5 if ui_region else float(window_region.height) * 0.5

    tip_size = _tooltip_size_px()
    details = list(data.get("details") or [])
    chrome = float(hover_tooltip_chrome_height(len(details) or 1))
    # Top status strip above the thumb
    status_band = 16.0
    ui_x = float(ui_region.x) if ui_region else float(window_region.x + window_region.width)
    ui_y = float(ui_region.y) if ui_region else float(window_region.y)
    tx, ty, tw, th = npanel_tooltip_rect(
        region_width=float(window_region.width),
        region_height=float(window_region.height),
        ui_region_x=ui_x,
        window_region_x=float(window_region.x),
        window_region_y=float(window_region.y),
        ui_region_y=ui_y,
        tile_center_y_ui=float(tile_cy),
        size=tip_size,
        chrome_height=chrome + status_band,
    )

    bar_draw.draw_rect(tx, ty, tw, th, (0.1, 0.1, 0.1, 0.95))
    footer_h = chrome
    img_bottom = ty + footer_h
    img_h = max(48.0, th - footer_h - status_band - 4.0)
    # Dark well behind the image so letterboxed mesh thumbs stay readable.
    bar_draw.draw_rect(tx + 8, img_bottom, tw - 16, img_h, (0.110, 0.110, 0.110, 1))
    thumb_path = (data.get("thumb_path") or "").strip()
    drew = False
    if thumb_path:
        drew = bar_draw.draw_image(
            thumb_path, tx + 8, img_bottom, tw - 16, img_h, fit="contain"
        )
    if not drew:
        ph = _placeholder_path()
        if not (
            ph
            and bar_draw.draw_image(
                ph, tx + 8, img_bottom, tw - 16, img_h, fit="contain"
            )
        ):
            pass  # well already drawn

    name = (data.get("name") or "Asset")[:40]
    # Bottom stack: name, then labeled meta (License / Source / product)
    y_text = ty + 6.0
    line_h = 13.0
    meta_rows = list(reversed(details[-4:]))  # draw bottom-up
    for label, value in meta_rows:
        label = (label or "").strip()
        value = (value or "").strip()
        if not label and not value:
            continue
        if label and value:
            bar_draw.draw_text(
                f"{label}",
                tx + 10,
                y_text,
                size=10,
                color=(0.55, 0.55, 0.55, 1),
            )
            # Value sits after a fixed label column (~58px) for easy scanning
            bar_draw.draw_text(
                value[:36],
                tx + 68,
                y_text,
                size=11,
                color=(0.82, 0.82, 0.82, 1),
            )
        else:
            text = value or label
            if data.get("auth_gated") and not label:
                text = f"{text} · Sign in required".strip(" ·")
            bar_draw.draw_text(
                text[:48],
                tx + 10,
                y_text,
                size=11,
                color=(0.72, 0.72, 0.72, 1),
            )
        y_text += line_h
    pin = "Pinned · " if hover_preview.is_pinned() else ""
    bar_draw.draw_text(pin + name, tx + 10, y_text + 2.0, size=13, color=(1, 1, 1, 1))
    try:
        status = hover_preview.preview_status_label(context)
    except Exception:
        status = "Hover · click name to select"
    bar_draw.draw_text(
        status,
        tx + 10,
        ty + th - 14,
        size=10,
        color=(0.85, 0.7, 0.4, 1),
    )


def ensure_handler() -> None:
    """Install the POST_PIXEL draw handler if missing."""
    global _draw_handle
    if _draw_handle is not None:
        return
    try:
        _draw_handle = bpy.types.SpaceView3D.draw_handler_add(
            _draw_callback, (), "WINDOW", "POST_PIXEL"
        )
    except Exception:
        _draw_handle = None


def remove_handler() -> None:
    """Remove the POST_PIXEL draw handler."""
    global _draw_handle
    if _draw_handle is None:
        return
    try:
        bpy.types.SpaceView3D.draw_handler_remove(_draw_handle, "WINDOW")
    except Exception:
        pass
    _draw_handle = None


def tag_window_redraw(context=None) -> None:
    """Redraw 3D View WINDOW regions so the tooltip appears/disappears."""
    try:
        ctx = context or bpy.context
        for area in ctx.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()
    except Exception:
        pass
