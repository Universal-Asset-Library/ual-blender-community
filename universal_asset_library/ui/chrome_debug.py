# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""Optional chrome estimate vs draw audit (``UAL_CHROME_DEBUG=1``).

Also hosts ``presence_overlay_use_hit`` (kept for hit-geometry tests; N-panel
GPU in-library borders were retired in 0.3.62).
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, Optional

_debug_last = 0.0
_draw_ledger: Dict[str, Any] = {}
_estimate_ledger: Dict[str, Any] = {}
_scroll_layout: Dict[str, Any] = {}
_scroll_overlay: Dict[str, Any] = {}

_INTERVAL_S = 2.0


def chrome_debug_enabled() -> bool:
    return str(os.environ.get("UAL_CHROME_DEBUG", "") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def note_draw_chrome(**kwargs: Any) -> None:
    """Merge what Main / Assets panels actually drew this pass."""
    if not chrome_debug_enabled():
        return
    _draw_ledger.update(kwargs)
    _draw_ledger["_mono"] = time.monotonic()
    _maybe_print()


def note_estimate_chrome(**kwargs: Any) -> None:
    """Record estimate_assets_chrome_ui_units inputs + resulting top px."""
    if not chrome_debug_enabled():
        return
    _estimate_ledger.clear()
    _estimate_ledger.update(kwargs)
    _estimate_ledger["_mono"] = time.monotonic()
    _maybe_print()


def note_scroll_at_layout(*, scroll_y: float, region_h: float, first_row_top: float) -> None:
    if not chrome_debug_enabled():
        return
    _scroll_layout.clear()
    _scroll_layout.update(
        {
            "scroll_y": float(scroll_y),
            "region_h": float(region_h),
            "first_row_top": float(first_row_top),
            "_mono": time.monotonic(),
        }
    )


def note_scroll_at_overlay(*, scroll_y: float, first_row_top_used: float) -> None:
    if not chrome_debug_enabled():
        return
    _scroll_overlay.clear()
    _scroll_overlay.update(
        {
            "scroll_y": float(scroll_y),
            "first_row_top_used": float(first_row_top_used),
            "_mono": time.monotonic(),
        }
    )
    _maybe_print()


def _maybe_print() -> None:
    global _debug_last
    if not chrome_debug_enabled():
        return
    if not _estimate_ledger or not _draw_ledger:
        return
    now = time.monotonic()
    if now - _debug_last < _INTERVAL_S:
        return
    _debug_last = now
    keys = (
        "changelog_visible",
        "onboarding_visible",
        "toolbar_rows",
        "download_option_rows",
        "capability_lines",
    )
    diffs = []
    for k in keys:
        ev = _estimate_ledger.get(k)
        dv = _draw_ledger.get(k)
        if ev != dv:
            diffs.append(f"{k}: estimate={ev!r} draw={dv!r}")
    print("UAL chrome DEBUG — estimate:", {k: _estimate_ledger.get(k) for k in keys})
    print(
        "UAL chrome DEBUG — estimate units/top_px:",
        _estimate_ledger.get("units"),
        _estimate_ledger.get("top_px"),
        "region_w=",
        _estimate_ledger.get("region_width"),
    )
    print("UAL chrome DEBUG — draw:", {k: _draw_ledger.get(k) for k in keys})
    if diffs:
        print("UAL chrome DEBUG — MISMATCH:", "; ".join(diffs))
    else:
        print("UAL chrome DEBUG — flags match")
    if _scroll_layout or _scroll_overlay:
        print(
            "UAL chrome DEBUG — scroll layout:",
            dict(_scroll_layout),
            "overlay:",
            dict(_scroll_overlay),
        )
        sy_l = float(_scroll_layout.get("scroll_y") or 0.0)
        sy_o = float(_scroll_overlay.get("scroll_y") or 0.0)
        if abs(sy_l - sy_o) > 1.0:
            print(
                f"UAL chrome DEBUG — SCROLL MISMATCH delta={sy_o - sy_l:.1f}px "
                f"(layout={sy_l:.1f} overlay={sy_o:.1f})"
            )


def presence_overlay_use_hit(
    hit: Optional[Dict[str, Any]],
    scroll_now: float,
) -> Dict[str, Any]:
    """Hit dict for green outlines — use Assets-draw stamped region geometry.

    ``note_assets_layout`` already writes ``first_row_top`` in region pixels.
    Never re-apply view2d here: UILayout draw and POST_PIXEL disagree on
    ``region_to_view`` (0 vs real ymin), which shifted outlines onto the
    Download / Quality-Format chrome.
    """
    del scroll_now
    if not hit:
        return {}
    return dict(hit)
