# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""N-panel in-library indicator — UILayout only (no GPU overlay).

Blender does not expose UILayout widget pixel bounds. A POST_PIXEL green
border from chrome-estimated ``first_row_top`` cannot stay locked to
``template_icon`` tiles across panel widths (narrow / medium / wide). Same
class of bug that retired ``selection_overlay``.

Online grid tiles show a green status **dot** at the top-right of the thumb
box (``asset_grid._draw_square_tile`` + ``thumbnails.in_library_dot_icon_id``).
List mode uses the same dot. The GPU asset bar still draws its own green
outline from bar-owned layout math.
"""

from __future__ import annotations

import bpy

_draw_handle = None


def tag_redraw(context=None) -> None:
    """Redraw N-panel / VIEW_3D after in_library flags change (UILayout green dot)."""
    try:
        ctx = context or bpy.context
        for area in getattr(getattr(ctx, "screen", None), "areas", None) or ():
            if area.type == "VIEW_3D":
                area.tag_redraw()
    except Exception:
        pass


def ensure_handler() -> None:
    """No-op: remove any leftover UI-region GPU handler from older builds."""
    remove_handler()


def remove_handler() -> None:
    """Remove a leftover UI-region presence draw handler if present."""
    global _draw_handle
    if _draw_handle is None:
        return
    try:
        bpy.types.SpaceView3D.draw_handler_remove(_draw_handle, "UI")
    except Exception:
        pass
    _draw_handle = None
