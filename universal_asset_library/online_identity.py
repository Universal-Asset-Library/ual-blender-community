# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""Detect Fab / Megascans identity for gated import behavior."""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

_FAB_SEGMENTS = frozenset({"fab", "megascans", "quixel"})


def _path_segments(path: str) -> set:
    parts = os.path.normpath(path).replace("\\", "/").lower().split("/")
    return set(parts)


def has_fab_path_segment(path: str) -> bool:
    return bool(_path_segments(path) & _FAB_SEGMENTS)


def read_fab_sidecar(folder: str) -> Optional[Dict[str, Any]]:
    for name in ("fab_import.json", ".assetlibrary.bundle.json"):
        path = os.path.join(folder, name)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            return data
    return None


def is_fab_or_megascans_asset(path: str, cfg: Optional[Dict[str, Any]] = None) -> bool:
    """True only for Fab/Megascans — never Sketchfab/Poly Haven/etc."""
    del cfg  # reserved for future overrides
    if not path:
        return False
    if has_fab_path_segment(path):
        return True
    folder = path if os.path.isdir(path) else os.path.dirname(path)
    sidecar = read_fab_sidecar(folder)
    if not sidecar:
        return False
    source = str(sidecar.get("source_id") or sidecar.get("source") or "").lower()
    if source in {"fab", "megascans", "quixel"}:
        return True
    if sidecar.get("fab_uid") or sidecar.get("listing_id"):
        return True
    return False
