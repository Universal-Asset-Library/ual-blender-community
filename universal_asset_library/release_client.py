# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""Release update-check client (ual-api / website). Pure Python — no bpy."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Optional

from .http_ssl import urlopen
from .release_manifest import (
    evaluate_update_manifest,
    resolve_require_signed_update_check,
)
from .ui_strings import ADDON_VERSION

PRODUCT_SLUG = "blender"
CHANNELS = ("stable", "beta")
USER_AGENT = "UniversalAssetLibrary-Blender/{} UpdateCheck"


@dataclass(frozen=True)
class UpdateCheckResult:
    product: str
    channel: str
    current_version: str
    latest_version: Optional[str]
    update_available: bool
    download_available: bool
    force_update: bool
    message: str
    release_notes: str
    download_page_url: Optional[str]
    ok: bool
    error: str = ""
    manifest_signed: bool = False
    manifest_verified: Optional[bool] = None
    release_sha256: Optional[str] = None
    artifact_url: Optional[str] = None
    artifact_filename: Optional[str] = None
    artifact_sha256: Optional[str] = None

    @property
    def skipped(self) -> bool:
        return self.error == "not_configured"


def normalize_api_base(raw: str) -> str:
    return str(raw or "").strip().rstrip("/")


def normalize_channel(raw: str) -> str:
    channel = str(raw or "stable").strip().lower()
    return channel if channel in CHANNELS else "stable"


def resolve_check_endpoint(api_base: str) -> str:
    base = normalize_api_base(api_base)
    if not base:
        raise ValueError("api_base_url is empty")
    lower = base.lower()
    if lower.endswith("/api/v1"):
        return f"{base}/releases/check"
    if lower.endswith("/api"):
        return f"{base}/v1/releases/check"
    # Direct ual-api origins (default 4000; local smoke often uses 4010).
    if "ual-api" in lower or ":4000" in lower or ":4010" in lower:
        return f"{base}/api/v1/releases/check"
    return f"{base}/api/releases/check"


def build_check_url(
    api_base: str,
    *,
    product: str = PRODUCT_SLUG,
    current_version: str,
    channel: str = "stable",
    flavor: str = "",
) -> str:
    endpoint = resolve_check_endpoint(api_base)
    if not flavor:
        try:
            from . import package_flavor as flavor_mod

            flavor = str(getattr(flavor_mod, "FLAVOR", "pro") or "pro")
        except Exception:
            flavor = "pro"
    query = urllib.parse.urlencode(
        {
            "product": product,
            "currentVersion": current_version,
            "channel": normalize_channel(channel),
            "flavor": flavor if flavor in ("community", "pro") else "pro",
        }
    )
    return f"{endpoint}?{query}"


def download_page_url(api_base: str) -> Optional[str]:
    base = normalize_api_base(api_base)
    if not base:
        return None
    lower = base.lower()
    if "ual-api" in lower or ":4000" in lower:
        return None
    return f"{base}/download"


def parse_check_response(
    payload: Dict[str, Any],
    *,
    current_version: str,
    channel: str,
    api_base: str = "",
) -> UpdateCheckResult:
    latest = payload.get("latestVersion")
    if latest is None:
        latest = payload.get("version")
    latest_s = str(latest).strip() if latest else None
    if latest_s == "":
        latest_s = None
    update_available = bool(payload.get("updateAvailable"))
    download_available = bool(payload.get("downloadAvailable"))
    force_update = bool(payload.get("forceUpdate"))
    message = str(payload.get("message") or "").strip()
    notes = str(payload.get("releaseNotes") or "").strip()
    from .update_installer import extract_artifact_info

    artifact = extract_artifact_info(payload)
    art_sha = artifact.get("sha256") or None
    require_signed = resolve_require_signed_update_check(
        api_base=api_base,
        channel=normalize_channel(channel),
    )
    trust = evaluate_update_manifest(
        payload,
        require_signed=require_signed,
        api_base=api_base,
    )
    if trust.get("reject"):
        return UpdateCheckResult(
            product=str(payload.get("product") or PRODUCT_SLUG),
            channel=normalize_channel(str(payload.get("channel") or channel)),
            current_version=str(payload.get("currentVersion") or current_version),
            latest_version=latest_s,
            update_available=False,
            download_available=False,
            force_update=False,
            message="Update rejected: release manifest signature failed.",
            release_notes=notes,
            download_page_url=download_page_url(api_base),
            ok=False,
            error="manifest_invalid",
            manifest_signed=bool(trust.get("signed")),
            manifest_verified=trust.get("verified"),
            release_sha256=trust.get("sha256") or art_sha,
            artifact_url=artifact.get("url"),
            artifact_filename=artifact.get("filename"),
            artifact_sha256=art_sha or trust.get("sha256"),
        )
    base_msg = message or (
        f"Update available: {latest_s}"
        if update_available and latest_s
        else "Update check completed."
    )
    suffix = str(trust.get("message_suffix") or "")
    return UpdateCheckResult(
        product=str(payload.get("product") or PRODUCT_SLUG),
        channel=normalize_channel(str(payload.get("channel") or channel)),
        current_version=str(payload.get("currentVersion") or current_version),
        latest_version=latest_s,
        update_available=update_available,
        download_available=download_available,
        force_update=force_update,
        message=(base_msg + suffix).strip(),
        release_notes=notes,
        download_page_url=download_page_url(api_base),
        ok=True,
        error="",
        manifest_signed=bool(trust.get("signed")),
        manifest_verified=trust.get("verified"),
        release_sha256=trust.get("sha256") or art_sha,
        artifact_url=artifact.get("url"),
        artifact_filename=artifact.get("filename"),
        artifact_sha256=art_sha or trust.get("sha256"),
    )


def check_for_update(
    api_base: str,
    current_version: str | None = None,
    channel: str = "stable",
    *,
    timeout: float = 15.0,
    product: str = PRODUCT_SLUG,
) -> UpdateCheckResult:
    """Blocking HTTP update check. Call from a background thread only."""
    version = str(current_version or ADDON_VERSION)
    base = normalize_api_base(api_base)
    if not base:
        return UpdateCheckResult(
            product=product,
            channel=normalize_channel(channel),
            current_version=version,
            latest_version=None,
            update_available=False,
            download_available=False,
            force_update=False,
            message="Set Releases API base URL in Preferences (ual-api or website origin).",
            release_notes="",
            download_page_url=None,
            ok=False,
            error="not_configured",
        )
    try:
        url = build_check_url(
            base,
            product=product,
            current_version=version,
            channel=channel,
        )
    except ValueError as exc:
        return UpdateCheckResult(
            product=product,
            channel=normalize_channel(channel),
            current_version=version,
            latest_version=None,
            update_available=False,
            download_available=False,
            force_update=False,
            message=str(exc),
            release_notes="",
            download_page_url=None,
            ok=False,
            error="bad_base",
        )

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT.format(version),
            "Accept": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="ignore")
            status = getattr(response, "status", 200)
    except urllib.error.HTTPError as exc:
        return UpdateCheckResult(
            product=product,
            channel=normalize_channel(channel),
            current_version=version,
            latest_version=None,
            update_available=False,
            download_available=False,
            force_update=False,
            message=f"Update check failed (HTTP {exc.code}).",
            release_notes="",
            download_page_url=download_page_url(base),
            ok=False,
            error=f"http_{exc.code}",
        )
    except Exception as exc:  # noqa: BLE001
        return UpdateCheckResult(
            product=product,
            channel=normalize_channel(channel),
            current_version=version,
            latest_version=None,
            update_available=False,
            download_available=False,
            force_update=False,
            message=f"Update check failed: {exc}",
            release_notes="",
            download_page_url=download_page_url(base),
            ok=False,
            error=str(exc),
        )

    if status and int(status) >= 400:
        return UpdateCheckResult(
            product=product,
            channel=normalize_channel(channel),
            current_version=version,
            latest_version=None,
            update_available=False,
            download_available=False,
            force_update=False,
            message=f"Update check failed (HTTP {status}).",
            release_notes="",
            download_page_url=download_page_url(base),
            ok=False,
            error=f"http_{status}",
        )

    try:
        payload = json.loads(raw) if raw else {}
        if not isinstance(payload, dict):
            raise ValueError("expected JSON object")
    except Exception as exc:  # noqa: BLE001
        return UpdateCheckResult(
            product=product,
            channel=normalize_channel(channel),
            current_version=version,
            latest_version=None,
            update_available=False,
            download_available=False,
            force_update=False,
            message="Invalid update-check response.",
            release_notes="",
            download_page_url=download_page_url(base),
            ok=False,
            error=f"bad_json:{exc}",
        )

    return parse_check_response(
        payload,
        current_version=version,
        channel=channel,
        api_base=base,
    )
