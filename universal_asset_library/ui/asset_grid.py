# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""Square asset grid + readable list rows for the UAL N-panel.

Hard constraints (Blender RNA)
-----------------------------
- ``template_icon(scale=…)`` draws a **large** preview but is **not clickable**.
- ``operator(..., icon_value=…)`` is clickable but the icon stays ~button-sized
  (~32px) and **cannot** be scaled — never use it as the main thumb.
- Modern pattern (Thicket / Blender RNA): large ``template_icon`` + clickable
  select/drag operator (``asset_key`` + index).

Grid strategy
-------------
1. **Large square thumbs** via row wrap + ``template_icon(scale=…)``.
2. **Select / drag** via a full-width native operator under the thumb.
3. Optional **lock** footer for auth-gated online sources.
4. N-panel pointer hover stays off; GPU Asset Bar owns pixel hover.
5. Height-capped Assets viewport — chrome stays on-screen; only the visible
   tile window changes (``assets_view_scroll``).

List strategy
-------------
1. Leading ``template_icon(scale=LIST_THUMB_SCALE)`` — **no wrapping box**
   (a sibling ``scale_y`` name bar used to stretch an empty ``box()`` around a
   fixed-size icon; the preview must be the row’s visual surface).
2. Full-height name operator to the right (``depress`` = selected).
3. Same height-capped window as grid (taller rows → fewer visible at once).
"""

from __future__ import annotations

import time
from typing import NamedTuple, Sequence, Tuple

try:
    import bpy  # type: ignore
except ImportError:  # pure tests / no Blender
    bpy = None  # type: ignore

from .. import thumbnails
from .. import selection_keys
from ..ui_strings import item_is_auth_gated

_UI_UNIT_PX = 20.0
_MAX_UILIST_SCALE = 4.0
_MAX_FLOW_SCALE = 10.0
_MIN_CELL_PX = 64
# Soft gap between cells (grid_metrics width math) + inter-row UILayout stride
CELL_GAP_PX = 6
_CELL_GAP_PX = CELL_GAP_PX
_MAX_GRID_COLS = 8
_FOOTER_RESERVE_UI = 1.6
_MIN_VISIBLE_ROWS = 1
_MAX_VISIBLE_ROWS = 12
# Box chrome + select/drag row under template_icon
GRID_BOX_PAD_UI = 0.45
GRID_SELECT_BAR_UI = 1.0
# Top-right in-library green-dot strip (Online grid — reserved on every Online tile)
IN_LIBRARY_BADGE_UI = 0.65
# template_icon scale inset inside the box
ICON_SCALE_INSET = 0.25
# List view: large ``template_icon`` (never operator icon_value — stays ~32px).
# ~3.5 UI units ≈ 70px — readable; icon draws edge-to-edge (no wrapping box).
LIST_THUMB_SCALE = 3.5
# Row stride ≈ thumb height + inter-row UILayout gap (no box chrome).
LIST_ROW_UI = 3.5

# Same-draw memo for grid_metrics (region pointer + width + thumb + ui unit).
_grid_metrics_memo_key = None
_grid_metrics_memo_val = None
_debug_last: dict = {}


def _grid_debug(where: str, exc: BaseException, *, interval_s: float = 8.0) -> None:
    """Rate-limited stderr so panel-draw excepts are not silent forever."""
    now = time.monotonic()
    last = float(_debug_last.get(where) or 0.0)
    if now - last < interval_s:
        return
    _debug_last[where] = now
    print(f"UAL grid ({where}): {type(exc).__name__}: {exc}")


def context_ui_unit_px(context) -> float:
    """Pixel size of one UI unit (``20 * ui_scale``) — matches ``template_icon``."""
    try:
        return _UI_UNIT_PX * float(context.preferences.system.ui_scale)
    except Exception as exc:
        _grid_debug("ui_scale", exc)
        return _UI_UNIT_PX


def drawn_icon_scale(scale: float) -> float:
    """Actual ``template_icon`` scale drawn inside the tile box."""
    return max(2.0, float(scale) - float(ICON_SCALE_INSET))


def hit_cell_dimensions(
    side_px: float,
    *,
    icon_side_px: float = 0.0,
    ui_unit: float = 20.0,
    reserve_lock_footer: bool = False,
    reserve_in_library_badge: bool = False,
) -> Tuple[float, float]:
    """``(cell_w, cell_h)`` for analytic hover hit-test vs drawn grid tiles.

    Columns use ``align=True`` (packed horizontally → width ≈ ``side_px``).
    Vertical stride must match the real stack: optional in-library badge +
    **drawn icon** (inset scale) + box pad + select bar + optional lock +
    ``CELL_GAP_PX``.

    Using full ``side_px`` as the icon height (legacy) overshot real rows after
    the box+select-bar layout — hover on tile A resolved to tile B above it.
    """
    gap = float(CELL_GAP_PX)
    unit = float(ui_unit)
    cell_w = max(1.0, float(side_px))
    # Prefer the real drawn icon height; fall back to column side for older callers
    icon = float(icon_side_px) if float(icon_side_px or 0) > 1.0 else cell_w
    badge = float(IN_LIBRARY_BADGE_UI) * unit if reserve_in_library_badge else 0.0
    box_pad = float(GRID_BOX_PAD_UI) * unit
    select_bar = float(GRID_SELECT_BAR_UI) * unit
    lock = (0.7 * unit) if reserve_lock_footer else 0.0
    cell_h = max(1.0, badge + icon + box_pad + select_bar + lock + gap)
    return cell_w, cell_h


def grid_hit_metrics(
    context,
    *,
    reserve_lock_footer: bool = False,
    reserve_in_library_badge: bool = False,
) -> Tuple[int, float, float, float, float, float]:
    """Return ``(cols, scale, side_px, icon_side_px, cell_w, cell_h)`` in sync with draw."""
    cols, scale, ui_x = grid_metrics(context)
    unit = context_ui_unit_px(context)
    side_px = max(unit * 2.8, float(ui_x) * unit)
    icon_side_px = drawn_icon_scale(scale) * unit
    cell_w, cell_h = hit_cell_dimensions(
        side_px,
        icon_side_px=icon_side_px,
        ui_unit=unit,
        reserve_lock_footer=reserve_lock_footer,
        reserve_in_library_badge=reserve_in_library_badge,
    )
    return cols, scale, side_px, icon_side_px, cell_w, cell_h


def list_hit_cell_height(ui_unit: float = 20.0) -> float:
    """Row stride for list-view hover hit-test (scaled thumb + UILayout gap)."""
    return max(1.0, float(LIST_ROW_UI) * float(ui_unit) + float(CELL_GAP_PX))


class AssetViewport(NamedTuple):
    """Window into a full asset list that fits the N-panel region height."""

    start: int
    count: int
    total: int
    cols: int
    max_scroll: int
    rows_visible: int


def compute_asset_viewport(
    *,
    total: int,
    cols: int,
    cell_h: float,
    region_height: float,
    chrome_above_px: float,
    footer_reserve_px: float,
    scroll: int = 0,
    min_rows: int = _MIN_VISIBLE_ROWS,
    max_rows: int = _MAX_VISIBLE_ROWS,
) -> AssetViewport:
    """Return a row-aligned window of assets that fits below chrome / above footer.

    Blender UILayout cannot scroll a sub-region — drawing every tile pushes
    Load More off-screen when thumbs are large. Cap visible rows to the
    remaining region height instead.
    """
    total = max(0, int(total or 0))
    cols = max(1, int(cols or 1))
    cell_h = max(1.0, float(cell_h or 1.0))
    available = float(region_height) - float(chrome_above_px) - float(footer_reserve_px)
    if available < cell_h:
        rows = max(1, int(min_rows))
    else:
        rows = int(available // cell_h)
        rows = max(int(min_rows), min(rows, int(max_rows)))
    capacity = max(cols, rows * cols)
    if total <= 0:
        return AssetViewport(0, 0, 0, cols, 0, rows)
    # Row-align the last start. A raw ``total - capacity`` that is not a
    # multiple of ``cols`` left Down enabled forever, hid the last partial
    # row, and kept Load More off because ``start`` never reached max_scroll.
    overflow = max(0, total - capacity)
    max_scroll = ((overflow + cols - 1) // cols) * cols if overflow else 0
    raw = max(0, min(int(scroll or 0), max_scroll))
    start = (raw // cols) * cols
    start = max(0, min(start, max_scroll))
    count = min(capacity, total - start)
    return AssetViewport(
        start=start,
        count=count,
        total=total,
        cols=cols,
        max_scroll=max_scroll,
        rows_visible=rows,
    )


def should_show_load_more(*, online: bool, has_more: bool, start: int, max_scroll: int) -> bool:
    """Load More fetches the next *site* page — only at the end of already-loaded tiles.

    ▲▼ / wheel browse the loaded window. Showing Load More on row 1 of 40 made it
    look like a second scrollbar and hid that more results were already in memory.
    """
    if not online or not has_more:
        return False
    return int(start or 0) >= int(max_scroll or 0)


def footer_reserve_px(ui_unit: float) -> float:
    return float(_FOOTER_RESERVE_UI) * max(1.0, float(ui_unit or _UI_UNIT_PX))


def window_items(items, start: int, count: int) -> list:
    """Slice a bpy collection or sequence without requiring Python slicing."""
    if items is None or count <= 0:
        return []
    start = max(0, int(start or 0))
    end = start + max(0, int(count or 0))
    try:
        n = len(items)
    except Exception:
        return []
    end = min(end, n)
    if start >= end:
        return []
    return [items[i] for i in range(start, end)]


def viewport_from_context(context, ui, *, online: bool, mode: str):
    """Height-capped window that keeps N-panel chrome on-screen.

    Returns ``(viewport, cell_w, cell_h, icon_h)``.
    """
    from . import hover_preview
    from .. import preferences

    items = None
    if ui is not None:
        items = ui.online_results if online else ui.assets
    try:
        total = len(items) if items is not None else 0
    except Exception:
        total = 0

    cfg = None
    try:
        cfg = preferences.load_active_config()
    except Exception as exc:
        _grid_debug("load_active_config", exc)
        cfg = {}

    unit = context_ui_unit_px(context)
    try:
        region_h = float(getattr(context.region, "height", 600) or 600)
    except Exception:
        region_h = 600.0
    try:
        chrome = float(
            hover_preview.estimate_assets_content_top_px(context, ui, cfg=cfg) or 0.0
        )
    except Exception:
        chrome = 120.0
    footer = footer_reserve_px(unit)
    try:
        if bool(getattr(ui, "download_active", False)):
            footer += 2.4 * unit
    except Exception:
        pass

    reserve_lock = False
    try:
        if online and ui is not None:
            sid = str(getattr(ui, "online_source", "") or "")
            reserve_lock = item_is_auth_gated(sid, cfg)
    except Exception:
        pass

    mode_key = (mode or "grid").strip().lower()
    if mode_key == "list":
        cols = 1
        cell_h = list_hit_cell_height(unit)
        cell_w = max(1.0, float(getattr(getattr(context, "region", None), "width", 200) or 200) - 24.0)
        icon_h = float(LIST_THUMB_SCALE) * float(unit)
    else:
        cols, _scale, _side, icon_h, cell_w, cell_h = grid_hit_metrics(
            context,
            reserve_lock_footer=reserve_lock,
            reserve_in_library_badge=bool(online),
        )

    scroll = 0
    try:
        scroll = int(getattr(ui, "assets_view_scroll", 0) or 0)
    except Exception:
        scroll = 0

    vp = compute_asset_viewport(
        total=total,
        cols=cols,
        cell_h=cell_h,
        region_height=region_h,
        chrome_above_px=chrome,
        footer_reserve_px=footer,
        scroll=scroll,
    )
    if vp.max_scroll > 0:
        # Scroll strip takes a row — recompute so the panel still fits.
        vp = compute_asset_viewport(
            total=total,
            cols=cols,
            cell_h=cell_h,
            region_height=region_h,
            chrome_above_px=chrome,
            footer_reserve_px=footer + (1.2 * unit),
            scroll=scroll,
        )
    return vp, float(cell_w), float(cell_h), float(icon_h)


def thumb_px(context) -> int:
    pkg = __package__.rsplit(".", 1)[0]
    try:
        prefs = context.preferences.addons[pkg].preferences
        size = int(getattr(prefs, "thumb_size", 128) or 128)
    except Exception as exc:
        _grid_debug("thumb_px_prefs", exc)
        size = 128
    return thumbnails.clamp_thumb_size(size)


def panel_width(context) -> int:
    width = 280
    try:
        if context.region:
            width = max(160, int(context.region.width) - 24)
    except Exception:
        pass
    return width


def grid_metrics(context) -> Tuple[int, float, float]:
    """Return ``(columns, icon_scale, cell_side_ui_units)`` for square tiles.

    Fills the N-panel width: pick how many columns fit near the preferred
    thumb size, then **grow** each cell so ``cols * cell ≈ panel width``.
    That removes the blank strip on the right when the panel is wide.

    ``template_icon(scale)`` draws ``scale * (20 * ui_scale)`` px. Scale must
    divide by the scaled UI unit — using bare ``20`` made the GPU selection
    square larger than the real thumb container under HiDPI / ui_scale ≠ 1.

    ``viewport_from_context`` → ``grid_hit_metrics`` and ``draw_square_grid``
    both call this in one panel draw; memoize identical inputs for that pass.
    """
    global _grid_metrics_memo_key, _grid_metrics_memo_val
    unit = context_ui_unit_px(context)
    prefs_px = thumb_px(context)
    width = max(_MIN_CELL_PX, panel_width(context))
    try:
        region = getattr(context, "region", None)
        try:
            ptr = int(region.as_pointer())  # type: ignore[union-attr]
        except Exception:
            ptr = id(region)
        rwidth = int(getattr(region, "width", 0) or 0) if region is not None else 0
        key = (ptr, rwidth, int(prefs_px), float(unit))
    except Exception:
        key = None
    if (
        key is not None
        and key == _grid_metrics_memo_key
        and _grid_metrics_memo_val is not None
    ):
        return _grid_metrics_memo_val
    gap = _CELL_GAP_PX
    # Columns near preferred thumb size (allow 1 col on a narrow panel).
    target = max(_MIN_CELL_PX, int(prefs_px))
    cols = max(1, min(width // max(target + gap, 1), _MAX_GRID_COLS))
    cell_px = max(_MIN_CELL_PX, (width // cols) - gap)
    # Too roomy → add a column (up to max) so we do not leave a fat blank gutter.
    while cols < _MAX_GRID_COLS and cell_px > prefs_px * 1.25:
        nxt = cols + 1
        nxt_px = max(_MIN_CELL_PX, (width // nxt) - gap)
        if nxt_px < prefs_px * 0.72:
            break
        cols = nxt
        cell_px = nxt_px
    # Too tight → drop a column so thumbs stay readable.
    while cols > 1 and cell_px < prefs_px * 0.72:
        cols -= 1
        cell_px = max(_MIN_CELL_PX, (width // cols) - gap)
    # Grow to consume leftover width (responsive fill).
    cell_px = max(_MIN_CELL_PX, (width // cols) - gap)
    scale = max(2.0, min(cell_px / max(1.0, unit), _MAX_FLOW_SCALE))
    side = max(2.8, scale)
    result = (cols, scale, side)
    if key is not None:
        _grid_metrics_memo_key = key
        _grid_metrics_memo_val = result
    return result


def uilist_metrics(context) -> Tuple[int, float]:
    cols, scale, _side = grid_metrics(context)
    # UIList grid icons are denser; keep a slightly smaller scale ceiling.
    scale = max(1.8, min(scale, _MAX_UILIST_SCALE))
    return cols, scale


def thumb_scale(context) -> float:
    return grid_metrics(context)[1]


def grid_columns(context) -> int:
    return uilist_metrics(context)[0]


def grid_list_rows(item_count: int, columns: int) -> int:
    cols = max(1, int(columns) or 1)
    n = max(0, int(item_count) or 0)
    if n <= 0:
        return 4
    needed = (n + cols - 1) // cols
    return max(3, min(needed, 8))


def draw_uilist_grid_item(layout, context, item, **_kwargs) -> None:
    layout.alignment = "CENTER"
    _cols, scale = uilist_metrics(context)
    raw_icon = int(getattr(item, "preview_icon_id", 0) or 0)
    try:
        grid_icon = thumbnails.resolve_grid_icon_id(raw_icon)
    except Exception as exc:
        _grid_debug("resolve_grid_icon_id", exc)
        grid_icon = raw_icon or 0
    if grid_icon:
        layout.template_icon(icon_value=grid_icon, scale=scale)
    else:
        # Empty cell — no mesh/material/HDRI placeholder icon
        layout.label(text="")


def draw_template_list_grid(
    layout,
    context,
    *,
    listtype_name: str,
    list_id: str,
    dataptr,
    propname: str,
    active_dataptr,
    active_propname: str,
    item_count: int,
) -> bool:
    if getattr(bpy.types, listtype_name, None) is None:
        return False
    cols, _scale = uilist_metrics(context)
    rows = grid_list_rows(item_count, cols)
    try:
        layout.template_list(
            listtype_name,
            list_id,
            dataptr,
            propname,
            active_dataptr,
            active_propname,
            rows=rows,
            maxrows=max(rows, 10),
            type="GRID",
            columns=cols,
        )
        return True
    except Exception:
        return False


def draw_square_grid(
    layout,
    context,
    items: Sequence,
    *,
    selected_index: int,
    select_operator: str,
    name_attr: str = "name",
    fallback_name: str = "asset",
    online_cfg=None,
    index_offset: int = 0,
) -> None:
    """Square thumb tiles (no type-icon footer — lock only when auth-gated).

    Uses fixed-width row wrapping — **not** ``even_columns`` ``grid_flow``, which
    stretches cells into landscape rectangles when the panel is wide.

    ``items`` may be a viewport window; ``index_offset`` is the absolute start index.
    Name is on the select bar under each thumb (no separate label row).
    """
    del fallback_name
    cols, scale, ui_x = grid_metrics(context)
    side = max(2.8, float(ui_x))
    offset = max(0, int(index_offset or 0))
    row = None
    for local_i, item in enumerate(items):
        if local_i % cols == 0:
            row = layout.row(align=True)
            row.alignment = "LEFT"
        assert row is not None
        index = offset + local_i
        _draw_square_tile(
            row,
            item,
            index=index,
            selected=(index == selected_index),
            scale=scale,
            side=side,
            select_operator=select_operator,
            name_attr=name_attr,
            online_cfg=online_cfg,
        )


def _draw_square_tile(
    row,
    item,
    *,
    index: int,
    selected: bool,
    scale: float,
    side: float,
    select_operator: str,
    name_attr: str = "name",
    online_cfg=None,
) -> None:
    """Large ``template_icon`` preview + full-width native select/drag button.

    Blender cannot scale ``operator`` icons — using ``icon_value`` alone leaves
    tiny thumbs in huge empty buttons. Pattern: big ``template_icon`` + operator
    under it (``asset_key`` / index, ``depress`` = selected).
    """
    cell = row.column(align=True)
    cell.ui_units_x = side
    cell.alignment = "CENTER"

    raw_icon = int(getattr(item, "preview_icon_id", 0) or 0)
    try:
        icon_value = thumbnails.resolve_grid_icon_id(raw_icon)
    except Exception as exc:
        _grid_debug("resolve_grid_icon_id", exc)
        icon_value = raw_icon or 0

    source_id = getattr(item, "source_id", "") or ""
    if online_cfg is not None:
        key = selection_keys.key_for_online_item(item)
    else:
        key = selection_keys.key_for_library_item(item)

    name = getattr(item, name_attr, None) or getattr(item, "path", "") or "asset"
    short = name if len(name) <= 14 else (name[:12] + "…")

    box = cell.box()
    box.ui_units_x = side
    box.alignment = "CENTER"
    in_library = online_cfg is not None and bool(getattr(item, "in_library", False))
    # Online: top-right in-library status above the thumb (UILayout — no GPU).
    # Always reserve the badge row so in/out-of-library tiles share one cell height.
    if online_cfg is not None:
        badge = box.row(align=True)
        badge.alignment = "RIGHT"
        badge.scale_y = float(IN_LIBRARY_BADGE_UI)
        if in_library:
            try:
                dot_id = int(thumbnails.in_library_dot_icon_id() or 0)
            except Exception as exc:
                _grid_debug("in_library_dot_icon_id", exc)
                dot_id = 0
            if dot_id:
                # template_icon scales custom previews; label icons stay tiny.
                badge.template_icon(icon_value=dot_id, scale=0.85)
            else:
                badge.label(text="", icon="CHECKMARK")
        else:
            badge.label(text="")
    icon_scale = drawn_icon_scale(scale)
    if icon_value:
        box.template_icon(icon_value=icon_value, scale=icon_scale)
    else:
        ph = box.column(align=True)
        ph.ui_units_x = max(2.0, side - 0.4)
        ph.ui_units_y = max(2.0, side - 0.4)
        ph.alignment = "CENTER"
        ph.label(text="")

    # Full-width select / drag (native RNA — not estimated hover hit-test).
    # Name lives on this bar only — no second label under the tile.
    bar = box.row(align=True)
    bar.scale_y = GRID_SELECT_BAR_UI
    op = bar.operator(
        select_operator,
        text=short,
        emboss=True,
        depress=bool(selected),
    )
    op.index = int(index)
    try:
        op.asset_key = key
    except Exception:
        pass

    if online_cfg is not None and source_id and item_is_auth_gated(source_id, online_cfg):
        foot = cell.row(align=True)
        foot.scale_y = 0.7
        lock = foot.operator(
            "ual.auth_required",
            text="",
            icon="LOCKED",
            emboss=False,
        )
        try:
            lock.source_id = source_id
        except Exception:
            pass


def draw_list_rows(
    layout,
    items: Sequence,
    *,
    selected_index: int,
    select_operator: str,
    name_attr: str = "name",
    show_type: bool = False,
    online_cfg=None,
    index_offset: int = 0,
) -> None:
    """List view: large leading thumb + left-aligned name (clickable).

    Uses the same ``template_icon`` + operator pattern as the grid. Do **not**
    put the preview on ``operator(..., icon_value=…)`` — Blender keeps those
    icons at ~32px regardless of button size.

    Type icons/labels are off by default. Pass ``show_type=True`` only if a
    future setting re-enables them.
    """
    offset = max(0, int(index_offset or 0))
    thumb_scale = float(LIST_THUMB_SCALE)
    for local_i, item in enumerate(items):
        index = offset + local_i
        selected = index == selected_index
        raw_icon = int(getattr(item, "preview_icon_id", 0) or 0)
        try:
            grid_icon = thumbnails.resolve_grid_icon_id(raw_icon)
        except Exception as exc:
            _grid_debug("resolve_grid_icon_id", exc)
            grid_icon = raw_icon or 0
        name = getattr(item, name_attr, None) or getattr(item, "path", "") or "asset"
        # List has horizontal room — keep names readable (grid uses shorter labels).
        label = name if len(name) <= 42 else (name[:40] + "…")
        source_id = getattr(item, "source_id", "") or ""
        if online_cfg is not None:
            key = selection_keys.key_for_online_item(item)
        else:
            key = selection_keys.key_for_library_item(item)

        row = layout.row(align=True)

        if online_cfg is not None and bool(getattr(item, "in_library", False)):
            try:
                dot_id = int(thumbnails.in_library_dot_icon_id() or 0)
            except Exception as exc:
                _grid_debug("in_library_dot_icon_id", exc)
                dot_id = 0
            badge = row.column(align=True)
            badge.alignment = "CENTER"
            if dot_id:
                badge.template_icon(icon_value=dot_id, scale=0.85)
            else:
                badge.label(text="", icon="CHECKMARK")

        if online_cfg is not None and source_id and item_is_auth_gated(source_id, online_cfg):
            lock = row.operator(
                "ual.auth_required",
                text="",
                icon="LOCKED",
                emboss=False,
            )
            try:
                lock.source_id = source_id
            except Exception:
                pass

        # Leading preview — template_icon alone (fills its own size).
        # Do not wrap in box(): a sibling scale_y name bar stretches the box
        # taller than the icon and leaves a dark empty frame around a tiny thumb.
        thumb = row.column(align=True)
        thumb.ui_units_x = thumb_scale
        thumb.alignment = "CENTER"
        if grid_icon:
            thumb.template_icon(icon_value=grid_icon, scale=thumb_scale)
        else:
            ph = thumb.column(align=True)
            ph.ui_units_x = thumb_scale
            ph.ui_units_y = thumb_scale
            ph.alignment = "CENTER"
            ph.label(text="", icon="OUTLINER_OB_MESH")

        # Name / select / drag — match thumb height for a solid hit target.
        main = row.column(align=True)
        main.scale_y = thumb_scale
        main.alignment = "EXPAND"
        op = main.operator(
            select_operator,
            text=label,
            emboss=True,
            depress=bool(selected),
        )
        op.index = int(index)
        try:
            op.asset_key = key
        except Exception:
            pass

        if show_type:
            from .icons import icon_for_type, label_for_type

            type_icon = icon_for_type(getattr(item, "asset_type", "") or "mesh")
            meta = row.column(align=True)
            meta.scale_y = thumb_scale
            meta.label(
                text=label_for_type(getattr(item, "asset_type", "") or ""),
                icon=type_icon,
            )
