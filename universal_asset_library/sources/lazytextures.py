# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""LazyTextures source connector."""

from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from collections import OrderedDict
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..download_utils import (
    download_urls_parallel,
    ensure_zip_archive_path,
    extract_zip_members,
    stream_download_file,
)
from ..http_ssl import urlopen
from ..lru_util import SOURCE_DETAIL_CACHE_MAX, lru_put
from .base import AssetResult, AssetSource, SearchResults

API_BASE = "https://lazytextures.net"
USER_AGENT = "UniversalAssetLibrary-Blender/0.1"
VALID_ASSET_TYPES = ("models", "materials", "hdris", "textures", "other")
RESOLUTION_RE = re.compile(r"(\d+)\s*k", re.IGNORECASE)
_RES_SORT = {"1k": 0, "2k": 1, "4k": 2, "6k": 3, "8k": 4}


def _http_json(path: str, params: Optional[Dict[str, str]] = None) -> Dict:
    query = urllib.parse.urlencode(params or {})
    url = "{}{}".format(API_BASE, path)
    if query:
        url = "{}?{}".format(url, query)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=45) as response:
        raw = response.read().decode("utf-8", errors="ignore")
    payload = json.loads(raw) if raw else {}
    return payload if isinstance(payload, dict) else {}


def _resolve_basepath(asset_type: str, name: str) -> str:
    """Look up ``basePath`` via a quick search when metadata doesn't carry it."""
    try:
        params = {"q": name, "limit": "5", "offset": "0", "sortBy": "relevance"}
        payload = _http_json("/{}/Search".format(asset_type), params)
        for item in payload.get("results") or payload.get("data") or []:
            if isinstance(item, dict) and str(item.get("name") or "") == name:
                return str(item.get("basePath") or "")
    except Exception:
        pass
    return ""


def _fetch_infos_json(basepath: str, asset_type: str, name: str) -> Dict:
    """Fetch per-asset ``Infos.json`` when ``/Asset/`` detail returns HTTP 500."""
    if not basepath:
        basepath = _resolve_basepath(asset_type, name)
    candidates: List[str] = []
    if basepath:
        candidates.append("{}{}/Infos.json".format(API_BASE, basepath))
    type_folder = {
        "materials": "Materials",
        "textures": "Textures",
        "models": "Models",
        "hdris": "HDRIs",
        "other": "Other",
    }.get(asset_type, "Materials")
    candidates.append(
        "{}/Assets/{}/{}/Infos.json".format(API_BASE, type_folder, urllib.parse.quote(name))
    )
    for url in candidates:
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urlopen(request, timeout=30) as response:
                raw = response.read().decode("utf-8", errors="ignore")
            payload = json.loads(raw) if raw else {}
            if isinstance(payload, dict) and payload:
                return payload
        except Exception:
            continue
    return {}


def _infos_to_detail(infos: Dict) -> Dict:
    """Convert ``Infos.json`` into the shape ``_list_archive_entries`` expects."""
    archives: Dict[str, Any] = {}
    textures: Dict[str, Any] = {}

    for res_key, dl_entry in (infos.get("downloads") or {}).items():
        res = _normalize_resolution(str(res_key))
        if isinstance(dl_entry, dict):
            archives[res] = [dl_entry]
        elif isinstance(dl_entry, list):
            archives[res] = dl_entry

    for res_key, tex_list in (infos.get("textures") or {}).items():
        res = _normalize_resolution(str(res_key))
        if isinstance(tex_list, list):
            textures[res] = [
                entry for entry in tex_list
                if isinstance(entry, dict)
                and str(entry.get("type") or "").lower() != "preview"
            ]

    return {"data": {"archives": archives, "textures": textures}}


def _asset_id(asset_type: str, name: str) -> str:
    return "{}/{}".format(asset_type.strip().lower(), name.strip())


def _split_asset_id(asset_id: str) -> Tuple[str, str]:
    token = str(asset_id or "").strip()
    if "/" in token:
        asset_type, name = token.split("/", 1)
        return asset_type.lower(), name
    return "materials", token


def _normalize_resolution(value: str) -> str:
    match = RESOLUTION_RE.search(str(value or ""))
    if match:
        return "{}k".format(match.group(1))
    lowered = str(value or "").strip().lower()
    if lowered.endswith("k") and lowered[:-1].isdigit():
        return lowered
    return lowered


def _preview_url(item: Dict) -> str:
    preview = item.get("preview") or {}
    if isinstance(preview, dict):
        path = str(preview.get("path") or "").strip()
        if path:
            return "{}{}".format(API_BASE, path)
    return ""


def _library_asset_type(api_type: str) -> str:
    if api_type == "materials":
        return "material"
    if api_type == "textures":
        return "texture"
    if api_type == "hdris":
        return "hdri"
    if api_type == "models":
        return "mesh"
    return "texture"


def _lazytextures_asset_type(item: Dict, fallback: str = "materials") -> str:
    """Map API singular ``type`` (hdri/model/…) to tree names (hdris/models/…)."""
    for key in ("assetType", "type"):
        value = str(item.get(key) or "").strip().lower()
        if value in VALID_ASSET_TYPES:
            return value
        if value == "material":
            return "materials"
        if value == "model":
            return "models"
        if value == "hdri":
            return "hdris"
        if value == "texture":
            return "textures"
        if value in ("vdb", "vdbs", "volume", "volumes"):
            return "other"
    hint = str(fallback or "materials").strip().lower()
    return hint if hint in VALID_ASSET_TYPES else "materials"


def _result_from_item(item: Dict, asset_type_hint: str = "") -> Optional[AssetResult]:
    if not isinstance(item, dict):
        return None
    name = str(item.get("name") or "").strip()
    if not name:
        return None
    # Prefer hint when browsing a typed tree; API often returns singular ``type``
    # (hdri/model) which must map to hdris/models — never fall through to materials.
    api_type = _lazytextures_asset_type(item, fallback=asset_type_hint or "materials")
    return AssetResult(
        source_id="lazytextures",
        asset_id=_asset_id(api_type, name),
        name=name,
        asset_type=_library_asset_type(api_type),
        thumb_url=_preview_url(item),
        preview_url=_preview_url(item),
        license_name="CC0",
        attribution="LazyTextures / Damongraphics",
        categories=[str(item.get("category") or "")],
        tags=[str(tag) for tag in (item.get("tags") or []) if tag],
        metadata={
            "lazytextures_type": api_type,
            "lazytextures_name": name,
            "lazytextures_basepath": str(item.get("basePath") or ""),
        },
    )


def _iter_nested_file_lists(node: Any) -> List[Dict]:
    if isinstance(node, list):
        return [item for item in node if isinstance(item, dict)]
    if not isinstance(node, dict):
        return []
    files: List[Dict] = []
    for value in node.values():
        if isinstance(value, list):
            files.extend(item for item in value if isinstance(item, dict))
        elif isinstance(value, dict):
            for nested in value.values():
                if isinstance(nested, list):
                    files.extend(item for item in nested if isinstance(item, dict))
    return files


def _entry_from_file_dict(entry: Dict, resolution: str, *, kind: str) -> Optional[Dict]:
    path = str(entry.get("path") or "").strip()
    if not path:
        return None
    if not path.startswith("/"):
        path = "/" + path
    return {
        "resolution": resolution,
        "name": str(entry.get("name") or os.path.basename(path)),
        "path": path,
        "url": "{}{}".format(API_BASE, path),
        "kind": kind,
        "contains_all": bool(entry.get("containsAllAssets")),
    }


def _list_archive_entries(detail: Dict) -> List[Dict]:
    data = detail.get("data") if isinstance(detail.get("data"), dict) else detail
    entries: List[Dict] = []

    def _add_bucket(bucket: Any, *, kind: str) -> None:
        if not isinstance(bucket, dict):
            return
        for resolution_key, files_node in bucket.items():
            resolution = _normalize_resolution(str(resolution_key))
            for entry in _iter_nested_file_lists(files_node):
                built = _entry_from_file_dict(entry, resolution, kind=kind)
                if built is not None:
                    entries.append(built)

    _add_bucket(data.get("archives") or data.get("archiveFiles") or {}, kind="archive")
    _add_bucket(data.get("textures") or {}, kind="map")
    for entry in data.get("models") or []:
        if isinstance(entry, dict):
            built = _entry_from_file_dict(entry, "", kind="model")
            if built is not None:
                entries.append(built)
    unique: Dict[str, Dict] = {}
    for entry in entries:
        unique[str(entry.get("path") or "")] = entry
    entries = list(unique.values())
    entries.sort(
        key=lambda item: (
            _RES_SORT.get(item.get("resolution", ""), 99),
            0 if item.get("kind") == "archive" else 1,
        )
    )
    return entries


def _pick_entry(entries: List[Dict], resolution: Optional[str], fmt: Optional[str]) -> Dict:
    if not entries:
        return {}
    fmt_token = str(fmt or "").strip().lower()
    pool = list(entries)
    if fmt_token == "zip":
        zips = [item for item in pool if str(item.get("name") or "").lower().endswith(".zip")]
        if zips:
            pool = zips
    elif fmt_token in ("hdr", "exr", "fbx", "obj", "gltf", "glb", "vdb"):
        matched = [
            item for item in pool if str(item.get("name") or "").lower().endswith("." + fmt_token)
        ]
        if matched:
            pool = matched
    resolutions = sorted(
        {str(item.get("resolution") or "") for item in pool if item.get("resolution")},
        key=lambda token: _RES_SORT.get(token, 99),
    )
    if resolutions:
        token = str(resolution or "").strip().lower()
        # Empty/auto: prefer mid/2k-class when present, else highest (prefs resolve
        # usually passes an explicit token before download).
        if not token or token == "auto":
            for prefer in ("2k", "1k", "4k", "8k", "6k"):
                if prefer in resolutions:
                    token = prefer
                    break
            else:
                token = resolutions[-1]
        # Accept 2048 / 2K style from Preferences
        if token.isdigit():
            try:
                px = int(token)
                token = {
                    512: "1k",
                    1024: "1k",
                    2048: "2k",
                    4096: "4k",
                    8192: "8k",
                }.get(px, token)
            except ValueError:
                pass
        exact = [item for item in pool if str(item.get("resolution") or "").lower() == token]
        if exact:
            pool = exact
        else:
            # Nearest lower-or-equal by known sort
            from ..download_options import pick_resolution

            picked = pick_resolution(resolutions, token)
            if picked:
                exact = [
                    item
                    for item in pool
                    if str(item.get("resolution") or "").lower() == str(picked).lower()
                ]
                if exact:
                    pool = exact
    pool.sort(key=lambda item: (0 if item.get("contains_all") else 1,))
    return pool[0] if pool else {}


def _find_primary(extract_dir: str, asset_type: str) -> str:
    preferred = ""
    fallback = ""
    for dirpath, _, filenames in os.walk(extract_dir):
        for filename in filenames:
            path = os.path.join(dirpath, filename)
            lowered = filename.lower()
            if asset_type == "hdri" and lowered.endswith((".hdr", ".exr")):
                return path
            if lowered.endswith((".png", ".jpg", ".jpeg", ".tif", ".tiff", ".exr", ".hdr")):
                stem = os.path.splitext(filename)[0].lower()
                if any(key in stem for key in ("albedo", "basecolor", "diffuse", "color")):
                    preferred = path
                elif not fallback:
                    fallback = path
            elif lowered.endswith((".fbx", ".obj", ".gltf", ".glb")) and not fallback:
                fallback = path
    return preferred or fallback


class LazyTexturesSource(AssetSource):
    source_id = "lazytextures"
    display_name = "LazyTextures"

    TYPE_FILTERS = (
        ("All types", ""),
        ("Materials", "materials"),
        ("3D Models", "models"),
        ("HDRIs", "hdris"),
        ("Textures", "textures"),
        ("Other / VDBs", "other"),
    )

    def __init__(self) -> None:
        self._detail_cache: "OrderedDict[str, Dict]" = OrderedDict()

    def get_type_filters(self) -> List[tuple]:
        return list(self.TYPE_FILTERS)

    def search(
        self,
        query: str,
        category: Optional[str] = None,
        page: int = 1,
        page_size: int = 50,
        **kwargs,
    ) -> SearchResults:
        page_size = max(1, min(page_size, 100))
        page = max(1, page)
        offset = (page - 1) * page_size
        category_raw = str(category or "").strip().lower()
        typed = category_raw in VALID_ASSET_TYPES
        # Typed browse uses that API tree. All + empty query has no mixed
        # previews endpoint — materials is the catalog default (Houdini parity).
        asset_type = category_raw if typed else "materials"
        query_value = query.strip()

        if query_value:
            params = {"q": query_value, "limit": str(page_size), "offset": str(offset), "sortBy": "relevance"}
            if typed:
                payload = _http_json("/{}/Search".format(asset_type), params)
            else:
                payload = _http_json("/all/Search", params)
        else:
            payload = _http_json(
                "/previews-paginated/{}".format(asset_type),
                {"offset": str(offset), "limit": str(page_size), "sort": "name-asc"},
            )

        items = payload.get("results") or payload.get("data") or []
        pagination = payload.get("pagination") or {}
        results = []
        for item in items:
            built = _result_from_item(item, asset_type_hint=asset_type)
            if built is not None:
                results.append(built)
        total = int(pagination.get("total") or len(results))
        has_more = bool(pagination.get("hasMore")) or total > offset + len(results)
        return SearchResults(items=results, has_more=has_more, total_count=total)

    def _load_detail(self, asset: AssetResult) -> List[Dict]:
        api_type, name = _split_asset_id(asset.asset_id)
        meta = asset.metadata if isinstance(asset.metadata, dict) else {}
        api_type = str(meta.get("lazytextures_type") or api_type)
        name = str(meta.get("lazytextures_name") or name)
        basepath = str(meta.get("lazytextures_basepath") or "")
        cache_key = _asset_id(api_type, name)
        if cache_key in self._detail_cache:
            self._detail_cache.move_to_end(cache_key)
        else:
            payload: Dict = {}
            try:
                payload = _http_json("/Asset/{}/{}".format(api_type, urllib.parse.quote(name)))
            except Exception:
                payload = {}
            if not payload or not payload.get("data"):
                infos = _fetch_infos_json(basepath, api_type, name)
                if infos:
                    payload = _infos_to_detail(infos)
            lru_put(
                self._detail_cache,
                cache_key,
                payload if isinstance(payload, dict) else {},
                SOURCE_DETAIL_CACHE_MAX,
            )
        return _list_archive_entries(self._detail_cache[cache_key])

    def get_resolutions(self, asset: AssetResult, fmt: Optional[str] = None) -> List[str]:
        entries = self._load_detail(asset)
        resolutions = sorted(
            {
                str(item.get("resolution") or "").lower()
                for item in entries
                if str(item.get("resolution") or "").strip()
            },
            key=lambda token: _RES_SORT.get(token, 99),
        )
        return resolutions or ["1k", "2k", "4k", "8k"]

    def get_formats(self, asset: AssetResult) -> List[str]:
        entries = self._load_detail(asset)
        found = set()
        for entry in entries:
            name = str(entry.get("name") or "").lower()
            if name.endswith(".zip"):
                found.add("zip")
            for token in ("hdr", "exr", "fbx", "obj", "gltf", "glb", "vdb"):
                if name.endswith("." + token):
                    found.add(token)
        return sorted(found) or ["zip"]

    def download(
        self,
        asset: AssetResult,
        resolution: Optional[str],
        fmt: Optional[str],
        dest_folder: str,
        **kwargs,
    ) -> str:
        progress_callback: Optional[Callable[[int, int], None]] = kwargs.get("progress_callback")
        entries = self._load_detail(asset)
        if not entries:
            raise RuntimeError("LazyTextures asset has no downloadable files for {}.".format(asset.name))
        chosen = _pick_entry(entries, resolution, fmt)
        if not chosen:
            raise RuntimeError("No matching LazyTextures download for {}.".format(asset.name))

        chosen_res = str(chosen.get("resolution") or "").lower()
        asset_dir = dest_folder
        os.makedirs(asset_dir, exist_ok=True)

        # Prefer parallel individual map downloads over a single ZIP —
        # LazyTextures ZIPs are uncompressed so maps ≈ ZIP in total bytes
        # but parallel fetches are ~4-6x faster on typical connections.
        fmt_token = str(fmt or "").strip().lower()
        if fmt_token not in ("zip",) and asset.asset_type in ("material", "texture"):
            maps = [
                item
                for item in entries
                if item.get("kind") == "map"
                and str(item.get("resolution") or "").lower() == chosen_res
                and item.get("url")
                and item.get("name")
            ]
            if len(maps) >= 2:
                extract_dir = os.path.join(asset_dir, "extracted")
                os.makedirs(extract_dir, exist_ok=True)
                jobs = [
                    (str(item["url"]), os.path.join(extract_dir, str(item["name"])))
                    for item in maps
                ]
                download_urls_parallel(jobs, max_workers=6, progress_callback=progress_callback)
                primary = _find_primary(extract_dir, asset.asset_type)
                if primary:
                    return primary

        # Fallback: single file (ZIP or lone map/model/hdri)
        download_url = str(chosen.get("url") or "")
        filename = str(chosen.get("name") or os.path.basename(chosen.get("path", "")))
        if not download_url or not filename:
            raise RuntimeError("LazyTextures download URL is missing for {}.".format(asset.name))

        output_path = os.path.join(asset_dir, filename)
        stream_download_file(download_url, output_path, timeout=300, progress_callback=progress_callback)

        local_path = output_path
        try:
            zip_path = ensure_zip_archive_path(output_path)
            extract_dir = os.path.join(asset_dir, "extracted")
            extract_zip_members(zip_path, extract_dir, progress_callback=progress_callback)
            primary = _find_primary(extract_dir, asset.asset_type)
            if primary:
                local_path = primary
        except Exception:
            pass
        return local_path
