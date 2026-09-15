# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""Online connector credentials — separate from settings.json.

Stored at ``{cache}/config/online_secrets.json``. Sketchfab / Poly Pizza /
Fab session cookies and Epic OAuth tokens are never written back into
``settings.json``.
"""

from __future__ import annotations

import copy
import json
import os
from typing import Any, Dict, Optional, Tuple

_SECRETS_FILENAME = "online_secrets.json"

_SECRET_KEYS = (
    "sketchfab_token",
    "polypizza_api_key",
    "fab_sessionid",
    "fab_csrftoken",
    "epic_auth",
)


def secrets_path(base_dir: Optional[str] = None) -> str:
    from .config import settings_json_path

    return os.path.join(os.path.dirname(settings_json_path(base_dir)), _SECRETS_FILENAME)


def _empty_secrets() -> Dict[str, Any]:
    return {
        "sketchfab_token": "",
        "polypizza_api_key": "",
        "fab_sessionid": "",
        "fab_csrftoken": "",
        "epic_auth": {
            "access_token": "",
            "refresh_token": "",
            "token_type": "bearer",
            "account_id": "",
            "display_name": "",
            "expires_at": 0.0,
        },
    }


def _normalize_epic_auth(raw: Any) -> Dict[str, Any]:
    epic = raw if isinstance(raw, dict) else {}
    try:
        expires_at = float(epic.get("expires_at") or 0)
    except (TypeError, ValueError):
        expires_at = 0.0
    return {
        "access_token": str(epic.get("access_token", "") or ""),
        "refresh_token": str(epic.get("refresh_token", "") or ""),
        "token_type": str(epic.get("token_type", "bearer") or "bearer"),
        "account_id": str(epic.get("account_id", "") or ""),
        "display_name": str(epic.get("display_name", "") or ""),
        "expires_at": expires_at,
    }


def _epic_auth_has_secrets(epic: Dict[str, Any]) -> bool:
    return bool(
        str(epic.get("access_token") or "").strip()
        or str(epic.get("refresh_token") or "").strip()
    )


def load_online_secrets(base_dir: Optional[str] = None) -> Dict[str, Any]:
    data = _empty_secrets()
    path = secrets_path(base_dir)
    if not os.path.isfile(path):
        return data
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, json.JSONDecodeError, TypeError):
        return data
    if not isinstance(raw, dict):
        return data
    data["sketchfab_token"] = str(raw.get("sketchfab_token") or "").strip()
    data["polypizza_api_key"] = str(raw.get("polypizza_api_key") or "").strip()
    data["fab_sessionid"] = str(raw.get("fab_sessionid") or "").strip()
    data["fab_csrftoken"] = str(raw.get("fab_csrftoken") or "").strip()
    data["epic_auth"] = _normalize_epic_auth(raw.get("epic_auth"))
    return data


def _restrict_secrets_file_permissions(path: str) -> None:
    if os.name == "posix" and os.path.isfile(path):
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def save_online_secrets(
    secrets: Dict[str, Any],
    base_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Write secrets dict (only known fields). Returns normalized copy."""
    current = load_online_secrets(base_dir)
    out = _empty_secrets()
    for key in _SECRET_KEYS:
        if key not in secrets:
            if key == "epic_auth":
                out[key] = copy.deepcopy(current.get(key) or _empty_secrets()["epic_auth"])
            else:
                out[key] = str(current.get(key) or "").strip()
            continue
        if key == "epic_auth":
            out[key] = _normalize_epic_auth(secrets.get(key))
        else:
            out[key] = str(secrets.get(key) or "").strip()

    path = secrets_path(base_dir)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(out, handle, indent=2)
        handle.write("\n")
    os.replace(tmp, path)
    _restrict_secrets_file_permissions(path)
    return out


def extract_secrets_from_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Read inline connector secrets from a normalized config dict."""
    online = cfg.get("online") if isinstance(cfg.get("online"), dict) else {}
    return {
        "sketchfab_token": str(online.get("sketchfab_token") or "").strip(),
        "polypizza_api_key": str(online.get("polypizza_api_key") or "").strip(),
        "fab_sessionid": str(online.get("fab_sessionid") or "").strip(),
        "fab_csrftoken": str(online.get("fab_csrftoken") or "").strip(),
        "epic_auth": _normalize_epic_auth(cfg.get("epic_auth")),
    }


def _secrets_payload_nonempty(secrets: Dict[str, Any]) -> bool:
    if str(secrets.get("sketchfab_token") or "").strip():
        return True
    if str(secrets.get("polypizza_api_key") or "").strip():
        return True
    if str(secrets.get("fab_sessionid") or "").strip():
        return True
    if str(secrets.get("fab_csrftoken") or "").strip():
        return True
    if _epic_auth_has_secrets(secrets.get("epic_auth") or {}):
        return True
    return False


def persist_secrets_from_config(
    cfg: Dict[str, Any],
    base_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Merge connector secrets from *cfg* into online_secrets.json."""
    inline = extract_secrets_from_config(cfg)
    if not _secrets_payload_nonempty(inline):
        return load_online_secrets(base_dir)
    return save_online_secrets(inline, base_dir)


def strip_secrets_for_disk(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy safe to write into settings.json (secrets cleared)."""
    disk = copy.deepcopy(cfg)
    online = disk.setdefault("online", {})
    if isinstance(online, dict):
        online["sketchfab_token"] = ""
        online["polypizza_api_key"] = ""
        online["fab_sessionid"] = ""
        online["fab_csrftoken"] = ""
    disk["epic_auth"] = _empty_secrets()["epic_auth"]
    return disk


def apply_online_secrets(
    cfg: Dict[str, Any],
    base_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Inject secrets file values into *cfg* (in-memory use)."""
    secrets = load_online_secrets(base_dir)
    online = cfg.setdefault("online", {})
    if isinstance(online, dict):
        online["sketchfab_token"] = str(secrets.get("sketchfab_token") or "").strip()
        online["polypizza_api_key"] = str(secrets.get("polypizza_api_key") or "").strip()
        online["fab_sessionid"] = str(secrets.get("fab_sessionid") or "").strip()
        online["fab_csrftoken"] = str(secrets.get("fab_csrftoken") or "").strip()
    cfg["epic_auth"] = copy.deepcopy(secrets.get("epic_auth") or _empty_secrets()["epic_auth"])
    return cfg


def migrate_online_secrets_from_settings(
    cfg: Dict[str, Any],
    base_dir: Optional[str] = None,
) -> Tuple[Dict[str, Any], bool]:
    """Move legacy plaintext tokens from settings.json into online_secrets.json."""
    inline = extract_secrets_from_config(cfg)
    if not _secrets_payload_nonempty(inline):
        return cfg, False

    current = load_online_secrets(base_dir)
    merged = copy.deepcopy(current)
    changed = False
    for key in ("sketchfab_token", "polypizza_api_key", "fab_sessionid", "fab_csrftoken"):
        value = str(inline.get(key) or "").strip()
        if value and str(merged.get(key) or "").strip() != value:
            merged[key] = value
            changed = True
    inline_epic = _normalize_epic_auth(inline.get("epic_auth"))
    if _epic_auth_has_secrets(inline_epic):
        if merged.get("epic_auth") != inline_epic:
            merged["epic_auth"] = inline_epic
            changed = True

    if changed or not os.path.isfile(secrets_path(base_dir)):
        save_online_secrets(merged, base_dir)
    return cfg, True
