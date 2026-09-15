# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""Pure helpers for online source auth kwargs (no bpy)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

# AddonPreferences StringProperties that must never be clobbered by empty RNA
SECRET_ONLINE_PREF_KEYS = (
    "sketchfab_token",
    "polypizza_api_key",
    "fab_sessionid",
    "fab_csrftoken",
)


def token_kwargs_for_source(source_id: str, cfg_or_online: Dict[str, Any] | None) -> Dict[str, Any]:
    """Build download/search auth kwargs for a source.

    Pass the **full** config dict for Fab so ``epic_auth`` is included.
    A bare ``online`` block still works for cookies / API keys.
    """
    raw = cfg_or_online or {}
    online = raw["online"] if isinstance(raw.get("online"), dict) else raw
    token_kwargs: Dict[str, Any] = {}
    sid = (source_id or "").strip().lower()
    if sid == "sketchfab":
        token_kwargs["token"] = online.get("sketchfab_token") or ""
    if sid == "polypizza":
        token_kwargs["api_key"] = online.get("polypizza_api_key") or ""
    if sid == "fab":
        token_kwargs["fab_sessionid"] = online.get("fab_sessionid") or ""
        token_kwargs["fab_csrftoken"] = online.get("fab_csrftoken") or ""
        epic = raw.get("epic_auth")
        if isinstance(epic, dict) and epic:
            token_kwargs["epic_auth"] = epic
    return token_kwargs


def merge_secret_value(pref_value: Any, disk_value: Any) -> str:
    """Prefer non-empty Preferences value; otherwise keep disk (settings.json)."""
    pref = str(pref_value or "").strip()
    if pref:
        return pref
    return str(disk_value or "").strip()


def apply_secret_merge(online: Dict[str, Any], prefs_secrets: Dict[str, Any]) -> Dict[str, Any]:
    """Merge secret keys into ``online`` without wiping disk when prefs are empty."""
    out = dict(online or {})
    for key in SECRET_ONLINE_PREF_KEYS:
        out[key] = merge_secret_value(prefs_secrets.get(key), out.get(key))
    return out


def auth_health_notes(
    cfg: Optional[Dict[str, Any]],
    *,
    enabled_sources: Optional[Sequence[str]] = None,
) -> List[str]:
    """Local (no HTTP) notes when an enabled gated source lacks credentials."""
    from . import ui_strings

    raw = cfg or {}
    online = raw.get("online") if isinstance(raw.get("online"), dict) else {}
    if enabled_sources is None:
        enabled_sources = online.get("enabled_sources") or []
    enabled = {str(s).strip().lower() for s in enabled_sources if s}
    notes: List[str] = []
    for sid, label, how in (
        ("sketchfab", "Sketchfab", "Get Sketchfab Token in Preferences → Online"),
        ("polypizza", "Poly Pizza", "Get Poly Pizza API Key in Preferences → Online"),
        ("fab", "Fab / Megascans", "Sign In with Epic… in Preferences → Online"),
    ):
        if sid not in enabled:
            continue
        if not ui_strings.source_auth_configured(sid, raw):
            notes.append(f"{label}: needs sign-in — {how}")
    return notes
