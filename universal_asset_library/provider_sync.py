# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""Report auth-gated online source setup to ual-api (booleans only — no secrets)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from .entitlement_client import (
    account_addon_key,
    account_ready_for_api,
    account_user_id,
    addon_auth_headers,
)
from .http_ssl import urlopen
from .release_client import normalize_api_base
from .ui_strings import ADDON_VERSION, source_auth_configured

SOURCE_TO_PROVIDER_ID: Dict[str, str] = {
    "sketchfab": "sketchfab",
    "polypizza": "poly-pizza",
    "fab": "fab",
}

USER_AGENT = "UniversalAssetLibrary-Blender/{} ProviderSync"


@dataclass
class ProviderSyncResult:
    ok: bool
    skipped: bool = False
    reason: str = ""
    posted: int = 0
    errors: list[str] = field(default_factory=list)


def api_base_from_config(cfg: Optional[Dict[str, Any]]) -> str:
    releases = (cfg or {}).get("releases") if isinstance((cfg or {}).get("releases"), dict) else {}
    return normalize_api_base(str((releases or {}).get("api_base_url") or ""))


def resolve_providers_endpoint(api_base: str) -> str:
    base = normalize_api_base(api_base)
    if not base:
        raise ValueError("api_base_url is empty")
    lower = base.lower()
    if lower.endswith("/api/v1"):
        return f"{base}/providers"
    if lower.endswith("/api"):
        return f"{base}/v1/providers"
    if "ual-api" in lower or ":4000" in lower or ":4010" in lower:
        return f"{base}/api/v1/providers"
    return f"{base}/api/v1/providers"


def connection_status_for_source(source_id: str, cfg: Dict[str, Any]) -> str:
    return "connected" if source_auth_configured(source_id, cfg) else "disconnected"


def provider_statuses_from_config(cfg: Dict[str, Any]) -> Dict[str, str]:
    return {
        provider_id: connection_status_for_source(source_id, cfg)
        for source_id, provider_id in SOURCE_TO_PROVIDER_ID.items()
    }


def post_provider_status(
    api_base: str,
    user_id: str,
    provider_id: str,
    status: str,
    cfg: Optional[Dict[str, Any]],
    *,
    timeout: float = 12.0,
) -> None:
    if status not in ("connected", "disconnected"):
        raise ValueError(f"invalid status: {status}")
    endpoint = resolve_providers_endpoint(api_base)
    payload = json.dumps(
        {
            "userId": user_id,
            "providerId": provider_id,
            "status": status,
            "addonKey": account_addon_key(cfg),
        },
    ).encode("utf-8")
    headers = {
        "User-Agent": USER_AGENT.format(ADDON_VERSION),
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    headers.update(addon_auth_headers(cfg))
    request = urllib.request.Request(
        endpoint,
        data=payload,
        method="POST",
        headers=headers,
    )
    with urlopen(request, timeout=timeout) as response:
        code = int(getattr(response, "status", 200) or 200)
        if code >= 400:
            raise urllib.error.HTTPError(endpoint, code, "provider sync failed", {}, None)


def sync_provider_connections(
    cfg: Dict[str, Any],
    *,
    timeout: float = 12.0,
) -> ProviderSyncResult:
    """Blocking HTTP sync. Call from a background thread only."""
    user_id = account_user_id(cfg)
    api_base = api_base_from_config(cfg)
    if not account_ready_for_api(cfg):
        return ProviderSyncResult(
            ok=True,
            skipped=True,
            reason="missing_user_or_api_base",
        )
    statuses = provider_statuses_from_config(cfg)
    posted = 0
    errors: list[str] = []
    for provider_id, status in statuses.items():
        try:
            post_provider_status(api_base, user_id, provider_id, status, cfg, timeout=timeout)
            posted += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{provider_id}: {exc}")
    return ProviderSyncResult(ok=not errors, posted=posted, errors=errors)


def schedule_provider_sync(cfg: Optional[Dict[str, Any]] = None) -> bool:
    """Queue a background sync when account user id + API base are configured."""
    from . import preferences
    from . import tasks_queue

    try:
        cfg = cfg or preferences.prefs_to_config()
    except Exception:
        return False
    if not account_ready_for_api(cfg):
        return False

    def _work() -> None:
        sync_provider_connections(cfg)

    tasks_queue.run_in_background(_work)
    return True
