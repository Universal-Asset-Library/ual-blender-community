# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""PBRPX source connector."""

from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from collections import OrderedDict
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..download_utils import download_urls_parallel, stream_download_file
from ..http_ssl import urlopen
from ..lru_util import SOURCE_DETAIL_CACHE_MAX, lru_put
from .base import AssetResult, AssetSource, SearchResults

API_BASE = "https://api.pbrpx.com"
USER_AGENT = "UniversalAssetLibrary-Blender/0.1"
APP_AGENT = "UniversalAssetLibrary-Blender/0.1"
REQUEST_HEADERS = {
    "User-Agent": USER_AGENT,
    "App-Agent": APP_AGENT,
    "Accept": "application/json",
}
_RES_RE = re.compile(r"^(\d+)\s*k$", re.IGNORECASE)
_LOD_NAME_RE = re.compile(r"_LOD\d+", re.IGNORECASE)
_MESH_EXTS = (".fbx", ".obj", ".gltf", ".glb")
_MAP_EXTS = (".jpg", ".jpeg", ".png", ".tif", ".tiff", ".exr", ".hdr")
_HDRI_EXTS = (".exr", ".hdr")
_SKIP_NAME_PARTS = ("preview", "config.json")
_RES_ORDER = {"1k": 0, "2k": 1, "4k": 2, "8k": 3, "16k": 4}
# api.pbrpx.com has no text search (q/search/keyword are ignored). Cap how many
# listing pages we scan when filtering client-side (Houdini parity).
_QUERY_SCAN_MAX_PAGES = 40


def _http_json(path: str, params: Optional[Dict[str, Any]] = None) -> Dict:
    query_pairs: List[Tuple[str, str]] = []
    for key, value in (params or {}).items():
        if isinstance(value, (list, tuple)):
            for item in value:
                if item not in (None, ""):
                    query_pairs.append((key, str(item)))
        elif value not in (None, ""):
            query_pairs.append((key, str(value)))
    url = "{}{}".format(API_BASE, path)
    if query_pairs:
        url = "{}?{}".format(url, urllib.parse.urlencode(query_pairs))
    request = urllib.request.Request(url, headers=dict(REQUEST_HEADERS))
    with urlopen(request, timeout=45) as response:
        raw = response.read().decode("utf-8", errors="ignore")
    payload = json.loads(raw) if raw else {}
    return payload if isinstance(payload, dict) else {}


def _normalize_resolution(value: str) -> str:
    token = str(value or "").strip()
    match = _RES_RE.match(token)
    if match:
        return "{}K".format(match.group(1))
    if token.lower().endswith("k") and token[:-1].isdigit():
        return "{}K".format(token[:-1])
    return token.upper() if token.isdigit() else token


def _sort_resolutions(values: Sequence[str]) -> List[str]:
    unique: List[str] = []
    seen = set()
    for value in values:
        norm = _normalize_resolution(value)
        if norm and norm.lower() not in seen:
            seen.add(norm.lower())
            unique.append(norm)
    unique.sort(key=lambda item: (_RES_ORDER.get(item.lower(), 50), item.lower()))
    return unique


def _library_type_from_category(category: str) -> str:
    lowered = str(category or "").strip().lower()
    if lowered in ("3d_models", "3d models", "models"):
        return "mesh"
    if lowered in ("hdri", "hdris"):
        return "hdri"
    return "texture"


def _result_from_item(item: Dict) -> Optional[AssetResult]:
    if not isinstance(item, dict):
        return None
    product_name = str(item.get("productName") or "").strip()
    if not product_name:
        return None
    path_key = str(item.get("path_key") or "").strip()
    category = path_key.replace("\\", "/").split("/", 1)[0] if path_key else "Textures"
    library_type = _library_type_from_category(category)
    thumb = str(item.get("thumbnail") or "").strip()
    return AssetResult(
        source_id="pbrpx",
        asset_id=product_name,
        name=product_name,
        asset_type=library_type,
        thumb_url=thumb,
        preview_url=thumb,
        license_name="PBRPX (non-commercial / academic free)",
        attribution="PBRPX — commercial use requires a separate license",
        categories=[category],
        tags=[str(tag) for tag in (item.get("tagsEN") or []) if tag],
        resolutions=_sort_resolutions(item.get("resolutions") or []),
        formats=["maps"] if library_type != "mesh" else ["fbx", "maps"],
        metadata={"pbrpx_type": category, "pbrpx_product": product_name},
    )


def _item_matches_query(item: Dict, query: str) -> bool:
    """Match listing fields the public API actually returns (no server-side q=)."""
    needle = str(query or "").strip().lower()
    if not needle:
        return True
    blobs: List[str] = [
        str(item.get("productName") or ""),
        str(item.get("latinName") or ""),
        str(item.get("path_key") or ""),
    ]
    common = item.get("commonNameEN") or []
    if isinstance(common, list):
        blobs.extend(str(v) for v in common)
    tags = item.get("tagsEN") or []
    if isinstance(tags, list):
        blobs.extend(str(v) for v in tags)
    hay = " ".join(blobs).lower()
    return all(token in hay for token in needle.split())


def _entry_resolution(name: str, url: str = "") -> str:
    """Resolution lives in ``1K/…`` prefixes on ``name`` and/or the CDN URL.

    Docs sometimes show basename-only ``name`` while ``downloadUrl`` still has
    ``/Textures/…/2K/file.jpg``. Matching only ``name`` then selects zero maps.
    """
    for token in (name, url):
        parts = str(token or "").replace("\\", "/").split("/")
        for part in parts:
            stripped = part.strip()
            if _RES_RE.match(stripped) or (
                stripped.lower().endswith("k") and stripped[:-1].isdigit()
            ):
                return _normalize_resolution(stripped)
    return ""


def _should_skip_file(name: str, url: str) -> bool:
    if not url or str(name).endswith("/") or str(url).endswith("/"):
        return True
    lowered = str(name or "").lower()
    base = os.path.basename(str(name).replace("\\", "/")).lower()
    if any(part in lowered for part in _SKIP_NAME_PARTS):
        return True
    if base.endswith(".blend"):
        return True
    if _LOD_NAME_RE.search(base):
        return True
    return False


def _parse_file_list(file_list: Any) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    if not isinstance(file_list, list):
        return entries
    for raw in file_list:
        if not isinstance(raw, dict):
            continue
        url = str(raw.get("downloadUrl") or "").strip()
        name = str(raw.get("name") or "").strip()
        if _should_skip_file(name, url):
            continue
        base = os.path.basename(name.replace("\\", "/").rstrip("/"))
        ext = os.path.splitext(base)[1].lower()
        resolution = _entry_resolution(name, url)
        kind = "other"
        if ext in _MESH_EXTS:
            kind = "mesh"
        elif ext in _HDRI_EXTS and not resolution:
            kind = "hdri"
        elif ext in _MAP_EXTS:
            kind = "map"
        elif ext in _HDRI_EXTS:
            kind = "hdri"
        try:
            size = int(raw.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        entries.append(
            {
                "basename": base,
                "url": url,
                "ext": ext,
                "kind": kind,
                "resolution": resolution,
                "size": size,
            }
        )
    return entries


def _prefer_raster_over_exr(entries: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep JPG/PNG when the same map stem also has a huge EXR (Houdini parity).

    Vegetation atlases ship both; downloading every 4K EXR looks like a hang.
    """
    by_stem: Dict[str, Dict[str, Any]] = {}
    for entry in entries:
        stem = os.path.splitext(str(entry.get("basename") or ""))[0].lower()
        prev = by_stem.get(stem)
        if prev is None:
            by_stem[stem] = entry
            continue
        prev_ext = str(prev.get("ext") or "")
        cur_ext = str(entry.get("ext") or "")
        if prev_ext in (".exr", ".hdr") and cur_ext in (".jpg", ".jpeg", ".png"):
            by_stem[stem] = entry
    return list(by_stem.values())


def _maps_for_resolution(
    entries: Sequence[Dict[str, Any]],
    resolution: str,
    *,
    hdri: bool = False,
) -> List[Dict[str, Any]]:
    want = _normalize_resolution(resolution).lower()
    chosen: List[Dict[str, Any]] = []
    for entry in entries:
        if hdri:
            if entry.get("ext") not in _HDRI_EXTS:
                continue
        elif entry.get("kind") != "map":
            continue
        res = str(entry.get("resolution") or "").lower()
        if want and res != want:
            continue
        if not want and res:
            continue
        chosen.append(entry)
    if hdri:
        return chosen
    return _prefer_raster_over_exr(chosen)


def _resolve_resolution(available: List[str], preferred: Optional[str]) -> str:
    options = [_normalize_resolution(item) for item in available if item]
    token = _normalize_resolution(preferred or "")
    if token:
        for item in options:
            if item.lower() == token.lower():
                return item
    for want in ("2K", "1K", "4K"):
        for item in options:
            if item.lower() == want.lower():
                return item
    if options:
        return options[0]
    return token or "2K"


class PbrpxSource(AssetSource):
    source_id = "pbrpx"
    display_name = "PBRPX"

    TYPE_FILTERS = (
        ("All types", ""),
        ("Textures", "Textures"),
        ("3D Models", "3D_Models"),
        ("HDRIs", "HDRI"),
    )

    def __init__(self) -> None:
        self._info_cache: "OrderedDict[str, Dict]" = OrderedDict()
        # (query, category) → matched listings already scanned (Load More reuse)
        self._query_scan_key: Tuple[str, str] = ("", "")
        self._query_scan_items: List[AssetResult] = []
        self._query_scan_pages: int = 0
        self._query_scan_complete: bool = False

    def get_type_filters(self) -> List[tuple]:
        return list(self.TYPE_FILTERS)

    def _load_info(self, product_name: str) -> Dict:
        key = str(product_name or "").strip()
        if key in self._info_cache:
            self._info_cache.move_to_end(key)
            return self._info_cache[key]
        payload = _http_json("/info", {"productName": key})
        lru_put(
            self._info_cache,
            key,
            payload if isinstance(payload, dict) else {},
            SOURCE_DETAIL_CACHE_MAX,
        )
        return self._info_cache[key]

    def search(
        self,
        query: str,
        category: Optional[str] = None,
        page: int = 1,
        page_size: int = 50,
        **kwargs,
    ) -> SearchResults:
        page = max(1, int(page or 1))
        page_size = max(1, min(int(page_size or 50), 50))
        category_value = str(category or "").strip()
        query_value = str(query or "").strip()

        if not query_value:
            params: Dict[str, Any] = {"page": page, "pageSize": page_size}
            if category_value:
                params["categorys"] = [category_value]
            payload = _http_json("/assets", params)
            results = []
            for item in payload.get("data") or []:
                built = _result_from_item(item if isinstance(item, dict) else {})
                if built is not None:
                    results.append(built)
            pagination = payload.get("pagination") or {}
            try:
                total = int(pagination.get("total") or len(results))
            except (TypeError, ValueError):
                total = len(results)
            try:
                total_pages = int(pagination.get("totalPages") or 1)
            except (TypeError, ValueError):
                total_pages = 1
            return SearchResults(
                items=results,
                has_more=page < total_pages,
                total_count=total,
            )

        scan_key = (query_value.lower(), category_value.lower())
        if scan_key != self._query_scan_key:
            self._query_scan_key = scan_key
            self._query_scan_items = []
            self._query_scan_pages = 0
            self._query_scan_complete = False

        needed = page * page_size
        api_page = self._query_scan_pages
        while (
            not self._query_scan_complete
            and api_page < _QUERY_SCAN_MAX_PAGES
            and len(self._query_scan_items) < needed
        ):
            api_page += 1
            params = {"page": api_page, "pageSize": 50}
            if category_value:
                params["categorys"] = [category_value]
            payload = _http_json("/assets", params)
            batch = payload.get("data") or []
            pagination = payload.get("pagination") or {}
            try:
                total_pages = int(pagination.get("totalPages") or api_page)
            except (TypeError, ValueError):
                total_pages = api_page
            if not batch:
                self._query_scan_complete = True
                break
            for item in batch:
                if not isinstance(item, dict) or not _item_matches_query(item, query_value):
                    continue
                built = _result_from_item(item)
                if built is not None:
                    self._query_scan_items.append(built)
            self._query_scan_pages = api_page
            if api_page >= total_pages:
                self._query_scan_complete = True
                break

        matched = self._query_scan_items
        start = (page - 1) * page_size
        slice_items = matched[start : start + page_size]
        has_more = len(matched) > start + page_size or (
            not self._query_scan_complete and self._query_scan_pages < _QUERY_SCAN_MAX_PAGES
        )
        return SearchResults(
            items=slice_items,
            has_more=has_more,
            total_count=len(matched),
        )

    def get_resolutions(self, asset: AssetResult, fmt: Optional[str] = None) -> List[str]:
        if asset.resolutions:
            return _sort_resolutions(asset.resolutions)
        info = self._load_info(asset.asset_id)
        return _sort_resolutions(info.get("resolutions") or []) or ["1K", "2K", "4K"]

    def get_formats(self, asset: AssetResult) -> List[str]:
        if asset.asset_type == "mesh":
            return ["fbx", "obj", "maps"]
        if asset.asset_type == "hdri":
            return ["exr", "hdr"]
        return ["maps"]

    def download(
        self,
        asset: AssetResult,
        resolution: Optional[str],
        fmt: Optional[str],
        dest_folder: str,
        **kwargs,
    ) -> str:
        progress_callback: Optional[Callable[[int, int], None]] = kwargs.get("progress_callback")
        info = self._load_info(asset.asset_id)
        if not info:
            raise RuntimeError("PBRPX asset metadata not found for {}.".format(asset.asset_id))
        entries = _parse_file_list(info.get("file_list"))
        if not entries:
            raise RuntimeError("PBRPX asset has no downloadable files for {}.".format(asset.name))

        available_res = _sort_resolutions(
            list(info.get("resolutions") or []) + [str(e.get("resolution") or "") for e in entries if e.get("resolution")]
        )
        chosen_res = _resolve_resolution(available_res, resolution)
        asset_dir = os.path.join(dest_folder, asset.asset_id)
        maps_dir = os.path.join(asset_dir, chosen_res or "maps")
        os.makedirs(maps_dir, exist_ok=True)

        downloads: List[Tuple[str, str]] = []
        primary = ""

        if asset.asset_type == "hdri":
            maps = _maps_for_resolution(entries, chosen_res, hdri=True)
            if not maps:
                maps = [e for e in entries if e.get("ext") in _HDRI_EXTS]
            if not maps:
                raise RuntimeError("No HDRI download is available for this PBRPX asset.")
            entry = maps[0]
            primary = os.path.join(maps_dir, str(entry.get("basename")))
            downloads.append((str(entry.get("url")), primary))
        elif asset.asset_type == "mesh":
            fmt_token = str(fmt or "fbx").lower()
            meshes = [e for e in entries if e.get("kind") == "mesh"]
            mesh = next((e for e in meshes if str(e.get("ext")) == ".{}".format(fmt_token.lstrip("."))), None)
            if mesh is None:
                mesh = next((e for e in meshes if e.get("ext") == ".fbx"), None) or (meshes[0] if meshes else None)
            if mesh is None:
                raise RuntimeError("No mesh download is available for this PBRPX asset.")
            primary = os.path.join(asset_dir, str(mesh.get("basename")))
            downloads.append((str(mesh.get("url")), primary))
            for entry in _maps_for_resolution(entries, chosen_res, hdri=False):
                out = os.path.join(maps_dir, str(entry.get("basename")))
                downloads.append((str(entry.get("url")), out))
        else:
            maps = _maps_for_resolution(entries, chosen_res, hdri=False)
            if not maps:
                raise RuntimeError(
                    "No {} texture maps are available for this PBRPX asset.".format(chosen_res)
                )
            preferred = None
            for entry in maps:
                base = str(entry.get("basename") or "").lower()
                if any(token in base for token in ("albedo", "basecolor", "diffuse", "_alb")):
                    preferred = entry
                    break
            preferred = preferred or maps[0]
            for entry in maps:
                out = os.path.join(maps_dir, str(entry.get("basename")))
                downloads.append((str(entry.get("url")), out))
                if entry is preferred:
                    primary = out

        if not downloads or not primary:
            raise RuntimeError("PBRPX download selection produced no files for {}.".format(asset.name))

        headers = {"App-Agent": APP_AGENT}
        total_jobs = len(downloads)
        for index, (url, path) in enumerate(downloads):
            def _file_progress(done: int, total: int, *, _i=index) -> None:
                if progress_callback is None:
                    return
                # File index as coarse steps; byte progress fills the current file.
                if total and total > 0:
                    progress_callback(_i * 100 + int(done * 100 / total), total_jobs * 100)
                else:
                    progress_callback(_i, total_jobs)

            stream_download_file(
                url,
                path,
                timeout=300,
                extra_headers=headers,
                progress_callback=_file_progress if progress_callback else None,
            )
        if not os.path.isfile(primary):
            raise RuntimeError("PBRPX download finished but primary file is missing.")
        return primary
