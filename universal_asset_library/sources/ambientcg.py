# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""ambientCG source connector."""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from collections import OrderedDict
from typing import Callable, Dict, List, Optional

from ..download_utils import ensure_zip_archive_path, extract_zip_members, stream_download_file
from ..http_ssl import urlopen
from ..lru_util import SOURCE_DETAIL_CACHE_MAX, lru_put
from .base import AssetResult, AssetSource, SearchResults

API_BASE = "https://ambientcg.com/api/v2/full_json"
USER_AGENT = "UniversalAssetLibrary-Blender/0.1"
RESOLUTION_PRIORITY = ("8k", "4k", "2k", "1k")
IMPORT_SUFFIXES = (
    ".png",
    ".jpg",
    ".jpeg",
    ".exr",
    ".hdr",
    ".fbx",
    ".obj",
    ".gltf",
    ".glb",
    ".blend",
)


def _http_json(url: str, params: Optional[Dict[str, str]] = None) -> Dict:
    query = urllib.parse.urlencode(params or {})
    full_url = "{}?{}".format(url, query) if query else url
    request = urllib.request.Request(full_url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=30) as response:
        raw = response.read().decode("utf-8", errors="ignore")
    payload = json.loads(raw) if raw else {}
    return payload if isinstance(payload, dict) else {}


def _list_resolutions(download_folders: Dict) -> List[str]:
    seen = set()
    resolutions: List[str] = []
    for folder in (download_folders or {}).values():
        if not isinstance(folder, dict):
            continue
        for category in (folder.get("downloadFiletypeCategories") or {}).values():
            if not isinstance(category, dict):
                continue
            for entry in category.get("downloads") or []:
                if not isinstance(entry, dict):
                    continue
                attribute = str(entry.get("attribute") or "").lower()
                for token in RESOLUTION_PRIORITY:
                    if token in attribute and token not in seen:
                        seen.add(token)
                        resolutions.append(token)
    return resolutions or list(RESOLUTION_PRIORITY)


def _list_formats(download_folders: Dict) -> List[str]:
    found = set()
    for folder in (download_folders or {}).values():
        if not isinstance(folder, dict):
            continue
        for category in (folder.get("downloadFiletypeCategories") or {}).values():
            if not isinstance(category, dict):
                continue
            for entry in category.get("downloads") or []:
                if not isinstance(entry, dict):
                    continue
                filetype = str(entry.get("filetype") or "").lower()
                attribute = str(entry.get("attribute") or "").lower()
                if "zip" in filetype or "zip" in attribute:
                    found.add("zip")
                for token in ("exr", "hdr", "fbx", "obj", "gltf", "png", "jpg"):
                    if token in filetype or token in attribute:
                        found.add(token)
    return sorted(found) or ["zip"]


def _pick_download_url(download_folders: Dict, resolution: str, fmt: str) -> str:
    fmt = fmt.lower()
    res_l = str(resolution or "").strip().lower()
    for folder in (download_folders or {}).values():
        if not isinstance(folder, dict):
            continue
        for category in (folder.get("downloadFiletypeCategories") or {}).values():
            if not isinstance(category, dict):
                continue
            for entry in category.get("downloads") or []:
                if not isinstance(entry, dict):
                    continue
                url = str(entry.get("downloadLink") or entry.get("fullDownloadPath") or "")
                if not url:
                    continue
                attribute = str(entry.get("attribute") or "").lower()
                filetype = str(entry.get("filetype") or fmt).lower()
                if res_l in attribute and (fmt in filetype or fmt in attribute):
                    return url
    if not res_l or res_l == "auto":
        for folder in (download_folders or {}).values():
            if not isinstance(folder, dict):
                continue
            for category in (folder.get("downloadFiletypeCategories") or {}).values():
                if not isinstance(category, dict):
                    continue
                for entry in category.get("downloads") or []:
                    if not isinstance(entry, dict):
                        continue
                    url = str(entry.get("downloadLink") or entry.get("fullDownloadPath") or "")
                    attribute = str(entry.get("attribute") or "").lower()
                    filetype = str(entry.get("filetype") or fmt).lower()
                    if url and (fmt in filetype or fmt in attribute):
                        return url
    return ""


def _resolve_resolution(available: List[str], preferred: Optional[str]) -> str:
    options = [str(item).lower() for item in available if item]
    token = str(preferred or "").strip().lower()
    if token and token in options:
        return token
    for candidate in RESOLUTION_PRIORITY:
        if candidate in options:
            return candidate
    return options[0] if options else "2k"


def _find_primary(extract_dir: str) -> str:
    preferred = ""
    hdri = ""
    hdri_size = -1
    fallback = ""
    for dirpath, _, filenames in os.walk(extract_dir):
        for filename in filenames:
            path = os.path.join(dirpath, filename)
            ext = os.path.splitext(filename)[1].lower()
            if ext not in IMPORT_SUFFIXES:
                continue
            if ext in (".hdr", ".exr"):
                try:
                    size = int(os.path.getsize(path))
                except OSError:
                    size = 0
                if size > hdri_size:
                    hdri = path
                    hdri_size = size
                continue
            stem = os.path.splitext(filename)[0].lower()
            if any(key in stem for key in ("albedo", "basecolor", "diffuse", "color")):
                preferred = path
            elif not fallback:
                fallback = path
    return hdri or preferred or fallback


class AmbientCGSource(AssetSource):
    source_id = "ambientcg"
    display_name = "ambientCG"

    TYPE_FILTERS = (
        ("All types", ""),
        ("Materials", "Material"),
        ("3D Models", "3DModel"),
        ("HDRIs", "HDRI"),
    )

    def __init__(self) -> None:
        self._download_meta_cache: "OrderedDict[str, Dict]" = OrderedDict()

    def get_type_filters(self) -> List[tuple]:
        return list(self.TYPE_FILTERS)

    def _load_download_asset(self, asset_id: str) -> Dict:
        key = str(asset_id or "").strip()
        if not key:
            return {}
        if key in self._download_meta_cache:
            self._download_meta_cache.move_to_end(key)
            return self._download_meta_cache[key]
        # ``id`` selects assets; ``include`` only lists data sections (not asset ids).
        # Putting the asset id in ``include`` returns an unrelated catalog page
        # (often Ground110) and stages the wrong ZIP under the HDRI name.
        payload = _http_json(
            API_BASE,
            params={"id": key, "include": "downloadData"},
        )
        rows = payload.get("foundAssets") or []
        row = {}
        for candidate in rows:
            if not isinstance(candidate, dict):
                continue
            if str(candidate.get("assetId") or "").strip().lower() == key.lower():
                row = candidate
                break
        if not row and rows and isinstance(rows[0], dict) and len(rows) == 1:
            row = rows[0]
        lru_put(self._download_meta_cache, key, row, SOURCE_DETAIL_CACHE_MAX)
        return row

    def search(
        self,
        query: str,
        category: Optional[str] = None,
        page: int = 1,
        page_size: int = 50,
        **kwargs,
    ) -> SearchResults:
        params = {
            "limit": str(max(1, min(page_size, 100))),
            "offset": str(max(0, page - 1) * max(1, page_size)),
            "include": "previewData",
        }
        if query.strip():
            params["q"] = query.strip()
        category_value = (category or "").strip()
        client_filter = ""
        if category_value and category_value != "3DModel":
            params["type"] = category_value
        elif category_value == "3DModel":
            # ambientCG currently ignores type=3DModel (returns the full catalog).
            # Client-filter alone yields an empty grid; q=3D surfaces the 3D* models.
            client_filter = "3DModel"
            params["type"] = "3DModel"
            if not query.strip():
                params["q"] = "3D"
        payload = _http_json(API_BASE, params=params)
        results: List[AssetResult] = []
        for item in payload.get("foundAssets") or []:
            if not isinstance(item, dict):
                continue
            asset_id = str(item.get("assetId") or "")
            if not asset_id:
                continue
            data_type = str(item.get("dataType") or "texture")
            if client_filter and data_type != client_filter:
                continue
            display_data = item.get("displayData") or {}
            display = str(display_data.get("displayName") or item.get("displayName") or asset_id)
            preview_data = item.get("previewData") or item.get("previewImage") or {}
            preview = ""
            if isinstance(preview_data, dict):
                preview = str(
                    preview_data.get("256-PNG")
                    or preview_data.get("512-PNG")
                    or preview_data.get("1024-PNG")
                    or preview_data.get("previewImage")
                    or ""
                )
            results.append(
                AssetResult(
                    source_id=self.source_id,
                    asset_id=asset_id,
                    name=display,
                    asset_type=data_type,
                    thumb_url=preview,
                    preview_url=preview,
                    license_name="CC0",
                    attribution="ambientCG / Lennart Demes",
                    categories=[str(item.get("category") or "")],
                    tags=[str(tag) for tag in item.get("tags", [])],
                    resolutions=_list_resolutions(item.get("downloadFolders") or {}),
                    metadata={"ambientcg_type": data_type},
                )
            )
        total = int(payload.get("numberOfResults") or len(results))
        offset = max(0, page - 1) * max(1, page_size)
        has_more = bool(payload.get("nextPageHttp")) or (offset + len(results) < total)
        return SearchResults(items=results, has_more=has_more, total_count=total)

    def get_resolutions(self, asset: AssetResult, fmt: Optional[str] = None) -> List[str]:
        row = self._load_download_asset(asset.asset_id)
        return _list_resolutions(row.get("downloadFolders") or {})

    def get_formats(self, asset: AssetResult) -> List[str]:
        row = self._load_download_asset(asset.asset_id)
        return _list_formats(row.get("downloadFolders") or {})

    def download(
        self,
        asset: AssetResult,
        resolution: Optional[str],
        fmt: Optional[str],
        dest_folder: str,
        **kwargs,
    ) -> str:
        progress_callback: Optional[Callable[[int, int], None]] = kwargs.get("progress_callback")
        row = self._load_download_asset(asset.asset_id)
        if not row:
            raise RuntimeError("ambientCG asset metadata not found for {}.".format(asset.asset_id))
        download_folders = row.get("downloadFolders") or {}
        available_formats = _list_formats(download_folders)
        available_resolutions = _list_resolutions(download_folders)
        chosen_fmt = str(fmt or "").strip().lower()
        if not chosen_fmt or chosen_fmt == "auto":
            chosen_fmt = "zip" if "zip" in available_formats else (available_formats[0] if available_formats else "zip")
        if chosen_fmt not in available_formats and available_formats:
            chosen_fmt = available_formats[0]
        chosen_res = _resolve_resolution(available_resolutions, resolution)
        selected_url = _pick_download_url(download_folders, chosen_res, chosen_fmt)
        if not selected_url:
            raise RuntimeError(
                "No {} {} download is available for this ambientCG asset.".format(chosen_res, chosen_fmt)
            )

        asset_dir = os.path.join(dest_folder, asset.asset_id)
        os.makedirs(asset_dir, exist_ok=True)
        parsed = urllib.parse.urlparse(selected_url)
        filename = os.path.basename(parsed.path)
        if not filename or filename.lower() in ("get", "download"):
            query = urllib.parse.parse_qs(parsed.query)
            filename = query.get("file", ["{}.zip".format(asset.asset_id)])[0]
        output_path = os.path.join(asset_dir, filename)
        stream_download_file(
            selected_url,
            output_path,
            progress_callback=progress_callback,
            source_id=self.source_id,
        )

        local_path = output_path
        try:
            zip_path = ensure_zip_archive_path(output_path)
            extract_dir = os.path.join(asset_dir, "extracted")
            extract_zip_members(zip_path, extract_dir, progress_callback=progress_callback)
            primary = _find_primary(extract_dir)
            if primary:
                local_path = primary
        except Exception:
            pass
        return local_path
