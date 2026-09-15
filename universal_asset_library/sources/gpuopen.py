# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""AMD GPUOpen MaterialX Library source connector."""

from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import OrderedDict
from typing import Callable, Dict, List, Optional, Tuple

from ..download_utils import ensure_zip_archive_path, extract_zip_members, stream_download_file
from ..http_ssl import urlopen
from ..lru_util import lru_put
from .base import AssetResult, AssetSource, SearchResults

API_BASE = "https://api.matlib.gpuopen.com/api"
USER_AGENT = "UniversalAssetLibrary-Blender/0.3"
RENDER_THUMB_URL = "{}/renders/{{}}/download_thumbnail/".format(API_BASE)
PACKAGE_LABEL_RE = re.compile(r"(\d+k)\s+(\d+b)", re.IGNORECASE)
_RES_SORT = {"1k": 0, "2k": 1, "4k": 2, "8k": 3}
_BIT_SORT = {"8b": 0, "16b": 1}
# Search-time hints until package enrich fills real lists (hover + Size picker)
_DEFAULT_RESOLUTIONS = ["1k", "2k", "4k"]
_DEFAULT_FORMATS = ["8b", "16b"]
_MATERIAL_CACHE_MAX = 64
_PACKAGE_CACHE_MAX = 64


def _http_json(url: str, params: Optional[Dict[str, str]] = None) -> Dict:
    query = urllib.parse.urlencode(params or {})
    full_url = "{}?{}".format(url, query) if query else url
    request = urllib.request.Request(full_url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=30) as response:
        raw = response.read().decode("utf-8", errors="ignore")
    payload = json.loads(raw) if raw else {}
    return payload if isinstance(payload, dict) else {}


def _parse_package_label(label: str) -> Tuple[str, str]:
    match = PACKAGE_LABEL_RE.search(str(label or ""))
    if not match:
        lowered = str(label or "").strip().lower()
        for token in ("1k", "2k", "4k", "8k"):
            if token in lowered:
                return token, "8b"
        return "", "8b"
    return match.group(1).lower(), match.group(2).lower()


def _normalize_download_url(file_url: str, package_id: str) -> str:
    """Return the ZIP download URL (never the JSON package metadata document)."""
    token = str(package_id or "").strip()
    fallback = "{}/packages/{}/download/".format(API_BASE, token) if token else ""
    raw = str(file_url or "").strip()
    if not raw:
        return fallback
    cleaned = raw.rstrip("/")
    if cleaned.lower().endswith("/download"):
        return cleaned + "/"
    if token and cleaned.lower().endswith("/packages/{}".format(token.lower())):
        return fallback or (cleaned + "/download/")
    if "/download" in cleaned.lower():
        return cleaned if cleaned.endswith("/") else cleaned + "/"
    return fallback or raw


def _fetch_package_meta(package_id: str) -> Dict:
    if not package_id:
        return {}
    try:
        payload = _http_json("{}/packages/{}/".format(API_BASE, package_id))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _fetch_material_packages(material: Dict) -> List[Dict]:
    package_ids = [str(item) for item in (material.get("packages") or []) if item]
    if not package_ids:
        return []
    packages: List[Dict] = []
    worker_count = max(1, min(6, len(package_ids)))

    def _one(package_id: str) -> Optional[Dict]:
        payload = _fetch_package_meta(package_id)
        if not payload:
            return None
        resolution, bit_depth = _parse_package_label(str(payload.get("label") or ""))
        return {
            "id": package_id,
            "label": str(payload.get("label") or ""),
            "resolution": resolution,
            "bit_depth": bit_depth,
            "download_url": _normalize_download_url(
                str(payload.get("file_url") or "").strip(),
                package_id,
            ),
            "filename": str(payload.get("file") or ""),
        }

    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        futures = {pool.submit(_one, pid): pid for pid in package_ids}
        for future in as_completed(futures):
            try:
                row = future.result()
            except Exception:
                row = None
            if row:
                packages.append(row)
    packages.sort(
        key=lambda item: (
            _RES_SORT.get(item.get("resolution", ""), 99),
            _BIT_SORT.get(item.get("bit_depth", ""), 99),
        )
    )
    return packages


def _resolve_resolution(available: List[str], preferred: Optional[str]) -> str:
    options = [str(item).lower() for item in available if item]
    token = str(preferred or "").strip().lower()
    if token and token in options:
        return token
    for candidate in ("2k", "4k", "1k", "8k"):
        if candidate in options:
            return candidate
    return options[0] if options else "2k"


def _pick_package(packages: List[Dict], resolution: Optional[str], fmt: Optional[str]) -> Dict:
    if not packages:
        return {}
    resolutions = sorted(
        {str(item.get("resolution") or "") for item in packages if item.get("resolution")},
        key=lambda token: _RES_SORT.get(token, 99),
    )
    bit_depths = sorted(
        {str(item.get("bit_depth") or "") for item in packages if item.get("bit_depth")},
        key=lambda token: _BIT_SORT.get(token, 99),
    )
    chosen_res = _resolve_resolution(resolutions, resolution)
    chosen_fmt = str(fmt or "8b").strip().lower()
    if chosen_fmt not in bit_depths and bit_depths:
        chosen_fmt = bit_depths[0]
    exact = [
        item
        for item in packages
        if str(item.get("resolution") or "").lower() == chosen_res
        and str(item.get("bit_depth") or "").lower() == chosen_fmt
    ]
    if exact:
        return exact[0]
    res_matches = [item for item in packages if str(item.get("resolution") or "").lower() == chosen_res]
    if res_matches:
        return res_matches[0]
    return packages[0]


def _material_thumb_url(material: Dict) -> str:
    render_ids = material.get("renders_order") or material.get("renders") or []
    for render_id in render_ids:
        token = str(render_id or "").strip()
        if token:
            return RENDER_THUMB_URL.format(token)
    return ""


def _find_mtlx(extract_dir: str) -> str:
    preferred = ""
    for dirpath, _, filenames in os.walk(extract_dir):
        for filename in filenames:
            path = os.path.join(dirpath, filename)
            ext = os.path.splitext(filename)[1].lower()
            if ext == ".mtlx":
                return path
            if ext in (".png", ".jpg", ".jpeg", ".exr") and not preferred:
                preferred = path
    return preferred


def _delete_archive_quiet(path: str) -> None:
    try:
        if path and os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass


class GPUOpenSource(AssetSource):
    source_id = "gpuopen"
    display_name = "GPUOpen MaterialX"

    BASE_TYPE_FILTERS = (
        ("All materials", ""),
        ("Static", "Static"),
        ("Parametric", "Parametric"),
    )

    def __init__(self) -> None:
        self._category_titles: Dict[str, str] = {}
        self._categories_loaded = False
        self._material_cache: "OrderedDict[str, Dict]" = OrderedDict()
        self._package_cache: "OrderedDict[str, List[Dict]]" = OrderedDict()

    def _cache_put(self, cache: OrderedDict, key: str, value, maxsize: int) -> None:
        lru_put(cache, key, value, maxsize)

    def _ensure_categories(self) -> None:
        if self._categories_loaded:
            return
        self._categories_loaded = True
        try:
            payload = _http_json("{}/categories/".format(API_BASE), {"limit": "100"})
            for item in payload.get("results") or []:
                if isinstance(item, dict):
                    category_id = str(item.get("id") or "")
                    title = str(item.get("title") or "").strip()
                    if category_id and title:
                        self._category_titles[category_id] = title
        except (OSError, ValueError, TypeError):
            pass

    def get_type_filters(self) -> List[tuple]:
        self._ensure_categories()
        filters = list(self.BASE_TYPE_FILTERS)
        for title in sorted(self._category_titles.values(), key=lambda value: value.lower()):
            category_id = next(
                (key for key, value in self._category_titles.items() if value == title),
                "",
            )
            if category_id:
                filters.append((title, "cat:{}".format(category_id)))
        return filters

    def _build_result(self, item: Dict) -> Optional[AssetResult]:
        asset_id = str(item.get("id") or "")
        if not asset_id:
            return None
        material_type = str(item.get("material_type") or "Static")
        category_id = str(item.get("category") or "")
        title = str(item.get("title") or asset_id)
        author = str(item.get("author") or "AMD")
        category_title = self._category_titles.get(category_id, category_id)
        thumb_url = _material_thumb_url(item)
        return AssetResult(
            source_id=self.source_id,
            asset_id=asset_id,
            name=title,
            asset_type="material",
            thumb_url=thumb_url,
            preview_url=thumb_url,
            license_name=str(item.get("license") or "MIT Public Domain"),
            attribution="{} / AMD GPUOpen MaterialX Library".format(author),
            categories=[category_title] if category_title else [],
            tags=[material_type] if material_type else [],
            # Hints until get_resolutions/get_formats enrich from package meta
            resolutions=list(_DEFAULT_RESOLUTIONS),
            formats=list(_DEFAULT_FORMATS),
            metadata={
                "material_type": material_type,
                "category_id": category_id,
                "mtlx_filename": str(item.get("mtlx_filename") or ""),
            },
        )

    def search(
        self,
        query: str,
        category: Optional[str] = None,
        page: int = 1,
        page_size: int = 50,
        **kwargs,
    ) -> SearchResults:
        # Do not block browse on /categories/ (adds ~0.8–1s). Titles resolve
        # from the in-memory map when already warm; otherwise category id is fine.
        page_size = max(1, min(page_size, 100))
        params = {
            "limit": str(page_size),
            "offset": str(max(0, page - 1) * page_size),
        }
        if query.strip():
            params["search"] = query.strip()

        category_value = (category or "").strip()
        client_material_type = ""
        client_category_id = ""
        if category_value.startswith("cat:"):
            client_category_id = category_value[4:]
            self._ensure_categories()
        elif category_value in ("Static", "Parametric"):
            client_material_type = category_value

        if client_material_type or client_category_id:
            matched: List[AssetResult] = []
            offset = 0
            fetch_size = 100
            while len(matched) < page * page_size and offset < 500:
                batch_params = dict(params)
                batch_params["limit"] = str(fetch_size)
                batch_params["offset"] = str(offset)
                payload = _http_json("{}/materials/".format(API_BASE), batch_params)
                raw_items = payload.get("results") or []
                if not raw_items:
                    break
                for item in raw_items:
                    if not isinstance(item, dict):
                        continue
                    if client_material_type and str(item.get("material_type") or "") != client_material_type:
                        continue
                    if client_category_id and str(item.get("category") or "") != client_category_id:
                        continue
                    built = self._build_result(item)
                    if built is not None:
                        matched.append(built)
                if not payload.get("next"):
                    break
                offset += len(raw_items)
            start = (page - 1) * page_size
            page_items = matched[start : start + page_size]
            return SearchResults(
                items=page_items,
                has_more=len(matched) > start + page_size,
                total_count=len(matched),
            )

        payload = _http_json("{}/materials/".format(API_BASE), params)
        results = []
        for item in payload.get("results") or []:
            if isinstance(item, dict):
                built = self._build_result(item)
                if built is not None:
                    results.append(built)
        total = int(payload.get("count") or len(results))
        has_more = bool(payload.get("next")) and len(results) >= page_size
        return SearchResults(items=results, has_more=has_more, total_count=total)

    def _load_material(self, asset_id: str) -> Dict:
        key = str(asset_id or "").strip()
        if key in self._material_cache:
            self._material_cache.move_to_end(key)
            return self._material_cache[key]
        payload = _http_json("{}/materials/{}/".format(API_BASE, key))
        material = payload if isinstance(payload, dict) else {}
        self._cache_put(self._material_cache, key, material, _MATERIAL_CACHE_MAX)
        return material

    def _packages_for_material(self, material: Dict) -> List[Dict]:
        asset_id = str(material.get("id") or "")
        if asset_id in self._package_cache:
            self._package_cache.move_to_end(asset_id)
            return self._package_cache[asset_id]
        packages = _fetch_material_packages(material)
        self._cache_put(self._package_cache, asset_id, packages, _PACKAGE_CACHE_MAX)
        return packages

    def get_resolutions(self, asset: AssetResult, fmt: Optional[str] = None) -> List[str]:
        material = self._load_material(asset.asset_id)
        packages = self._packages_for_material(material)
        resolutions = sorted(
            {str(item.get("resolution") or "") for item in packages if item.get("resolution")},
            key=lambda token: _RES_SORT.get(token, 99),
        )
        return resolutions or list(_DEFAULT_RESOLUTIONS)

    def get_formats(self, asset: AssetResult) -> List[str]:
        material = self._load_material(asset.asset_id)
        packages = self._packages_for_material(material)
        bit_depths = sorted(
            {str(item.get("bit_depth") or "") for item in packages if item.get("bit_depth")},
            key=lambda token: _BIT_SORT.get(token, 99),
        )
        return bit_depths or list(_DEFAULT_FORMATS)

    def download(
        self,
        asset: AssetResult,
        resolution: Optional[str],
        fmt: Optional[str],
        dest_folder: str,
        **kwargs,
    ) -> str:
        progress_callback: Optional[Callable[[int, int], None]] = kwargs.get("progress_callback")
        material = self._load_material(asset.asset_id)
        if not material:
            raise RuntimeError("GPUOpen material metadata not found for {}.".format(asset.asset_id))
        packages = self._packages_for_material(material)
        chosen = _pick_package(packages, resolution, fmt)
        if not chosen:
            raise RuntimeError("No download package is available for {}.".format(asset.name))
        download_url = str(chosen.get("download_url") or "")
        if not download_url:
            raise RuntimeError("GPUOpen package has no download URL for {}.".format(asset.name))

        asset_dir = os.path.join(dest_folder, asset.asset_id)
        os.makedirs(asset_dir, exist_ok=True)
        filename = str(chosen.get("filename") or "").strip()
        if not filename:
            filename = "{}_{}.zip".format(asset.asset_id, chosen.get("label", "package").replace(" ", "_"))
        output_path = os.path.join(asset_dir, filename)
        try:
            stream_download_file(
                download_url, output_path, timeout=300, progress_callback=progress_callback
            )
        except RuntimeError as exc:
            raise RuntimeError(
                "GPUOpen download failed to save {}: {}".format(filename, exc)
            ) from exc

        zip_path = ensure_zip_archive_path(output_path)
        extract_dir = os.path.join(asset_dir, "extracted")
        if progress_callback is not None:
            try:
                progress_callback(0, -1)
            except Exception:
                pass
        try:
            extract_zip_members(zip_path, extract_dir, progress_callback=progress_callback)
        except Exception as exc:
            raise RuntimeError(
                "GPUOpen ZIP extract failed for {}: {}".format(asset.name, exc)
            ) from exc
        primary = _find_mtlx(extract_dir)
        if not primary:
            raise RuntimeError(
                "Downloaded ZIP for {} but no MaterialX (.mtlx) or texture maps "
                "were found inside. Try another resolution or bit depth.".format(asset.name)
            )
        _delete_archive_quiet(zip_path if zip_path != primary else "")
        if zip_path != output_path:
            _delete_archive_quiet(output_path)

        sidecar = os.path.join(asset_dir, "{}.license.json".format(asset.asset_id))
        try:
            with open(sidecar, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "source": self.display_name,
                        "license": asset.license_name or "MIT Public Domain",
                        "attribution": asset.attribution or "AMD GPUOpen MaterialX Library",
                        "asset_id": asset.asset_id,
                        "asset_type": "material_set",
                        "url": download_url,
                        "format": str(chosen.get("bit_depth") or ""),
                        "resolution": str(chosen.get("resolution") or ""),
                        "material_type": str(material.get("material_type") or ""),
                    },
                    handle,
                    indent=2,
                )
        except OSError:
            pass
        return primary
