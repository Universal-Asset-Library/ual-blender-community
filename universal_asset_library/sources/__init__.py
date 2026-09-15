# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""Source registry — singleton instances so in-memory enrich/search caches stick."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Type

from .base import AssetResult, AssetSource, SearchResults

__all__ = [
    "AssetResult",
    "AssetSource",
    "SearchResults",
    "get_source",
    "get_configured_sources",
    "list_source_ids",
    "clear_source_singletons",
]

_INSTANCES: Dict[str, AssetSource] = {}


def _registry() -> Dict[str, Type[AssetSource]]:
    from . import (
        ambientcg,
        gpuopen,
        lazytextures,
        pbrpx,
        polyhaven,
        polypizza,
        sketchfab,
        threedscans,
    )

    registry: Dict[str, Type[AssetSource]] = {
        "polyhaven": polyhaven.PolyHavenSource,
        "ambientcg": ambientcg.AmbientCGSource,
        "gpuopen": gpuopen.GPUOpenSource,
        "lazytextures": lazytextures.LazyTexturesSource,
        "threedscans": threedscans.ThreeDScansSource,
        "pbrpx": pbrpx.PbrpxSource,
        "polypizza": polypizza.PolyPizzaSource,
        "sketchfab": sketchfab.SketchfabSource,
    }
    try:
        from . import fab

        registry["fab"] = fab.FabSource
    except ImportError:
        pass
    return registry


def get_source(source_id: str) -> AssetSource:
    """Return a process-wide singleton for *source_id* (Houdini get_configured_sources parity).

    Creating ``cls()`` on every call wiped Three D Scans ``_media_cache`` and forced
    N enrich HTTP round-trips per grid paint.
    """
    sid = str(source_id or "").strip().lower()
    existing = _INSTANCES.get(sid)
    if existing is not None:
        return existing
    registry = _registry()
    cls = registry.get(sid)
    if cls is None:
        raise KeyError(source_id)
    instance = cls()
    _INSTANCES[sid] = instance
    return instance


def get_configured_sources(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, AssetSource]:
    """Return enabled online connectors from settings."""
    from .. import config as ual_config

    normalized = ual_config.coerce_config(cfg)
    online = normalized.get("online") if isinstance(normalized.get("online"), dict) else {}
    enabled = list(online.get("enabled_sources") or list_source_ids())
    configured: Dict[str, AssetSource] = {}
    for source_id in enabled:
        sid = str(source_id or "").strip().lower()
        if not sid:
            continue
        try:
            configured[sid] = get_source(sid)
        except KeyError:
            continue
    return configured


def clear_source_singletons() -> None:
    """Test helper — drop cached source instances."""
    _INSTANCES.clear()


def list_source_ids() -> List[str]:
    ids = [
        "polyhaven",
        "ambientcg",
        "gpuopen",
        "lazytextures",
        "threedscans",
        "pbrpx",
        "polypizza",
        "sketchfab",
    ]
    if "fab" in _registry():
        ids.append("fab")
    return ids
