# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""Online platform status + connectivity probes (Houdini Health Check parity).

Pure helpers + last-result cache for Preferences UI. HTTP runs off the main
thread via ``tasks_queue``; never call ``probe_all_platforms`` on the UI thread.
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import ui_strings
from .http_ssl import urlopen

# status: disabled | need_auth | ok | warn | fail | unknown | checking
USER_AGENT = "UniversalAssetLibrary-Blender/0.3 HealthCheck"

# Lightweight GET probes — same family as Houdini main_panel health worker
PROBE_ENDPOINTS: Dict[str, Tuple[str, str]] = {
    "polyhaven": ("https://api.polyhaven.com/assets?type=models", "Poly Haven API"),
    "ambientcg": ("https://ambientcg.com/api/v2/full_json?limit=1", "ambientCG API"),
    "gpuopen": ("https://api.matlib.gpuopen.com/api/materials/?limit=1", "GPUOpen API"),
    "lazytextures": ("https://lazytextures.net:3000/total-assets", "LazyTextures API"),
    "threedscans": (
        "https://threedscans.com/wp-json/wp/v2/posts?per_page=1",
        "Three D Scans API",
    ),
    "pbrpx": ("https://api.pbrpx.com/assets?pageSize=1", "PBRPX API"),
    "fab": (
        "https://www.fab.com/i/listings/search?count=1&is_free=1&seller=Quixel%20Megascans",
        "Fab API",
    ),
}

SOURCE_ORDER = (
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

_STATE: Dict[str, Any] = {
    "checking": False,
    "checked_at": 0.0,
    "rows": [],  # list[dict]
    "summary": "",
}


@dataclass
class PlatformRow:
    source_id: str
    label: str
    enabled: bool = True
    auth_required: bool = False
    auth_ok: bool = True
    status: str = "unknown"  # disabled|need_auth|ok|warn|fail|unknown|checking
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def get_cached_state() -> Dict[str, Any]:
    return {
        "checking": bool(_STATE.get("checking")),
        "checked_at": float(_STATE.get("checked_at") or 0),
        "rows": list(_STATE.get("rows") or []),
        "summary": str(_STATE.get("summary") or ""),
    }


def clear_cached_state() -> None:
    """Drop platform probe results (Memory / Clear Cache)."""
    _STATE["checking"] = False
    _STATE["checked_at"] = 0.0
    _STATE["rows"] = []
    _STATE["summary"] = ""


def set_checking(active: bool) -> None:
    _STATE["checking"] = bool(active)


def store_results(rows: Sequence[PlatformRow], summary: str = "") -> None:
    _STATE["checking"] = False
    _STATE["checked_at"] = time.time()
    _STATE["rows"] = [r.to_dict() if isinstance(r, PlatformRow) else dict(r) for r in rows]
    _STATE["summary"] = summary or summarize_rows(rows)


def summarize_rows(rows: Sequence[Any]) -> str:
    counts = {"ok": 0, "warn": 0, "fail": 0, "need_auth": 0, "disabled": 0, "unknown": 0}
    for raw in rows:
        row = raw if isinstance(raw, dict) else (raw.to_dict() if hasattr(raw, "to_dict") else {})
        st = str(row.get("status") or "unknown")
        if st in counts:
            counts[st] += 1
        elif st == "checking":
            counts["unknown"] += 1
        else:
            counts["unknown"] += 1
    parts = []
    if counts["ok"]:
        parts.append(f"{counts['ok']} online")
    if counts["need_auth"]:
        parts.append(f"{counts['need_auth']} need sign-in")
    if counts["warn"]:
        parts.append(f"{counts['warn']} warnings")
    if counts["fail"]:
        parts.append(f"{counts['fail']} offline")
    if counts["disabled"]:
        parts.append(f"{counts['disabled']} turned off")
    if counts["unknown"] and not parts:
        parts.append("not checked yet")
    return " · ".join(parts) if parts else "Ready"


def blender_icon_for_status(status: str) -> str:
    return {
        "ok": "CHECKMARK",
        "warn": "ERROR",
        "fail": "CANCEL",
        "need_auth": "LOCKED",
        "disabled": "CHECKBOX_DEHLT",
        "checking": "TIME",
        "unknown": "QUESTION",
    }.get((status or "").strip().lower(), "QUESTION")


def build_local_rows(
    cfg: Optional[Dict[str, Any]],
    *,
    enabled_sources: Optional[Sequence[str]] = None,
) -> List[PlatformRow]:
    """Auth/enable indicators without HTTP (instant Preferences paint)."""
    raw = cfg or {}
    online = raw.get("online") if isinstance(raw.get("online"), dict) else {}
    if enabled_sources is None:
        enabled_sources = online.get("enabled_sources") or list(SOURCE_ORDER)
    enabled = {str(s).strip().lower() for s in enabled_sources if s}
    rows: List[PlatformRow] = []
    for sid in SOURCE_ORDER:
        label = SOURCE_LABELS.get(sid, sid)
        is_on = sid in enabled
        needs = ui_strings.source_needs_auth(sid)
        auth_ok = ui_strings.source_auth_configured(sid, raw) if needs else True
        if not is_on:
            rows.append(
                PlatformRow(
                    sid,
                    label,
                    enabled=False,
                    auth_required=needs,
                    auth_ok=auth_ok,
                    status="disabled",
                    detail="Disabled in Preferences",
                )
            )
            continue
        if needs and not auth_ok:
            if sid == "fab":
                # Browse works without Epic; downloads need sign-in
                rows.append(
                    PlatformRow(
                        sid,
                        label,
                        enabled=True,
                        auth_required=True,
                        auth_ok=False,
                        status="warn",
                        detail="Browse free — Sign In with Epic for downloads",
                    )
                )
                continue
            how = {
                "sketchfab": "Add API token below",
                "polypizza": "Add API key below",
            }.get(sid, "Configure auth")
            rows.append(
                PlatformRow(
                    sid,
                    label,
                    enabled=True,
                    auth_required=True,
                    auth_ok=False,
                    status="need_auth",
                    detail=how,
                )
            )
            continue
        # Prefer last connectivity result when available
        prev = _row_from_cache(sid)
        if prev and prev.get("status") in ("ok", "warn", "fail") and prev.get("detail"):
            rows.append(
                PlatformRow(
                    sid,
                    label,
                    enabled=True,
                    auth_required=needs,
                    auth_ok=auth_ok,
                    status=str(prev["status"]),
                    detail=str(prev.get("detail") or "Last check"),
                )
            )
        else:
            detail = "Auth OK — press Check Platforms" if needs else "Press Check Platforms"
            rows.append(
                PlatformRow(
                    sid,
                    label,
                    enabled=True,
                    auth_required=needs,
                    auth_ok=auth_ok,
                    status="unknown",
                    detail=detail,
                )
            )
    return rows


def _row_from_cache(source_id: str) -> Optional[Dict[str, Any]]:
    for row in _STATE.get("rows") or []:
        if isinstance(row, dict) and row.get("source_id") == source_id:
            return row
    return None


def _http_ok(url: str, *, headers: Optional[Dict[str, str]] = None, timeout: float = 12.0) -> Tuple[bool, str]:
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json,*/*"}
    if headers:
        hdrs.update(headers)
    request = urllib.request.Request(url, headers=hdrs)
    try:
        with urlopen(request, timeout=timeout) as response:
            code = int(getattr(response, "status", None) or response.getcode() or 0)
            if 200 <= code < 400:
                return True, f"HTTP {code}"
            return False, f"HTTP {code}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001
        from .ui_strings import friendly_error

        return False, friendly_error(exc)[:80]


def probe_all_platforms(
    cfg: Optional[Dict[str, Any]],
    *,
    enabled_sources: Optional[Sequence[str]] = None,
    online_access: bool = True,
) -> List[PlatformRow]:
    """Blocking network probes — call from a background thread only."""
    raw = cfg or {}
    online = raw.get("online") if isinstance(raw.get("online"), dict) else {}
    if enabled_sources is None:
        enabled_sources = online.get("enabled_sources") or list(SOURCE_ORDER)
    enabled = {str(s).strip().lower() for s in enabled_sources if s}

    if not online_access:
        return [
            PlatformRow(
                sid,
                SOURCE_LABELS.get(sid, sid),
                enabled=sid in enabled,
                auth_required=ui_strings.source_needs_auth(sid),
                auth_ok=ui_strings.source_auth_configured(sid, raw),
                status="fail" if sid in enabled else "disabled",
                detail="Blender online access disabled" if sid in enabled else "Disabled",
            )
            for sid in SOURCE_ORDER
        ]

    rows: List[PlatformRow] = []
    for sid in SOURCE_ORDER:
        label = SOURCE_LABELS.get(sid, sid)
        needs = ui_strings.source_needs_auth(sid)
        auth_ok = ui_strings.source_auth_configured(sid, raw) if needs else True
        if sid not in enabled:
            rows.append(
                PlatformRow(
                    sid,
                    label,
                    enabled=False,
                    auth_required=needs,
                    auth_ok=auth_ok,
                    status="disabled",
                    detail="Disabled",
                )
            )
            continue
        if needs and not auth_ok:
            # Fab browse is public — do not short-circuit to need_auth (probe catalog).
            if sid == "fab":
                url, _lab = PROBE_ENDPOINTS["fab"]
                ok, detail = _http_ok(url)
                rows.append(
                    PlatformRow(
                        sid,
                        label,
                        enabled=True,
                        auth_required=True,
                        auth_ok=False,
                        status="warn" if ok else "fail",
                        detail=(
                            "Browse OK — Sign In with Epic for downloads"
                            if ok
                            else detail
                        ),
                    )
                )
                continue
            rows.append(
                PlatformRow(
                    sid,
                    label,
                    enabled=True,
                    auth_required=True,
                    auth_ok=False,
                    status="need_auth",
                    detail={
                        "sketchfab": "Missing API token",
                        "polypizza": "Missing API key",
                    }.get(sid, "Missing credentials"),
                )
            )
            continue

        if sid == "sketchfab":
            token = str(online.get("sketchfab_token") or "").strip()
            ok, detail = _http_ok(
                "https://api.sketchfab.com/v3/me",
                headers={"Authorization": "Token {}".format(token)},
            )
            rows.append(
                PlatformRow(
                    sid,
                    label,
                    enabled=True,
                    auth_required=True,
                    auth_ok=True,
                    status="ok" if ok else "fail",
                    detail="Token accepted" if ok else detail,
                )
            )
            continue

        if sid == "polypizza":
            token = str(online.get("polypizza_api_key") or "").strip()
            ok, detail = _http_ok(
                "https://api.poly.pizza/v1.1/search?limit=1&page=0&Animated=0",
                headers={"X-Auth-Token": token},
            )
            rows.append(
                PlatformRow(
                    sid,
                    label,
                    enabled=True,
                    auth_required=True,
                    auth_ok=True,
                    status="ok" if ok else "fail",
                    detail="API key accepted" if ok else detail,
                )
            )
            continue

        if sid == "fab":
            # Public search + optional Epic session verify
            url, _lab = PROBE_ENDPOINTS["fab"]
            ok, detail = _http_ok(url)
            epic_note = ""
            try:
                from . import epic_oauth

                if epic_oauth.epic_session_configured(raw):
                    try:
                        epic_oauth.ensure_epic_session(epic_oauth.epic_session_from_config(raw))
                        epic_note = "; Epic sign-in OK"
                    except Exception as exc:  # noqa: BLE001
                        from .ui_strings import friendly_error

                        epic_note = f"; Epic: {friendly_error(exc)[:50]}"
                        ok = ok  # browse may still work
                        if ok:
                            rows.append(
                                PlatformRow(
                                    sid,
                                    label,
                                    enabled=True,
                                    auth_required=True,
                                    auth_ok=True,
                                    status="warn",
                                    detail=f"Browse OK{epic_note}",
                                )
                            )
                            continue
                elif str(online.get("fab_sessionid") or "").strip():
                    epic_note = "; browser cookies set"
            except Exception:
                pass
            rows.append(
                PlatformRow(
                    sid,
                    label,
                    enabled=True,
                    auth_required=True,
                    auth_ok=True,
                    status="ok" if ok else "fail",
                    detail=(("Browse OK" if ok else detail) + epic_note),
                )
            )
            continue

        url, _lab = PROBE_ENDPOINTS.get(sid, ("", ""))
        if not url:
            rows.append(
                PlatformRow(
                    sid,
                    label,
                    enabled=True,
                    status="unknown",
                    detail="No probe URL",
                )
            )
            continue
        ok, detail = _http_ok(
            url,
            headers={"App-Agent": "UniversalAssetLibrary/1.0"} if sid == "pbrpx" else None,
        )
        rows.append(
            PlatformRow(
                sid,
                label,
                enabled=True,
                auth_required=False,
                auth_ok=True,
                status="ok" if ok else "fail",
                detail="Online" if ok else detail,
            )
        )
    return rows


def rows_for_preferences_draw(
    cfg: Optional[Dict[str, Any]],
    *,
    enabled_sources: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    """Rows for Preferences: live check cache, else local auth snapshot."""
    state = get_cached_state()
    if state["checking"]:
        base = build_local_rows(cfg, enabled_sources=enabled_sources)
        out = []
        for row in base:
            d = row.to_dict()
            if d["status"] not in ("disabled", "need_auth"):
                d["status"] = "checking"
                d["detail"] = "Checking…"
            out.append(d)
        return out
    if state["rows"] and state["checked_at"]:
        # Refresh auth/disabled flags from current prefs while keeping probe detail
        local = {r.source_id: r for r in build_local_rows(cfg, enabled_sources=enabled_sources)}
        merged: List[Dict[str, Any]] = []
        for raw in state["rows"]:
            sid = str(raw.get("source_id") or "")
            loc = local.get(sid)
            if loc is None:
                merged.append(dict(raw))
                continue
            if loc.status in ("disabled", "need_auth"):
                merged.append(loc.to_dict())
            else:
                item = dict(raw)
                item["enabled"] = loc.enabled
                item["auth_required"] = loc.auth_required
                item["auth_ok"] = loc.auth_ok
                merged.append(item)
        return merged
    return [r.to_dict() for r in build_local_rows(cfg, enabled_sources=enabled_sources)]
