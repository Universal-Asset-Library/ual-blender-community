# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""N-Panel UI: Library / Online browser (BlenderKit-style square grid).

Layout (top - bottom): scope - gap - filter (Search | Type+View [+Source]) -
Assets (toolbar + grid) - Downloads (while active) - Status (collapsed).
Tips live on operator tooltips.
"""

from __future__ import annotations

import bpy

from . import asset_grid
from .icons import view_mode_icon, ACTION_ICONS
from ..ui_strings import (
    ADDON_VERSION,
    CHANGELOG_BLURB,
    MATERIAL_BLEND_SETS_PICK_TIP,
    assets_header_label,
    changelog_seen,
    empty_state_cta,
    empty_state_lines,
    item_is_auth_gated,
    onboarding_banner_should_show,
    source_auth_configured,
    source_needs_search_auth,
    split_status_for_narrow,
    viewport_range_label,
)

# Bump these when forcing Blender to drop stale registered panel classes
_PANEL_MAIN = "UAL_PT_main_v024"
_PANEL_LIST = "UAL_PT_assets_v024"
_PANEL_BLEND = "UAL_PT_material_blend_v027"
_PANEL_DL = "UAL_PT_downloads_v024"
_PANEL_STATUS = "UAL_PT_status_v024"
_UL_ASSETS = "UAL_UL_assets_v024"
_UL_ONLINE = "UAL_UL_online_v024"


def _wm_ui(context):
    from .. import preferences

    return preferences.get_wm_ui(context)


def _note_draw_chrome(**kwargs) -> None:
    """Accumulate Main/Assets draw chrome for UAL_CHROME_DEBUG audit."""
    try:
        from .chrome_debug import note_draw_chrome

        note_draw_chrome(**kwargs)
    except Exception:
        pass


def _active_view_mode(ui) -> str:
    """Resolve grid/list mode; safe if a stale WM PropertyGroup is still attached."""
    if ui is None:
        return "grid"
    try:
        scope = ui.scope
    except Exception:
        scope = "library"
    if scope == "online":
        try:
            return ui.online_view_mode or "grid"
        except Exception:
            return "grid"
    try:
        return ui.view_mode or "grid"
    except Exception:
        return "grid"


def _onboarding_banner_dismissed() -> bool:
    try:
        from .. import preferences

        cfg = preferences.load_active_config()
        return bool((cfg.get("ui") or {}).get("onboarding_banner_dismissed", False))
    except Exception:
        return False


def _onboarding_banner_visible(ui) -> bool:
    dismissed = _onboarding_banner_dismissed()
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
    return onboarding_banner_should_show(
        dismissed=dismissed, scope=scope, library_empty=empty
    )


def _online_search_auth_needed(ui) -> bool:
    sid = str(getattr(ui, "online_source", "") or "") if ui is not None else ""
    if not source_needs_search_auth(sid):
        return False
    try:
        from .. import preferences

        return not source_auth_configured(sid, preferences.load_active_config())
    except Exception:
        return True


def _online_selection_auth_gated(ui) -> bool:
    results = getattr(ui, "online_results", None) if ui is not None else None
    if not results:
        return False
    idx = int(getattr(ui, "online_selected_index", 0) or 0)
    if idx < 0 or idx >= len(results):
        return False
    item = results[idx]
    sid = str(getattr(item, "source_id", "") or getattr(ui, "online_source", "") or "")
    try:
        from .. import preferences

        return item_is_auth_gated(sid, preferences.load_active_config())
    except Exception:
        return False


def _changelog_banner_visible() -> bool:
    try:
        from .. import preferences

        return not changelog_seen(preferences.load_active_config(), ADDON_VERSION)
    except Exception:
        return False


def _draw_empty_state(layout, ui) -> None:
    """Two short labels (narrow sidebar safe) + CTAs."""
    box = layout.box()
    col = box.column(align=True)
    col.scale_y = 0.9
    did_search = bool(getattr(ui, "online_did_search", False))
    type_filter = getattr(ui, "filter_type", "ALL") or "ALL"
    auth_needed = ui.scope == "online" and _online_search_auth_needed(ui)
    status = str(getattr(ui, "status_message", "") or "")
    searching = (not did_search) and status.lower().startswith("searching")
    line1, line2 = empty_state_lines(
        ui.scope,
        did_search=did_search,
        type_filter=type_filter,
        source_id=getattr(ui, "online_source", "") or "",
        auth_needed=auth_needed,
        searching=searching,
    )
    icon = {
        "favorites": ACTION_ICONS["favorite_off"],
        "online": ACTION_ICONS["auth"] if auth_needed and not did_search else ACTION_ICONS["search"],
    }.get(ui.scope, ACTION_ICONS["library"])
    col.label(text=line1, icon=icon)
    col.label(text=line2)
    cta = empty_state_cta(
        ui.scope,
        did_search=did_search,
        type_filter=type_filter,
        auth_needed=auth_needed,
        searching=searching,
    )
    if cta == "search":
        row = col.row(align=True)
        row.operator("ual.online_search", text="Search", icon=ACTION_ICONS["search"])
    elif cta == "preferences":
        row = col.row(align=True)
        row.operator("ual.auth_required", text="Open Preferences", icon=ACTION_ICONS["preferences"])
    elif cta == "type_all":
        row = col.row(align=True)
        row.operator("ual.clear_type_filter", text="Type = All", icon=ACTION_ICONS["type_filter"])
    elif cta == "library":
        row = col.row(align=True)
        row.operator("ual.rebuild_index", text="Rebuild Index", icon=ACTION_ICONS["rebuild_index"])
        row.operator("ual.open_preferences", text="Open Preferences", icon=ACTION_ICONS["preferences"])


def _library_fav_icon(ui) -> str:
    fav_icon = ACTION_ICONS["favorite_off"]
    try:
        if ui.assets and 0 <= ui.selected_index < len(ui.assets):
            from .. import config as ual_config
            from .. import preferences

            cfg = preferences.load_active_config()
            path = ui.assets[ui.selected_index].path
            if ual_config.is_favorite(cfg, path):
                fav_icon = ACTION_ICONS["favorite_on"]
    except Exception:
        pass
    return fav_icon


def _bar_button_depressed() -> bool:
    try:
        from . import asset_bar

        return bool(asset_bar.is_visible())
    except Exception:
        return False


def _draw_toolbar_library(layout, ui) -> None:
    """Primary (Import/Drop/Bar) | Favorite/Hide | Refresh/Index | Delete."""
    from .layout_breakpoints import (
        action_text,
        library_toolbar_wraps,
        region_width,
        show_primary_action_labels,
        show_secondary_action_labels,
    )

    fav_icon = _library_fav_icon(ui)
    width = region_width()
    primary = show_primary_action_labels(width)
    extra = show_secondary_action_labels(width)

    def _primary(row) -> None:
        row.operator(
            "ual.import_selected",
            text=action_text("Import", show=primary),
            icon=ACTION_ICONS["import"],
        )
        row.operator(
            "ual.asset_drag_drop",
            text=action_text("Drop", show=extra),
            icon=ACTION_ICONS["drop"],
        )
        try:
            from .. import pro_features
            from . import asset_bar

            bar_ok = pro_features.asset_bar_enabled()
        except Exception:
            bar_ok = False
            asset_bar = None
        if bar_ok and asset_bar is not None:
            asset_bar.draw_toolbar_button(
                row,
                text=action_text("Bar", show=extra),
                icon=ACTION_ICONS["asset_bar"],
                depress=_bar_button_depressed(),
            )

    def _list_ops(row) -> None:
        row.operator("ual.toggle_favorite", text="", icon=fav_icon)
        row.operator("ual.hide_selected_from_index", text="", icon=ACTION_ICONS["hide"])

    def _maint(row) -> None:
        row.operator("ual.refresh_list", text="", icon=ACTION_ICONS["refresh_list"])
        row.operator("ual.rebuild_index", text="", icon=ACTION_ICONS["rebuild_index"])

    def _danger(row) -> None:
        row.operator("ual.delete_selected", text="", icon=ACTION_ICONS["delete"])

    if library_toolbar_wraps(width):
        row = layout.row(align=True)
        _primary(row)
        row = layout.row(align=True)
        _list_ops(row)
        row.separator(factor=0.4)
        _maint(row)
        row.separator(factor=0.4)
        _danger(row)
        return
    row = layout.row(align=True)
    _primary(row)
    row.separator(factor=0.4)
    _list_ops(row)
    row.separator(factor=0.4)
    _maint(row)
    row.separator(factor=0.4)
    _danger(row)


def _draw_toolbar_online(layout, ui) -> bool:
    """Primary (Download/Drop/Bar) | Refresh thumbs — Search lives on the filter row.

    Returns True when the Size/Format download-options row was drawn.
    """
    from .layout_breakpoints import (
        action_text,
        region_width,
        show_primary_action_labels,
        show_secondary_action_labels,
    )

    gated = _online_selection_auth_gated(ui)
    has_results = bool(getattr(ui, "online_results", None))
    can_fetch = has_results and not gated
    width = region_width()
    primary = show_primary_action_labels(width)
    extra = show_secondary_action_labels(width)
    row = layout.row(align=True)
    if ui.download_active:
        row.operator("ual.download_cancel", text="", icon=ACTION_ICONS["cancel"])
    else:
        dl = row.row(align=True)
        dl.enabled = can_fetch
        dl.operator(
            "ual.online_download_import",
            text=action_text("Download", show=primary),
            icon=ACTION_ICONS["download"],
        )
    drop = row.row(align=True)
    drop.enabled = can_fetch
    drop.operator(
        "ual.asset_drag_drop",
        text=action_text("Drop", show=extra),
        icon=ACTION_ICONS["drop"],
    )
    try:
        from .. import pro_features
        from . import asset_bar

        bar_ok = pro_features.asset_bar_enabled()
    except Exception:
        bar_ok = False
        asset_bar = None
    if bar_ok and asset_bar is not None:
        asset_bar.draw_toolbar_button(
            row,
            text=action_text("Bar", show=extra),
            icon=ACTION_ICONS["asset_bar"],
            depress=_bar_button_depressed(),
        )
    row.separator(factor=0.4)
    row.operator("ual.refresh_online_thumbs", text="", icon=ACTION_ICONS["refresh_thumbs"])
    return bool(_draw_online_download_options(layout, ui))


def _draw_online_source_caps(layout, ui) -> bool:
    """WIDE only: one condensed types · size · format line. True if drawn."""
    from .layout_breakpoints import region_width, show_capability_line

    if not show_capability_line(region_width()):
        return False
    from ..download_options import source_capability_inline

    sid = str(getattr(ui, "online_source", "") or "")
    line = source_capability_inline(sid)
    if not line:
        return False
    col = layout.column(align=True)
    col.scale_y = 0.85
    col.label(text=line)
    return True


def _draw_online_download_options(layout, ui) -> bool:
    """Per-asset size/format — one row; disabled 'Not offered' when the site has none.

    Returns True when the Size/Format row was drawn.
    """
    from .layout_breakpoints import region_width, show_download_option_labels
    from ..download_options import (
        format_fixed_label,
        format_ui_label,
        show_format_ui,
        show_resolution_ui,
        size_ui_label,
        source_download_caps,
        split_option_csv,
    )

    results = getattr(ui, "online_results", None)
    if not results:
        return False
    idx = int(getattr(ui, "online_selected_index", 0) or 0)
    if idx < 0 or idx >= len(results):
        return False
    item = results[idx]
    source_id = str(getattr(item, "source_id", "") or getattr(ui, "online_source", "") or "")
    resolutions = split_option_csv(
        getattr(ui, "online_dl_res_items", "") or getattr(item, "resolutions", "") or ""
    )
    formats = split_option_csv(
        getattr(ui, "online_dl_fmt_items", "") or getattr(item, "formats", "") or ""
    )
    caps = source_download_caps(source_id)
    labeled = show_download_option_labels(region_width())
    box = layout.box()
    opts = box.row(align=True)

    size_label = size_ui_label(source_id)
    if show_resolution_ui(source_id, resolutions):
        opts.prop(ui, "online_dl_resolution", text=size_label if labeled else "")
    else:
        dead = opts.row()
        dead.enabled = False
        dead.label(text="Not offered" if caps.size_kind == "none" else caps.size_hint)

    fmt_label = format_ui_label(source_id)
    if show_format_ui(formats, source_id):
        opts.prop(ui, "online_dl_format", text=fmt_label if labeled else "")
    else:
        dead = opts.row()
        dead.enabled = False
        dead.label(text=format_fixed_label(source_id, formats))
    return True


def _ui_unit_px(context) -> float:
    try:
        return 20.0 * float(context.preferences.system.ui_scale)
    except Exception:
        return 20.0


def _draw_assets_footer(layout, ui, *, online: bool, viewport=None) -> None:
    """Pager (loaded tiles) + Load More (next site page) on one row."""
    from .layout_breakpoints import action_text, is_narrow, region_width, show_primary_action_labels

    has_pager = viewport is not None and int(getattr(viewport, "max_scroll", 0) or 0) > 0
    show_more = False
    if viewport is not None:
        show_more = asset_grid.should_show_load_more(
            online=online,
            has_more=bool(getattr(ui, "online_has_more", False)),
            start=int(viewport.start),
            max_scroll=int(viewport.max_scroll),
        )
    elif online and bool(getattr(ui, "online_has_more", False)):
        show_more = True
    if not has_pager and not show_more:
        return

    width = region_width()
    more_label = action_text("Load More", show=show_primary_action_labels(width))
    if has_pager:
        row = layout.row(align=True)
        up_row = row.row(align=True)
        up_row.enabled = int(viewport.start) > 0
        up = up_row.operator("ual.assets_scroll", text="", icon="TRIA_UP")
        up.direction = "UP"
        row.label(text=viewport_range_label(viewport.start, viewport.count, viewport.total))
        down_row = row.row(align=True)
        down_row.enabled = int(viewport.start) < int(viewport.max_scroll)
        down = down_row.operator("ual.assets_scroll", text="", icon="TRIA_DOWN")
        down.direction = "DOWN"
        if show_more:
            row.separator(factor=0.6)
            row.operator("ual.online_load_more", text=more_label, icon=ACTION_ICONS["load_more"])
        return
    more = layout.row()
    more.alignment = "RIGHT"
    more.operator("ual.online_load_more", text=more_label if not is_narrow(width) else "", icon=ACTION_ICONS["load_more"])


def _draw_asset_grid(layout, context, ui, *, online: bool) -> None:
    """Square thumbs — height-capped window so Search / toolbar stay on-screen."""
    from . import hover_preview

    if online:
        items = getattr(ui, "online_results", None)
        if items is None:
            layout.label(text="Reload add-on: disable then enable Universal Asset Library", icon="ERROR")
            return
        select_op = "ual.select_online_index"
        selected = ui.online_selected_index
    else:
        items = getattr(ui, "assets", None)
        if items is None:
            layout.label(text="Reload add-on: disable then enable Universal Asset Library", icon="ERROR")
            return
        select_op = "ual.select_library_index"
        selected = ui.selected_index

    full_cfg = None
    try:
        from .. import preferences

        full_cfg = preferences.load_active_config()
    except Exception:
        full_cfg = {}

    vp, cell_w, cell_h, icon_h = asset_grid.viewport_from_context(
        context, ui, online=online, mode="grid"
    )
    window = asset_grid.window_items(items, vp.start, vp.count)
    asset_grid.draw_square_grid(
        layout,
        context,
        window,
        selected_index=selected,
        select_operator=select_op,
        online_cfg=full_cfg if online else None,
        index_offset=vp.start,
    )
    try:
        hover_preview.note_assets_layout(
            context,
            online=online,
            mode="grid",
            item_count=vp.count,
            columns=vp.cols,
            cell_w=cell_w,
            cell_h=cell_h,
            index_offset=vp.start,
            icon_h=icon_h,
        )
    except Exception:
        pass
    _draw_assets_footer(layout, ui, online=online, viewport=vp)


def _draw_asset_list(layout, context, ui, *, online: bool) -> None:
    """List rows — height-capped window; operators own click/drag."""
    from . import hover_preview

    if online:
        items = getattr(ui, "online_results", None)
        if items is None:
            layout.label(text="Reload add-on: disable then enable Universal Asset Library", icon="ERROR")
            return
        select_op = "ual.select_online_index"
        selected = ui.online_selected_index
        show_type = False
    else:
        items = getattr(ui, "assets", None)
        if items is None:
            layout.label(text="Reload add-on: disable then enable Universal Asset Library", icon="ERROR")
            return
        select_op = "ual.select_library_index"
        selected = ui.selected_index
        show_type = False

    full_cfg = None
    if online:
        try:
            from .. import preferences

            full_cfg = preferences.load_active_config()
        except Exception:
            full_cfg = {}

    vp, cell_w, cell_h, icon_h = asset_grid.viewport_from_context(
        context, ui, online=online, mode="list"
    )
    window = asset_grid.window_items(items, vp.start, vp.count)
    asset_grid.draw_list_rows(
        layout,
        window,
        selected_index=selected,
        select_operator=select_op,
        show_type=show_type,
        online_cfg=full_cfg if online else None,
        index_offset=vp.start,
    )
    try:
        hover_preview.note_assets_layout(
            context,
            online=online,
            mode="list",
            item_count=vp.count,
            columns=1,
            cell_w=cell_w,
            cell_h=cell_h,
            index_offset=vp.start,
            icon_h=icon_h,
        )
    except Exception:
        pass
    _draw_assets_footer(layout, ui, online=online, viewport=vp)


class UAL_UL_assets_v020(bpy.types.UIList):
    bl_idname = _UL_ASSETS

    def draw_item(
        self, context, layout, data, item, icon, active_data, active_propname, index=0, flt_flag=0
    ):
        if self.layout_type in {"DEFAULT", "COMPACT"}:
            icon_value = int(getattr(item, "preview_icon_id", 0) or 0)
            row = layout.row(align=True)
            if icon_value:
                # Scalable preview — operator icon_value stays ~32px.
                row.template_icon(icon_value=icon_value, scale=asset_grid.LIST_THUMB_SCALE)
            row.label(text=item.name or item.path)
        elif self.layout_type == "GRID":
            asset_grid.draw_uilist_grid_item(layout, context, item)


class UAL_UL_online_v020(bpy.types.UIList):
    bl_idname = _UL_ONLINE

    def draw_item(
        self, context, layout, data, item, icon, active_data, active_propname, index=0, flt_flag=0
    ):
        if self.layout_type in {"DEFAULT", "COMPACT"}:
            icon_value = int(getattr(item, "preview_icon_id", 0) or 0)
            row = layout.row(align=True)
            if icon_value:
                row.template_icon(icon_value=icon_value, scale=asset_grid.LIST_THUMB_SCALE)
            row.label(text=item.name)
            if item.license_name:
                row.label(text=item.license_name)
        elif self.layout_type == "GRID":
            asset_grid.draw_uilist_grid_item(layout, context, item)


class UAL_PT_main_v020(bpy.types.Panel):
    bl_label = "Universal Asset Library"
    bl_idname = _PANEL_MAIN
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "UAL"

    def draw_header(self, context):
        row = self.layout.row(align=True)
        row.label(text=ADDON_VERSION)
        row.operator("ual.open_preferences", text="", icon=ACTION_ICONS["preferences"])

    def draw(self, context):
        layout = self.layout
        ui = _wm_ui(context)
        if ui is None:
            layout.label(
                text="UI outdated - Disable then Enable Universal Asset Library",
                icon="ERROR",
            )
            return

        # Dismissible per-version changelog (not a permanent INFO banner)
        changelog_visible = _changelog_banner_visible()
        if changelog_visible:
            tip = layout.box()
            tip.scale_y = 0.85
            tip.label(text=f"UAL {ADDON_VERSION}", icon="INFO")
            tip.label(text=CHANGELOG_BLURB)
            row = tip.row(align=True)
            row.operator("ual.dismiss_changelog", text="Got it", icon="CHECKMARK")

        onboarding_visible = _onboarding_banner_visible(ui)
        if onboarding_visible:
            tip = layout.box()
            tip.scale_y = 0.85
            tip.label(text="First run: set library + cache", icon="INFO")
            tip.label(text="in Preferences")
            row = tip.row(align=True)
            row.operator("ual.open_preferences", text="Quick Setup", icon=ACTION_ICONS["preferences"])
            row.operator("ual.dismiss_onboarding_banner", text="", icon="X")

        layout.prop(ui, "scope", expand=True)
        layout.separator(factor=0.35)

        # Filter block: Search takes most of the row; Type + View stay compact
        from .layout_breakpoints import (
            region_width,
            search_split_factor,
            show_filter_labels,
        )

        width = region_width(context)
        labeled = show_filter_labels(width)
        online = getattr(ui, "scope", "library") == "online"
        filters = layout.column(align=True)
        split = filters.split(factor=search_split_factor(width), align=True)
        search_side = split.row(align=True)
        if online:
            if hasattr(ui, "online_query"):
                search_side.prop(ui, "online_query", text="")
            else:
                search_side.prop(ui, "search_query", text="")
            search_side.operator("ual.online_search", text="", icon=ACTION_ICONS["search"])
        else:
            search_side.prop(ui, "search_query", text="", icon=ACTION_ICONS["search"])
        tools = split.row(align=True)
        type_cell = tools.row(align=True)
        if online:
            from ..online_type_filter import should_skip_search

            if should_skip_search(
                str(getattr(ui, "online_source", "") or ""),
                getattr(ui, "filter_type", "ALL") or "ALL",
            ):
                type_cell.alert = True
        type_cell.prop(
            ui, "filter_type", text="Type" if labeled else "", icon=ACTION_ICONS["type_filter"]
        )
        mode = _active_view_mode(ui)
        tools.operator(
            "ual.toggle_view_mode",
            text="",
            icon=view_mode_icon(mode),
            depress=(mode == "list"),
        )
        capability_lines = 0
        if online:
            filters.prop(
                ui, "online_source", text="Source" if labeled else "", icon=ACTION_ICONS["source"]
            )
            if _draw_online_source_caps(filters, ui):
                capability_lines = 1
        _note_draw_chrome(
            changelog_visible=bool(changelog_visible),
            onboarding_visible=bool(onboarding_visible),
            capability_lines=int(capability_lines),
        )


class UAL_PT_assets_v020(bpy.types.Panel):
    bl_label = "Assets"
    bl_idname = _PANEL_LIST
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "UAL"
    bl_parent_id = _PANEL_MAIN

    def draw_header(self, context):
        # Fold live count into the collapsible title (no standalone count row)
        ui = _wm_ui(context)
        self.bl_label = assets_header_label(ui) if ui is not None else "Assets"

    def draw(self, context):
        layout = self.layout
        ui = _wm_ui(context)
        if ui is None:
            layout.label(
                text="UI outdated - Disable then Enable Universal Asset Library",
                icon="ERROR",
            )
            return
        self.bl_label = assets_header_label(ui)
        mode = _active_view_mode(ui)
        scope = getattr(ui, "scope", "library") or "library"

        # Content-scoped actions sit with Assets (not above the filter block)
        download_option_rows = 0
        if scope == "online":
            if _draw_toolbar_online(layout, ui):
                download_option_rows = 1
            toolbar_rows = 1
        else:
            from ..ui_strings import assets_toolbar_row_count
            from .layout_breakpoints import region_width

            _draw_toolbar_library(layout, ui)
            toolbar_rows = assets_toolbar_row_count(scope, region_width(context))
        _note_draw_chrome(
            toolbar_rows=int(toolbar_rows),
            download_option_rows=int(download_option_rows),
        )

        if scope == "online":
            items = getattr(ui, "online_results", None)
            if items is None:
                layout.label(text="Reload add-on: disable then enable Universal Asset Library", icon="ERROR")
                return
            if not items:
                _draw_empty_state(layout, ui)
                return
            if mode == "grid":
                _draw_asset_grid(layout, context, ui, online=True)
            else:
                _draw_asset_list(layout, context, ui, online=True)
        else:
            items = getattr(ui, "assets", None)
            if items is None:
                layout.label(text="Reload add-on: disable then enable Universal Asset Library", icon="ERROR")
                return
            if not items:
                _draw_empty_state(layout, ui)
                return
            if mode == "grid":
                _draw_asset_grid(layout, context, ui, online=False)
            else:
                _draw_asset_list(layout, context, ui, online=False)


class UAL_PT_material_blend_v020(bpy.types.Panel):
    bl_label = "Material Blend"
    bl_idname = _PANEL_BLEND
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "UAL"
    bl_parent_id = _PANEL_MAIN
    bl_options = {"DEFAULT_CLOSED"}
    bl_order = 15

    @classmethod
    def poll(cls, context):
        return context.mode == "OBJECT"

    def draw(self, context):
        from ..material_blend import constants as blend_c
        from ..material_blend import ops as blend_ops
        from .icons import ACTION_ICONS
        from .layout_breakpoints import region_width, tier

        layout = self.layout
        scene = context.scene
        width = region_width(context)
        compact = tier(width) == "narrow"

        layout.prop(scene, "ual_blend_mode", expand=True)
        layout.prop(scene, "ual_blend_blend_normals")
        mode = getattr(scene, "ual_blend_mode", blend_c.MODE_OBJECT)

        if mode == blend_c.MODE_SETS:
            # Targets (receive) — header + icon actions on one row
            head = layout.row(align=True)
            n_tgt = len(getattr(scene, "ual_blend_set_a", []) or [])
            head.label(text=f"Targets ({n_tgt})")
            head.operator("ual.material_blend_set_assign_a", text="", icon="ADD")
            head.operator("ual.material_blend_set_clear_a", text="", icon="X")
            layout.template_list(
                "UAL_UL_blend_set",
                "targets",
                scene,
                "ual_blend_set_a",
                scene,
                "ual_blend_set_a_index",
                rows=2,
            )
            pick_a = layout.column(align=True)
            pick_a.prop(scene, "ual_blend_pick_obj_a", text="Pick")
            pick_a.prop(scene, "ual_blend_pick_coll_a", text="Collection")

            # Source (surface material)
            head = layout.row(align=True)
            n_src = len(getattr(scene, "ual_blend_set_b", []) or [])
            head.label(text=f"Source ({n_src})")
            head.operator("ual.material_blend_set_assign_b", text="", icon="ADD")
            head.operator("ual.material_blend_set_clear_b", text="", icon="X")
            layout.template_list(
                "UAL_UL_blend_set",
                "source",
                scene,
                "ual_blend_set_b",
                scene,
                "ual_blend_set_b_index",
                rows=2,
            )
            pick_b = layout.column(align=True)
            pick_b.prop(scene, "ual_blend_pick_obj_b", text="Pick")
            pick_b.prop(scene, "ual_blend_pick_coll_b", text="Collection")

            tip = layout.column(align=True)
            tip.scale_y = 0.85
            tip.label(text=MATERIAL_BLEND_SETS_PICK_TIP, icon="INFO")

            msg = blend_ops.sets_blend_message(context)
            if msg:
                layout.label(text=msg, icon="INFO")

            row = layout.row(align=True)
            row.scale_y = 1.15
            row.operator(
                "ual.material_blend_sets_blend",
                text="" if compact else "Blend",
                icon=ACTION_ICONS.get("material_blend", "NODE_MATERIAL"),
            )
            row.operator(
                "ual.material_blend_apply",
                text="" if compact else "Apply",
                icon="CHECKMARK",
            )
            row.operator(
                "ual.material_blend_remove",
                text="" if compact else "Remove",
                icon="X",
            )

            # Distance + edge noise (same Object method)
            sources = []
            targets = []
            try:
                from ..material_blend import sets_state as blend_sets

                sources = blend_sets.resolve_set_meshes(scene, "ual_blend_set_b")
                targets = blend_sets.resolve_set_meshes(scene, "ual_blend_set_a")
            except Exception:
                sources = []
                targets = []

            dist = layout.column(align=True)
            dist.label(text="Distance / Noise")
            ctrl = None
            active = context.object
            if sources:
                dist.prop(sources[0].ual_blend, "blend_max", text="Max")
            # Prefer selected Target for Scale/Noise; else first Target
            if (
                active is not None
                and getattr(active, "type", "") == "MESH"
                and active in targets
            ):
                ctrl = active
            elif targets:
                ctrl = targets[0]
            elif (
                active is not None
                and getattr(active, "type", "") == "MESH"
                and not getattr(active.ual_blend, "blend_source", False)
            ):
                ctrl = active
            if ctrl is not None:
                dist.prop(ctrl.ual_blend, "blend_scale", text="Scale")
                dist.prop(ctrl.ual_blend, "blend_noise_scale", text="Noise")
                dist.prop(ctrl.ual_blend, "blend_noise_amount", text="Amount")
            tip = layout.column(align=True)
            tip.scale_y = 0.85
            tip.label(text="Outer=Max×Scale · Amount 0=smooth")
            return

        tip = layout.column(align=True)
        tip.scale_y = 0.85
        tip.label(text="Active = source · other selected = targets")

        msg = blend_ops.create_selection_message(context)
        if msg:
            warn = layout.box()
            warn.label(text=msg, icon="INFO")

        row = layout.row(align=True)
        row.scale_y = 1.15
        row.operator(
            "ual.material_blend_create",
            text="" if compact else "Create",
            icon=ACTION_ICONS.get("material_blend", "NODE_MATERIAL"),
        )
        row.operator(
            "ual.material_blend_apply",
            text="" if compact else "Apply",
            icon="CHECKMARK",
        )
        row.operator(
            "ual.material_blend_remove",
            text="" if compact else "Remove",
            icon="X",
        )

        active = context.object
        if active is not None and getattr(active, "type", "") == "MESH":
            dist = layout.box()
            dist.label(text="Distance / Noise", icon="DRIVER_DISTANCE")
            # Max on Source; Scale/Noise only on Targets (Blendit mapping)
            is_src = bool(getattr(active.ual_blend, "blend_source", False))
            src = active if is_src else getattr(active.ual_blend, "source_obj", None)
            if src is not None:
                dist.prop(src.ual_blend, "blend_max", text="Max")
            else:
                dist.prop(active.ual_blend, "blend_max", text="Max")
            if not is_src:
                dist.prop(active.ual_blend, "blend_scale", text="Scale")
                dist.prop(active.ual_blend, "blend_noise_scale", text="Noise")
                dist.prop(active.ual_blend, "blend_noise_amount", text="Amount")
            tip = dist.column(align=True)
            tip.scale_y = 0.85
            if is_src:
                tip.label(text="Source Max · select a Target for Scale/Noise")
            else:
                tip.label(text="Outer=Max×Scale · Amount 0=smooth")


class UAL_PT_downloads_v020(bpy.types.Panel):
    bl_label = "Downloads"
    bl_idname = _PANEL_DL
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "UAL"
    bl_parent_id = _PANEL_MAIN
    bl_order = 20

    @classmethod
    def poll(cls, context):
        from .. import download_progress

        ui = _wm_ui(context)
        return bool(ui is not None and ui.download_active) or download_progress.has_active_tasks()

    def draw(self, context):
        from .. import download_progress

        layout = self.layout
        ui = _wm_ui(context)
        if ui is None:
            return
        tasks = download_progress.snapshot_tasks()
        if not tasks and ui.download_active:
            tasks = [
                {
                    "task_id": ui.download_task_id,
                    "name": ui.download_label or "Download",
                    "phase": "download",
                    "progress": max(0, ui.status_progress),
                    "done_bytes": 0,
                    "total_bytes": 0,
                }
            ]
        for i, task in enumerate(tasks):
            box = layout.box()
            row = box.row(align=True)
            row.label(text=str(task.get("name") or "Download"), icon=ACTION_ICONS["download"])
            op = row.operator("ual.download_cancel", text="", icon=ACTION_ICONS["cancel"])
            op.task_id = str(task.get("task_id") or ui.download_task_id or "")
            prog = float(task.get("progress") or 0)
            if i == 0:
                factor = float(getattr(ui, "status_progress_display", 0.0) or 0.0)
                if factor <= 0.0:
                    factor = max(0.0, min(prog / 100.0, 1.0))
            else:
                factor = max(0.0, min(prog / 100.0, 1.0))
            if hasattr(box, "progress"):
                box.progress(
                    factor=max(0.0, min(factor, 1.0)),
                    type="BAR",
                    text=f"{int(round(factor * 100.0))}%",
                )
            else:
                box.label(text=f"{int(prog)}%")
            from .layout_breakpoints import region_width, show_download_speed

            if show_download_speed(region_width(context)):
                speed = download_progress.format_speed(float(task.get("speed_bps") or 0))
                eta = download_progress.format_eta(float(task.get("eta_seconds") or -1))
                bits = [p for p in (speed, f"ETA {eta}" if eta else "") if p]
                if bits:
                    meta = box.row()
                    meta.scale_y = 0.8
                    line = " · ".join(bits)
                    meta.label(text=line[:28])


class UAL_PT_status_v020(bpy.types.Panel):
    bl_label = "Status"
    bl_idname = _PANEL_STATUS
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "UAL"
    bl_parent_id = _PANEL_MAIN
    bl_options = {"DEFAULT_CLOSED"}
    bl_order = 30

    def draw(self, context):
        from .. import download_progress

        layout = self.layout
        ui = _wm_ui(context)
        for line in split_status_for_narrow(ui.status_message or "Ready"):
            layout.label(text=line, icon="INFO")
        downloading = bool(ui.download_active) or download_progress.has_active_tasks()
        if not downloading and ui.status_progress >= 0:
            factor = float(getattr(ui, "status_progress_display", 0.0) or 0.0)
            if factor <= 0.0:
                factor = max(0.0, min(ui.status_progress / 100.0, 1.0))
            if hasattr(layout, "progress"):
                layout.progress(
                    factor=max(0.0, min(factor, 1.0)),
                    type="BAR",
                    text=f"{ui.status_progress}%",
                )
            else:
                layout.prop(ui, "status_progress", text="Progress", slider=True)


_CLASSES = (
    UAL_UL_assets_v020,
    UAL_UL_online_v020,
    UAL_PT_main_v020,
    UAL_PT_assets_v020,
    UAL_PT_material_blend_v020,
    UAL_PT_downloads_v020,
    UAL_PT_status_v020,
)

_LEGACY_CLASS_NAMES = (
    "UAL_UL_assets",
    "UAL_UL_online",
    "UAL_PT_main",
    "UAL_PT_list",
    "UAL_PT_downloads",
    "UAL_PT_status",
    "UAL_UL_assets_v012",
    "UAL_UL_online_v012",
    "UAL_PT_main_v012",
    "UAL_PT_assets_v012",
    "UAL_PT_downloads_v012",
    "UAL_PT_status_v012",
    "UAL_UL_assets_v013",
    "UAL_UL_online_v013",
    "UAL_PT_main_v013",
    "UAL_PT_assets_v013",
    "UAL_PT_downloads_v013",
    "UAL_PT_status_v013",
    "UAL_UL_assets_v014",
    "UAL_UL_online_v014",
    "UAL_PT_main_v014",
    "UAL_PT_assets_v014",
    "UAL_PT_downloads_v014",
    "UAL_PT_status_v014",
    "UAL_UL_assets_v015",
    "UAL_UL_online_v015",
    "UAL_PT_main_v015",
    "UAL_PT_assets_v015",
    "UAL_PT_downloads_v015",
    "UAL_PT_status_v015",
    "UAL_UL_assets_v020",
    "UAL_UL_online_v020",
    "UAL_PT_main_v020",
    "UAL_PT_assets_v020",
    "UAL_PT_downloads_v020",
    "UAL_PT_status_v020",
    "UAL_UL_assets_v021",
    "UAL_UL_online_v021",
    "UAL_PT_main_v021",
    "UAL_PT_assets_v021",
    "UAL_PT_downloads_v021",
    "UAL_PT_status_v021",
    "UAL_UL_assets_v022",
    "UAL_UL_online_v022",
    "UAL_PT_main_v022",
    "UAL_PT_assets_v022",
    "UAL_PT_downloads_v022",
    "UAL_PT_status_v022",
    "UAL_UL_assets_v023",
    "UAL_UL_online_v023",
    "UAL_PT_main_v023",
    "UAL_PT_assets_v023",
    "UAL_PT_downloads_v023",
    "UAL_PT_status_v023",
)


def _unregister_named(name: str) -> None:
    from .. import register_utils

    cls = getattr(bpy.types, name, None)
    if cls is None:
        return
    register_utils.unregister_class_safe(cls)


def register():
    from .. import register_utils

    for name in _LEGACY_CLASS_NAMES:
        _unregister_named(name)
    register_utils.register_classes(_CLASSES)


def unregister():
    from .. import register_utils

    register_utils.unregister_classes(_CLASSES)
    for name in _LEGACY_CLASS_NAMES:
        _unregister_named(name)
