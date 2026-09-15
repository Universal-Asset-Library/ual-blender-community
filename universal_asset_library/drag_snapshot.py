# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""Immutable drag/drop asset snapshots (pure — no bpy).

Capture identity + download options at gesture start so async search/filter
cannot retarget Import/Drop to a different list index.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Iterable, Mapping, MutableMapping, Optional

from .selection_keys import key_for_library_item, key_for_online_item, online_key


def snapshot_library_item(item: Any) -> Dict[str, Any]:
    """Freeze library row fields needed for Drop (no RNA after return)."""
    path = str(getattr(item, "path", "") or "")
    return {
        "kind": "library",
        "key": key_for_library_item(item),
        "path": path,
        "name": str(getattr(item, "name", "") or "") or os.path.basename(path) or "Asset",
        "asset_type": str(getattr(item, "asset_type", "") or "mesh"),
        "icon_id": int(getattr(item, "preview_icon_id", 0) or 0),
        "source_id": str(getattr(item, "source_id", "") or "local"),
        "asset_id": "",
        "formats": [],
        "resolutions": [],
        "license_name": "",
        "thumb_url": "",
        "thumb_path": str(getattr(item, "thumb_path", "") or ""),
    }


def snapshot_online_item(item: Any, *, source_fallback: str = "") -> Dict[str, Any]:
    """Freeze online row fields for download + Drop."""
    source_id = str(getattr(item, "source_id", "") or source_fallback or "").strip()
    asset_id = str(getattr(item, "asset_id", "") or "").strip()
    formats_csv = str(getattr(item, "formats", "") or "")
    resolutions_csv = str(getattr(item, "resolutions", "") or "")
    formats = [f for f in formats_csv.split(",") if f]
    resolutions = [r for r in resolutions_csv.split(",") if r]
    return {
        "kind": "online",
        "key": key_for_online_item(item)
        if (getattr(item, "source_id", None) or getattr(item, "asset_id", None))
        else online_key(source_id, asset_id),
        "path": "",
        "name": str(getattr(item, "name", "") or "") or asset_id or "Online asset",
        "asset_type": str(getattr(item, "asset_type", "") or "mesh"),
        "icon_id": int(getattr(item, "preview_icon_id", 0) or 0),
        "source_id": source_id,
        "asset_id": asset_id,
        "formats": formats,
        "resolutions": resolutions,
        "license_name": str(getattr(item, "license_name", "") or ""),
        "thumb_url": str(getattr(item, "thumb_url", "") or ""),
        "thumb_path": str(getattr(item, "thumb_path", "") or ""),
    }


def snapshot_is_complete(snap: Mapping[str, Any]) -> bool:
    kind = str(snap.get("kind") or "")
    key = str(snap.get("key") or "")
    if not key:
        return False
    if kind == "library":
        return bool(str(snap.get("path") or "").strip())
    if kind == "online":
        return bool(str(snap.get("source_id") or "").strip() and str(snap.get("asset_id") or "").strip())
    return False


def library_path_ready_for_import(path: str) -> bool:
    """True when a local path still exists as a file or package folder."""
    p = os.path.normpath(str(path or "").strip())
    if not p:
        return False
    return os.path.isfile(p) or os.path.isdir(p)


def online_key_still_in_results(results: Iterable[Any], key: str) -> bool:
    """True if *key* is still present in the live Online results collection."""
    want = str(key or "")
    if not want:
        return False
    for item in results or ():
        if key_for_online_item(item) == want:
            return True
    return False


def mouse_in_rect(
    mouse_x: float,
    mouse_y: float,
    *,
    x: float,
    y: float,
    width: float,
    height: float,
) -> bool:
    """Inclusive-start exclusive-end hit test (window/region pixels)."""
    return (
        float(x) <= float(mouse_x) < float(x) + float(width)
        and float(y) <= float(mouse_y) < float(y) + float(height)
    )


def drop_target_is_eligible_view3d(
    *,
    area_type: str,
    region_type: str,
    mouse_x: float,
    mouse_y: float,
    area_x: float,
    area_y: float,
    area_width: float,
    area_height: float,
    region_x: float = 0.0,
    region_y: float = 0.0,
    region_width: float = 0.0,
    region_height: float = 0.0,
) -> bool:
    """True when the cursor is over a VIEW_3D WINDOW region (strict — no fallback)."""
    if str(area_type or "") != "VIEW_3D":
        return False
    if str(region_type or "") != "WINDOW":
        return False
    if not mouse_in_rect(
        mouse_x,
        mouse_y,
        x=area_x,
        y=area_y,
        width=area_width,
        height=area_height,
    ):
        return False
    # Region.x / Region.y are window-relative (Blender API) — do not also subtract area.
    rx = float(mouse_x) - float(region_x)
    ry = float(mouse_y) - float(region_y)
    if region_width <= 0 or region_height <= 0:
        return True
    # Inclusive start, exclusive end — matches ``mouse_in_rect`` / GPU ghost gate
    return 0.0 <= rx < float(region_width) and 0.0 <= ry < float(region_height)


def apply_snap_to_operator_state(op: MutableMapping[str, Any], snap: Mapping[str, Any]) -> None:
    """Copy snapshot fields onto a drag operator-like mapping (tests / helpers)."""
    op["asset_key"] = str(snap.get("key") or "")
    op["asset_name"] = str(snap.get("name") or "")
    op["asset_type"] = str(snap.get("asset_type") or "")
    op["local_path"] = str(snap.get("path") or "")
    op["source_id"] = str(snap.get("source_id") or "")
    op["asset_id"] = str(snap.get("asset_id") or "")
