# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""Poly Pizza source connector."""

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

API_BASE = "https://api.poly.pizza/v1.1"
USER_AGENT = "UniversalAssetLibrary-Blender/0.1"

POLYPIZZA_CATEGORIES = (
    ("All categories", ""),
    ("Nature", "Nature"),
    ("Animals", "Animals"),
    ("People & Characters", "PeopleAndCharacters"),
    ("Buildings & Architecture", "BuildingsAndArchitecture"),
    ("Furniture & Decor", "FurnitureAndDecor"),
    ("Food & Drink", "FoodAndDrink"),
    ("Transport", "Transport"),
    ("Weapons", "Weapons"),
    ("Objects", "Objects"),
    ("Clutter", "Clutter"),
    ("Scenes & Levels", "ScenesAndLevels"),
    ("Other", "Other"),
)


def _first_str(item: Dict, *keys: str) -> str:
    for key in keys:
        value = item.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _http_json(path: str, api_token: str, params: Optional[Dict[str, str]] = None) -> Dict:
    token = str(api_token or "").strip()
    if not token:
        raise RuntimeError(
            "Poly Pizza API key required. Add it in addon preferences "
            "(https://poly.pizza/settings/api)."
        )
    query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v not in (None, "")})
    url = "{}{}".format(API_BASE, path)
    if query:
        url = "{}?{}".format(url, query)
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "X-Auth-Token": token,
        },
    )
    with urlopen(request, timeout=45) as response:
        raw = response.read().decode("utf-8", errors="ignore")
    payload = json.loads(raw) if raw else {}
    return payload if isinstance(payload, dict) else {}


def _result_from_item(item: Dict) -> Optional[AssetResult]:
    if not isinstance(item, dict):
        return None
    asset_id = _first_str(item, "ID", "Id", "id")
    title = _first_str(item, "Title", "title", "Name", "name")
    if not asset_id or not title:
        return None
    download = _first_str(item, "Download", "download")
    creator = item.get("Creator") or item.get("creator") or {}
    creator_name = ""
    if isinstance(creator, dict):
        creator_name = _first_str(creator, "Username", "username", "Name", "name")
    return AssetResult(
        source_id="polypizza",
        asset_id=asset_id,
        name=title,
        asset_type="mesh",
        thumb_url=_first_str(item, "Thumbnail", "thumbnail"),
        preview_url=_first_str(item, "Thumbnail", "thumbnail"),
        license_name=_first_str(item, "Licence", "License", "license") or "CC-BY",
        attribution=creator_name or "Poly Pizza",
        categories=[_first_str(item, "Category", "category")] if _first_str(item, "Category", "category") else [],
        tags=[str(tag) for tag in (item.get("Tags") or item.get("tags") or []) if tag],
        formats=["glb"],
        metadata={"polypizza_download": download, "creator": creator_name},
    )


class PolyPizzaSource(AssetSource):
    source_id = "polypizza"
    display_name = "Poly Pizza"
    requires_token = True

    def __init__(self) -> None:
        self._api_token = ""
        self._model_cache: "OrderedDict[str, Dict]" = OrderedDict()

    def configure(self, source_cfg: Optional[Dict[str, str]] = None) -> None:
        cfg = source_cfg or {}
        self._api_token = str(cfg.get("api_token", "") or "").strip()

    def _token_from_kwargs(self, kwargs: Dict) -> str:
        return str(kwargs.get("api_key") or kwargs.get("api_token") or self._api_token or "").strip()

    def get_type_filters(self) -> List[tuple]:
        return list(POLYPIZZA_CATEGORIES)

    def _load_model(self, asset_id: str, api_token: str) -> Dict:
        key = str(asset_id or "").strip()
        if key in self._model_cache:
            self._model_cache.move_to_end(key)
            return self._model_cache[key]
        payload = _http_json("/model/{}".format(urllib.parse.quote(key, safe="")), api_token)
        lru_put(
            self._model_cache,
            key,
            payload if isinstance(payload, dict) else {},
            SOURCE_DETAIL_CACHE_MAX,
        )
        return self._model_cache[key]

    def search(
        self,
        query: str,
        category: Optional[str] = None,
        page: int = 1,
        page_size: int = 50,
        **kwargs,
    ) -> SearchResults:
        api_token = self._token_from_kwargs(kwargs)
        if not api_token:
            raise RuntimeError(
                "Poly Pizza requires an API key. Pass api_key=… or configure it in addon preferences."
            )
        page = max(1, int(page or 1))
        page_size = max(1, min(int(page_size or 32), 32))
        api_page = max(0, page - 1)
        query_value = str(query or "").strip()
        category_value = str(category or "").strip()
        params = {"limit": str(page_size), "page": str(api_page)}
        if category_value:
            params["category"] = category_value
        # Empty keyword browse: API requires License, Animated, or Category
        # (HTTP 400 otherwise). Keyword path /search/{q} does not need this.
        if not query_value and not category_value:
            params["Animated"] = "0"
        path = (
            "/search/{}".format(urllib.parse.quote(query_value, safe=""))
            if query_value
            else "/search"
        )
        payload = _http_json(path, api_token, params=params)
        raw_results = payload.get("results") or payload.get("Results") or []
        items: List[AssetResult] = []
        for row in raw_results:
            built = _result_from_item(row if isinstance(row, dict) else {})
            if built is not None:
                items.append(built)
        total = int(payload.get("total") or payload.get("Total") or len(items))
        offset = api_page * page_size
        return SearchResults(items=items, has_more=offset + len(items) < total, total_count=total)

    def get_formats(self, asset: AssetResult) -> List[str]:
        return ["glb"]

    def download(
        self,
        asset: AssetResult,
        resolution: Optional[str],
        fmt: Optional[str],
        dest_folder: str,
        **kwargs,
    ) -> str:
        api_token = self._token_from_kwargs(kwargs)
        if not api_token:
            raise RuntimeError("Poly Pizza API key is required for downloads (api_key kwarg).")
        progress_callback: Optional[Callable[[int, int], None]] = kwargs.get("progress_callback")

        download_url = str(asset.metadata.get("polypizza_download") or "").strip()
        if not download_url:
            model = self._load_model(asset.asset_id, api_token)
            download_url = _first_str(model, "Download", "download")
        if not download_url:
            download_url = "{}/model/{}/download?format=glb".format(
                API_BASE,
                urllib.parse.quote(asset.asset_id, safe=""),
            )

        asset_dir = os.path.join(dest_folder, asset.asset_id)
        os.makedirs(asset_dir, exist_ok=True)
        output_path = os.path.join(asset_dir, "{}.glb".format(asset.asset_id))
        stream_download_file(
            download_url,
            output_path,
            timeout=180,
            extra_headers={"X-Auth-Token": api_token},
            progress_callback=progress_callback,
        )

        local_path = output_path
        try:
            zip_path = ensure_zip_archive_path(output_path)
            extract_dir = os.path.join(asset_dir, "extracted")
            extract_zip_members(zip_path, extract_dir, progress_callback=progress_callback)
            for dirpath, _, filenames in os.walk(extract_dir):
                for filename in filenames:
                    if filename.lower().endswith((".glb", ".gltf")):
                        local_path = os.path.join(dirpath, filename)
                        break
                if local_path != output_path:
                    break
        except Exception:
            pass
        if not os.path.isfile(local_path):
            raise RuntimeError("Poly Pizza download failed for {}.".format(asset.name))
        return local_path
