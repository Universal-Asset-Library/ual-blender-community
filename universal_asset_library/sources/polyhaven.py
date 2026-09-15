# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""Poly Haven connector."""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse
import urllib.request
from typing import Dict, List, Optional

from ..download_utils import stream_download_file
from ..http_ssl import urlopen
from .base import AssetResult, AssetSource, SearchResults

API_BASE = "https://api.polyhaven.com"
USER_AGENT = "UniversalAssetLibrary-Blender/0.1"

TYPE_MAP = {"0": "hdri", "1": "texture", "2": "mesh"}

_CATALOG: Optional[dict] = None
_CATALOG_AT = 0.0
_CATALOG_TTL = 10 * 60
_CATALOG_LOCK = threading.Lock()


def _assets_catalog() -> dict:
    """Memoize GET /assets — Type/query changes stay client-side after first fetch."""
    global _CATALOG, _CATALOG_AT
    now = time.time()
    with _CATALOG_LOCK:
        if _CATALOG is not None and (now - _CATALOG_AT) < _CATALOG_TTL:
            return _CATALOG
    data = _http_json(f"{API_BASE}/assets")
    if isinstance(data, dict) and data:
        with _CATALOG_LOCK:
            _CATALOG = data
            _CATALOG_AT = time.time()
        return data
    with _CATALOG_LOCK:
        return _CATALOG if isinstance(_CATALOG, dict) else {}


def _http_json(url: str, params: Optional[Dict[str, str]] = None) -> dict:
    query = urllib.parse.urlencode(params or {})
    full = f"{url}?{query}" if query else url
    req = urllib.request.Request(full, headers={"User-Agent": USER_AGENT})
    with urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8", errors="ignore")
    return json.loads(raw) if raw else {}


class PolyHavenSource(AssetSource):
    source_id = "polyhaven"
    display_name = "Poly Haven"

    def search(self, query: str, category: Optional[str] = None, page: int = 1, page_size: int = 50, **kwargs) -> SearchResults:
        from ..search_text import (
            asset_matches_query,
            normalize_search_query,
            sort_assets_by_query,
        )

        data = _assets_catalog()
        items: List[AssetResult] = []
        q = normalize_search_query(query)
        for asset_id, meta in (data or {}).items():
            if not isinstance(meta, dict):
                continue
            name = str(meta.get("name") or asset_id)
            tags = list(meta.get("tags") or [])
            cats = list(meta.get("categories") or [])
            if q and not asset_matches_query(
                q, name=name, asset_id=asset_id, tags=tags, categories=cats
            ):
                continue
            atype = TYPE_MAP.get(str(meta.get("type")), "mesh")
            cat = str(category or "").strip().lower()
            if cat in ("hdris", "hdri") and atype != "hdri":
                continue
            if cat in ("models", "mesh", "model") and atype != "mesh":
                continue
            if cat in ("textures", "texture", "material", "materials") and atype not in (
                "texture",
                "material",
                "material_set",
            ):
                continue
            # Bunny CDN optimizer — sized thumbs stay under the download cap
            # (full /thumbs/{id}.png is often >1–2 MiB and was silently rejected).
            thumb = (
                f"https://cdn.polyhaven.com/asset_img/thumbs/{asset_id}.png"
                f"?width=512&height=512"
            )
            items.append(
                AssetResult(
                    source_id=self.source_id,
                    asset_id=asset_id,
                    name=name,
                    asset_type=atype,
                    thumb_url=thumb,
                    license_name="CC0",
                    categories=cats,
                    tags=tags,
                    resolutions=["1k", "2k", "4k", "8k"],
                    formats=["gltf", "hdr", "jpg", "png"] if atype != "mesh" else ["gltf", "fbx", "blend"],
                )
            )
        if q:
            items = sort_assets_by_query(items, q)
        start = max(0, (page - 1) * page_size)
        page_items = items[start : start + page_size]
        return SearchResults(items=page_items, has_more=start + page_size < len(items), total_count=len(items))

    def download(self, asset: AssetResult, resolution: Optional[str], fmt: Optional[str], dest_folder: str, **kwargs) -> str:
        os.makedirs(dest_folder, exist_ok=True)
        files = _http_json(f"{API_BASE}/files/{asset.asset_id}")
        res = resolution or "1k"
        fmt = (fmt or "").lower()
        # HDRI
        if asset.asset_type == "hdri":
            block = (files.get("hdri") or {}).get(res) or {}
            entry = block.get("hdr") or block.get("exr") or next(iter(block.values()), None)
            if isinstance(entry, dict):
                url = entry.get("url")
                if url:
                    out = os.path.join(dest_folder, os.path.basename(urllib.parse.urlparse(url).path) or f"{asset.asset_id}.hdr")
                    stream_download_file(url, out)
                    return out
        # Texture maps
        if asset.asset_type == "texture":
            for map_name, resolutions in (files or {}).items():
                if not isinstance(resolutions, dict):
                    continue
                res_block = resolutions.get(res) or next(iter(resolutions.values()), {})
                if not isinstance(res_block, dict):
                    continue
                # prefer png/jpg
                for key in ("png", "jpg", "jpeg", "exr"):
                    entry = res_block.get(key)
                    if isinstance(entry, dict) and entry.get("url"):
                        url = entry["url"]
                        out = os.path.join(dest_folder, os.path.basename(urllib.parse.urlparse(url).path))
                        stream_download_file(url, out)
                        break
            return dest_folder
        # Mesh — gltf package
        gltf = (files.get("gltf") or {}).get(res) or (files.get("gltf") or {}).get("1k")
        if isinstance(gltf, dict):
            url = (gltf.get("gltf") or gltf.get("url") or {}).get("url") if isinstance(gltf.get("gltf"), dict) else gltf.get("url")
            # Poly Haven structure: files["gltf"]["1k"]["gltf"] = {url, include:{...}}
            node = None
            for _k, v in (files.get("gltf") or {}).items():
                if isinstance(v, dict) and "gltf" in v:
                    node = v["gltf"]
                    break
            if isinstance(node, dict) and node.get("url"):
                main = node["url"]
                out_main = os.path.join(
                    dest_folder,
                    os.path.basename(urllib.parse.urlparse(main).path) or f"{asset.asset_id}.gltf",
                )
                stream_download_file(main, out_main)
                includes = node.get("include") or {}
                for rel, meta in includes.items():
                    if isinstance(meta, dict) and meta.get("url"):
                        target = os.path.normpath(os.path.join(dest_folder, rel))
                        os.makedirs(os.path.dirname(target), exist_ok=True)
                        if os.path.commonpath([dest_folder, target]) == os.path.normpath(dest_folder):
                            stream_download_file(meta["url"], target)
                # Return the package folder so staging keeps glTF bins/textures together
                return dest_folder
        raise RuntimeError(f"No downloadable files for Poly Haven asset {asset.asset_id}")
