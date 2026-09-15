# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""Idempotent cancel of UAL hover / drag / asset-bar gestures.

Call on scope/view/source changes and during addon unregister so modals and
draw handlers never outlive the interaction that started them.
"""

from __future__ import annotations

from typing import Any, Optional


_cancel_depth = 0


def cancel_all_interactions(
    context: Any = None,
    *,
    reason: str = "",
    cancel_drag: bool = True,
    close_asset_bar: bool = True,
) -> None:
    """Best-effort teardown of active UAL overlay / modal gestures.

    Safe to call repeatedly (including from nested unregister paths).
    """
    global _cancel_depth
    if _cancel_depth > 0:
        return
    _cancel_depth += 1
    try:
        try:
            from .ui import hover_preview

            hover_preview.set_select_hold_active(False)
            hover_preview.cancel_scheduled()
            hover_preview.set_drag_modal_active(False)
            hover_preview.invalidate_assets_hit(clear_hover=True)
        except Exception:
            pass

        if cancel_drag:
            try:
                from .ui import drag_drop

                drag_drop.force_cancel_active(context)
            except Exception:
                pass

        if close_asset_bar:
            try:
                from .ui import asset_bar

                asset_bar.force_close(context)
            except Exception:
                pass

        if reason:
            try:
                print("[UAL] cancel_all_interactions: {}".format(reason))
            except Exception:
                pass
    finally:
        _cancel_depth -= 1
