# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""N-panel width tiers — one place for chrome labels, wrap, and disclosure.

Tiers (``context.region.width``):
- NARROW  < 220   icon-only; Library toolbar wraps
- MEDIUM  220–360 Import/Download text; Type/Source unlabeled
- WIDE    > 360   Drop/Bar + Type/Source labels; capability line; speed/ETA
"""

from __future__ import annotations

NARROW_MAX = 220.0
WIDE_MIN = 360.0
SEARCH_SPLIT = 0.62


def search_split_factor(width: float) -> float:
    """Search vs Type+View: give Search more room when the N-panel is narrow."""
    t = tier(width)
    if t == "narrow":
        return 0.72
    if t == "wide":
        return 0.58
    return SEARCH_SPLIT


def region_width(context=None) -> float:
    try:
        import bpy

        ctx = context if context is not None else bpy.context
        return float(getattr(getattr(ctx, "region", None), "width", 320) or 320)
    except Exception:
        return 320.0


def tier(width: float) -> str:
    w = float(width or 0.0)
    if w < NARROW_MAX:
        return "narrow"
    if w > WIDE_MIN:
        return "wide"
    return "medium"


def is_narrow(width: float) -> bool:
    return tier(width) == "narrow"


def is_wide(width: float) -> bool:
    return tier(width) == "wide"


def show_filter_labels(width: float) -> bool:
    """Type / Source property names."""
    return is_wide(width)


def show_primary_action_labels(width: float) -> bool:
    """Import / Download / Load More short text."""
    return not is_narrow(width)


def show_secondary_action_labels(width: float) -> bool:
    """Drop into Scene / Asset Bar — addon-specific icons need words when room."""
    return is_wide(width)


def show_capability_line(width: float) -> bool:
    return is_wide(width)


def show_download_option_labels(width: float) -> bool:
    return is_wide(width)


def show_download_speed(width: float) -> bool:
    return is_wide(width)


def library_toolbar_wraps(width: float) -> bool:
    return is_narrow(width)


def action_text(label: str, *, show: bool) -> str:
    return str(label or "") if show else ""
