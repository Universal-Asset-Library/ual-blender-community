# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""Persistent settings JSON (survives addon disable; complements AddonPreferences)."""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Tuple

from . import paths

CONFIG_FILENAME = "settings.json"

# Production website origin — override in Settings if needed.
DEFAULT_UPDATES_API_BASE = "https://universalassetlibrary.com"

_PRODUCTION_UPDATES_HOSTS = frozenset(
    {
        "universalassetlibrary.com",
        "www.universalassetlibrary.com",
    }
)


def updates_api_url_warning(url: str) -> str:
    """Return a user-facing warning when Updates API URL looks unsafe or non-production."""
    from urllib.parse import urlparse

    text = str(url or "").strip().rstrip("/")
    if not text:
        return ""
    parsed = urlparse(text)
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").strip().lower()
    if host in ("localhost", "127.0.0.1", "::1"):
        return ""
    if scheme != "https":
        return "Warning: Updates API should use HTTPS. Plain HTTP can expose download metadata."
    if host not in _PRODUCTION_UPDATES_HOSTS:
        return (
            "Warning: non-production Updates API host ({0}). Use only for staging.".format(host or "?")
        )
    return ""

# Bumped after each successful atomic write so Preferences can skip re-parse.
_save_generation = 0
_load_config_calls = 0
_save_config_calls = 0


def save_generation() -> int:
    """Monotonic counter — increments when ``save_config`` finishes a write."""
    return int(_save_generation)


def load_config_call_count() -> int:
    """How many times ``load_config`` entered (disk / bak / default path)."""
    return int(_load_config_calls)


def save_config_call_count() -> int:
    """How many times ``save_config`` finished an atomic write."""
    return int(_save_config_calls)


def reset_config_io_counts() -> None:
    global _load_config_calls, _save_config_calls
    _load_config_calls = 0
    _save_config_calls = 0


# Keys Restore Defaults must never wipe (favorites live in settings.json)
RESTORE_PRESERVE_PREF_KEYS = (
    "sketchfab_token",
    "polypizza_api_key",
    "fab_sessionid",
    "fab_csrftoken",
    "cache_dir",
    "library_root",
)

DEFAULTS: Dict[str, Any] = {
    "library_roots": [],
    "cache_dir": "",
    "favorites": [],
    # Top-level Fab download auth (not under online.*) — written by Sign In with Epic
    "epic_auth": {
        "access_token": "",
        "refresh_token": "",
        "token_type": "bearer",
        "account_id": "",
        "display_name": "",
        "expires_at": 0.0,
    },
    "online": {
        "enabled_sources": [
            "polyhaven",
            "ambientcg",
            "gpuopen",
            "lazytextures",
            "threedscans",
            "pbrpx",
            "polypizza",
            "sketchfab",
            "fab",
        ],
        "sketchfab_token": "",
        "polypizza_api_key": "",
        "fab_sessionid": "",
        "fab_csrftoken": "",
        "keep_cache": False,
        "copy_to_library": True,
        "index_downloads": True,
        "auto_search_on_open": False,
        "auto_open_asset_bar_on_search": False,
        "default_download_resolution": "2048",
    },
    "ui": {
        "last_scope": "library",
        "last_source": "polyhaven",
        "search_query": "",
        "view_mode": "grid",
        "thumb_size": 112,
        "hover_preview_enabled": True,
        "hover_preview_size": 320,
        "hover_preview_delay_ms": 160,
        "hover_preview_pin_enabled": True,
        "onboarding_banner_dismissed": False,
        "changelog_seen_version": "",
        "hide_asset_bar_when_ual_sidebar": True,
    },
    "online_ui": {
        "view_mode": "grid",
    },
    "import": {
        "apply_material_to_selected": True,
        "material_import_automap": True,
        "material_blend_max_distance": 1.0,
        "material_blend_min_distance": 1.0,
        "material_blend_live_noise_scale": 1.0,
        "material_blend_live_noise_amount": 1.0,
        "material_blend_blend_normals": True,
        "strip_megascans_billboard_lods": True,
        "fab_unit_scale_mode": "cm_to_m",
        "normal_map_space": "OPENGL",
        "texture_naming_profile": "auto",
        "normalize_scale": False,
        "pivot_to_base": False,
        "place_at_world_origin": True,
        "organize_into_collection": True,
        "unit_scale": 1.0,
        "match_size_on_import": False,
        "match_size_target": 1.0,
    },
    "releases": {
        "api_base_url": DEFAULT_UPDATES_API_BASE,
        "channel": "stable",
    },
    "account": {
        "user_id": "",
        "addon_key": "",
    },
}


def _config_path(base_dir: Optional[str] = None) -> str:
    root = base_dir or paths.default_cache_dir()
    paths.ensure_dir(root)
    return os.path.join(root, "config", CONFIG_FILENAME)


def settings_json_path(base_dir: Optional[str] = None) -> str:
    """Absolute path to ``settings.json`` under the cache config folder."""
    return _config_path(base_dir)


def default_config() -> Dict[str, Any]:
    cfg = json.loads(json.dumps(DEFAULTS))
    root = paths.default_library_root()
    cfg["library_roots"] = [root]
    cfg["cache_dir"] = paths.default_cache_dir(root)
    return cfg


def coerce_config(raw: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cfg = default_config()
    if not raw:
        return cfg
    for key, value in raw.items():
        if key in cfg and isinstance(cfg[key], dict) and isinstance(value, dict):
            merged = dict(cfg[key])
            merged.update(value)
            cfg[key] = merged
        else:
            cfg[key] = value
    roots = [os.path.normpath(r) for r in (cfg.get("library_roots") or []) if r]
    if not roots:
        roots = [paths.default_library_root()]
    cfg["library_roots"] = roots
    cfg["cache_dir"] = paths.repair_cache_dir(str(cfg.get("cache_dir") or ""), roots)
    favs = cfg.get("favorites") or []
    cfg["favorites"] = [os.path.normpath(p) for p in favs if p]
    ui = cfg.setdefault("ui", {})
    mode = str(ui.get("view_mode") or "grid").lower()
    ui["view_mode"] = mode if mode in ("grid", "list") else "grid"
    try:
        ui["thumb_size"] = max(64, min(int(ui.get("thumb_size") or 112), 160))
    except (TypeError, ValueError):
        ui["thumb_size"] = 112
    ui["hover_preview_enabled"] = bool(ui.get("hover_preview_enabled", True))
    try:
        ui["hover_preview_size"] = max(160, min(int(ui.get("hover_preview_size") or 320), 512))
    except (TypeError, ValueError):
        ui["hover_preview_size"] = 320
    try:
        ui["hover_preview_delay_ms"] = max(120, min(int(ui.get("hover_preview_delay_ms") or 160), 800))
    except (TypeError, ValueError):
        ui["hover_preview_delay_ms"] = 160
    ui["hover_preview_pin_enabled"] = bool(ui.get("hover_preview_pin_enabled", True))
    ui["onboarding_banner_dismissed"] = bool(ui.get("onboarding_banner_dismissed", False))
    ui["changelog_seen_version"] = str(ui.get("changelog_seen_version") or "")
    ui.pop("show_names_in_grid", None)  # legacy — select bar already shows names
    ui["hide_asset_bar_when_ual_sidebar"] = bool(
        ui.get("hide_asset_bar_when_ual_sidebar", True)
    )
    online_ui = cfg.setdefault("online_ui", {})
    omode = str(online_ui.get("view_mode") or "grid").lower()
    online_ui["view_mode"] = omode if omode in ("grid", "list") else "grid"
    online = cfg.setdefault("online", {})
    online["copy_to_library"] = bool(online.get("copy_to_library", True))
    online["keep_cache"] = bool(online.get("keep_cache", False))
    if not online["copy_to_library"]:
        online["keep_cache"] = False
    online["index_downloads"] = bool(online.get("index_downloads", True))
    online["auto_search_on_open"] = bool(online.get("auto_search_on_open", False))
    online["auto_open_asset_bar_on_search"] = bool(online.get("auto_open_asset_bar_on_search", False))
    enabled = online.get("enabled_sources")
    if not isinstance(enabled, list) or not enabled:
        online["enabled_sources"] = list(DEFAULTS["online"]["enabled_sources"])
    else:
        online["enabled_sources"] = [str(s) for s in enabled if s]
    try:
        from . import package_flavor as flavor_mod

        if str(getattr(flavor_mod, "FLAVOR", "") or "").strip().lower() == "community":
            online["enabled_sources"] = [
                sid for sid in online["enabled_sources"] if str(sid) != "fab"
            ]
    except Exception:
        pass
    imp = cfg.setdefault("import", {})
    scale = str(imp.get("fab_unit_scale_mode") or "cm_to_m").lower()
    imp["fab_unit_scale_mode"] = scale if scale in ("auto", "off", "cm_to_m") else "cm_to_m"
    nspace = str(imp.get("normal_map_space") or "OPENGL").upper()
    imp["normal_map_space"] = nspace if nspace in ("OPENGL", "DIRECTX") else "OPENGL"
    profile = str(imp.get("texture_naming_profile") or "auto").lower()
    allowed_profiles = ("auto", "generic", "megascans", "ambientcg", "polyhaven")
    imp["texture_naming_profile"] = profile if profile in allowed_profiles else "auto"
    imp["strip_megascans_billboard_lods"] = bool(imp.get("strip_megascans_billboard_lods", True))
    imp["apply_material_to_selected"] = bool(imp.get("apply_material_to_selected", True))
    imp["material_import_automap"] = bool(imp.get("material_import_automap", True))
    try:
        imp["material_blend_max_distance"] = max(
            0.0, min(float(imp.get("material_blend_max_distance") or 1.0), 100.0)
        )
    except (TypeError, ValueError):
        imp["material_blend_max_distance"] = 1.0
    try:
        imp["material_blend_min_distance"] = max(
            0.0, min(float(imp.get("material_blend_min_distance") or 1.0), 100.0)
        )
    except (TypeError, ValueError):
        imp["material_blend_min_distance"] = 1.0
    # Drop legacy Sets GN live strength/spread keys if present
    imp.pop("material_blend_live_strength", None)
    imp.pop("material_blend_live_spread", None)
    try:
        imp["material_blend_live_noise_scale"] = max(
            0.0, float(imp.get("material_blend_live_noise_scale") or 1.0)
        )
    except (TypeError, ValueError):
        imp["material_blend_live_noise_scale"] = 1.0
    try:
        imp["material_blend_live_noise_amount"] = max(
            0.0, min(float(imp.get("material_blend_live_noise_amount") or 1.0), 2.0)
        )
    except (TypeError, ValueError):
        imp["material_blend_live_noise_amount"] = 1.0
    imp["material_blend_blend_normals"] = bool(imp.get("material_blend_blend_normals", True))
    # Drop legacy Transfer UV key if present
    imp.pop("material_blend_transfer_uv", None)
    imp["normalize_scale"] = bool(imp.get("normalize_scale", False))
    imp["pivot_to_base"] = bool(imp.get("pivot_to_base", False))
    imp["place_at_world_origin"] = bool(imp.get("place_at_world_origin", True))
    imp["organize_into_collection"] = bool(imp.get("organize_into_collection", True))
    imp["match_size_on_import"] = bool(imp.get("match_size_on_import", False))
    try:
        imp["unit_scale"] = max(0.001, min(float(imp.get("unit_scale") or 1.0), 1000.0))
    except (TypeError, ValueError):
        imp["unit_scale"] = 1.0
    try:
        imp["match_size_target"] = max(0.001, min(float(imp.get("match_size_target") or 1.0), 1000.0))
    except (TypeError, ValueError):
        imp["match_size_target"] = 1.0
    res = str(online.get("default_download_resolution") or "2048").strip()
    # Accept legacy numeric / k forms + ORIGINAL (highest available tier)
    if res.upper() == "ORIGINAL":
        online["default_download_resolution"] = "ORIGINAL"
    elif res.lower() in ("1k", "1024"):
        online["default_download_resolution"] = "1024"
    elif res.lower() in ("2k", "2048"):
        online["default_download_resolution"] = "2048"
    elif res.lower() in ("4k", "4096"):
        online["default_download_resolution"] = "4096"
    elif res.lower() in ("8k", "8192"):
        online["default_download_resolution"] = "8192"
    elif res in ("512", "1024", "2048", "4096", "8192", "auto") or res.isdigit():
        online["default_download_resolution"] = res
    else:
        online["default_download_resolution"] = "2048"
    releases = cfg.setdefault("releases", {})
    if not isinstance(releases, dict):
        releases = {}
        cfg["releases"] = releases
    releases["api_base_url"] = (
        str(releases.get("api_base_url") or "").strip().rstrip("/")
        or DEFAULT_UPDATES_API_BASE
    )
    channel = str(releases.get("channel") or "stable").strip().lower()
    releases["channel"] = channel if channel in ("stable", "beta") else "stable"
    account = cfg.setdefault("account", {})
    if not isinstance(account, dict):
        account = {}
        cfg["account"] = account
    account["user_id"] = str(account.get("user_id") or "").strip()
    account["addon_key"] = str(account.get("addon_key") or "").strip()
    account["installation_id"] = str(account.get("installation_id") or "").strip()
    if not isinstance(account.get("seat"), dict):
        account["seat"] = {}
    return cfg


def _apply_secrets_after_load(
    cfg: Dict[str, Any],
    base_dir: Optional[str],
) -> Tuple[Dict[str, Any], bool]:
    try:
        from .online_secrets import (
            apply_online_secrets,
            migrate_online_secrets_from_settings,
        )

        cfg, migrated = migrate_online_secrets_from_settings(cfg, base_dir)
        cfg = apply_online_secrets(cfg, base_dir)
        return cfg, migrated
    except Exception:
        try:
            from .online_secrets import apply_online_secrets

            return apply_online_secrets(cfg, base_dir), False
        except Exception:
            return cfg, False


def _finalize_loaded_config(cfg: Dict[str, Any], base_dir: Optional[str]) -> Dict[str, Any]:
    cfg, migrated = _apply_secrets_after_load(cfg, base_dir)
    if migrated:
        save_config(cfg, base_dir)
    return cfg


def load_config(base_dir: Optional[str] = None) -> Dict[str, Any]:
    global _load_config_calls
    _load_config_calls += 1
    path = _config_path(base_dir)
    if not os.path.isfile(path):
        bak = path + ".bak"
        if os.path.isfile(bak):
            try:
                with open(bak, "r", encoding="utf-8") as handle:
                    raw = json.load(handle)
                cfg = coerce_config(raw if isinstance(raw, dict) else None)
                save_config(cfg, base_dir)
                return _finalize_loaded_config(cfg, base_dir)
            except (OSError, json.JSONDecodeError):
                pass
        cfg = default_config()
        save_config(cfg, base_dir)
        return _finalize_loaded_config(cfg, base_dir)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, json.JSONDecodeError):
        bak = path + ".bak"
        if os.path.isfile(bak):
            try:
                with open(bak, "r", encoding="utf-8") as handle:
                    raw = json.load(handle)
                return _finalize_loaded_config(
                    coerce_config(raw if isinstance(raw, dict) else None),
                    base_dir,
                )
            except (OSError, json.JSONDecodeError):
                pass
        return _finalize_loaded_config(default_config(), base_dir)
    return _finalize_loaded_config(coerce_config(raw if isinstance(raw, dict) else None), base_dir)


def save_config(cfg: Dict[str, Any], base_dir: Optional[str] = None) -> None:
    """Atomically write settings.json (temp + fsync + replace) and keep ``.bak``."""
    cleaned = coerce_config(cfg)
    effective_dir = str(cleaned.get("cache_dir") or "").strip()
    if not effective_dir:
        effective_dir = str(base_dir or paths.default_cache_dir()).strip()
    path = _config_path(effective_dir)
    directory = os.path.dirname(path)
    paths.ensure_dir(directory)
    try:
        from .online_secrets import persist_secrets_from_config, strip_secrets_for_disk

        persist_secrets_from_config(cleaned, effective_dir)
        disk_cfg = strip_secrets_for_disk(cleaned)
    except Exception:
        disk_cfg = cleaned
    payload = json.dumps(disk_cfg, indent=2)
    tmp_path = path + ".tmp"
    bak_path = path + ".bak"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.write("\n")
        handle.flush()
        try:
            os.fsync(handle.fileno())
        except OSError:
            pass
    if os.path.isfile(path):
        try:
            if os.path.isfile(bak_path):
                os.remove(bak_path)
            os.replace(path, bak_path)
        except OSError:
            try:
                import shutil

                shutil.copy2(path, bak_path)
            except OSError:
                pass
    os.replace(tmp_path, path)
    global _save_generation, _save_config_calls
    _save_generation += 1
    _save_config_calls += 1


def norm_asset_path_key(path: str) -> str:
    return os.path.normcase(os.path.normpath(path or ""))


def toggle_favorite(cfg: Dict[str, Any], path: str) -> bool:
    """Toggle favorite; return True if now favorited. Preserves MRU order."""
    key = norm_asset_path_key(path)
    if not key:
        return False
    favs: List[str] = list(cfg.get("favorites") or [])
    keys = [norm_asset_path_key(p) for p in favs]
    if key in keys:
        cfg["favorites"] = [p for p in favs if norm_asset_path_key(p) != key]
        return False
    favs.insert(0, os.path.normpath(path))
    cfg["favorites"] = favs
    return True


def is_favorite(cfg: Dict[str, Any], path: str) -> bool:
    key = norm_asset_path_key(path)
    return key in {norm_asset_path_key(p) for p in (cfg.get("favorites") or [])}


def remove_path_list_references(cfg: Dict[str, Any], path: str) -> None:
    """Remove a path from favorites (and any future path lists)."""
    key = norm_asset_path_key(path)
    if not key:
        return
    favs = list(cfg.get("favorites") or [])
    cfg["favorites"] = [p for p in favs if norm_asset_path_key(p) != key]
    # Also drop favorites that lived *inside* a deleted package folder
    folder_key = key.rstrip("\\/")
    cfg["favorites"] = [
        p
        for p in (cfg.get("favorites") or [])
        if not (
            norm_asset_path_key(p) == folder_key
            or norm_asset_path_key(p).startswith(folder_key + os.sep)
            or norm_asset_path_key(p).startswith(folder_key + "/")
        )
    ]


def prune_stale_favorite_paths(
    cfg: Dict[str, Any],
    indexed_paths: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Drop favorite entries that no longer exist on disk or in the index."""
    favs = list(cfg.get("favorites") or [])
    if indexed_paths:
        indexed_keys = {norm_asset_path_key(path) for path in indexed_paths if path}
        if indexed_keys:
            pruned = [path for path in favs if norm_asset_path_key(path) in indexed_keys]
            if len(pruned) != len(favs):
                cfg = dict(cfg)
                cfg["favorites"] = pruned
            return cfg
    pruned = [
        path
        for path in favs
        if path and (os.path.isfile(path) or os.path.isdir(path))
    ]
    if len(pruned) != len(favs):
        cfg = dict(cfg)
        cfg["favorites"] = pruned
    return cfg
