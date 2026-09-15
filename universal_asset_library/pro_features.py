# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""Fail-closed Pro feature gates for Fab, asset bar, and cloud backup.

Community ZIPs omit those modules. Pro ZIPs ship the modules but keep them locked
until a UAL API key is stored and the cached seat grants the feature. Clearing
the API key or using Disconnect removes credentials and locks the seat.
"""

from __future__ import annotations

import importlib.util
import os
from typing import Any, Dict, Optional

FEATURE_FAB = "fab_megascans"
FEATURE_ASSET_BAR = "asset_bar"
FEATURE_CLOUD_BACKUP = "cloud_library_backup"

# Paths relative to this package (works as universal_asset_library.* in tests
# and as bl_ext.user_default.universal_asset_library.* inside Blender 5.x).
_MODULE_RELATIVE = {
    FEATURE_FAB: "sources.fab",
    FEATURE_ASSET_BAR: "ui.asset_bar",
    FEATURE_CLOUD_BACKUP: "cloud_backup",
}


def package_flavor() -> str:
    try:
        from . import package_flavor as flavor_mod

        return str(getattr(flavor_mod, "FLAVOR", "pro") or "pro").strip().lower()
    except Exception:
        return "pro"


def is_community_package() -> bool:
    return package_flavor() == "community"


def public_store_build() -> bool:
    """True for the public Superhive Community export (no Connect / Pro unlock UI)."""
    try:
        from . import package_flavor as flavor_mod

        return bool(getattr(flavor_mod, "PUBLIC_STORE", False))
    except Exception:
        return False


def _module_on_disk(rel: str) -> bool:
    """True when the Pro module file/package exists beside this file."""
    root = os.path.dirname(os.path.abspath(__file__))
    parts = [p for p in str(rel or "").split(".") if p]
    if not parts:
        return False
    base = os.path.join(root, *parts)
    return os.path.isfile(base + ".py") or os.path.isfile(os.path.join(base, "__init__.py"))


def module_available(feature: str) -> bool:
    """True when the Pro module shipped with this install.

    Prefer ``find_spec`` on ``{__package__}.{rel}`` so Blender extensions
    (``bl_ext…``) resolve correctly; fall back to a filesystem check so a
    mismatched package name still detects Community ZIP omissions.
    """
    rel = _MODULE_RELATIVE.get(feature)
    if not rel:
        return False
    pkg = (__package__ or "").strip()
    if pkg:
        try:
            if importlib.util.find_spec(f"{pkg}.{rel}") is not None:
                return True
        except (ImportError, ValueError, ModuleNotFoundError):
            pass
    # Legacy absolute name (older installs / odd PYTHONPATH layouts).
    try:
        if importlib.util.find_spec(f"universal_asset_library.{rel}") is not None:
            return True
    except (ImportError, ValueError, ModuleNotFoundError):
        pass
    return _module_on_disk(rel)


def _cfg(cfg: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if cfg is not None:
        return cfg
    try:
        from . import preferences

        return preferences.load_active_config()
    except Exception:
        return None


def pro_feature_enabled(feature: str, cfg: Optional[Dict[str, Any]] = None) -> bool:
    """True only when the module shipped, a UAL API key is stored, and the seat allows it."""
    if not module_available(feature):
        return False
    if is_community_package():
        return False
    from . import entitlement_client

    resolved = _cfg(cfg)
    if not entitlement_client.account_addon_key(resolved):
        return False
    return entitlement_client.has_feature(resolved, feature)


def fab_enabled(cfg: Optional[Dict[str, Any]] = None) -> bool:
    return pro_feature_enabled(FEATURE_FAB, cfg)


def asset_bar_enabled(cfg: Optional[Dict[str, Any]] = None) -> bool:
    return pro_feature_enabled(FEATURE_ASSET_BAR, cfg)


def cloud_backup_enabled(cfg: Optional[Dict[str, Any]] = None) -> bool:
    if not module_available(FEATURE_CLOUD_BACKUP):
        return False
    if is_community_package():
        return False
    from . import entitlement_client

    resolved = _cfg(cfg)
    if not entitlement_client.account_addon_key(resolved):
        return False
    return entitlement_client.cloud_backup_allowed(resolved)


def seat_is_pro(cfg: Optional[Dict[str, Any]] = None) -> bool:
    """True when the cached Blender seat is an active paid Pro plan."""
    from . import entitlement_client

    seat = entitlement_client.cached_seat(_cfg(cfg))
    if str(seat.get("status") or "") != "active":
        return False
    plan_id = str(seat.get("planId") or "").strip().lower()
    if plan_id.startswith("pro"):
        return True
    features = seat.get("features") or []
    return any(
        name in features
        for name in (FEATURE_FAB, FEATURE_ASSET_BAR, FEATURE_CLOUD_BACKUP)
    )


def needs_pro_package(cfg: Optional[Dict[str, Any]] = None) -> bool:
    """Community ZIP cannot enable Fab; a Pro seat needs the Pro package on disk."""
    if not is_community_package():
        return False
    return seat_is_pro(cfg)


def fab_lock_hint(cfg: Optional[Dict[str, Any]] = None) -> str:
    """User-facing copy when Fab is locked in Preferences → Online → Sources."""
    if needs_pro_package(cfg):
        return (
            "Your account is Pro — install the Pro add-on package from UAL Account above. "
            "The Community ZIP does not include Fab."
        )
    if is_community_package():
        return (
            "Fab needs UAL Pro — subscribe at universalassetlibrary.com, "
            "then Connect + Refresh seat in UAL Account above."
        )
    from . import entitlement_client

    resolved = _cfg(cfg)
    seat = entitlement_client.cached_seat(resolved)
    signed_in = bool(
        entitlement_client.account_user_id(resolved)
        and entitlement_client.account_addon_key(resolved)
    )
    if signed_in and seat.get("deviceBound") is False:
        return (
            "Fab needs this device activated — free a slot at Account → Devices, "
            "then Refresh seat in UAL Account above."
        )
    return "Fab needs UAL Pro — Connect + Refresh seat in UAL Account above."
