# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""Public website / auth-page URLs for Online Preferences (open in browser).

Pure data — no bpy. Used by Preferences buttons so artists do not hunt for
API-key / Epic sign-in pages manually.
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

# Homepage for each source (Enabled sources → Website)
SOURCE_WEBSITE_URLS: Dict[str, str] = {
    "polyhaven": "https://polyhaven.com",
    "ambientcg": "https://ambientcg.com",
    "gpuopen": "https://matlib.gpuopen.com",
    "lazytextures": "https://lazytextures.net",
    "threedscans": "https://threedscans.com",
    "pbrpx": "https://pbrpx.com",
    "polypizza": "https://poly.pizza",
    "sketchfab": "https://sketchfab.com",
    "fab": "https://www.fab.com",
}

# Where to create / copy credentials (API keys box + Fab auth)
# label, url
SOURCE_AUTH_LINKS: Dict[str, Tuple[str, str]] = {
    "sketchfab": (
        "Get Sketchfab Token",
        "https://sketchfab.com/settings/password",
    ),
    "polypizza": (
        "Get Poly Pizza API Key",
        "https://poly.pizza/settings/api",
    ),
    "fab_site": (
        "Open Fab.com",
        "https://www.fab.com",
    ),
}


def website_url(source_id: str) -> str:
    return str(SOURCE_WEBSITE_URLS.get((source_id or "").strip().lower()) or "").strip()


def auth_link(key: str) -> Tuple[str, str]:
    """Return ``(button_label, url)`` or ``(\"\", \"\")``."""
    pair = SOURCE_AUTH_LINKS.get((key or "").strip().lower())
    if not pair:
        return "", ""
    return str(pair[0]), str(pair[1])


def epic_authorize_login_url() -> str:
    """Epic browser login that yields an authorizationCode JSON page."""
    try:
        from . import epic_oauth

        return str(getattr(epic_oauth, "EPIC_AUTHORIZE_LOGIN_URL", "") or "")
    except Exception:
        return ""


def is_http_url(url: Optional[str]) -> bool:
    text = str(url or "").strip()
    return text.startswith("https://") or text.startswith("http://")
