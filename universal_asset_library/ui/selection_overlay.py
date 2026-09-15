# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""Legacy GPU selection overlay — disabled.

Blender does not expose UILayout widget pixel bounds to Python. Drawing a
POST_PIXEL square from estimated/calibrated hit geometry cannot stay locked
to ``template_icon`` tiles (wrong place/size). Grid selection is UILayout-
native in ``asset_grid._draw_square_tile`` (box + depress). List uses
operator ``depress``. Asset bar still draws its own GPU outline.
"""

from __future__ import annotations

import bpy

_draw_handle = None


def ensure_handler() -> None:
    """No-op: remove any leftover GPU handler from older builds."""
    remove_handler()


def remove_handler() -> None:
    """Remove a leftover UI-region selection draw handler if present."""
    global _draw_handle
    if _draw_handle is None:
        # Still try purge in case handle was lost across reload
        return
    try:
        bpy.types.SpaceView3D.draw_handler_remove(_draw_handle, "UI")
    except Exception:
        pass
    _draw_handle = None


def tag_ui_redraw(context=None) -> None:
    """Redraw N-panel / VIEW_3D after selection changes (UILayout depress)."""
    try:
        ctx = context or bpy.context
        for area in ctx.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()
    except Exception:
        pass
