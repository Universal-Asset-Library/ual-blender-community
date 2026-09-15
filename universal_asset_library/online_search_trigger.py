# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""When Online search should run (pure — no bpy).

Library/Favorites keep live ``search_query`` (TEXTEDIT_UPDATE).
Online uses ``online_query`` *without* that flag so Enter / click-away confirms
the field and can trigger search. Type and Source changes also re-search.

Do **not** search on every Online keystroke (network cost).
Do **not** auto-search while the addon is still restoring WM props on enable
— ``arm_online_auto_search()`` must run *before* restoring ``scope`` /
``online_source`` so Type/Source updates can schedule after enable.
"""

from __future__ import annotations

from typing import Any


def online_query_text(ui: Any) -> str:
    """Text sent to connectors. Prefer ``online_query``; fall back for older WM RNA."""
    if ui is None:
        return ""
    raw = getattr(ui, "online_query", None)
    if raw is None:
        raw = getattr(ui, "search_query", "") or ""
    return str(raw or "")


def should_schedule_online_search(scope: str, *, armed: bool, force: bool = False) -> bool:
    """True when a Type/Source/Enter confirm may hit the network.

    ``force`` is for user Source/Type changes — those must search even if the
    session never armed (partial reload / RestrictContext enable).
    """
    if str(scope or "") != "online":
        return False
    if force:
        return True
    return bool(armed)
