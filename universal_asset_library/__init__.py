# ##### BEGIN GPL LICENSE BLOCK #####
#
#  This program is free software; you can redistribute it and/or
#  modify it under the terms of the GNU General Public License
#  as published by the Free Software Foundation; either version 3
#  of the License, or (at your option) any later version.
#
# ##### END GPL LICENSE BLOCK #####
"""Universal Asset Library — Blender extension entry point."""

from __future__ import annotations

bl_info = {
    "name": "Universal Asset Library",
    "author": "Universal Asset Library",
        "version": (0, 3, 96),
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar > UAL",
    "description": "Browse local and online assets; import meshes and PBR materials",
    "category": "Import-Export",
}


def ensure_pro_ui_registered() -> None:
    """Register Fab/Epic + Asset Bar RNA when Pro modules are on disk.

    Seat/feature gates still hide UI via ``pro_features.*_enabled()``. Registering
    on module presence (not live seat) avoids a Community→Pro race: Connect /
    seat refresh can finish *after* ``register()``, which previously left Bar
    and Epic operators permanently unregistered until a full restart.
    """
    import bpy  # noqa: F401 — Blender-only

    from . import pro_features

    if pro_features.is_community_package():
        return

    try:
        from .ui import asset_bar
    except ImportError:
        asset_bar = None
    try:
        from .ui import epic_signin
    except ImportError:
        epic_signin = None

    if asset_bar is not None and pro_features.module_available(pro_features.FEATURE_ASSET_BAR):
        if not hasattr(bpy.types, "UAL_OT_asset_bar"):
            try:
                asset_bar.register()
            except Exception:
                pass
    if epic_signin is not None and pro_features.module_available(pro_features.FEATURE_FAB):
        if not hasattr(bpy.types, "UAL_OT_epic_exchange_signin"):
            try:
                epic_signin.register()
            except Exception:
                pass


def register() -> None:
    import bpy  # noqa: F401 — Blender-only

    from . import material_blend
    from . import preferences
    from . import register_utils
    from . import tasks_queue
    from . import thumbnails
    from .ui import drag_drop, hover_preview, operators, panels

    # Recover from a previous partial enable (stuck PropertyGroup RNA).
    preferences._del_wm_ui_attrs()
    register_utils.purge_named_types(getattr(preferences, "_LEGACY_RNA_NAMES", ()))

    thumbnails.register_previews()
    preferences.register()
    material_blend.register()
    operators.register()
    panels.register()
    hover_preview.register()
    ensure_pro_ui_registered()
    drag_drop.register()
    tasks_queue.register()
    preferences.ensure_storage_defaults()
    try:
        from . import entitlement_client

        entitlement_client.maybe_schedule_seat_refresh()
    except Exception:
        pass
    try:
        from . import update_manager

        update_manager.maybe_schedule_update_check()
    except Exception:
        pass
    # Arm Online auto-search BEFORE restoring scope/source. Otherwise
    # ``online_source`` RNA update clears the grid and schedule_online_search
    # no-ops (still disarmed), breaking auto_search_on_open.
    try:
        operators.arm_online_auto_search()
    except Exception:
        pass
    # Restore last view modes / scope from disk config (Grid is default for both)
    try:
        cfg = preferences.prefs_to_config()
        ui = preferences.get_wm_ui()
        if ui is not None:
            if hasattr(ui, "view_mode"):
                ui.view_mode = (cfg.get("ui") or {}).get("view_mode") or "grid"
            if hasattr(ui, "online_view_mode"):
                ui.online_view_mode = (cfg.get("online_ui") or {}).get("view_mode") or "grid"
            last_scope = (cfg.get("ui") or {}).get("last_scope") or "library"
            if last_scope in ("library", "favorites", "online") and hasattr(ui, "scope"):
                ui.scope = last_scope
            last_source = (cfg.get("ui") or {}).get("last_source") or "polyhaven"
            from . import pro_features

            if last_source == "fab" and not pro_features.fab_enabled(cfg):
                last_source = "polyhaven"
            try:
                if hasattr(ui, "online_source"):
                    ui.online_source = last_source
            except Exception:
                pass
            if getattr(ui, "scope", "library") in ("library", "favorites"):
                operators._fill_library_list(bpy.context)
    except Exception:
        pass


def unregister() -> None:
    from . import interaction_cancel
    from . import material_blend
    from . import preferences
    from . import tasks_queue
    from . import thumbnails
    from .ui import drag_drop, hover_preview, operators, panels

    try:
        from .ui import asset_bar
    except ImportError:
        asset_bar = None
    try:
        from .ui import epic_signin
    except ImportError:
        epic_signin = None

    try:
        interaction_cancel.cancel_all_interactions(None, reason="unregister")
    except Exception:
        pass
    tasks_queue.unregister()
    if epic_signin is not None:
        try:
            epic_signin.unregister()
        except Exception:
            pass
    drag_drop.unregister()
    if asset_bar is not None:
        try:
            asset_bar.unregister()
        except Exception:
            pass
    hover_preview.unregister()
    panels.unregister()
    operators.unregister()
    material_blend.unregister()
    preferences.unregister()
    try:
        from . import cache_maintenance

        cache_maintenance.clear_memory_caches()
    except Exception:
        pass
    thumbnails.unregister_previews()
