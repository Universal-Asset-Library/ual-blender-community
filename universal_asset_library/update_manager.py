# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""Cached update-check state for Preferences UI (mirrors online_health)."""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from .release_client import UpdateCheckResult

_STATE: Dict[str, Any] = {
    "checking": False,
    "installing": False,
    "checked_at": 0.0,
    "result": None,  # dict snapshot of UpdateCheckResult
    "summary": "",
}


def get_cached_state() -> Dict[str, Any]:
    return {
        "checking": bool(_STATE.get("checking")),
        "installing": bool(_STATE.get("installing")),
        "checked_at": float(_STATE.get("checked_at") or 0),
        "result": dict(_STATE["result"]) if isinstance(_STATE.get("result"), dict) else None,
        "summary": str(_STATE.get("summary") or ""),
    }


def clear_cached_state() -> None:
    _STATE["checking"] = False
    _STATE["installing"] = False
    _STATE["checked_at"] = 0.0
    _STATE["result"] = None
    _STATE["summary"] = ""


def set_checking(active: bool) -> None:
    _STATE["checking"] = bool(active)


def store_result(result: UpdateCheckResult) -> None:
    _STATE["checking"] = False
    _STATE["checked_at"] = time.time()
    _STATE["result"] = {
        "product": result.product,
        "channel": result.channel,
        "current_version": result.current_version,
        "latest_version": result.latest_version,
        "update_available": result.update_available,
        "download_available": result.download_available,
        "force_update": result.force_update,
        "message": result.message,
        "release_notes": result.release_notes,
        "download_page_url": result.download_page_url,
        "ok": result.ok,
        "error": result.error,
        "artifact_url": result.artifact_url,
        "artifact_filename": result.artifact_filename,
        "artifact_sha256": result.artifact_sha256 or result.release_sha256,
    }
    if not result.ok:
        _STATE["summary"] = result.message
    elif result.force_update or result.update_available:
        _STATE["summary"] = result.message or f"Update available: {result.latest_version}"
    else:
        _STATE["summary"] = result.message or "Up to date"


def set_installing(active: bool) -> None:
    _STATE["installing"] = bool(active)


def is_installing() -> bool:
    return bool(_STATE.get("installing"))


def set_summary(text: str) -> None:
    _STATE["summary"] = str(text or "")


def summarize_cached() -> str:
    return str(_STATE.get("summary") or "")


def update_install_offered(state: Optional[Dict[str, Any]] = None) -> bool:
    """True when cached check found a newer version and install may proceed."""
    st = state if isinstance(state, dict) else get_cached_state()
    if st.get("checking") or st.get("installing"):
        return False
    result = st.get("result")
    if not isinstance(result, dict) or not result.get("ok"):
        return False
    return bool(result.get("update_available"))


DEFAULT_AUTO_CHECK_INTERVAL_SEC = 6 * 3600  # 6 hours
_auto_check_scheduled_at = 0.0


def update_check_is_stale(min_interval_sec: float = DEFAULT_AUTO_CHECK_INTERVAL_SEC) -> bool:
    checked = float(_STATE.get("checked_at") or 0)
    if checked <= 0:
        return True
    return (time.time() - checked) >= float(min_interval_sec)


def maybe_schedule_update_check(
    *,
    min_interval_sec: float = DEFAULT_AUTO_CHECK_INTERVAL_SEC,
) -> bool:
    """Background Check for Updates at most once per interval (no modal / no toast spam)."""
    global _auto_check_scheduled_at
    if _STATE.get("checking") or _STATE.get("installing"):
        return False
    if not update_check_is_stale(min_interval_sec):
        return False
    now = time.time()
    if now - float(_auto_check_scheduled_at or 0) < 30.0:
        return False
    try:
        import bpy
    except Exception:
        return False
    if not getattr(bpy.app, "online_access", True):
        return False

    try:
        from . import preferences
        from . import release_client
        from . import tasks_queue
        from .ui_strings import ADDON_VERSION
    except Exception:
        return False

    prefs = preferences._get_prefs()
    api_base = ""
    channel = "stable"
    if prefs:
        api_base = str(getattr(prefs, "releases_api_base_url", "") or "").strip()
        channel = str(getattr(prefs, "release_channel", "stable") or "stable")
    if not api_base:
        try:
            from . import config as ual_config

            api_base = str(ual_config.DEFAULT_UPDATES_API_BASE or "").strip()
        except Exception:
            return False
    if not api_base:
        return False

    _auto_check_scheduled_at = now
    set_checking(True)

    def work() -> None:
        result = None
        err = ""
        try:
            result = release_client.check_for_update(api_base, ADDON_VERSION, channel)
        except Exception as exc:  # noqa: BLE001
            err = str(exc)

        def done() -> None:
            if err:
                set_checking(False)
                set_summary(err[:120])
            elif result is not None:
                store_result(result)
            try:
                for window in bpy.context.window_manager.windows:
                    for area in window.screen.areas:
                        area.tag_redraw()
            except Exception:
                pass

        try:
            tasks_queue.add_task(done)
        except Exception:
            done()

    try:
        tasks_queue.run_in_background(work)
        return True
    except Exception:
        set_checking(False)
        return False
