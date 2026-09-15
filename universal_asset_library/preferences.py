# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""AddonPreferences (Edit → Preferences → Extensions) + WindowManager UI state.

All durable settings live here — not in the N-panel — so the browser stays
uncluttered (BlenderKit placement pattern; Houdini category content).
"""

from __future__ import annotations

import copy
import os

import bpy
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    StringProperty,
)
from bpy.types import AddonPreferences, PropertyGroup, UIList

from . import config as ual_config
from . import paths

# WindowManager pointer. Bump when PropertyGroup RNA props change so Blender
# cannot keep a stale/incomplete group (e.g. missing ``online_results``).

# Throttle Preferences Backup cloud-status prefetch (avoid HTTP every redraw).
_BACKUP_CLOUD_PREFETCH_STARTED = False
_BACKUP_CLOUD_PREFETCH_KEY = ""
WM_UI_ATTR = "ual_pg5"
_LEGACY_WM_UI_ATTRS = ("ual", "ual_pg", "ual_pg2", "ual_pg3", "ual_pg4")
_REQUIRED_UI_PROP_NAMES = ("scope", "assets", "online_results", "status_message")

# Static filter enum — Blender 5.2 rejects *callback* EnumProperty with icon
# 5-tuples during PropertyGroup registration (partial RNA = broken panel).
_FILTER_TYPE_ITEMS_STATIC = (
    ("ALL", "All", "All asset types", "FILTER", 0),
    ("mesh", "Mesh", "3D meshes / models", "MESH_DATA", 1),
    ("material_set", "Material", "PBR materials / shaders", "MATERIAL_DATA", 2),
    ("texture", "Texture", "Texture / map sets", "TEXTURE", 3),
    ("hdri", "HDRI", "Environment / world HDRIs", "WORLD_DATA", 4),
)

SOURCE_IDS = (
    "polyhaven",
    "ambientcg",
    "gpuopen",
    "lazytextures",
    "threedscans",
    "pbrpx",
    "polypizza",
    "sketchfab",
    "fab",
)

SOURCE_LABELS = {
    "polyhaven": "Poly Haven",
    "ambientcg": "ambientCG",
    "gpuopen": "GPUOpen MaterialX",
    "lazytextures": "LazyTextures",
    "threedscans": "Three D Scans",
    "pbrpx": "PBRPX",
    "polypizza": "Poly Pizza",
    "sketchfab": "Sketchfab",
    "fab": "Fab / Megascans",
}


def _get_prefs():
    return bpy.context.preferences.addons[__package__].preferences


def library_roots_from_prefs(prefs=None) -> list:
    prefs = prefs or _get_prefs()
    roots = [
        os.path.normpath(item.path)
        for item in prefs.library_roots
        if (item.path or "").strip()
    ]
    if not roots and (prefs.library_root or "").strip():
        roots = [os.path.normpath(prefs.library_root.strip())]
    if not roots:
        roots = [paths.default_library_root()]
    return roots


def active_cache_dir(prefs=None) -> str:
    """Repaired UAL CACHE path from Preferences (never empty / library-nested)."""
    prefs = prefs or _get_prefs()
    return paths.repair_cache_dir(prefs.cache_dir or "", library_roots_from_prefs(prefs))


# In-memory settings.json — invalidate only when ``save_config`` writes (generation).
_active_config_cache = None
_active_config_cache_dir = None
_active_config_cache_gen = -1


def invalidate_active_config_cache() -> None:
    """Drop the load_active_config memory cache (next read hits disk)."""
    global _active_config_cache, _active_config_cache_dir, _active_config_cache_gen
    _active_config_cache = None
    _active_config_cache_dir = None
    _active_config_cache_gen = -1


def load_active_config() -> dict:
    """Load settings.json from the active (repaired) cache — not bare default cache.

    Cached in memory until ``save_config`` writes (or cache_dir changes). Callers
    must not mutate the returned dict unless they also persist via save_config.
    """
    global _active_config_cache, _active_config_cache_dir, _active_config_cache_gen
    try:
        cache_dir = active_cache_dir()
    except Exception:
        cache_dir = ""
    gen = ual_config.save_generation()
    if (
        _active_config_cache is not None
        and _active_config_cache_dir == cache_dir
        and _active_config_cache_gen == gen
    ):
        return _active_config_cache
    try:
        cfg = ual_config.load_config(cache_dir) if cache_dir else ual_config.load_config()
    except Exception:
        cfg = ual_config.load_config()
    _active_config_cache = cfg
    _active_config_cache_dir = cache_dir
    _active_config_cache_gen = gen
    return cfg


def save_prefs_config(cfg=None, *, skip_remote_sync: bool = False) -> dict:
    """Persist config under repaired cache_dir and sync prefs.cache_dir when needed."""
    global _active_config_cache, _active_config_cache_dir, _active_config_cache_gen
    _cancel_debounced_prefs_save()
    prefs = _get_prefs()
    cfg = cfg or prefs_to_config()
    repaired = paths.repair_cache_dir(
        str(cfg.get("cache_dir") or prefs.cache_dir or ""),
        list(cfg.get("library_roots") or library_roots_from_prefs(prefs)),
    )
    cfg["cache_dir"] = repaired
    raw = (prefs.cache_dir or "").strip()
    if (
        not raw
        or os.path.normcase(os.path.normpath(raw))
        != os.path.normcase(os.path.normpath(repaired))
    ):
        prefs.cache_dir = repaired
    ual_config.save_config(cfg, repaired)
    _active_config_cache = cfg
    _active_config_cache_dir = repaired
    _active_config_cache_gen = ual_config.save_generation()
    if skip_remote_sync:
        return cfg
    try:
        from . import provider_sync

        provider_sync.schedule_provider_sync(cfg)
    except Exception:
        pass
    try:
        from . import entitlement_client

        entitlement_client.schedule_seat_refresh(cfg)
    except Exception:
        pass
    return cfg


def hydrate_prefs_from_config(prefs=None) -> None:
    """Copy secrets from settings.json into empty AddonPreferences (prevent wipe on register)."""
    from .online_auth import SECRET_ONLINE_PREF_KEYS

    prefs = prefs or _get_prefs()
    try:
        cfg = ual_config.load_config(active_cache_dir(prefs))
    except Exception:
        return
    online = cfg.get("online") or {}
    for key in SECRET_ONLINE_PREF_KEYS:
        if (getattr(prefs, key, "") or "").strip():
            continue
        disk = str(online.get(key) or "").strip()
        if disk:
            try:
                setattr(prefs, key, disk)
            except Exception:
                pass
    releases = cfg.get("releases") if isinstance(cfg.get("releases"), dict) else {}
    if not str(getattr(prefs, "releases_api_base_url", "") or "").strip():
        disk_base = str((releases or {}).get("api_base_url") or "").strip()
        try:
            prefs.releases_api_base_url = disk_base or ual_config.DEFAULT_UPDATES_API_BASE
        except Exception:
            pass
    if not str(getattr(prefs, "account_user_id", "") or "").strip():
        account = cfg.get("account") if isinstance(cfg.get("account"), dict) else {}
        disk_user = str((account or {}).get("user_id") or "").strip()
        if disk_user:
            try:
                prefs.account_user_id = disk_user
            except Exception:
                pass
    if not str(getattr(prefs, "account_addon_key", "") or "").strip():
        account = cfg.get("account") if isinstance(cfg.get("account"), dict) else {}
        disk_key = str((account or {}).get("addon_key") or "").strip()
        if disk_key:
            try:
                prefs.account_addon_key = disk_key
            except Exception:
                pass


def hydrate_storage_prefs_from_config(prefs=None) -> None:
    """Apply library/cache/copy flags from settings.json when RNA is empty or unsafe.

    Used after terminal/repair writes so Blender Preferences pick up shared-library
    fixes without wiping secrets. RNA still wins on later user edits via save_prefs.
    """
    prefs = prefs or _get_prefs()
    try:
        cfg = ual_config.load_config(active_cache_dir(prefs))
    except Exception:
        return
    online = cfg.get("online") if isinstance(cfg.get("online"), dict) else {}

    disk_roots = [
        os.path.normpath(r)
        for r in (cfg.get("library_roots") or [])
        if isinstance(r, str) and r.strip()
    ]
    rna_roots = [
        os.path.normpath(item.path) for item in prefs.library_roots if (item.path or "").strip()
    ]
    rna_keys = {os.path.normcase(r) for r in rna_roots}
    missing = [r for r in disk_roots if os.path.normcase(r) not in rna_keys]
    if disk_roots and (not rna_roots or missing):
        try:
            if not rna_roots:
                prefs.library_roots.clear()
                for root in disk_roots:
                    item = prefs.library_roots.add()
                    item.path = root
                prefs.library_root = disk_roots[0]
            else:
                for root in missing:
                    item = prefs.library_roots.add()
                    item.path = root
        except Exception:
            pass

    disk_cache = str(cfg.get("cache_dir") or "").strip()
    if disk_cache:
        try:
            prefs.cache_dir = paths.repair_cache_dir(
                disk_cache,
                [item.path for item in prefs.library_roots if item.path] or disk_roots,
            )
        except Exception:
            pass

    if "copy_to_library" in online:
        try:
            prefs.copy_to_library = bool(online.get("copy_to_library"))
        except Exception:
            pass
    if "index_downloads" in online:
        try:
            prefs.index_downloads = bool(online.get("index_downloads"))
        except Exception:
            pass
    if "keep_cache" in online:
        try:
            prefs.keep_cache = bool(online.get("keep_cache"))
        except Exception:
            pass


# Slider RNA updates fire on every drag tick. Persist once after the value settles.
_PREFS_SAVE_DEBOUNCE_SEC = 0.2
_prefs_save_pending = False


def _prefs_app_timers():
    return getattr(getattr(bpy, "app", None), "timers", None)


def reschedule_one_shot_timer(callback, delay_sec, timers=None) -> bool:
    """Cancel+re-register ``callback`` as a one-shot. Returns False if timers unavailable."""
    timers = timers if timers is not None else _prefs_app_timers()
    if timers is None:
        return False
    try:
        if timers.is_registered(callback):
            timers.unregister(callback)
    except Exception:
        pass
    timers.register(callback, first_interval=float(delay_sec))
    return True


def _flush_debounced_prefs_save():
    global _prefs_save_pending
    _prefs_save_pending = False
    try:
        save_prefs_config()
    except Exception:
        pass
    return None


def _cancel_debounced_prefs_save() -> None:
    global _prefs_save_pending
    _prefs_save_pending = False
    timers = _prefs_app_timers()
    if timers is None:
        return
    try:
        if timers.is_registered(_flush_debounced_prefs_save):
            timers.unregister(_flush_debounced_prefs_save)
    except Exception:
        pass


def _schedule_debounced_prefs_save() -> None:
    """Write settings.json ~200ms after the last slider tick (not on every tick)."""
    global _prefs_save_pending
    _prefs_save_pending = True
    try:
        if reschedule_one_shot_timer(
            _flush_debounced_prefs_save, _PREFS_SAVE_DEBOUNCE_SEC
        ):
            return
    except Exception:
        pass
    _flush_debounced_prefs_save()


def _sync_prefs_update(_self, _context) -> None:
    """Write AddonPreferences → settings.json when a durable prop changes."""
    try:
        save_prefs_config()
    except Exception:
        pass


def _account_addon_key_update(self, _context) -> None:
    """Clear stored credentials and lock the seat when the API key field is emptied."""
    key = str(getattr(self, "account_addon_key", "") or "").strip()
    if not key:
        try:
            from . import entitlement_client

            cfg = load_active_config()
            entitlement_client.clear_account(cfg)
            cfg.setdefault("account", {})
            cfg["account"]["addon_key"] = ""
            cfg["account"]["user_id"] = ""
            try:
                self.account_user_id = ""
            except Exception:
                pass
            save_prefs_config(cfg, skip_remote_sync=True)
        except Exception:
            pass
        return
    _sync_prefs_update(self, _context)


def _sync_blend_normals_prefs(self, context) -> None:
    """Import-tab default also drives the N-panel scene toggle (one authority)."""
    _sync_prefs_update(self, context)
    try:
        scene = getattr(context, "scene", None) if context else None
        if scene is not None and hasattr(scene, "ual_blend_blend_normals"):
            scene.ual_blend_blend_normals = bool(self.material_blend_blend_normals)
    except Exception:
        pass


def _sync_prefs_slider_update(_self, _context) -> None:
    """Slider tick: RNA is already live; only the disk write is debounced."""
    _schedule_debounced_prefs_save()


def _invalidate_layout_hit_cache(context) -> None:
    try:
        from .ui import hover_preview
        from .ui import operators as ops

        ops.reset_assets_view_scroll(context)
        hover_preview.invalidate_assets_hit(clear_hover=True)
    except Exception:
        pass


def _sync_prefs_layout_update(self, context) -> None:
    """Persist prefs that change Assets grid geometry and drop hover hit cache."""
    _sync_prefs_update(self, context)
    _invalidate_layout_hit_cache(context)


def _sync_prefs_thumb_size_update(self, context) -> None:
    """Thumbnail Size slider: live grid reflow, debounced settings.json write.

    Hit-cache invalidation and scroll reset stay per-tick — they are cheap
    in-memory ops so the N-panel grid resizes while dragging. Only the disk
    write waits until the value settles (~200ms).
    """
    _schedule_debounced_prefs_save()
    _invalidate_layout_hit_cache(context)


def _on_hover_preview_enabled_changed(self, context) -> None:
    _sync_prefs_update(self, context)
    try:
        from .ui import hover_preview

        if not bool(getattr(self, "hover_preview_enabled", True)):
            hover_preview.reset_hover_state()
        if not bool(getattr(self, "hover_preview_pin_enabled", True)):
            hover_preview.set_pinned(False)
    except Exception:
        pass


class UAL_LibraryRootItem(PropertyGroup):
    path: StringProperty(
        name="Path",
        subtype="DIR_PATH",
        default="",
        update=_sync_prefs_update,
    )


class UAL_UL_library_roots(UIList):
    bl_idname = "UAL_UL_library_roots"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        layout.prop(item, "path", text="", emboss=False)


class UAL_AssetItem(PropertyGroup):
    # HIDDEN so UIList / layout never auto-surfaces a name bar under thumbs
    name: StringProperty(name="Name", default="", options={"HIDDEN"})
    path: StringProperty(name="Path", default="")
    asset_type: StringProperty(name="Type", default="mesh")
    thumb_path: StringProperty(name="Thumb", default="")
    source_id: StringProperty(name="Source", default="local")
    preview_icon_id: IntProperty(name="Preview Icon", default=0)


class UAL_OnlineResultItem(PropertyGroup):
    name: StringProperty(name="Name", default="", options={"HIDDEN"})
    asset_id: StringProperty(name="ID", default="")
    source_id: StringProperty(name="Source", default="")
    asset_type: StringProperty(name="Type", default="mesh")
    thumb_url: StringProperty(name="Thumb URL", default="")
    license_name: StringProperty(name="License", default="")
    formats: StringProperty(name="Formats", default="")
    resolutions: StringProperty(name="Resolutions", default="")
    preview_icon_id: IntProperty(name="Preview Icon", default=0)
    thumb_path: StringProperty(name="Thumb Path", default="")
    in_library: BoolProperty(
        name="In Library",
        default=False,
        options={"HIDDEN"},
        description="True when this Online result is already saved for the current Size/Format",
    )


class UAL_AddonPreferences(AddonPreferences):
    """Shown under Preferences → Get Extensions → Universal Asset Library (expanded)."""

    bl_idname = __package__

    preferences_tab: EnumProperty(
        name="Category",
        items=(
            ("LIBRARY", "My Library", "Library folders and cache", "FILE_FOLDER", 0),
            ("ONLINE", "Online", "Sources, download storage, API keys", "WORLD", 1),
            ("PREVIEWS", "Previews", "Preview images and hover preview", "IMAGE_DATA", 2),
            ("IMPORT", "Import", "Mesh / material / Fab import defaults", "IMPORT", 3),
        ),
        default="LIBRARY",
    )
    show_quick_setup: BoolProperty(
        name="Quick Setup",
        description="Show library/cache paths in the setup summary (issues always show)",
        default=False,
    )
    show_cache_clear_details: BoolProperty(
        name="Choose what to clear",
        description="Show individual cache-clear modes (destructive)",
        default=False,
    )
    show_online_diagnostics: BoolProperty(
        name="Platform diagnostics",
        description="Show per-source Online / Offline / Need sign-in table",
        default=False,
    )
    show_all_online_sources: BoolProperty(
        name="Show all sources",
        description="Deprecated — Preferences always lists every connector",
        default=True,
    )
    show_advanced_download: BoolProperty(
        name="Advanced download options",
        description="Auto-search on open and related download extras",
        default=False,
    )
    show_advanced_import: BoolProperty(
        name="Advanced import options",
        description="Texture naming, normal space, normalize scale",
        default=False,
    )
    show_fab_cookie_fallback: BoolProperty(
        name="Fab cookie fallback (advanced)",
        description="Browser cookie backup if Epic download fails",
        default=False,
    )

    library_roots: CollectionProperty(type=UAL_LibraryRootItem)
    library_roots_index: IntProperty(name="Active Root", default=0)
    library_root: StringProperty(
        name="Primary Library Root",
        subtype="DIR_PATH",
        default="",
        description="Used when the roots list is empty (legacy single-root)",
        update=_sync_prefs_update,
    )
    cache_dir: StringProperty(
        name="Cache Directory",
        subtype="DIR_PATH",
        default="",
        description="Temporary CDN staging (UAL CACHE) — not the library",
        update=_sync_prefs_update,
    )
    releases_api_base_url: StringProperty(
        name="Releases API Base URL",
        description=(
            "Website origin for seat refresh and update checks. "
            "Production: https://universalassetlibrary.com. "
            "Local ual-web: http://localhost:3000. Not the ual-api :4000 URL."
        ),
        default=ual_config.DEFAULT_UPDATES_API_BASE,
        update=_sync_prefs_update,
    )
    account_user_id: StringProperty(
        name="UAL Account User ID",
        description="Auto-filled after Connect. Not shown for editing — the API key identifies the account.",
        default="",
        update=_sync_prefs_update,
    )
    account_addon_key: StringProperty(
        name="UAL API Key",
        description=(
            "API key from Account → Profile on the UAL website (starts with ualak_). "
            "Paste and click Connect — no email, password, or user ID needed. "
            "Clear the field or use Disconnect to lock Pro features on this install."
        ),
        default="",
        subtype="PASSWORD",
        update=_account_addon_key_update,
    )
    backup_cloud_status: StringProperty(
        name="Cloud Backup Status",
        description="Last known cloud backup revision or status (Preferences → Backup)",
        default="Cloud: not fetched",
        options={"HIDDEN"},
    )
    release_channel: EnumProperty(
        name="Release Channel",
        description="Channel used by Check for Updates",
        items=(
            ("stable", "Stable", "Published stable releases"),
            ("beta", "Beta", "Beta channel when published"),
        ),
        default="stable",
        update=_sync_prefs_update,
    )
    keep_cache: BoolProperty(
        name="Keep Cache After Save",
        description="Leave CDN files in the cache after copying to Online Downloads (only when Copy Downloads is on)",
        default=False,
        update=_sync_prefs_update,
    )
    copy_to_library: BoolProperty(
        name="Copy Downloads to Library",
        description=(
            "Stage online downloads into Library/Online Downloads/… (recommended). "
            "Required so Houdini UAL can see the same assets when you share the library folder. "
            "Never share the cache folder with Houdini"
        ),
        default=True,
        update=_sync_prefs_update,
    )

    enable_polyhaven: BoolProperty(name="Poly Haven", default=True, update=_sync_prefs_update)
    enable_ambientcg: BoolProperty(name="ambientCG", default=True, update=_sync_prefs_update)
    enable_gpuopen: BoolProperty(name="GPUOpen", default=True, update=_sync_prefs_update)
    enable_lazytextures: BoolProperty(name="LazyTextures", default=True, update=_sync_prefs_update)
    enable_threedscans: BoolProperty(name="Three D Scans", default=True, update=_sync_prefs_update)
    enable_pbrpx: BoolProperty(name="PBRPX", default=True, update=_sync_prefs_update)
    # Default OFF until API key / token is set (Houdini parity — avoid failed Search)
    enable_polypizza: BoolProperty(name="Poly Pizza", default=False, update=_sync_prefs_update)
    enable_sketchfab: BoolProperty(name="Sketchfab", default=False, update=_sync_prefs_update)
    enable_fab: BoolProperty(name="Fab / Megascans", default=True, update=_sync_prefs_update)

    sketchfab_token: StringProperty(
        name="Sketchfab API Token",
        description="Personal API token from sketchfab.com/settings/password (use Get Sketchfab Token)",
        subtype="PASSWORD",
        default="",
        update=_sync_prefs_update,
    )
    polypizza_api_key: StringProperty(
        name="Poly Pizza API Key",
        description="API key from poly.pizza/settings/api (use Get Poly Pizza API Key)",
        subtype="PASSWORD",
        default="",
        update=_sync_prefs_update,
    )
    fab_sessionid: StringProperty(
        name="Fab Session ID",
        description="Optional fab.com fab_sessionid cookie (backup if Epic download fails)",
        subtype="PASSWORD",
        default="",
        update=_sync_prefs_update,
    )
    fab_csrftoken: StringProperty(
        name="Fab CSRF Token",
        description="Optional fab.com fab_csrftoken cookie (backup if Epic download fails)",
        subtype="PASSWORD",
        default="",
        update=_sync_prefs_update,
    )

    thumb_size: IntProperty(
        name="Thumbnail Size",
        description="Shared grid/list thumb size (64–160)",
        default=112,
        min=64,
        max=160,
        update=_sync_prefs_thumb_size_update,
    )
    hover_preview_enabled: BoolProperty(
        name="GPU Hover Preview",
        description=(
            "Show a large GPU tooltip for the tile under the cursor "
            "(does not change selection — click the name bar to select)"
        ),
        default=True,
        update=_on_hover_preview_enabled_changed,
    )
    hover_preview_size: IntProperty(
        name="Hover Preview Size",
        description="Floating preview image size in pixels (160–512)",
        default=320,
        min=160,
        max=512,
        update=_sync_prefs_slider_update,
    )
    hover_preview_delay_ms: IntProperty(
        name="Hover Preview Delay (ms)",
        description="Debounce before first showing the floating preview (default 160; tile-to-tile swaps are instant)",
        default=160,
        min=120,
        max=800,
        update=_sync_prefs_slider_update,
    )
    hover_preview_pin_enabled: BoolProperty(
        name="Allow Pin Hover Preview",
        description="Ctrl+Shift+Space pins the current asset for hover; Esc unpins",
        default=True,
        update=_on_hover_preview_enabled_changed,
    )

    # Online pipeline
    index_downloads: BoolProperty(
        name="Index Downloads into Library",
        description="Add Online Downloads folders to the library index after save",
        default=True,
        update=_sync_prefs_update,
    )
    auto_search_on_open: BoolProperty(
        name="Auto-Search Online on Open",
        description="Also re-search when opening the Online tab if results are already showing. Switching Source always searches.",
        default=False,
        update=_sync_prefs_update,
    )
    auto_open_asset_bar_on_search: BoolProperty(
        name="Auto-Open Asset Bar on Search",
        description="After Online search completes, open the GPU asset bar if it is not already open (opt-in)",
        default=False,
        update=_sync_prefs_update,
    )
    hide_asset_bar_when_ual_sidebar: BoolProperty(
        name="Hide Asset Bar when UAL Sidebar Opens",
        description=(
            "When the UAL sidebar tab is visible, close the GPU asset bar. "
            "Closing the sidebar does not bring the bar back — use the Bar button to show it, "
            "or the X on the bar to hide it. Item/Tool/View tabs do not close the bar"
        ),
        default=True,
        update=_sync_prefs_update,
    )
    default_download_resolution: EnumProperty(
        name="Default Max Texture Quality",
        description=(
            "Max texture/HDRI size when a source offers multiple tiers "
            "(Poly Haven, ambientCG, GPUOpen, LazyTextures, PBRPX). "
            "Fab maps this to Megascans quality (2K→Mid, 4K→High, 8K/Original→Raw). "
            "Ignored for Sketchfab, Poly Pizza, and Three D Scans (no size tiers)"
        ),
        items=(
            ("512", "512", "Prefer ≤512 when available"),
            ("1024", "1K", "Prefer ≤1K"),
            ("2048", "2K", "Prefer ≤2K (default)"),
            ("4096", "4K", "Prefer ≤4K"),
            ("8192", "8K", "Prefer ≤8K"),
            ("ORIGINAL", "Original (highest)", "Highest available tier / Fab Raw"),
        ),
        default="2048",
        update=_sync_prefs_update,
    )
    match_size_on_import: BoolProperty(
        name="Match Size on Import",
        description="Uniformly scale imported mesh to fit a target bbox size",
        default=False,
        update=_sync_prefs_update,
    )
    match_size_target: FloatProperty(
        name="Match Size Target",
        default=1.0,
        min=0.01,
        max=1000.0,
        update=_sync_prefs_slider_update,
    )
    unit_scale: FloatProperty(
        name="Unit Scale",
        description="Extra uniform scale applied after import (non-Fab)",
        default=1.0,
        min=0.0001,
        max=1000.0,
        update=_sync_prefs_slider_update,
    )
    material_import_automap: BoolProperty(
        name="Auto-map textures (Smart UV)",
        description=(
            "When applying materials, run Smart UV Project if the mesh has no usable UVs. "
            "Has no effect when Apply material is off"
        ),
        default=True,
        update=_sync_prefs_update,
    )

    # Import extras
    normal_map_space: EnumProperty(
        name="Normal Map Space",
        items=(
            ("OPENGL", "OpenGL", "Y+ (Blender / Substance default)"),
            ("DIRECTX", "DirectX", "Flip green channel for DirectX normals"),
        ),
        default="OPENGL",
        update=_sync_prefs_update,
    )
    texture_naming_profile: EnumProperty(
        name="Texture Naming Profile",
        items=(
            ("auto", "Auto", ""),
            ("generic", "Generic", ""),
            ("megascans", "Megascans / Fab", ""),
            ("ambientcg", "ambientCG", ""),
            ("polyhaven", "Poly Haven", ""),
        ),
        default="auto",
        update=_sync_prefs_update,
    )
    normalize_scale: BoolProperty(
        name="Normalize Scale",
        default=False,
        update=_sync_prefs_update,
    )
    pivot_to_base: BoolProperty(
        name="Pivot to Base (legacy)",
        description="Only used when Place at World Origin is off — move mesh so lowest point sits on Z=0",
        default=False,
        update=_sync_prefs_update,
    )
    place_at_world_origin: BoolProperty(
        name="Place at World Origin",
        description=(
            "Meshes from Import, Download & Import, and Drop into Scene sit at "
            "(0,0,0) with the base on the ground (always visible when the view "
            "looks at the origin). Materials still assign to the Drop hit object. "
            "Off = Drop meshes use the raycast / ground hit instead"
        ),
        default=True,
        update=_sync_prefs_update,
    )
    organize_into_collection: BoolProperty(
        name="Put meshes into asset collection",
        description=(
            "After importing a 3D asset, move its objects into a new Outliner "
            "collection named after the asset (BlenderKit-style). Materials and "
            "HDRIs are unchanged"
        ),
        default=True,
        update=_sync_prefs_update,
    )

    apply_material_to_selected: BoolProperty(
        name="Apply material to selected / drop target",
        description=(
            "When on, assign imported materials to selected meshes (or the Drop hit object). "
            "When off, materials are created but not assigned"
        ),
        default=True,
        update=_sync_prefs_update,
    )
    material_blend_max_distance: FloatProperty(
        name="Material Blend Max Distance",
        description="Default outer proximity range for UAL Material Blend (Create)",
        subtype="DISTANCE",
        default=1.0,
        min=0.0,
        soft_max=5.0,
        update=_sync_prefs_update,
    )
    material_blend_min_distance: FloatProperty(
        name="Material Blend Min Distance",
        description="Default inner proximity for UAL Material Blend (Create)",
        subtype="DISTANCE",
        default=1.0,
        min=0.0,
        soft_max=2.0,
        update=_sync_prefs_update,
    )
    material_blend_live_noise_scale: FloatProperty(
        name="Default Noise Scale",
        description="Default edge Noise Scale for Material Blend Create",
        default=1.0,
        min=0.0,
        soft_max=32.0,
        update=_sync_prefs_update,
    )
    material_blend_live_noise_amount: FloatProperty(
        name="Default Noise Amount",
        description="Default edge Noise Amount for Material Blend Create",
        default=1.0,
        min=0.0,
        soft_max=2.0,
        update=_sync_prefs_update,
    )
    material_blend_blend_normals: BoolProperty(
        name="Blend Normals",
        description=(
            "Default for Material Blend Normals (also toggled in the UAL sidebar). "
            "Transfers custom normals from source (proximity-weighted)"
        ),
        default=True,
        update=_sync_blend_normals_prefs,
    )
    strip_megascans_billboard_lods: BoolProperty(
        name="Strip Megascans Billboard / Collision",
        description=(
            "Fab/Megascans plants: remove Billboard/LOD2+, UE collision/proxy "
            "(UCX_/Convex/…), and far LODs — keep hero LOD0/LOD1"
        ),
        default=True,
        update=_sync_prefs_update,
    )
    fab_unit_scale_mode: EnumProperty(
        name="Fab Unit Scale",
        description="cm→m for Quixel/Fab FBX/USD (Fab-gated only)",
        items=(
            ("cm_to_m", "Always cm → m", "Force ×0.01 on Fab meshes"),
            ("auto", "Auto (FBX/USD ×0.01)", "cm→m for FBX/USD; glTF stays meters"),
            ("off", "Off", "No unit conversion"),
        ),
        default="cm_to_m",
        update=_sync_prefs_update,
    )

    # Kept for older prefs JSON / operators; Sources UI always lists every connector.
    _CORE_SOURCE_IDS = (
        "polyhaven",
        "ambientcg",
        "sketchfab",
        "polypizza",
    )

    def _library_roots_list(self) -> list:
        roots = [item.path for item in self.library_roots if (item.path or "").strip()]
        if not roots and (self.library_root or "").strip():
            roots = [self.library_root]
        return roots

    def _quick_setup_issues(self) -> list:
        """Short human-readable setup issues for the Quick Setup header."""
        issues = []
        for warn in path_warnings_for_prefs(self):
            issues.append(warn)
        if not bool(getattr(bpy.app, "online_access", True)):
            issues.append("Allow Online Access is off")
        try:
            from . import online_health

            need = sum(
                1
                for r in online_health.build_local_rows(
                    prefs_to_config(),
                    enabled_sources=enabled_source_ids(self),
                )
                if r.status == "need_auth"
            )
            if need:
                issues.append(f"{need} site(s) need sign-in")
        except Exception:
            pass
        return issues

    def _visible_source_ids(self) -> list:
        """All connectors in Preferences — never hide rows behind a Show-all toggle.

        Nested UILayout boxes do not scroll independently; Blender Preferences
        scrolls the whole panel. Collapsing sources made Fab look missing.
        """
        return list(SOURCE_IDS)

    def _fab_prefs_unlocked(self) -> bool:
        try:
            from . import pro_features

            return bool(pro_features.fab_enabled())
        except Exception:
            return False

    def _fab_lock_hint(self) -> str:
        try:
            from . import pro_features

            return pro_features.fab_lock_hint(load_active_config())
        except Exception:
            return "Fab needs UAL Pro — Connect + Refresh seat in UAL Account above."

    def _draw_source_toggle_row(self, parent, sid: str, online_links, *, fab_ok: bool = True) -> None:
        row = parent.row(align=True)
        label = SOURCE_LABELS[sid]
        toggle = row.row(align=True)
        if sid == "fab" and not fab_ok:
            toggle.enabled = False
            toggle.prop(self, f"enable_{sid}", text="{} (locked)".format(label))
        else:
            toggle.prop(self, f"enable_{sid}", text=label)
        if online_links.website_url(sid):
            op = row.operator("ual.open_website", text="", icon="URL")
            op.link_id = sid
            op.url = ""

    def draw(self, context):
        layout = self.layout
        self._draw_account_signin(layout)
        layout.separator()
        self._draw_quick_setup(layout, context)
        layout.prop(self, "preferences_tab", expand=True)
        layout.separator()

        tab = self.preferences_tab
        if tab == "LIBRARY":
            self._draw_library(layout)
        elif tab == "ONLINE":
            self._draw_online(layout)
        elif tab == "PREVIEWS":
            self._draw_previews(layout)
        else:
            self._draw_import(layout)

        layout.separator()
        footer = layout.box()
        auto = footer.column(align=True)
        auto.scale_y = 0.85
        auto.label(text="Saved automatically when you change options", icon="INFO")
        row = footer.row(align=True)
        row.operator("ual.health_check", text="Health Check", icon="CHECKMARK")
        row.operator(
            "ual.send_diagnostics_report",
            text="Send Diagnostics",
            icon="FILE_REFRESH",
        )
        row.operator("ual.check_online_platforms", text="Check online sources", icon="WORLD")
        row2 = footer.row(align=True)
        row2.operator("ual.open_cache_folder", text="Open Cache", icon="TEMP")
        row2.operator("ual.open_settings_folder", text="Open Settings Folder", icon="FOLDER_REDIRECT")
        row3 = footer.row(align=True)
        row3.operator("ual.restore_default_prefs", text="Restore Defaults", icon="LOOP_BACK")
        row3.operator("ual.sync_prefs_to_config", text="Write settings now", icon="FILE_TICK")
        try:
            from .ui_strings import ADDON_VERSION, CHANGELOG_BLURB

            ver = footer.column(align=True)
            ver.scale_y = 0.85
            ver.label(text=f"UAL {ADDON_VERSION}")
            ver.label(text=CHANGELOG_BLURB)
        except Exception:
            pass

    def _draw_account_signin(self, layout) -> None:
        try:
            from . import pro_features

            public_store = pro_features.public_store_build()
        except Exception:
            public_store = False

        box = layout.box()
        box.label(text="UAL Account", icon="USER")
        help_col = box.column(align=True)
        help_col.scale_y = 0.85
        if public_store:
            help_col.label(text="Free Community build — local library and eight public online sources.")
            help_col.label(text="Pro (Fab, GPU bar, cloud backup) is on the website — not in this add-on.")
            links = box.row(align=True)
            op_home = links.operator("ual.open_website", text="Website", icon="URL")
            op_home.link_id = ""
            op_home.url = "https://universalassetlibrary.com"
            op_pro = links.operator("ual.open_website", text="Get Pro", icon="URL")
            op_pro.link_id = ""
            op_pro.url = "https://universalassetlibrary.com/pricing"
            box.separator()
            box.label(text="Updates", icon="FILE_REFRESH")
            row_chk = box.row(align=True)
            row_chk.operator("ual.check_for_updates", text="Check for Updates", icon="FILE_REFRESH")
            return

        help_col.label(text="Free Blender Community and paid Pro both need a UAL website account.")
        help_col.label(text="Community: local library + eight public online sources.")
        help_col.label(text="Pro adds Fab, GPU asset bar, and cloud metadata backup.")
        help_col.label(text="Paste your UAL API key (ualak_…) from Account → Profile only.")
        links = box.row(align=True)
        op_reg = links.operator("ual.open_website", text="Create Free Account", icon="URL")
        op_reg.link_id = ""
        op_reg.url = "https://universalassetlibrary.com/register"
        op_prof = links.operator("ual.open_website", text="Open Profile", icon="USER")
        op_prof.link_id = ""
        op_prof.url = "https://universalassetlibrary.com/app/account/profile"
        box.prop(self, "account_addon_key", text="UAL API key")
        row = box.row(align=True)
        row.operator("ual.connect_api_key", text="Connect", icon="USER")
        row.operator("ual.disconnect_account", text="Disconnect", icon="X")
        uid = str(getattr(self, "account_user_id", "") or "").strip()
        if uid:
            box.label(text="Connected (account resolved from API key)", icon="INFO")
        else:
            box.label(text="Not connected yet", icon="INFO")
        row_key = box.row(align=True)
        row_key.operator("ual.refresh_seat", text="Refresh seat", icon="FILE_REFRESH")
        try:
            from . import preferences_pro_ui

            preferences_pro_ui.draw_install_pro_offer(box)
        except ImportError:
            pass
        try:
            from .entitlement_client import cached_seat, feature_labels

            active_cfg = load_active_config()
            seat = cached_seat(active_cfg)
            cfg_acct = (
                active_cfg.get("account") if isinstance(active_cfg.get("account"), dict) else {}
            )
            last_err = str(cfg_acct.get("last_seat_error") or "").strip()
            if last_err:
                err_row = box.row()
                err_row.alert = True
                err_row.label(text=last_err[:120], icon="ERROR")
            if seat.get("planLabel") or seat.get("planId"):
                extra = ""
                if seat.get("deviceBound"):
                    extra = " · device bound"
                elif seat.get("deviceError"):
                    extra = f" · {seat.get('deviceError')}"
                status = str(seat.get("status") or "unknown")
                label = seat.get("planLabel") or seat.get("planId") or "Community"
                icon = "INFO" if status == "active" and seat.get("deviceBound") is not False else "ERROR"
                feats = ", ".join(feature_labels(list(seat.get("features") or []))[:4])
                box.label(
                    text=f"Plan: {label} ({status}){extra}",
                    icon=icon,
                )
                if feats:
                    box.label(text=feats)
                if status != "active":
                    box.label(
                        text="Reconnect: paste a fresh UAL API key from Profile, then Connect.",
                        icon="ERROR",
                    )
                elif seat.get("deviceBound") is False:
                    lim = box.row()
                    lim.alert = True
                    lim.label(
                        text="Device not bound — free a slot at Account → Devices (max 3), then Refresh seat.",
                        icon="ERROR",
                    )
            else:
                box.label(text="Seat: not refreshed yet", icon="INFO")
        except Exception:
            pass

        # Updates stay in the Account box (top of Preferences).
        box.separator()
        box.label(text="Updates", icon="FILE_REFRESH")
        upd_help = box.column(align=True)
        upd_help.scale_y = 0.85
        upd_help.label(text="Check when a newer UAL package is published. Install, then restart Blender.")
        row_u = box.row(align=True)
        row_u.prop(self, "releases_api_base_url", text="Updates API")
        row_u.prop(self, "release_channel", text="")
        warn = ual_config.updates_api_url_warning(str(self.releases_api_base_url or ""))
        if warn:
            warn_row = box.row()
            warn_row.alert = True
            warn_row.label(text=warn, icon="ERROR")
        row_chk = box.row(align=True)
        try:
            from . import update_manager

            update_manager.maybe_schedule_update_check()
            st = update_manager.get_cached_state()
            if st.get("installing"):
                row_chk.enabled = False
                row_chk.label(text="Downloading / installing update…", icon="TIME")
            elif st.get("checking"):
                row_chk.enabled = False
                row_chk.label(text="Checking for updates…", icon="TIME")
            elif update_manager.update_install_offered(st):
                row_chk.operator(
                    "ual.download_and_install_update",
                    text="Download & Install Update",
                    icon="IMPORT",
                )
            else:
                row_chk.operator(
                    "ual.check_for_updates",
                    text="Check for Updates",
                    icon="FILE_REFRESH",
                )
            if not st.get("checking") and not st.get("installing") and st.get("summary"):
                box.label(text=str(st["summary"])[:120], icon="INFO")
            if update_manager.update_install_offered(st):
                box.label(text="Restart Blender after install to load the new version.", icon="ERROR")
        except Exception:
            row_chk.operator(
                "ual.check_for_updates",
                text="Check for Updates",
                icon="FILE_REFRESH",
            )

        self._draw_backup_section(layout)

    def _draw_backup_section(self, layout) -> None:
        try:
            from . import preferences_pro_ui

            preferences_pro_ui.draw_backup_section(self, layout)
        except ImportError:
            return

    def _maybe_prefetch_cloud_backup_status(self) -> None:
        """Once per Preferences session, fetch cloud revision without blocking draw."""
        try:
            from . import preferences_pro_ui

            preferences_pro_ui.maybe_prefetch_cloud_backup_status(self)
        except ImportError:
            return

    def _draw_quick_setup(self, layout, context) -> None:
        """Status summary only — Health Check / tabs live in the footer and tab bar."""
        issues = self._quick_setup_issues()
        roots = self._library_roots_list()
        ready = bool(roots) and not issues
        if ready:
            header_text = "Setup complete"
        elif not roots:
            header_text = "Setup — choose a library folder"
        elif len(issues) == 1:
            header_text = f"Setup — {issues[0]}"
        else:
            header_text = f"Setup — {len(issues)} issues need attention"

        box = layout.box()
        if ready:
            header = box.row(align=True)
            header.prop(
                self,
                "show_quick_setup",
                text=header_text,
                icon="TRIA_DOWN" if self.show_quick_setup else "TRIA_RIGHT",
                emboss=False,
            )
            if not self.show_quick_setup:
                tip = box.row(align=True)
                tip.scale_y = 0.85
                n = len(roots)
                tip.label(
                    text=f"{n} library folder{'s' if n != 1 else ''} · Library tab",
                    icon="CHECKMARK",
                )
                return
        else:
            header = box.row(align=True)
            header.label(text=header_text, icon="ERROR")

        col = box.column(align=True)
        col.scale_y = 0.9
        if len(roots) > 1:
            col.label(text=f"Library: {len(roots)} folders (see Library tab)", icon="FILE_FOLDER")
        else:
            root_label = roots[0] if roots else "(set a library folder on the Library tab)"
            col.label(text=f"Library: {root_label}", icon="FILE_FOLDER")
        for warn in path_warnings_for_prefs(self):
            col.label(text=warn, icon="ERROR")
        col.label(text=f"Cache: {self.cache_dir or '(auto)'}", icon="TEMP")
        online_ok = bool(getattr(bpy.app, "online_access", True))
        col.label(
            text="Online access: on"
            if online_ok
            else "Turn on Allow Online Access in Blender Preferences → System",
            icon="WORLD" if online_ok else "ERROR",
        )
        try:
            from . import online_health

            state = online_health.get_cached_state()
            summary = state.get("summary") or ""
            if state.get("checking"):
                col.label(text="Sites: checking…", icon="TIME")
            elif summary:
                col.label(text=f"Sites: {summary}", icon="WORLD")
        except Exception:
            pass
        hint = box.column(align=True)
        hint.scale_y = 0.85
        hint.label(text="Edit folders on the Library tab. Run Health Check at the bottom.")

    def _draw_platform_status(self, layout) -> None:
        """Collapsible per-platform indicator strip (Houdini health parity)."""
        from . import online_health

        box = layout.box()
        header = box.row(align=True)
        header.prop(
            self,
            "show_online_diagnostics",
            text="Platform diagnostics",
            icon="TRIA_DOWN" if self.show_online_diagnostics else "TRIA_RIGHT",
            emboss=False,
        )
        if not self.show_online_diagnostics:
            try:
                state = online_health.get_cached_state()
                summary = state.get("summary") or ""
                tip = box.row(align=True)
                tip.scale_y = 0.85
                if state.get("checking"):
                    tip.label(text="Checking…", icon="TIME")
                elif summary:
                    tip.label(text=summary, icon="INFO")
                else:
                    tip.label(text="Use Check online sources at the bottom to test APIs", icon="INFO")
            except Exception:
                pass
            return

        tip = box.column(align=True)
        tip.scale_y = 0.85
        tip.label(text="Online / Offline / Need sign-in. Health Check also runs this.")

        try:
            cfg = prefs_to_config()
            enabled = enabled_source_ids(self)
            rows = online_health.rows_for_preferences_draw(cfg, enabled_sources=enabled)
            state = online_health.get_cached_state()
        except Exception:
            rows = []
            state = {}

        col = box.column(align=True)
        if not rows:
            col.label(text="No sources", icon="INFO")
            return
        for row_data in rows:
            sid = str(row_data.get("source_id") or "")
            label = str(row_data.get("label") or SOURCE_LABELS.get(sid, sid))
            status = str(row_data.get("status") or "unknown")
            detail = str(row_data.get("detail") or "")
            icon = online_health.blender_icon_for_status(status)
            status_word = {
                "ok": "Online",
                "fail": "Offline",
                "warn": "Warning",
                "need_auth": "Need sign-in",
                "disabled": "Off",
                "checking": "Checking…",
                "unknown": "Not checked",
            }.get(status, status)
            line = f"{label} — {status_word}"
            if detail and status in ("need_auth", "fail", "warn", "ok") and len(detail) < 40:
                if status != "ok" or "Epic" in detail or "Token" in detail or "API" in detail:
                    line = f"{label} — {status_word}: {detail}"
            r = col.row(align=True)
            r.label(text=line, icon=icon)

        summary = state.get("summary") or online_health.summarize_rows(rows)
        foot = box.row(align=True)
        foot.scale_y = 0.9
        if state.get("checking"):
            foot.label(text="Checking all enabled platforms…", icon="TIME")
        elif state.get("checked_at"):
            foot.label(text=f"Last check: {summary}", icon="INFO")
        else:
            foot.label(text="Use Check online sources at the bottom to test APIs", icon="INFO")

    def _draw_cache_clear_controls(self, parent) -> None:
        """Primary Clear temporary… + expandable per-mode buttons."""
        clear = parent.box()
        clear.label(text="Clear temporary cache", icon="TRASH")
        tip = clear.column(align=True)
        tip.scale_y = 0.85
        tip.label(text="Never deletes library roots or Online Downloads/. Keeps assets.db.")
        try:
            from . import cache_maintenance as cm

            usage = cm.cache_usage_summary(self.cache_dir or "")
            tip.label(
                text="Thumbs {} · Staging {} · Index {}".format(
                    cm.format_bytes(int(usage.get("thumbs_bytes") or 0)),
                    cm.format_bytes(int(usage.get("staging_bytes") or 0)),
                    cm.format_bytes(int(usage.get("db_bytes") or 0)),
                )
            )
        except Exception:
            pass
        row = clear.row(align=True)
        op = row.operator("ual.clear_cache", text="Clear temporary…", icon="TRASH")
        op.mode = "REGENERABLE"
        clear.operator("ual.show_cache_usage", text="Show Cache Usage", icon="INFO")
        clear.prop(
            self,
            "show_cache_clear_details",
            text="Choose what to clear",
            icon="TRIA_DOWN" if self.show_cache_clear_details else "TRIA_RIGHT",
            emboss=False,
        )
        if not self.show_cache_clear_details:
            return
        row = clear.row(align=True)
        op = row.operator("ual.clear_cache", text="Memory", icon="TEMP")
        op.mode = "MEMORY"
        op = row.operator("ual.clear_cache", text="Online Thumbs", icon="IMAGE_DATA")
        op.mode = "ONLINE_THUMBS"
        op = row.operator("ual.clear_cache", text="All Thumbs", icon="FILE_IMAGE")
        op.mode = "ALL_THUMBS"
        row = clear.row(align=True)
        op = row.operator("ual.clear_cache", text="Catalogs / Enrich", icon="TEXT")
        op.mode = "CATALOGS"
        op = row.operator("ual.clear_cache", text="Staging…", icon="PACKAGE")
        op.mode = "STAGING"
        row = clear.row(align=True)
        op = row.operator("ual.clear_cache", text="All (Keep Index)…", icon="TRASH")
        op.mode = "ALL_KEEP_INDEX"

    def _draw_library(self, layout):
        box = layout.box()
        box.label(text="Library folders", icon="FILE_FOLDER")
        tip = box.column(align=True)
        tip.scale_y = 0.85
        tip.label(text="Permanent assets and Online Downloads/ live here (not the cache).")
        row = box.row()
        row.template_list(
            "UAL_UL_library_roots",
            "",
            self,
            "library_roots",
            self,
            "library_roots_index",
            rows=3,
        )
        col = row.column(align=True)
        col.operator("ual.prefs_add_library_root", text="", icon="ADD")
        col.operator("ual.prefs_remove_library_root", text="", icon="REMOVE")
        if not self.library_roots:
            box.prop(self, "library_root")
            box.label(text="Add folders with + or set the primary root above.", icon="INFO")
        for warn in path_warnings_for_prefs(self):
            if "library" in warn.lower() or "missing" in warn.lower():
                box.label(text=warn, icon="ERROR")

        cache = layout.box()
        cache.label(text="Cache (UAL CACHE)", icon="TEMP")
        tip = cache.column(align=True)
        tip.scale_y = 0.85
        tip.label(text="CDN staging, assets.db, and preview images. Keep separate from the library.")
        cache.prop(self, "cache_dir")
        for warn in path_warnings_for_prefs(self):
            if "cache" in warn.lower():
                cache.label(text=warn, icon="ERROR")
        self._draw_cache_clear_controls(cache)

        index = layout.box()
        index.label(text="Indexing", icon="OUTLINER")
        index.prop(self, "index_downloads")

    def _draw_online(self, layout):
        from . import online_links

        # --- Sources ---
        src = layout.box()
        src.label(text="Sources", icon="WORLD")
        tip = src.column(align=True)
        tip.scale_y = 0.85
        tip.label(text="Globe opens the site. Toggle shows the source in the Online scope.")
        fab_ok = self._fab_prefs_unlocked()
        public_ids = [sid for sid in self._visible_source_ids() if sid != "fab"]
        col = src.column(align=True)
        for sid in public_ids:
            self._draw_source_toggle_row(col, sid, online_links, fab_ok=fab_ok)

        show_fab_row = True
        try:
            from . import pro_features

            show_fab_row = not pro_features.is_community_package()
        except Exception:
            show_fab_row = True
        if show_fab_row:
            src.separator()
            try:
                from . import preferences_pro_ui

                preferences_pro_ui.draw_fab_source_section(
                    src, self, online_links, fab_ok=fab_ok
                )
            except ImportError:
                pass

        # --- Accounts ---
        keys = layout.box()
        keys.label(text="Accounts", icon="USER")
        tip = keys.column(align=True)
        tip.scale_y = 0.85
        tip.label(text="Connector tokens and Epic session are stored in:")
        tip.label(text="{cache}/config/online_secrets.json — UAL API key stays in settings.json.")
        tip.label(text="Leave a field empty to keep the saved value.")
        tip.label(text="Paste a new value to replace. Clearing the field does not erase disk.")
        keys.prop(self, "sketchfab_token")
        row = keys.row(align=True)
        sk_label, sk_url = online_links.auth_link("sketchfab")
        op = row.operator("ual.open_website", text=sk_label or "Get Sketchfab Token", icon="URL")
        op.link_id = "sketchfab"
        op.url = sk_url
        op = row.operator("ual.test_source_connection", text="Test", icon="CHECKMARK")
        op.source_id = "sketchfab"
        keys.prop(self, "polypizza_api_key")
        row = keys.row(align=True)
        pp_label, pp_url = online_links.auth_link("polypizza")
        op = row.operator("ual.open_website", text=pp_label or "Get Poly Pizza API Key", icon="URL")
        op.link_id = "polypizza"
        op.url = pp_url
        op = row.operator("ual.test_source_connection", text="Test", icon="CHECKMARK")
        op.source_id = "polypizza"

        try:
            from . import pro_features

            fab_ok = pro_features.fab_enabled()
            bar_ok = pro_features.asset_bar_enabled()
        except Exception:
            fab_ok = False
            bar_ok = False

        if fab_ok:
            fab = keys.box()
            signed_in = False
            status_text = "Epic account: Not Signed In"
            try:
                from . import epic_oauth

                cfg_probe = load_active_config()
                if epic_oauth.epic_session_configured(cfg_probe):
                    session = epic_oauth.epic_session_from_config(cfg_probe)
                    label = session.display_name or "Epic user"
                    status_text = f"Epic account: Signed In as {label}"
                    signed_in = True
                elif (self.fab_sessionid or "").strip():
                    status_text = "Epic account: Not Signed In (cookie backup set)"
            except Exception:
                pass
            fab.label(
                text="Fab / Megascans (Epic)",
                icon="USER" if signed_in else "LOCKED",
            )
            fab.label(text=status_text, icon="CHECKMARK" if signed_in else "INFO")
            help_col = fab.column(align=True)
            help_col.scale_y = 0.85
            help_col.label(text="1. Sign In with Epic…  2. Paste browser JSON  3. OK  4. Save")
            help_col.label(text="Browse works without login. Downloads need Epic sign-in.")
            row = fab.row(align=True)
            row.operator("ual.epic_exchange_signin", text="Sign In with Epic…", icon="URL")
            row.operator("ual.epic_sign_out", text="Sign Out", icon="X")
            row = fab.row(align=True)
            fab_label, fab_url = online_links.auth_link("fab_site")
            op = row.operator("ual.open_website", text=fab_label or "Open Fab.com", icon="URL")
            op.link_id = "fab_site"
            op.url = fab_url
            op = row.operator("ual.test_source_connection", text="Test Fab", icon="CHECKMARK")
            op.source_id = "fab"
            fab.prop(
                self,
                "show_fab_cookie_fallback",
                text="Cookie backup (if Epic download fails)",
                icon="TRIA_DOWN" if self.show_fab_cookie_fallback else "TRIA_RIGHT",
                emboss=False,
            )
            if self.show_fab_cookie_fallback:
                cookies = fab.box()
                c_help = cookies.column(align=True)
                c_help.scale_y = 0.85
                c_help.label(text="Cookie-Editor on fab.com: fab_sessionid and fab_csrftoken")
                cookies.prop(self, "fab_sessionid")
                cookies.prop(self, "fab_csrftoken")

        # --- Downloads ---
        pipe = layout.box()
        pipe.label(text="Downloads", icon="PACKAGE")
        share = pipe.column(align=True)
        share.scale_y = 0.85
        share.label(text="Share the library folder with Houdini (Online Downloads/). Never share cache.")
        share.label(
            text="Blender: {} · Houdini: UAL CACHE HOUDINI".format(paths.PRODUCT_CACHE_BASENAME)
        )
        share.label(text="Keep Copy to library ON. After Houdini saves, run Rebuild Index.")
        pipe.prop(self, "copy_to_library")
        keep = pipe.row()
        keep.enabled = bool(self.copy_to_library)
        keep.prop(self, "keep_cache")
        if not bool(self.copy_to_library):
            warn = pipe.column(align=True)
            warn.alert = True
            warn.label(text="Copy OFF: downloads stay in cache only (not visible in Houdini).", icon="ERROR")
        pipe.prop(self, "default_download_resolution")
        res_help = pipe.column(align=True)
        res_help.scale_y = 0.85
        if fab_ok:
            res_help.label(text="Applies to: Poly Haven, ambientCG, GPUOpen, LazyTextures, PBRPX, Fab.")
            res_help.label(text="No effect: Sketchfab, Poly Pizza, Three D Scans.")
            res_help.label(text="Fab: 2K→Mid · 4K→High · 8K/Original→Raw.")
        else:
            res_help.label(text="Applies to: Poly Haven, ambientCG, GPUOpen, LazyTextures, PBRPX.")
            res_help.label(text="No effect: Sketchfab, Poly Pizza, Three D Scans.")
        if bar_ok:
            pipe.prop(self, "auto_open_asset_bar_on_search")
            pipe.prop(self, "hide_asset_bar_when_ual_sidebar")
        pipe.prop(
            self,
            "show_advanced_download",
            text="Advanced download options",
            icon="TRIA_DOWN" if self.show_advanced_download else "TRIA_RIGHT",
            emboss=False,
        )
        if self.show_advanced_download:
            adv = pipe.box()
            adv.prop(self, "auto_search_on_open")

        # --- Diagnostics ---
        self._draw_platform_status(layout)

    def _draw_previews(self, layout):
        box = layout.box()
        box.label(text="Grid tiles (default view)", icon="IMAGE_DATA")
        box.prop(self, "thumb_size")
        tip = box.column(align=True)
        tip.scale_y = 0.85
        tip.label(text="Asset names appear on the select bar under each tile.")

        hover = layout.box()
        hover.label(text="GPU hover preview", icon="HIDE_OFF")
        tip = hover.column(align=True)
        tip.scale_y = 0.85
        tip.label(text="Large preview for the selected asset in the sidebar.")
        try:
            from . import preferences_pro_ui

            preferences_pro_ui.draw_asset_bar_hover_tip(tip)
        except ImportError:
            pass
        tip.label(text="Pin: Ctrl+Shift+Space   Unpin: Esc")
        hover.prop(self, "hover_preview_enabled")
        if self.hover_preview_enabled:
            hover.prop(self, "hover_preview_size")
            hover.prop(self, "hover_preview_delay_ms")
            hover.prop(self, "hover_preview_pin_enabled")

    def _draw_import(self, layout):
        mats = layout.box()
        mats.label(text="Materials", icon="MATERIAL")
        mats.prop(self, "apply_material_to_selected")
        row = mats.row()
        row.enabled = bool(self.apply_material_to_selected)
        row.prop(self, "material_import_automap")
        blend = mats.box()
        blend.label(text="Material Blend defaults", icon="NODE_MATERIAL")
        blend.prop(self, "material_blend_max_distance")
        blend.prop(self, "material_blend_min_distance")
        blend.prop(self, "material_blend_live_noise_scale")
        blend.prop(self, "material_blend_live_noise_amount")
        blend.prop(self, "material_blend_blend_normals")
        tip = blend.column(align=True)
        tip.scale_y = 0.85
        tip.label(text="Defaults for new Material Blend setups. Blend Normals syncs to the sidebar.")

        place = layout.box()
        place.label(text="Placement & size", icon="EMPTY_ARROWS")
        place.prop(self, "place_at_world_origin")
        place.prop(self, "match_size_on_import")
        if self.match_size_on_import:
            place.prop(self, "match_size_target")

        outliner = layout.box()
        outliner.label(text="Outliner", icon="OUTLINER_COLLECTION")
        outliner.prop(self, "organize_into_collection")
        tip = outliner.column(align=True)
        tip.scale_y = 0.85
        tip.label(text="Collection name = asset name (e.g. Wood Floor).")

        try:
            from . import pro_features

            fab_ok = pro_features.fab_enabled()
        except Exception:
            fab_ok = False
        if fab_ok:
            fab = layout.box()
            fab.label(text="Fab / Megascans only", icon="MESH_DATA")
            fab.prop(self, "strip_megascans_billboard_lods")
            fab.prop(self, "fab_unit_scale_mode")
            tip = fab.column(align=True)
            tip.scale_y = 0.85
            tip.label(text="Never apply to Sketchfab, Poly Haven, or other sources.")

        layout.prop(
            self,
            "show_advanced_import",
            text="Advanced import options",
            icon="TRIA_DOWN" if self.show_advanced_import else "TRIA_RIGHT",
            emboss=False,
        )
        if self.show_advanced_import:
            adv = layout.box()
            adv.label(text="Naming & normals", icon="NORMALS_FACE")
            adv.prop(self, "texture_naming_profile")
            adv.prop(self, "normal_map_space")
            scale = adv.box()
            scale.label(text="Scale & pivot", icon="EMPTY_ARROWS")
            scale.prop(self, "normalize_scale")
            scale.prop(self, "unit_scale")
            if not self.place_at_world_origin:
                scale.prop(self, "pivot_to_base")


_FILTER_TYPE_ENUM_CACHE: list = []


def _filter_type_items(self, _context):
    """Type filter with native Blender icons (Mesh / Material / Texture / HDRI)."""
    global _FILTER_TYPE_ENUM_CACHE
    try:
        from .ui.icons import filter_type_enum_items

        _FILTER_TYPE_ENUM_CACHE = list(filter_type_enum_items())
    except Exception:
        _FILTER_TYPE_ENUM_CACHE = [
            ("ALL", "All", "All asset types", "FILTER", 0),
            ("mesh", "Mesh", "3D meshes / models", "MESH_DATA", 1),
            ("material_set", "Material", "PBR materials / shaders", "MATERIAL_DATA", 2),
            ("texture", "Texture", "Texture / map sets", "TEXTURE", 3),
            ("hdri", "HDRI", "Environment / world HDRIs", "WORLD_DATA", 4),
        ]
    return _FILTER_TYPE_ENUM_CACHE


def _online_source_items(self, context):
    """N-panel source enum — enabled prefs + Pro gates (Fab never on Community)."""
    try:
        prefs = _get_prefs()
    except Exception:
        prefs = None
    if prefs is None:
        ids = [sid for sid in SOURCE_IDS if sid != "fab"]
    else:
        ids = enabled_source_ids(prefs)
    items = [(sid, SOURCE_LABELS[sid], "") for sid in ids if sid in SOURCE_LABELS]
    if not items:
        items = [("polyhaven", "Poly Haven", "")]
    return items


# Keep dynamic enum tuples alive (Blender RNA GC quirk)
_ONLINE_DL_RES_ENUM_CACHE: list = []
_ONLINE_DL_FMT_ENUM_CACHE: list = []


def _online_dl_resolution_items(self, _context):
    from .download_options import build_resolution_enum_items

    global _ONLINE_DL_RES_ENUM_CACHE
    sid = str(getattr(self, "online_dl_options_source_id", "") or "")
    csv = str(getattr(self, "online_dl_res_items", "") or "")
    _ONLINE_DL_RES_ENUM_CACHE = build_resolution_enum_items(sid, csv)
    return _ONLINE_DL_RES_ENUM_CACHE


def _online_dl_format_items(self, _context):
    from .download_options import build_format_enum_items

    global _ONLINE_DL_FMT_ENUM_CACHE
    csv = str(getattr(self, "online_dl_fmt_items", "") or "")
    sid = str(getattr(self, "online_dl_options_source_id", "") or "")
    _ONLINE_DL_FMT_ENUM_CACHE = build_format_enum_items(csv, source_id=sid)
    return _ONLINE_DL_FMT_ENUM_CACHE


def _refresh_online_in_library_flags(context=None) -> None:
    """Recompute Online tile ``in_library`` after Size/Format or disk changes."""
    try:
        from . import online_library_presence
        from .ui import selection_overlay

        ui = get_wm_ui(context) if context is not None else get_wm_ui()
        if ui is None or not getattr(ui, "online_results", None):
            return
        online_library_presence.refresh_online_result_in_library_flags(ui)
        selection_overlay.tag_ui_redraw(context)
        try:
            from .ui import library_presence_overlay

            library_presence_overlay.tag_redraw(context)
        except Exception:
            pass
    except Exception:
        pass


def _on_online_dl_resolution_changed(self, context) -> None:
    """Size change affects reuse / in-library indicators."""
    try:
        from .download_options import is_seeding_download_options

        if is_seeding_download_options():
            return
    except Exception:
        pass
    _refresh_online_in_library_flags(context)


def _on_online_dl_format_changed(self, context) -> None:
    """Format change may require Fab/texture resolution re-enrich + in-library refresh."""
    try:
        from .download_options import is_seeding_download_options

        if is_seeding_download_options():
            return
        from . import online_dl_options

        online_dl_options.sync_download_options_for_selection(self)
    except Exception:
        pass
    _refresh_online_in_library_flags(context)


def _on_selection_changed(self, context) -> None:
    # Hover is mouse-over driven — never open the card from selection RNA.
    # Persist last selection per scope (index + stable key) for tab restore.
    try:
        from . import selection_keys

        scope = getattr(self, "scope", "library") or "library"
        if scope == "online":
            idx = int(getattr(self, "online_selected_index", 0) or 0)
            self.last_online_selected_index = idx
            results = getattr(self, "online_results", None)
            if results and 0 <= idx < len(results):
                key = selection_keys.key_for_online_item(results[idx])
                self.online_selected_key = key
                self.last_online_selected_key = key
            try:
                from . import online_dl_options

                online_dl_options.sync_download_options_for_selection(self)
            except Exception:
                pass
        elif scope == "favorites":
            idx = int(getattr(self, "selected_index", 0) or 0)
            self.last_favorites_selected_index = idx
            assets = getattr(self, "assets", None)
            if assets and 0 <= idx < len(assets):
                key = selection_keys.key_for_library_item(assets[idx])
                self.selected_key = key
                self.last_favorites_selected_key = key
        else:
            idx = int(getattr(self, "selected_index", 0) or 0)
            self.last_library_selected_index = idx
            assets = getattr(self, "assets", None)
            if assets and 0 <= idx < len(assets):
                key = selection_keys.key_for_library_item(assets[idx])
                self.selected_key = key
                self.last_library_selected_key = key
    except Exception:
        pass
    # Refresh N-panel so UILayout depress / box selection updates
    try:
        from .ui import selection_overlay

        selection_overlay.tag_ui_redraw(context)
    except Exception:
        pass


def _persist_view_modes_from_wm(context) -> None:
    """Write Grid/List modes from WindowManager → settings.json."""
    try:
        ui = get_wm_ui(context)
        if ui is None:
            return
        cfg = prefs_to_config()
        cfg.setdefault("ui", {})["view_mode"] = ui.view_mode or "grid"
        cfg.setdefault("online_ui", {})["view_mode"] = ui.online_view_mode or "grid"
        cfg.setdefault("ui", {})["last_scope"] = ui.scope or "library"
        if ui.scope == "online":
            cfg.setdefault("ui", {})["last_source"] = ui.online_source or "polyhaven"
        ual_config.save_config(cfg, cfg["cache_dir"])
    except Exception:
        pass


def _on_view_mode_changed(self, context) -> None:
    _persist_view_modes_from_wm(context)
    # Drop grid/list hit cache — modes use different click owners + cell sizes
    try:
        from . import interaction_cancel
        from .ui import operators as ops

        interaction_cancel.cancel_all_interactions(
            context, reason="view_mode", close_asset_bar=False
        )
        ops.reset_assets_view_scroll(context)
    except Exception:
        pass


def _on_scope_changed(self, context) -> None:
    """Refresh library/favorites lists when scope changes; optional online auto-search."""
    try:
        from . import interaction_cancel
        from .ui import operators as ops

        interaction_cancel.cancel_all_interactions(
            context, reason="scope", close_asset_bar=False
        )
        ops.reset_assets_view_scroll(context)
    except Exception:
        pass
    _persist_view_modes_from_wm(context)
    try:
        from .ui import operators as ops

        if self.scope in ("library", "favorites"):
            from . import selection_keys

            if self.scope == "favorites":
                self.selected_key = str(
                    getattr(self, "last_favorites_selected_key", "") or ""
                )
            else:
                self.selected_key = str(
                    getattr(self, "last_library_selected_key", "") or ""
                )
            ops._fill_library_list(context)
            # If key was empty, fall back to last index
            if not str(getattr(self, "selected_key", "") or "") and len(self.assets):
                if self.scope == "favorites":
                    want = int(getattr(self, "last_favorites_selected_index", 0) or 0)
                else:
                    want = int(getattr(self, "last_library_selected_index", 0) or 0)
                selection_keys.apply_library_index(self, want)
        elif self.scope == "online":
            from . import selection_keys

            self.online_selected_key = str(
                getattr(self, "last_online_selected_key", "") or ""
            )
            if len(self.online_results):
                if self.online_selected_key:
                    selection_keys.sync_online_selection(self)
                else:
                    want = int(getattr(self, "last_online_selected_index", 0) or 0)
                    selection_keys.apply_online_index(self, want)
            prefs = _get_prefs()
            from .ui import operators as ops

            ops.arm_online_auto_search()
            empty = not len(getattr(self, "online_results", []) or [])
            always = bool(getattr(prefs, "auto_search_on_open", False))
            if empty or always:
                queued = ops.schedule_online_search(context, ui=self, force=True)
                if queued and not bool(getattr(self, "download_active", False)):
                    self.status_message = f"Searching {self.online_source}…"
            elif (
                not bool(getattr(self, "online_did_search", False))
                and not empty
                and not bool(getattr(self, "download_active", False))
            ):
                self.status_message = "Press Enter to Search"
    except Exception:
        pass


def _on_library_filter_changed(self, context) -> None:
    """Refresh Library/Favorites; re-run Online search so Type filter applies."""
    try:
        from . import interaction_cancel
        from .ui import operators as ops

        interaction_cancel.cancel_all_interactions(
            context, reason="filter", close_asset_bar=False
        )
        ops.reset_assets_view_scroll(context)
        if self.scope in ("library", "favorites"):
            ops._fill_library_list(context)
        elif self.scope == "online":
            ops.arm_online_auto_search()
            queued = ops.schedule_online_search(context, ui=self, force=True)
            if queued and not bool(getattr(self, "download_active", False)):
                self.status_message = f"Searching {self.online_source}…"
    except Exception:
        pass


def _on_search_query_changed(self, context) -> None:
    """Live-filter Library/Favorites as the user types (Houdini filter-bar parity).

    Online uses ``online_query`` (no TEXTEDIT_UPDATE) so Enter confirms search.
    """
    if self.scope not in ("library", "favorites"):
        return
    try:
        from .ui import operators as ops

        ops.schedule_library_list_fill(context)
    except Exception:
        pass


def _on_online_query_confirmed(self, context) -> None:
    """Enter or click-away on the Online search field — not per-keystroke."""
    if str(getattr(self, "scope", "") or "") != "online":
        return
    try:
        from .ui import operators as ops

        ops.schedule_online_search(context, ui=self, force=True)
    except Exception:
        pass


def _on_online_source_changed(self, context) -> None:
    """Clear stale results when the Online source changes (prevents cross-source mix).

    Also bumps ``online_search_gen`` so in-flight Search/Load More callbacks for the
    previous source are discarded (otherwise Three D Scans results can reappear after
    switching to Poly Haven). After enable, re-searches the new source (Type + query).
    """
    try:
        from . import interaction_cancel
        from .ui import operators as ops

        interaction_cancel.cancel_all_interactions(
            context, reason="online_source", close_asset_bar=False
        )
        try:
            self.online_search_gen = int(getattr(self, "online_search_gen", 0) or 0) + 1
        except Exception:
            pass
        self.online_results.clear()
        self.online_selected_index = 0
        self.online_selected_key = ""
        self.last_online_selected_key = ""
        self.online_page = 1
        self.online_has_more = False
        self.online_total_count = 0
        self.online_did_search = False
        try:
            self.online_search_fp = ""
        except Exception:
            pass
        try:
            self.online_dl_options_ready = False
            self.online_dl_res_items = ""
            self.online_dl_fmt_items = ""
            self.online_dl_bound_asset_id = ""
            self.online_dl_options_source_id = ""
            self.online_dl_options_asset_id = ""
        except Exception:
            pass
        # Disk thumbs + in-memory search cache for each source are kept (warm second Search)
        ops.arm_online_auto_search()
        queued = ops.schedule_online_search(context, ui=self, force=True)
        # Only nudge Online status when not downloading and scope is Online
        # (source RNA can fire during Library restore on enable).
        if str(getattr(self, "scope", "") or "") == "online" and not bool(
            getattr(self, "download_active", False)
        ):
            if queued:
                self.status_message = f"Searching {self.online_source}…"
            elif not queued:
                self.status_message = f"Source: {self.online_source} — press Enter or Search"
        ops.reset_assets_view_scroll(context)
    except Exception:
        pass
    _persist_view_modes_from_wm(context)


class UAL_PG_ui_v5(PropertyGroup):
    """WindowManager session UI state (RNA id bumped when props are added)."""

    scope: EnumProperty(
        name="Scope",
        items=(
            ("library", "My Library", "Local indexed assets", "FILE_FOLDER", 0),
            ("favorites", "Favorites", "Favorited paths", "SOLO_ON", 1),
            ("online", "Online", "Online sources", "WORLD", 2),
        ),
        default="library",
        update=_on_scope_changed,
    )
    search_query: StringProperty(
        name="Search",
        default="",
        options={"TEXTEDIT_UPDATE"},
        update=_on_search_query_changed,
        description="Library/Favorites: live filter as you type",
    )
    online_query: StringProperty(
        name="Online Search",
        default="",
        update=_on_online_query_confirmed,
        description="Type a name, then press Enter or the Search icon. Not live — avoids a request per key",
    )
    filter_type: EnumProperty(
        name="Type",
        items=_FILTER_TYPE_ITEMS_STATIC,
        default="ALL",
        update=_on_library_filter_changed,
        description="Filter by asset type (Library live; Online re-searches immediately)",
    )
    online_source: EnumProperty(
        name="Source",
        items=_online_source_items,
        update=_on_online_source_changed,
    )
    view_mode: EnumProperty(
        name="View",
        description="Grid or list — Grid is the default for Library and Online",
        items=(
            ("grid", "Grid", "Square thumbnail tiles"),
            ("list", "List", "Compact rows with thumbs"),
        ),
        default="grid",
        update=_on_view_mode_changed,
    )
    online_view_mode: EnumProperty(
        name="Online View",
        description="Grid or list — Grid is the default",
        items=(
            ("grid", "Grid", "Square thumbnail tiles"),
            ("list", "List", "Compact rows with thumbs"),
        ),
        default="grid",
        update=_on_view_mode_changed,
    )
    status_message: StringProperty(name="Status", default="Ready")
    status_progress: IntProperty(name="Progress", default=-1, min=-1, max=100)
    status_progress_display: FloatProperty(
        name="Progress Display",
        default=0.0,
        min=0.0,
        max=1.0,
        options={"HIDDEN"},
        description="Smoothed 0–1 factor for sidebar progress bars",
    )
    download_active: BoolProperty(name="Download Active", default=False)
    download_task_id: StringProperty(name="Download Task", default="")
    download_label: StringProperty(name="Download Label", default="")
    selected_index: IntProperty(name="Selected", default=0, update=_on_selection_changed)
    selected_key: StringProperty(
        name="Selected Key",
        default="",
        options={"HIDDEN"},
        description="Stable library selection key (lib:normalized_path)",
    )
    assets: CollectionProperty(type=UAL_AssetItem)
    online_results: CollectionProperty(type=UAL_OnlineResultItem)
    online_selected_index: IntProperty(name="Online Selected", default=0, update=_on_selection_changed)
    online_selected_key: StringProperty(
        name="Online Selected Key",
        default="",
        options={"HIDDEN"},
        description="Stable online selection key (online:source_id:asset_id)",
    )
    online_page: IntProperty(name="Online Page", default=1, min=1, options={"HIDDEN"})
    online_has_more: BoolProperty(name="Online Has More", default=False, options={"HIDDEN"})
    online_total_count: IntProperty(name="Online Total", default=0, min=0, options={"HIDDEN"})
    online_did_search: BoolProperty(name="Online Did Search", default=False, options={"HIDDEN"})
    online_search_fp: StringProperty(
        name="Online Search Fingerprint",
        default="",
        options={"HIDDEN"},
        description="source|query|type|page of the grid currently shown",
    )
    # Bumped on source switch / new Search — stale bg callbacks must match or discard
    online_search_gen: IntProperty(name="Online Search Gen", default=0, options={"HIDDEN"})
    # Per-asset download override (session only — not written to settings.json)
    online_dl_res_items: StringProperty(name="Res Items Cache", default="", options={"HIDDEN"})
    online_dl_fmt_items: StringProperty(name="Fmt Items Cache", default="", options={"HIDDEN"})
    online_dl_options_source_id: StringProperty(name="DL Source", default="", options={"HIDDEN"})
    online_dl_options_asset_id: StringProperty(name="DL Asset", default="", options={"HIDDEN"})
    online_dl_bound_asset_id: StringProperty(name="DL Bound", default="", options={"HIDDEN"})
    online_dl_options_ready: BoolProperty(name="DL Options Ready", default=False, options={"HIDDEN"})
    online_dl_resolution: EnumProperty(
        name="Resolution",
        description=(
            "Per-asset size/quality override. Default uses Preferences → "
            "Default Max Texture Quality. Hidden for Sketchfab / Poly Pizza / Three D Scans"
        ),
        items=_online_dl_resolution_items,
        update=_on_online_dl_resolution_changed,
    )
    online_dl_format: EnumProperty(
        name="Format",
        description="Per-asset file format override (Auto = first listing format)",
        items=_online_dl_format_items,
        update=_on_online_dl_format_changed,
    )
    # In-panel Assets viewport offset (row-aligned). Not persisted — Blender has no nested scroll.
    assets_view_scroll: IntProperty(
        name="Assets View Scroll",
        default=0,
        min=0,
        options={"HIDDEN"},
        description="First visible asset index in the height-capped Assets viewport",
    )
    # Session cache for scope tab restore (not persisted to settings.json)
    last_library_selected_index: IntProperty(name="Last Library Sel", default=0, options={"HIDDEN"})
    last_favorites_selected_index: IntProperty(name="Last Favorites Sel", default=0, options={"HIDDEN"})
    last_online_selected_index: IntProperty(name="Last Online Sel", default=0, options={"HIDDEN"})
    last_library_selected_key: StringProperty(name="Last Library Key", default="", options={"HIDDEN"})
    last_favorites_selected_key: StringProperty(name="Last Favorites Key", default="", options={"HIDDEN"})
    last_online_selected_key: StringProperty(name="Last Online Key", default="", options={"HIDDEN"})


# Back-compat aliases — RNA identifier is UAL_PG_ui_v5.
UAL_UIProps = UAL_PG_ui_v5
UAL_PG_ui_v2 = UAL_PG_ui_v5
UAL_PG_ui_v3 = UAL_PG_ui_v5
UAL_PG_ui_v4 = UAL_PG_ui_v5


def _del_wm_ui_attrs() -> None:
    """Remove current + legacy WindowManager UI pointers (reload-safe)."""
    for name in (WM_UI_ATTR,) + tuple(_LEGACY_WM_UI_ATTRS):
        if not hasattr(bpy.types.WindowManager, name):
            continue
        try:
            delattr(bpy.types.WindowManager, name)
        except Exception:
            try:
                del bpy.types.WindowManager.__dict__[name]  # type: ignore[attr-defined]
            except Exception:
                pass


def _ui_is_valid(ui) -> bool:
    """False for None or incomplete RNA groups missing session collections."""
    if ui is None:
        return False
    for prop in _REQUIRED_UI_PROP_NAMES:
        try:
            if not hasattr(ui, prop):
                return False
            if prop in ("assets", "online_results"):
                len(getattr(ui, prop))
        except Exception:
            return False
    return True


def _bind_wm_ui_pointer() -> bool:
    """Attach ``WindowManager.ual_pg5`` to a fully registered PropertyGroup.

    Returns True only when the live instance exposes ``online_results``.
    """
    from . import register_utils

    # Item types must exist before CollectionProperty host registers.
    for cls in (UAL_AssetItem, UAL_OnlineResultItem, UAL_PG_ui_v5):
        register_utils.register_class_safe(cls)
    _del_wm_ui_attrs()
    setattr(
        bpy.types.WindowManager,
        WM_UI_ATTR,
        bpy.props.PointerProperty(type=UAL_PG_ui_v5),
    )
    try:
        ui = getattr(bpy.context.window_manager, WM_UI_ATTR, None)
    except Exception:
        ui = None
    ok = _ui_is_valid(ui)
    if not ok:
        try:
            props = []
            if ui is not None:
                props = [
                    p.identifier
                    for p in ui.bl_rna.properties
                    if p.identifier not in ("rna_type", "name")
                ]
            print(
                "UAL: WM UI bind incomplete — props={} (expected online_results). "
                "Disable/Enable the addon.".format(props)
            )
        except Exception:
            pass
    return ok


def get_wm_ui(context=None):
    """Return a *valid* session UI PropertyGroup, or None.

    Never returns an incomplete RNA instance. Attempts one in-session rebind
    if the current pointer is invalid (F8 / partial register recovery).
    """
    if context is None:
        context = bpy.context
    wm = context.window_manager
    ui = getattr(wm, WM_UI_ATTR, None)
    if _ui_is_valid(ui):
        return ui
    for legacy in _LEGACY_WM_UI_ATTRS:
        legacy_ui = getattr(wm, legacy, None)
        if _ui_is_valid(legacy_ui):
            return legacy_ui
    try:
        if _bind_wm_ui_pointer():
            ui = getattr(wm, WM_UI_ATTR, None)
            if _ui_is_valid(ui):
                return ui
    except Exception as exc:
        try:
            print("UAL: failed to rebind WM UI pointer:", exc)
        except Exception:
            pass
    return None


def enabled_source_ids(prefs=None) -> list:
    prefs = prefs or _get_prefs()
    ids = [sid for sid in SOURCE_IDS if bool(getattr(prefs, f"enable_{sid}", True))]
    try:
        from . import pro_features

        if not pro_features.fab_enabled():
            ids = [sid for sid in ids if sid != "fab"]
    except Exception:
        ids = [sid for sid in ids if sid != "fab"]
    return ids


def path_warnings_for_prefs(prefs) -> list:
    """Inline path warnings for Preferences (missing roots / cache↔library collide)."""
    roots = [item.path for item in prefs.library_roots if (item.path or "").strip()]
    return paths.path_warnings(roots, prefs.cache_dir or "", library_root=prefs.library_root or "")


# Re-export for operators / callers that historically imported from preferences
RESTORE_PRESERVE_PREF_KEYS = ual_config.RESTORE_PRESERVE_PREF_KEYS


def prefs_to_config() -> dict:
    prefs = _get_prefs()
    roots = library_roots_from_prefs(prefs)
    repaired = paths.repair_cache_dir(prefs.cache_dir or "", roots)
    # Copy the generation-cached settings.json, then overlay live RNA.
    # Do not mutate ``load_active_config()`` in place — that dict is the cache.
    # ``save_prefs_config`` writes then stores this overlay and bumps
    # ``save_generation``, so the next ``load_active_config`` hit is not stale.
    cfg = copy.deepcopy(load_active_config())
    # Migrate favorites / Epic / online secrets if they still live under a colliding raw path
    raw = (prefs.cache_dir or "").strip()
    if raw and os.path.normcase(os.path.normpath(raw)) != os.path.normcase(
        os.path.normpath(repaired)
    ):
        try:
            from . import online_auth as _online_auth

            prior = ual_config.load_config(raw)
            if prior.get("favorites") and not cfg.get("favorites"):
                cfg["favorites"] = list(prior.get("favorites") or [])
            if isinstance(prior.get("epic_auth"), dict) and not cfg.get("epic_auth"):
                cfg["epic_auth"] = dict(prior.get("epic_auth") or {})
            prior_online = prior.get("online") if isinstance(prior.get("online"), dict) else {}
            cfg_online = cfg.setdefault("online", {})
            cfg["online"] = _online_auth.apply_secret_merge(cfg_online, prior_online)
            prior_account = prior.get("account") if isinstance(prior.get("account"), dict) else {}
            account = cfg.setdefault("account", {})
            if not str(account.get("addon_key") or "").strip():
                disk_key = str(prior_account.get("addon_key") or "").strip()
                if disk_key:
                    account["addon_key"] = disk_key
            if not str(account.get("user_id") or "").strip():
                disk_user = str(prior_account.get("user_id") or "").strip()
                if disk_user:
                    account["user_id"] = disk_user
        except Exception:
            pass
    cfg["library_roots"] = roots
    cfg["cache_dir"] = repaired
    online = cfg.setdefault("online", {})
    # Empty RNA (PASSWORD UI, pre-hydrate, enable/disable) must not wipe disk.
    # Non-empty Preferences still win so users can replace tokens.
    from . import online_auth

    online = online_auth.apply_secret_merge(
        online,
        {
            "sketchfab_token": prefs.sketchfab_token or "",
            "polypizza_api_key": prefs.polypizza_api_key or "",
            "fab_sessionid": prefs.fab_sessionid or "",
            "fab_csrftoken": prefs.fab_csrftoken or "",
        },
    )
    cfg["online"] = online
    online["keep_cache"] = bool(prefs.keep_cache)
    online["copy_to_library"] = bool(prefs.copy_to_library)
    online["enabled_sources"] = enabled_source_ids(prefs)
    online["index_downloads"] = bool(getattr(prefs, "index_downloads", True))
    online["auto_search_on_open"] = bool(getattr(prefs, "auto_search_on_open", False))
    online["auto_open_asset_bar_on_search"] = bool(
        getattr(prefs, "auto_open_asset_bar_on_search", False)
    )
    online["default_download_resolution"] = getattr(prefs, "default_download_resolution", "2048") or "2048"
    imp = cfg.setdefault("import", {})
    imp["apply_material_to_selected"] = bool(prefs.apply_material_to_selected)
    imp["strip_megascans_billboard_lods"] = bool(prefs.strip_megascans_billboard_lods)
    imp["fab_unit_scale_mode"] = prefs.fab_unit_scale_mode or "cm_to_m"
    imp["normal_map_space"] = getattr(prefs, "normal_map_space", "OPENGL") or "OPENGL"
    imp["texture_naming_profile"] = getattr(prefs, "texture_naming_profile", "auto") or "auto"
    imp["normalize_scale"] = bool(getattr(prefs, "normalize_scale", False))
    imp["pivot_to_base"] = bool(getattr(prefs, "pivot_to_base", False))
    imp["place_at_world_origin"] = bool(getattr(prefs, "place_at_world_origin", True))
    imp["organize_into_collection"] = bool(
        getattr(prefs, "organize_into_collection", True)
    )
    imp["unit_scale"] = float(getattr(prefs, "unit_scale", 1.0) or 1.0)
    imp["match_size_on_import"] = bool(getattr(prefs, "match_size_on_import", False))
    imp["match_size_target"] = float(getattr(prefs, "match_size_target", 1.0) or 1.0)
    imp["material_import_automap"] = bool(getattr(prefs, "material_import_automap", True))
    imp["material_blend_max_distance"] = float(
        getattr(prefs, "material_blend_max_distance", 1.0) or 1.0
    )
    imp["material_blend_min_distance"] = float(
        getattr(prefs, "material_blend_min_distance", 1.0) or 1.0
    )
    imp["material_blend_live_noise_scale"] = float(
        getattr(prefs, "material_blend_live_noise_scale", 1.0) or 1.0
    )
    imp["material_blend_live_noise_amount"] = float(
        getattr(prefs, "material_blend_live_noise_amount", 1.0) or 1.0
    )
    imp["material_blend_blend_normals"] = bool(
        getattr(prefs, "material_blend_blend_normals", True)
    )
    ui = cfg.setdefault("ui", {})
    ui["thumb_size"] = int(getattr(prefs, "thumb_size", 112) or 112)
    ui.pop("show_names_in_grid", None)  # legacy — select bar already shows names
    ui["hover_preview_enabled"] = bool(getattr(prefs, "hover_preview_enabled", True))
    ui["hover_preview_size"] = int(getattr(prefs, "hover_preview_size", 320) or 320)
    ui["hover_preview_delay_ms"] = int(getattr(prefs, "hover_preview_delay_ms", 160) or 160)
    ui["hover_preview_pin_enabled"] = bool(getattr(prefs, "hover_preview_pin_enabled", True))
    ui["hide_asset_bar_when_ual_sidebar"] = bool(
        getattr(prefs, "hide_asset_bar_when_ual_sidebar", True)
    )
    ui.setdefault("onboarding_banner_dismissed", bool(ui.get("onboarding_banner_dismissed", False)))
    ui.setdefault("changelog_seen_version", str(ui.get("changelog_seen_version") or ""))
    releases = cfg.setdefault("releases", {})
    releases["api_base_url"] = (
        str(getattr(prefs, "releases_api_base_url", "") or "").strip().rstrip("/")
        or ual_config.DEFAULT_UPDATES_API_BASE
    )
    channel = str(getattr(prefs, "release_channel", "stable") or "stable").strip().lower()
    releases["channel"] = channel if channel in ("stable", "beta") else "stable"
    account = cfg.setdefault("account", {})
    # Same empty-RNA guard as online secrets (enable/disable / hydrate races).
    from . import online_auth

    pref_user = str(getattr(prefs, "account_user_id", "") or "").strip()
    pref_key = str(getattr(prefs, "account_addon_key", "") or "").strip()
    account["user_id"] = online_auth.merge_secret_value(pref_user, account.get("user_id"))
    account["addon_key"] = online_auth.merge_secret_value(pref_key, account.get("addon_key"))
    # Preserve / sync view modes from WM when available
    try:
        wm_ui = get_wm_ui()
        if wm_ui is not None:
            ui["view_mode"] = getattr(wm_ui, "view_mode", None) or ui.get("view_mode") or "grid"
            cfg.setdefault("online_ui", {})["view_mode"] = (
                getattr(wm_ui, "online_view_mode", None)
                or (cfg.get("online_ui") or {}).get("view_mode")
                or "grid"
            )
            ui["last_scope"] = getattr(wm_ui, "scope", None) or ui.get("last_scope") or "library"
    except Exception:
        ui.setdefault("view_mode", "grid")
        cfg.setdefault("online_ui", {}).setdefault("view_mode", "grid")
    return cfg


def ensure_storage_defaults() -> None:
    prefs = _get_prefs()
    if not prefs.library_roots and not (prefs.library_root or "").strip():
        prefs.library_root = paths.default_library_root()
    if not prefs.library_roots and (prefs.library_root or "").strip():
        item = prefs.library_roots.add()
        item.path = prefs.library_root
    roots = [item.path for item in prefs.library_roots if item.path] or [prefs.library_root]
    roots = [r for r in roots if r]
    repaired = paths.repair_cache_dir(prefs.cache_dir or "", roots or [paths.default_library_root()])
    prefs.cache_dir = repaired
    for root in roots:
        if root:
            paths.ensure_dir(root)
    paths.ensure_dir(prefs.cache_dir)
    paths.ensure_dir(os.path.join(prefs.cache_dir, "config"))
    # Hydrate tokens from disk BEFORE save — empty RNA must not wipe settings.json
    hydrate_prefs_from_config(prefs)
    hydrate_storage_prefs_from_config(prefs)
    # Re-repair after storage hydrate (disk may point at legacy UAL CACHE)
    roots = [item.path for item in prefs.library_roots if item.path] or [prefs.library_root]
    roots = [r for r in roots if r]
    prefs.cache_dir = paths.repair_cache_dir(
        prefs.cache_dir or "", roots or [paths.default_library_root()]
    )
    paths.ensure_dir(prefs.cache_dir)
    save_prefs_config()


_CLASSES = (
    UAL_LibraryRootItem,
    UAL_UL_library_roots,
    UAL_AssetItem,
    UAL_OnlineResultItem,
    UAL_AddonPreferences,
    UAL_PG_ui_v5,
)

# Legacy / current RNA ids — purge dependents first so PropertyGroups can detach.
_LEGACY_RNA_NAMES = (
    "UAL_UIProps",
    "UAL_PG_ui",
    "UAL_PG_ui_v2",
    "UAL_PG_ui_v3",
    "UAL_PG_ui_v5",
    "UAL_PG_ui_v4",
    "UAL_AddonPreferences",
    "UAL_UL_library_roots",
    "UAL_LibraryRootItem",
    "UAL_AssetItem",
    "UAL_OnlineResultItem",
    "UAL_PG_library_root",
    "UAL_PG_asset_item",
    "UAL_PG_online_result",
)


def register() -> None:
    from . import register_utils

    _del_wm_ui_attrs()
    register_utils.purge_named_types(_LEGACY_RNA_NAMES)
    register_utils.register_classes(_CLASSES)
    if not _bind_wm_ui_pointer():
        # Last-resort: purge broken host RNA and register host alone again.
        register_utils.purge_named_types(("UAL_PG_ui_v5", "UAL_PG_ui_v4", "UAL_PG_ui_v3", "UAL_PG_ui"))
        register_utils.register_class_safe(UAL_AssetItem)
        register_utils.register_class_safe(UAL_OnlineResultItem)
        register_utils.register_class_safe(UAL_PG_ui_v5)
        _bind_wm_ui_pointer()


def unregister() -> None:
    from . import register_utils

    try:
        if _prefs_save_pending:
            _flush_debounced_prefs_save()
        else:
            _cancel_debounced_prefs_save()
    except Exception:
        pass
    _del_wm_ui_attrs()
    register_utils.unregister_classes(_CLASSES)
    register_utils.purge_named_types(_LEGACY_RNA_NAMES)
