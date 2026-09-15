# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""Fetch UAL plan/features from ual-api (no secrets). Pure Python — no bpy."""

from __future__ import annotations

import json
import platform
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .http_ssl import urlopen
from .release_client import normalize_api_base
from .ui_strings import ADDON_VERSION

PRODUCT_ID = "blender"
USER_AGENT = "UniversalAssetLibrary-Blender/{} SeatRefresh"

# Always available in the shipped GPL add-on. Never lock these locally.
CORE_FEATURES = ("local_library", "online_basic", "hover_preview")

PLAN_LABELS = {
    "community": "Community",
    "pro_monthly": "Pro (Monthly)",
    "pro_yearly": "Pro (Annual)",
    "pro_onetime": "Pro (One-time)",
}

FEATURE_LABELS = {
    "local_library": "Local library",
    "online_basic": "Online sources",
    "hover_preview": "Hover preview",
    "fab_megascans": "Fab / Megascans",
    "asset_bar": "Asset bar",
    "cloud_library_backup": "Cloud backup",
    "device_binding": "Device binding",
    "signed_downloads": "Signed downloads",
    "priority_support": "Priority support",
}

FEATURE_CLOUD_BACKUP = "cloud_library_backup"
SEAT_REFRESH_MIN_INTERVAL_SEC = 300


@dataclass
class SeatRefreshResult:
    ok: bool
    skipped: bool = False
    reason: str = ""
    seat: Dict[str, Any] = field(default_factory=dict)
    error: str = ""
    device_error: str = ""


def api_base_from_config(cfg: Optional[Dict[str, Any]]) -> str:
    from .config import DEFAULT_UPDATES_API_BASE

    releases = (cfg or {}).get("releases") if isinstance((cfg or {}).get("releases"), dict) else {}
    return normalize_api_base(
        str((releases or {}).get("api_base_url") or "") or DEFAULT_UPDATES_API_BASE
    )


def account_user_id(cfg: Optional[Dict[str, Any]]) -> str:
    account = (cfg or {}).get("account") if isinstance((cfg or {}).get("account"), dict) else {}
    return str((account or {}).get("user_id") or "").strip()


def account_addon_key(cfg: Optional[Dict[str, Any]]) -> str:
    account = (cfg or {}).get("account") if isinstance((cfg or {}).get("account"), dict) else {}
    return str((account or {}).get("addon_key") or "").strip()


def normalize_addon_key(raw: str) -> str:
    key = str(raw or "").strip()
    if key.lower().startswith("bearer "):
        key = key[7:].strip()
    return key


def account_ready_for_api(cfg: Optional[Dict[str, Any]]) -> bool:
    return bool(account_addon_key(cfg) and api_base_from_config(cfg))


def locked_offline_seat(old_seat: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Inactive Community seat persisted after disconnect or key removal."""
    old = dict(old_seat) if isinstance(old_seat, dict) else {}
    return {
        "productId": PRODUCT_ID,
        "planId": "community",
        "planLabel": plan_label("community"),
        "status": "inactive",
        "features": list(CORE_FEATURES),
        "expiresAt": old.get("expiresAt"),
        "maxDevices": int(old.get("maxDevices") or 3),
        "deviceCount": 0,
        "deviceBound": False,
        "deviceError": "",
        "refreshedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def clear_account(cfg: Dict[str, Any]) -> None:
    """Remove stored UAL API key and user id; lock cached seat to Community core."""
    account = cfg.setdefault("account", {})
    if not isinstance(account, dict):
        cfg["account"] = {}
        account = cfg["account"]
    old_seat = account.get("seat") if isinstance(account.get("seat"), dict) else None
    for key in ("user_id", "addon_key", "seat_fetched_at", "last_seat_error"):
        account.pop(key, None)
    account["seat"] = locked_offline_seat(old_seat)


def friendly_account_error(raw: Any) -> str:
    """Artist-facing copy for Connect / seat account errors."""
    text = str(raw or "").strip()
    lower = text.lower()
    if "invalid_addon_key" in lower:
        return "API key must start with ualak_. Copy a new key from Account → Profile."
    if "missing_addon_key" in lower:
        return "Paste your UAL API key from Account → Profile, then click Connect."
    if "missing_api_base" in lower:
        return "Set Updates API base URL first (website origin)."
    if "could not resolve account from api key" in lower:
        return "Could not resolve that API key. Rotate the key on Account → Profile and try Connect again."
    if "sign in or paste your add-on key" in lower or "paste your add-on key from account" in lower:
        return "That API key was rejected. Create or rotate a key on Account → Profile, then Connect."
    if "401" in lower or "unauthorized" in lower or "forbidden" in lower or "403" in lower:
        return (
            "UAL API key was rejected (expired or rotated). "
            "Account → Profile → Rotate UAL API key, paste into Preferences, then Connect."
        )
    if "device limit" in lower or "device_not_bound" in lower or "device_required" in lower:
        return (
            "Device limit reached or this install is not bound. "
            "Account → Devices → free a slot (max 3 per product), then Connect / Refresh seat."
        )
    return text or "Connect failed. Check the API key and try again."


def addon_auth_headers(cfg: Optional[Dict[str, Any]]) -> Dict[str, str]:
    key = account_addon_key(cfg)
    if not key:
        return {}
    headers = {
        "Authorization": "Bearer {}".format(key),
        "X-UAL-Addon-Key": key,
    }
    try:
        resolved = cfg if isinstance(cfg, dict) else {}
        iid = str((resolved.get("account") or {}).get("installation_id") or "").strip()
        if not iid and resolved:
            iid = ensure_installation_id(resolved)
        if iid:
            headers["X-UAL-Installation-Id"] = iid
    except Exception:
        pass
    return headers


def resolve_seat_url(api_base: str) -> str:
    base = normalize_api_base(api_base)
    if not base:
        raise ValueError("api_base_url is empty")
    lower = base.lower()
    if lower.endswith("/api/v1"):
        return f"{base}/account/seat"
    if lower.endswith("/api"):
        return f"{base}/v1/account/seat"
    if "ual-api" in lower or ":4000" in lower or ":4010" in lower:
        return f"{base}/api/v1/account/seat"
    return f"{base}/api/account/seat"


def resolve_activate_url(api_base: str) -> str:
    base = normalize_api_base(api_base)
    if not base:
        raise ValueError("api_base_url is empty")
    lower = base.lower()
    if lower.endswith("/api/v1"):
        return f"{base}/devices/activate"
    if lower.endswith("/api"):
        return f"{base}/v1/devices/activate"
    if "ual-api" in lower or ":4000" in lower or ":4010" in lower:
        return f"{base}/api/v1/devices/activate"
    return f"{base}/api/account/activate"


def resolve_releases_download_url(api_base: str) -> str:
    base = normalize_api_base(api_base)
    if not base:
        raise ValueError("api_base_url is empty")
    lower = base.lower()
    if lower.endswith("/api/v1"):
        return f"{base}/releases/download"
    if lower.endswith("/api"):
        return f"{base}/v1/releases/download"
    if "ual-api" in lower or ":4000" in lower or ":4010" in lower:
        return f"{base}/api/v1/releases/download"
    return f"{base}/api/releases/download"


def resolve_support_tickets_url(api_base: str) -> str:
    """Create-ticket endpoint used by DCC diagnostics bundles."""
    base = normalize_api_base(api_base)
    if not base:
        raise ValueError("api_base_url is empty")
    lower = base.lower()
    if lower.endswith("/api/v1"):
        return f"{base}/support/tickets"
    if lower.endswith("/api"):
        return f"{base}/v1/support/tickets"
    return f"{base}/api/v1/support/tickets"


def resolve_library_backup_url(api_base: str) -> str:
    base = normalize_api_base(api_base)
    if not base:
        raise ValueError("api_base_url is empty")
    lower = base.lower()
    if lower.endswith("/api/v1"):
        return f"{base}/library-backup"
    if lower.endswith("/api"):
        return f"{base}/v1/library-backup"
    return f"{base}/api/v1/library-backup"


def resolve_users_me_url(api_base: str) -> str:
    base = normalize_api_base(api_base)
    if not base:
        raise ValueError("api_base_url is empty")
    lower = base.lower()
    if lower.endswith("/api/v1"):
        return f"{base}/users/me"
    if lower.endswith("/api"):
        return f"{base}/v1/users/me"
    return f"{base}/api/v1/users/me"


def resolve_account_from_key(
    cfg: Optional[Dict[str, Any]],
    *,
    key: Optional[str] = None,
    timeout: float = 12.0,
) -> Dict[str, Any]:
    """Resolve account user ID from a UAL API key (ualak_*)."""
    api_base = api_base_from_config(cfg)
    addon_key = normalize_addon_key(key if key is not None else account_addon_key(cfg))
    if not api_base:
        return {"success": False, "error": "missing_api_base"}
    if not addon_key:
        return {"success": False, "error": "missing_addon_key"}
    if not addon_key.startswith("ualak_"):
        return {"success": False, "error": "invalid_addon_key"}

    temp_cfg = json.loads(json.dumps(cfg or {}))
    temp_cfg.setdefault("account", {})["addon_key"] = addon_key
    try:
        payload = _http_json(resolve_users_me_url(api_base), timeout=timeout, cfg=temp_cfg)
        user = payload.get("user") if isinstance(payload.get("user"), dict) else None
        user_id = str((user or {}).get("id") or "").strip()
        if not user_id:
            return {"success": False, "error": "invalid_response"}
        return {"success": True, "userId": user_id, "user": user}
    except urllib.error.HTTPError as exc:
        error_msg = _extract_json_error(exc) or str(getattr(exc, "reason", None) or exc)
        return {"success": False, "error": error_msg}
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": str(exc)}


def ensure_account_user_id(cfg: Dict[str, Any], *, timeout: float = 12.0) -> str:
    existing = account_user_id(cfg)
    if existing:
        return existing
    result = resolve_account_from_key(cfg, timeout=timeout)
    if result.get("success") and result.get("userId"):
        cfg.setdefault("account", {})["user_id"] = str(result["userId"])
        return str(result["userId"])
    return ""


def connect_with_api_key(
    cfg: Dict[str, Any],
    key: str,
    *,
    timeout: float = 12.0,
) -> Dict[str, Any]:
    """Validate a pasted UAL API key and store user_id + addon_key on *cfg*."""
    addon_key = normalize_addon_key(key)
    if not addon_key:
        return {"success": False, "error": "missing_addon_key"}
    if not addon_key.startswith("ualak_"):
        return {"success": False, "error": "invalid_addon_key"}
    cfg.setdefault("account", {})["addon_key"] = addon_key
    result = resolve_account_from_key(cfg, key=addon_key, timeout=timeout)
    if not result.get("success"):
        return result
    user_id = str(result.get("userId") or "").strip()
    cfg["account"]["user_id"] = user_id
    return {"success": True, "userId": user_id, "addonKey": addon_key}


def resolve_dcc_login_url(api_base: str) -> str:
    """
    DCC sign-in must hit ual-web (Next.js), not ual-api.
    Seat/download still use add-on key and can point at ual-api.
    """
    base = normalize_api_base(api_base)
    if not base:
        raise ValueError("api_base_url is empty")
    lower = base.lower()
    if "ual-api" in lower or ":4000" in lower or ":4010" in lower:
        raise ValueError("DCC login requires the website origin (ual-web).")
    return f"{base}/api/auth/dcc-login"


def dcc_login(
    cfg: Optional[Dict[str, Any]],
    *,
    email: str,
    password: str,
    timeout: float = 12.0,
) -> Dict[str, Any]:
    """
    Validate email/password on the website.
    When the account has no key yet, returns a new {addonKey}.
    When a key already exists, returns {existingKeyRequired: true} without rotating.
    """
    api_base = api_base_from_config(cfg)
    if not api_base:
        return {"success": False, "error": "missing_api_base"}
    email = str(email or "").strip()
    password = str(password or "")
    if not email or not password:
        return {"success": False, "error": "missing_email_or_password"}

    try:
        login_url = resolve_dcc_login_url(api_base)
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": str(exc)}

    body = {"email": email, "password": password}
    try:
        payload = _http_json(
            login_url,
            method="POST",
            body=body,
            timeout=timeout,
            cfg=cfg,
        )
        if isinstance(payload, dict) and payload.get("success"):
            if payload.get("addonKey"):
                return payload
            if payload.get("existingKeyRequired"):
                return {
                    "success": False,
                    "error": str(
                        payload.get("message")
                        or "Use your existing UAL API key from Account → Profile, or regenerate there."
                    ),
                    "existingKeyRequired": True,
                    "userId": payload.get("userId"),
                }
        return payload if isinstance(payload, dict) else {"success": False, "error": "invalid_response"}
    except urllib.error.HTTPError as exc:
        error_msg = _extract_json_error(exc) or str(getattr(exc, "reason", None) or exc)
        return {"success": False, "error": error_msg}
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": str(exc)}


def create_support_ticket(
    cfg: Optional[Dict[str, Any]],
    *,
    subject: str,
    body: str,
    diagnostics_json: Optional[Dict[str, Any]] = None,
    timeout: float = 12.0,
) -> Dict[str, Any]:
    """Create a support ticket with optional redacted diagnostics.

    Called by “Send Diagnostics Report” UI (opt-in).
    """
    api_base = api_base_from_config(cfg)
    if not api_base:
        return {"success": False, "error": "missing_api_base"}
    subject = str(subject or "").strip()
    body = str(body or "").strip()
    if not subject or not body:
        return {"success": False, "error": "missing_subject_or_body"}

    url = resolve_support_tickets_url(api_base)
    payload: Dict[str, Any] = {"subject": subject, "body": body}
    if isinstance(diagnostics_json, dict):
        try:
            payload["diagnosticsJson"] = json.dumps(diagnostics_json, separators=(",", ":"))
        except Exception:  # noqa: BLE001
            pass

    try:
        result = _http_json(url, method="POST", body=payload, timeout=timeout, cfg=cfg)
        return result if isinstance(result, dict) else {"success": False, "error": "invalid_response"}
    except urllib.error.HTTPError as exc:
        error_msg = _extract_json_error(exc) or str(getattr(exc, "reason", None) or exc)
        return {"success": False, "error": error_msg}
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": str(exc)}


def ensure_installation_id(cfg: Dict[str, Any]) -> str:
    account = cfg.setdefault("account", {})
    if not isinstance(account, dict):
        account = {}
        cfg["account"] = account
    existing = str(account.get("installation_id") or "").strip()
    if existing:
        return existing
    new_id = str(uuid.uuid4())
    account["installation_id"] = new_id
    return new_id


def cached_seat(cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    account = (cfg or {}).get("account") if isinstance((cfg or {}).get("account"), dict) else {}
    seat = (account or {}).get("seat")
    return dict(seat) if isinstance(seat, dict) else {}


def plan_label(plan_id: str) -> str:
    key = str(plan_id or "").strip()
    return PLAN_LABELS.get(key, key or "Community")


def has_feature(cfg: Optional[Dict[str, Any]], feature: str) -> bool:
    """Core DCC features stay on offline. Pro extras need an active cached seat."""
    name = str(feature or "").strip()
    if name in CORE_FEATURES:
        return True
    seat = cached_seat(cfg)
    if not seat_is_active(seat):
        return False
    if seat.get("deviceBound") is False:
        return False
    features = seat.get("features") or []
    return name in features


FEATURE_CLOUD_BACKUP = "cloud_library_backup"


def cloud_backup_allowed(cfg: Optional[Dict[str, Any]] = None) -> bool:
    """True when the seat matches ual-api ``hasProBackupAccess`` for library-backup."""
    seat = cached_seat(cfg)
    if not seat_is_active(seat):
        return False
    if seat.get("deviceBound") is False:
        return False
    features = list(seat.get("features") or [])
    if FEATURE_CLOUD_BACKUP in features:
        return True
    if "signed_downloads" in features:
        return True
    plan_id = str(seat.get("planId") or "").strip().lower()
    return bool(plan_id) and plan_id != "community"


def seat_is_active(seat: Optional[Dict[str, Any]]) -> bool:
    """True when status is active and expiresAt (if set) is still in the future."""
    if not isinstance(seat, dict) or not seat:
        return False
    if str(seat.get("status") or "") != "active":
        return False
    expires_raw = seat.get("expiresAt")
    if expires_raw in (None, ""):
        return True
    try:
        cleaned = str(expires_raw).strip().replace("Z", "+00:00")
        stamp = datetime.fromisoformat(cleaned)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp.astimezone(timezone.utc) > datetime.now(timezone.utc)
    except (TypeError, ValueError, OSError):
        return True


def _auth_refresh_failure(error: str) -> bool:
    text = str(error or "").lower()
    tokens = (
        "401",
        "403",
        "unauthorized",
        "forbidden",
        "invalid_addon_key",
        "invalid api key",
        "could not resolve account",
        "addon key",
    )
    return any(token in text for token in tokens)


def locked_seat_after_failed_refresh(
    cfg: Optional[Dict[str, Any]],
    result: SeatRefreshResult,
) -> Optional[Dict[str, Any]]:
    """Return a locked seat to persist when refresh failed for auth reasons."""
    if result.ok or result.skipped:
        return None
    if not _auth_refresh_failure(result.error):
        return None
    old = dict(result.seat or cached_seat(cfg) or {})
    old["status"] = "inactive"
    old["features"] = list(CORE_FEATURES)
    old["deviceError"] = str(result.error or "")[:220]
    old["refreshedAt"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return old


def seat_refresh_is_stale(
    cfg: Optional[Dict[str, Any]] = None,
    *,
    min_interval_sec: int = SEAT_REFRESH_MIN_INTERVAL_SEC,
) -> bool:
    """True when there is no refreshedAt or it is older than *min_interval_sec*."""
    seat = cached_seat(cfg)
    raw = str(seat.get("refreshedAt") or "").strip()
    if not raw:
        return True
    try:
        cleaned = raw.replace("Z", "+00:00")
        stamp = datetime.fromisoformat(cleaned)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - stamp.astimezone(timezone.utc)).total_seconds()
        return age >= float(max(0, int(min_interval_sec)))
    except (TypeError, ValueError, OSError):
        return True


def maybe_schedule_seat_refresh(
    cfg: Optional[Dict[str, Any]] = None,
    *,
    min_interval_sec: int = SEAT_REFRESH_MIN_INTERVAL_SEC,
) -> bool:
    """Background seat refresh when signed in and the cached seat is stale."""
    if cfg is None:
        from . import preferences

        try:
            cfg = preferences.prefs_to_config()
        except Exception:
            return False
    if not account_ready_for_api(cfg):
        return False
    if not seat_refresh_is_stale(cfg, min_interval_sec=min_interval_sec):
        return False
    return schedule_seat_refresh(cfg)


def mint_signed_download(
    cfg: Optional[Dict[str, Any]],
    *,
    channel: str = "stable",
    version: str = "",
    timeout: float = 12.0,
    flavor: str = "",
) -> Dict[str, Any]:
    """Ask the website / ual-api for a package URL using Account user ID + add-on key."""
    api_base = api_base_from_config(cfg)
    addon_key = account_addon_key(cfg)
    if not api_base:
        return {"success": False, "error": "missing_api_base"}
    if not addon_key:
        return {"success": False, "error": "missing_addon_key"}
    user_id = account_user_id(cfg) or ensure_account_user_id(cfg)
    if not user_id:
        return {"success": False, "error": "invalid_api_key"}
    requested = str(flavor or "").strip().lower()
    body: Dict[str, Any] = {
        "product": PRODUCT_ID,
        "channel": channel or "stable",
        "flavor": requested if requested in ("community", "pro") else "pro",
    }
    if requested not in ("community", "pro"):
        try:
            from . import package_flavor as flavor_mod

            body["flavor"] = str(getattr(flavor_mod, "FLAVOR", "pro") or "pro")
        except Exception:
            pass
    if user_id:
        body["userId"] = user_id
    if addon_key:
        body["addonKey"] = addon_key
    body["installationId"] = ensure_installation_id(cfg)
    if str(version or "").strip():
        body["version"] = str(version).strip()
    try:
        payload = _http_json(
            resolve_releases_download_url(api_base),
            method="POST",
            body=body,
            timeout=timeout,
            cfg=cfg,
        )
        url = str(payload.get("downloadUrl") or "").strip()
        if url:
            payload["success"] = True
            payload["downloadUrl"] = url
            return payload
        return {
            "success": False,
            "error": str(payload.get("error") or "No download URL"),
            **payload,
        }
    except urllib.error.HTTPError as exc:
        return {"success": False, "error": str(getattr(exc, "reason", None) or exc)}
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": str(exc)}


def feature_labels(features: List[str]) -> List[str]:
    return [FEATURE_LABELS.get(f, f) for f in features]


def fetch_library_backup_manifest(
    cfg: Optional[Dict[str, Any]],
    *,
    product_id: str = PRODUCT_ID,
    timeout: float = 12.0,
) -> Dict[str, Any]:
    api_base = api_base_from_config(cfg)
    if not api_base:
        return {"success": False, "error": "missing_api_base"}
    try:
        params = {"productId": product_id}
        if isinstance(cfg, dict):
            params["installationId"] = ensure_installation_id(cfg)
        url = "{}?{}".format(
            resolve_library_backup_url(api_base),
            urllib.parse.urlencode(params),
        )
        payload = _http_json(url, timeout=timeout, cfg=cfg)
        backup = payload.get("backup")
        return {"success": True, "backup": backup if isinstance(backup, dict) else None}
    except urllib.error.HTTPError as exc:
        return {
            "success": False,
            "error": _extract_json_error(exc) or str(getattr(exc, "reason", None) or exc),
        }
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": str(exc)}


def upload_library_backup_manifest(
    cfg: Optional[Dict[str, Any]],
    *,
    product_id: str = PRODUCT_ID,
    manifest: Dict[str, Any],
    expected_revision: Optional[int] = None,
    timeout: float = 20.0,
) -> Dict[str, Any]:
    api_base = api_base_from_config(cfg)
    if not api_base:
        return {"success": False, "error": "missing_api_base"}
    body: Dict[str, Any] = {
        "productId": product_id,
        "manifest": manifest,
        "installationId": ensure_installation_id(cfg) if isinstance(cfg, dict) else "",
    }
    if expected_revision is not None:
        body["expectedRevision"] = int(expected_revision)
    try:
        payload = _http_json(
            resolve_library_backup_url(api_base),
            method="PUT",
            body=body,
            timeout=timeout,
            cfg=cfg,
        )
        backup = payload.get("backup")
        return {"success": True, "backup": backup if isinstance(backup, dict) else None}
    except urllib.error.HTTPError as exc:
        return {
            "success": False,
            "error": _extract_json_error(exc) or str(getattr(exc, "reason", None) or exc),
        }
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": str(exc)}


def delete_library_backup_manifest(
    cfg: Optional[Dict[str, Any]],
    *,
    product_id: str = PRODUCT_ID,
    timeout: float = 12.0,
) -> Dict[str, Any]:
    api_base = api_base_from_config(cfg)
    if not api_base:
        return {"success": False, "error": "missing_api_base"}
    try:
        iid = ensure_installation_id(cfg) if isinstance(cfg, dict) else ""
        payload = _http_json(
            "{}?{}".format(
                resolve_library_backup_url(api_base),
                urllib.parse.urlencode(
                    {"productId": product_id, "installationId": iid}
                ),
            ),
            method="DELETE",
            body={"productId": product_id, "installationId": iid},
            timeout=timeout,
            cfg=cfg,
        )
        return {"success": bool(payload.get("success", True))}
    except urllib.error.HTTPError as exc:
        msg = _extract_json_error(exc) or str(getattr(exc, "reason", None) or exc)
        return {"success": False, "error": msg}
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": str(exc)}


def _extract_json_error(exc: urllib.error.HTTPError) -> str:
    """Try to pull a human-readable error from a JSON error response body."""
    try:
        raw = getattr(exc, "msg", "") or ""
        if raw.strip().startswith("{"):
            data = json.loads(raw)
            if isinstance(data, dict) and data.get("error"):
                return str(data["error"])
    except Exception:
        pass
    return ""


def _http_json(
    url: str,
    *,
    method: str = "GET",
    body: Optional[dict] = None,
    timeout: float = 12.0,
    cfg: Optional[Dict[str, Any]] = None,
) -> dict:
    data = None
    headers = {
        "User-Agent": USER_AGENT.format(ADDON_VERSION),
        "Accept": "application/json",
    }
    headers.update(addon_auth_headers(cfg))
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urlopen(request, timeout=timeout) as response:
        code = int(getattr(response, "status", 200) or 200)
        raw = response.read().decode("utf-8", errors="ignore")
        if code >= 400:
            raise urllib.error.HTTPError(url, code, raw or "request failed", {}, None)
    payload = json.loads(raw) if raw else {}
    if not isinstance(payload, dict):
        raise ValueError("expected JSON object")
    return payload


def detect_platform() -> str:
    system = platform.system().lower()
    if system.startswith("darwin"):
        return "macos"
    if system.startswith("win"):
        return "windows"
    return "linux"


def host_version() -> str:
    try:
        import bpy  # type: ignore

        return str(getattr(bpy.app, "version_string", "") or "")
    except Exception:
        return ""


def seat_from_payload(payload: Dict[str, Any], *, device_bound: bool, device_error: str = "") -> Dict[str, Any]:
    ent = payload.get("entitlement") if isinstance(payload.get("entitlement"), dict) else None
    license_row = payload.get("license") if isinstance(payload.get("license"), dict) else None
    plan_id = str((ent or {}).get("planId") or (license_row or {}).get("planId") or "community")
    features = list((ent or {}).get("features") or CORE_FEATURES)
    status = str((ent or {}).get("status") or ("active" if ent else "missing"))
    if "deviceBound" in payload:
        device_bound = bool(payload.get("deviceBound"))
    if not device_error:
        device_error = str(payload.get("deviceError") or "")
    if not device_bound:
        features = [f for f in features if f in CORE_FEATURES]
    return {
        "productId": PRODUCT_ID,
        "planId": plan_id,
        "planLabel": plan_label(plan_id),
        "status": status,
        "features": features,
        "expiresAt": (ent or {}).get("expiresAt"),
        "maxDevices": int(payload.get("maxDevices") or (license_row or {}).get("maxDevices") or 3),
        "deviceCount": int(payload.get("deviceCount") or 0),
        "deviceBound": bool(device_bound),
        "deviceError": device_error,
        "refreshedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def refresh_seat(
    cfg: Dict[str, Any],
    *,
    timeout: float = 12.0,
    app_version: str = "",
    host_ver: str = "",
    label: str = "Blender",
) -> SeatRefreshResult:
    """Blocking HTTP. Call from a background thread only."""
    api_base = api_base_from_config(cfg)
    addon_key = account_addon_key(cfg)
    if not api_base:
        return SeatRefreshResult(ok=True, skipped=True, reason="missing_api_base")
    if not addon_key:
        return SeatRefreshResult(ok=True, skipped=True, reason="missing_addon_key")
    user_id = ensure_account_user_id(cfg)
    if not user_id:
        return SeatRefreshResult(
            ok=False,
            error="Could not resolve account from API key.",
            seat=cached_seat(cfg),
        )
    installation_id = ensure_installation_id(cfg)
    seat_url = resolve_seat_url(api_base)
    device_bound = False
    device_error = ""
    try:
        result = _http_json(
            resolve_activate_url(api_base),
            method="POST",
            body={
                "userId": user_id,
                "productId": PRODUCT_ID,
                "installationId": installation_id,
                "label": label,
                "platform": detect_platform(),
                "appVersion": app_version or ADDON_VERSION,
                "hostVersion": host_ver or host_version(),
                "addonKey": addon_key,
            },
            timeout=timeout,
            cfg=cfg,
        )
        device_bound = bool(result.get("success"))
        if not device_bound:
            device_error = str(result.get("error") or "Device not bound")
    except urllib.error.HTTPError as exc:
        device_error = str(getattr(exc, "reason", None) or exc)
    except Exception as exc:  # noqa: BLE001
        device_error = str(exc)

    query = urllib.parse.urlencode(
        {"productId": PRODUCT_ID, "installationId": installation_id}
    )
    try:
        payload = _http_json(f"{seat_url}?{query}", timeout=timeout, cfg=cfg)
    except Exception as exc:  # noqa: BLE001
        return SeatRefreshResult(ok=False, error=str(exc), seat=cached_seat(cfg))

    seat = seat_from_payload(payload, device_bound=device_bound, device_error=device_error)
    cfg.setdefault("account", {})["seat"] = seat
    cfg["account"]["installation_id"] = installation_id
    return SeatRefreshResult(ok=True, seat=seat, device_error=device_error)


_on_seat_saved = None


def set_seat_saved_hook(callback) -> None:
    """Optional UI hook after a seat is persisted (main thread)."""
    global _on_seat_saved
    _on_seat_saved = callback


def persist_refreshed_config(cfg: Dict[str, Any]) -> None:
    from . import preferences

    preferences.save_prefs_config(cfg)


def schedule_seat_refresh(cfg: Optional[Dict[str, Any]] = None) -> bool:
    from . import preferences
    from . import tasks_queue

    try:
        cfg = cfg or preferences.prefs_to_config()
    except Exception:
        return False
    if not account_ready_for_api(cfg):
        return False

    def _work() -> None:
        result = refresh_seat(cfg)
        seat_to_save = None
        if result.ok and not result.skipped:
            seat_to_save = result.seat
        elif not result.ok:
            seat_to_save = locked_seat_after_failed_refresh(cfg, result)
        if seat_to_save is None:
            return

        def _save() -> None:
            try:
                live = preferences.prefs_to_config()
                live.setdefault("account", {})
                acct = cfg.get("account") if isinstance(cfg.get("account"), dict) else {}
                live["account"]["user_id"] = str(acct.get("user_id") or "").strip()
                live["account"]["addon_key"] = str(acct.get("addon_key") or "").strip()
                live["account"]["installation_id"] = acct.get("installation_id")
                live["account"]["seat"] = seat_to_save
                preferences.save_prefs_config(live, skip_remote_sync=True)
                hook = _on_seat_saved
                if hook:
                    hook(live)
            except Exception:
                pass

        tasks_queue.add_task(_save)

    tasks_queue.run_in_background(_work)
    return True
