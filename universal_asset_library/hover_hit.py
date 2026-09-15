# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""Pure hover hit-test math for N-panel asset grids (no bpy).

Region coords: origin bottom-left, ``my`` increases upward (Blender UI region).
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple


def hit_test_tile_index(
    mx: float,
    my: float,
    *,
    origin_x: float,
    first_row_top: float,
    cell_w: float,
    cell_h: float,
    cols: int,
    count: int,
    mode: str = "grid",
    icon_h: float = 0.0,
) -> int:
    """Return asset index under ``(mx, my)`` or ``-1`` if outside.

    Grid mode optionally restricts hits to the drawn thumb band via ``icon_h``
    (excludes select bar / lock / name below the icon). List mode uses full row.
    """
    if count <= 0 or cell_w <= 1 or cell_h <= 1:
        return -1
    cols = max(1, int(cols))
    mode_key = mode or "grid"

    if mode_key == "list":
        if my > first_row_top:
            return -1
        row = int((first_row_top - my) / cell_h)
        if row < 0 or row >= count:
            return -1
        return row

    if mx < origin_x:
        return -1
    rel_x = mx - origin_x
    col = int(rel_x / cell_w)
    if col < 0 or col >= cols:
        return -1
    if my > first_row_top:
        return -1
    row = int((first_row_top - my) / cell_h)
    if row < 0:
        return -1
    index = row * cols + col
    if index < 0 or index >= count:
        return -1
    # Icon-band gate: hover only over the drawn thumb, not the select bar / footer.
    ih = float(icon_h or 0.0)
    if ih > 1.0:
        icon_side = min(ih, float(cell_w))
        cell_left = float(origin_x) + col * float(cell_w)
        inset_x = max(0.0, (float(cell_w) - icon_side) * 0.5)
        ix0 = cell_left + inset_x
        ix1 = ix0 + icon_side
        iy1 = float(first_row_top) - row * float(cell_h)
        iy0 = iy1 - icon_side
        if not (ix0 <= mx < ix1 and iy0 <= my <= iy1):
            return -1
    return index


def apply_scroll_delta(first_row_top: float, scroll_now: float, scroll_at_anchor: float) -> float:
    """Adjust a region-space row top when the UI region view2d scrolls."""
    return float(first_row_top) + (float(scroll_now) - float(scroll_at_anchor))


def calibrate_origin_from_click(
    mx: float,
    my: float,
    index: int,
    *,
    cell_w: float,
    cell_h: float,
    cols: int,
    mode: str = "grid",
    icon_h: float = 0.0,
    prefer_first_row_top: Optional[float] = None,
) -> Tuple[float, float]:
    """Return ``(origin_x, first_row_top)`` snapped so ``index`` sits under the click.

    Horizontal: column centre under ``mx``.

    Vertical: pick ``first_row_top`` so ``hit_test_tile_index`` returns ``index``
    for ``(mx, my)``. If ``prefer_first_row_top`` (chrome / prior estimate)
    already places the click inside that cell, **keep it** — so select-bar
    clicks (below the thumb) do not yank the origin. Otherwise clamp the
    prefer value into the valid range, or fall back to full-cell centre.

    ``icon_h`` is API-compat only (GPU selection overlay removed).
    """
    del icon_h
    cols = max(1, int(cols))
    mode_key = mode or "grid"
    ch = float(cell_h)
    if mode_key == "list":
        return 0.0, float(my) + float(index) * ch + 0.5 * ch
    row, col = divmod(int(index), cols)
    cw = float(cell_w)
    origin_x = float(mx) - (col + 0.5) * cw
    # Valid tops: row == int((top - my) / ch)  →  my + row*ch <= top < my + (row+1)*ch
    lo = float(my) + float(row) * ch
    hi = lo + ch
    if prefer_first_row_top is not None:
        pref = float(prefer_first_row_top)
        if lo <= pref < hi:
            first_row_top = pref
        else:
            # Minimal slide so this click maps to ``index``
            first_row_top = min(max(pref, lo), hi - 1e-3)
    else:
        first_row_top = lo + 0.5 * ch
    return origin_x, first_row_top


def layout_signature(
    *,
    online: bool,
    mode: str,
    count: int,
    cols: int,
    thumb_size: int,
    changelog_visible: bool,
    onboarding_visible: bool,
    toolbar_rows: int = 1,
    hover_card_visible: bool = False,
    download_active: bool = False,
    region_width_bucket: int = 0,
    download_option_rows: int = 0,
    show_names: bool = False,
) -> Tuple:
    """Hashable signature — when it changes, drop click calibration.

    ``hover_card_visible`` is accepted for API compatibility but **must not**
    participate: N-panel large preview is a GPU overlay (no UILayout chrome).

    ``show_names`` is accepted for API compatibility but ignored — names are
    always on the select bar (prefs toggle removed in 0.3.82).

    ``download_active``, ``download_option_rows``, and ``region_width_bucket``
    *do* participate — progress / Resolution chrome and N-panel resize shift
    tiles under a calibrated origin.
    """
    del hover_card_visible  # intentionally ignored — see docstring
    del show_names
    return (
        bool(online),
        str(mode or "grid"),
        int(count),
        int(cols),
        int(thumb_size),
        bool(changelog_visible),
        bool(onboarding_visible),
        max(1, min(2, int(toolbar_rows or 1))),
        bool(download_active),
        int(region_width_bucket),
        max(0, min(2, int(download_option_rows or 0))),
    )


def hit_dict_with_scroll(hit: Optional[Dict], scroll_now: float) -> Optional[Dict]:
    """Return a shallow copy of hit with ``first_row_top`` adjusted for scroll.

    Uncalibrated hits stamp ``first_row_top`` in **region** pixels during
    Assets ``draw()`` (``region.height - chrome``). ``region.view2d.region_to_view``
    is often ``0`` in that path while POST_PIXEL / mouse-move see the real
    ``cur.ymin``. Re-applying the delta shifts outlines/hit boxes **up** onto
    Quality/Format chrome. Only click-calibrated anchors have a trustworthy
    ``scroll_y`` for live view2d tracking.
    """
    if not hit:
        return None
    out = dict(hit)
    if not out.get("calibrated"):
        return out
    anchor = float(out.get("scroll_y") or 0.0)
    base_top = float(out.get("first_row_top_base", out.get("first_row_top") or 0.0))
    out["first_row_top"] = apply_scroll_delta(base_top, scroll_now, anchor)
    return out


def absolute_tile_index(
    mx: float,
    my: float,
    hit: Optional[Dict],
    *,
    full_cell: bool = True,
    scroll_now: Optional[float] = None,
) -> int:
    """Return absolute asset index under ``(mx, my)`` or ``-1``.

    N-panel big preview uses ``full_cell=True`` (thumb + name bar). Pass
    ``scroll_now`` so a mouse-move between Assets draws still tracks view2d
    scroll instead of the last stored ``first_row_top``.
    """
    if not hit or int(hit.get("count") or 0) <= 0:
        return -1
    use = hit
    if scroll_now is not None:
        adj = hit_dict_with_scroll(hit, float(scroll_now))
        if adj:
            use = adj
    local = hit_test_tile_index(
        mx,
        my,
        origin_x=float(use.get("origin_x") or 0),
        first_row_top=float(use.get("first_row_top") or 0),
        cell_w=float(use.get("cell_w") or 0),
        cell_h=float(use.get("cell_h") or 0),
        cols=int(use.get("cols") or 1),
        count=int(use.get("count") or 0),
        mode=str(use.get("mode") or "grid"),
        icon_h=0.0 if full_cell else float(use.get("icon_h") or 0),
    )
    if local < 0:
        return -1
    return local + max(0, int(use.get("index_offset") or 0))


def tile_rect_in_region(
    index: int,
    *,
    origin_x: float,
    first_row_top: float,
    cell_w: float,
    cell_h: float,
    cols: int,
    count: int,
    index_offset: int = 0,
    mode: str = "grid",
    region_width: float = 0.0,
    icon_only: bool = True,
    icon_h: float = 0.0,
) -> Optional[Tuple[float, float, float, float]]:
    """Return ``(x, y, w, h)`` in UI-region coords for ``index``, or None.

    Origin is bottom-left. For grid ``icon_only``, the rect is the drawn thumb
    band at the top of the cell (excludes name/lock stride below). Pass
    ``icon_h`` when the drawn ``template_icon`` is inset vs column width.
    """
    offset = max(0, int(index_offset or 0))
    local = int(index) - offset
    n = max(0, int(count or 0))
    if local < 0 or local >= n or cell_w <= 1 or cell_h <= 1:
        return None
    cols = max(1, int(cols))
    mode_key = mode or "grid"
    ox = float(origin_x)
    top = float(first_row_top)
    cw = float(cell_w)
    ch = float(cell_h)
    if mode_key == "list":
        y = top - (local + 1) * ch
        w = max(cw, float(region_width) - ox - 4.0) if region_width else cw
        return (ox, y, w, ch)
    row, col = divmod(local, cols)
    x = ox + col * cw
    if icon_only:
        side = float(icon_h) if float(icon_h) > 1.0 else cw
        # Center inset icon in the column width (matches UILayout alignment).
        x = x + max(0.0, (cw - side) * 0.5)
        y = top - row * ch - side
        return (x, y, side, side)
    y = top - (row + 1) * ch
    return (x, y, cw, ch)


def selection_rect_from_anchor(
    index: int,
    *,
    anchor_index: int,
    anchor_rect: Tuple[float, float, float, float],
    cell_w: float,
    cell_h: float,
    cols: int,
    count: int,
    index_offset: int = 0,
    mode: str = "grid",
    scroll_now: float = 0.0,
    scroll_anchor: float = 0.0,
) -> Optional[Tuple[float, float, float, float]]:
    """Translate a click-anchored thumb rect to ``index`` (same window).

    Anchor is the square under the last calibrate click — more accurate than
    chrome estimates for GPU selection outlines.
    """
    offset = max(0, int(index_offset or 0))
    local = int(index) - offset
    anchor_local = int(anchor_index) - offset
    n = max(0, int(count or 0))
    if local < 0 or local >= n or anchor_local < 0 or anchor_local >= n:
        return None
    ax, ay, aw, ah = (float(v) for v in anchor_rect)
    dy_scroll = float(scroll_now) - float(scroll_anchor)
    mode_key = mode or "grid"
    if mode_key == "list":
        drow = local - anchor_local
        return (ax, ay - drow * float(cell_h) + dy_scroll, aw, ah)
    cols = max(1, int(cols))
    arow, acol = divmod(anchor_local, cols)
    row, col = divmod(local, cols)
    return (
        ax + (col - acol) * float(cell_w),
        ay - (row - arow) * float(cell_h) + dy_scroll,
        aw,
        ah,
    )


def should_ignore_hover_retarget(
    *,
    current_index: int,
    new_index: int,
    card_ready: bool,
    cursor_x: float,
    cursor_y: float,
    arm_x: float,
    arm_y: float,
    slop_px: float = 12.0,
) -> bool:
    """True when an open hover card should keep ``current_index``.

    - Same tile → ignore redundant updates.
    - Gap between tiles (``new_index < 0``) → keep showing current (sticky).
    - **Different tile under cursor → always retarget** (never freeze a wrong
      asset while the user is clearly over another cell). Cursor slop is unused
      for cross-tile cases; kept in the signature for API compatibility.
    """
    del cursor_x, cursor_y, arm_x, arm_y, slop_px  # API compat; see docstring
    if not card_ready or int(current_index) < 0:
        return False
    if int(new_index) == int(current_index):
        return True
    if int(new_index) < 0:
        return True
    return False


def npanel_tooltip_rect(
    *,
    region_width: float,
    region_height: float,
    ui_region_x: float,
    window_region_x: float,
    window_region_y: float,
    ui_region_y: float,
    tile_center_y_ui: float,
    size: float,
    margin: float = 8.0,
    chrome_height: float = 56.0,
) -> Tuple[float, float, float, float]:
    """Place a floating tooltip in 3D View WINDOW coords, left of the N-panel.

    ``tile_center_y_ui`` is the hovered tile centre in UI-region coordinates
    (origin bottom-left). Returns ``(x, y, w, h)`` in WINDOW region space.
    ``chrome_height`` is name + meta rows below the square thumb (default 56).
    """
    tip_w = float(size)
    tip_h = float(size) + max(40.0, float(chrome_height))
    # Right edge of tooltip sits just left of the N-panel (UI region).
    tip_right = float(ui_region_x) - float(window_region_x) - float(margin)
    x = tip_right - tip_w
    x = max(float(margin), min(x, float(region_width) - tip_w - float(margin)))
    # Map UI-region Y to WINDOW-region Y (same screen space, different origins).
    screen_y = float(ui_region_y) + float(tile_center_y_ui)
    cy = screen_y - float(window_region_y)
    y = cy - tip_h * 0.5
    y = max(float(margin), min(y, float(region_height) - tip_h - float(margin)))
    return (x, y, tip_w, tip_h)
