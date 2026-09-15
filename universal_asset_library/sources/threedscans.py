# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""Three D Scans source connector — WordPress REST API."""

from __future__ import annotations

import html
import json
import os
import re
import urllib.parse
import urllib.request
from collections import OrderedDict
from typing import Callable, Dict, List, Optional, Tuple

from ..download_utils import ensure_zip_archive_path, extract_zip_members, stream_download_file
from ..http_ssl import urlopen
from ..lru_util import SOURCE_DETAIL_CACHE_MAX, lru_put
from .base import AssetResult, AssetSource, SearchResults

API_BASE = "https://threedscans.com/wp-json/wp/v2"
SITE_URL = "https://threedscans.com"
USER_AGENT = "UniversalAssetLibrary-Blender/0.1"
POST_FIELDS = "id,slug,title,link,categories"


def _http_json_with_meta(url: str, params: Optional[Dict[str, str]] = None) -> Tuple[object, int, int]:
    query = urllib.parse.urlencode(params or {})
    full_url = "{}?{}".format(url, query) if query else url
    request = urllib.request.Request(full_url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=25) as response:
        raw = response.read().decode("utf-8", errors="ignore")
        total = int(response.headers.get("X-WP-Total", 0) or 0)
        pages = int(response.headers.get("X-WP-TotalPages", 0) or 0)
    if not raw:
        return {}, total, pages
    return json.loads(raw), total, pages


def _strip_html(text: str) -> str:
    cleaned = re.sub(r"<[^>]+>", "", str(text or ""))
    return html.unescape(cleaned).strip()


def _is_zip_download(url: str, mime_type: str) -> bool:
    lowered = str(url or "").lower()
    mime = str(mime_type or "").lower()
    return mime == "application/zip" or lowered.endswith(".zip")


def _is_stl_download(url: str, mime_type: str) -> bool:
    lowered = str(url or "").lower()
    mime = str(mime_type or "").lower()
    return lowered.endswith(".stl") or mime in ("application/sla", "model/stl")


def _is_obj_download(url: str, mime_type: str = "") -> bool:
    lowered = str(url or "").lower()
    mime = str(mime_type or "").lower()
    if lowered.endswith(".obj.zip") or lowered.endswith(".obj"):
        return True
    return mime in ("model/obj", "text/plain") and lowered.endswith(".obj")


def _is_ply_download(url: str, mime_type: str = "") -> bool:
    lowered = str(url or "").lower()
    return lowered.endswith(".ply.zip") or lowered.endswith(".ply")


def _mesh_download_rank(url: str, mime_type: str = "") -> int:
    """Lower is better. Site ships STL and OBJ (often as ``*.stl.zip`` / ``*.obj.zip``)."""
    lowered = str(url or "").lower()
    if lowered.endswith(".stl.zip"):
        return 0
    if lowered.endswith(".obj.zip"):
        return 1
    if lowered.endswith(".ply.zip"):
        return 2
    if _is_zip_download(url, mime_type):
        return 3
    if _is_stl_download(url, mime_type):
        return 4
    if _is_obj_download(url, mime_type):
        return 5
    if _is_ply_download(url, mime_type):
        return 6
    return 99


def _is_still_image_url(url: str) -> bool:
    return str(url or "").lower().endswith((".jpg", ".jpeg", ".png", ".webp"))


def _is_gif_url(url: str) -> bool:
    return str(url or "").lower().endswith(".gif")


def _preview_size_urls(item: dict) -> List[str]:
    urls: List[str] = []
    sizes = (item.get("media_details") or {}).get("sizes") or {}
    if not isinstance(sizes, dict):
        return urls
    # Prefer soft-scaled WP sizes (full sculpture). WordPress ``thumbnail``
    # (150×150 hard-crop) zooms into the torso — put it last (Houdini parity).
    for key in ("medium", "medium_large", "large", "thumbnail"):
        entry = sizes.get(key) or {}
        if isinstance(entry, dict):
            url = str(entry.get("source_url") or "").strip()
            if url:
                urls.append(url)
    return urls


def _collect_preview_urls(media_items: List[dict]) -> List[str]:
    """JPEG/PNG sizes first, then full stills, then sized GIFs — never full GIF."""
    still_sized: List[str] = []
    still_full: List[str] = []
    gif_sized: List[str] = []
    seen = set()

    def _add(bucket: List[str], url: str) -> None:
        cleaned = str(url or "").strip()
        if not cleaned:
            return
        key = cleaned.lower()
        if key in seen:
            return
        seen.add(key)
        bucket.append(cleaned)

    for item in media_items or []:
        if not isinstance(item, dict):
            continue
        mime_type = str(item.get("mime_type") or "").lower()
        source_url = str(item.get("source_url") or "").strip()
        if _is_zip_download(source_url, mime_type):
            continue
        sized = _preview_size_urls(item)
        for url in sized:
            if _is_still_image_url(url):
                _add(still_sized, url)
            elif _is_gif_url(url):
                _add(gif_sized, url)
        if source_url and mime_type.startswith("image/") and _is_still_image_url(source_url):
            _add(still_full, source_url)
        # Skip full GIF source_url — often 5–6 MiB and breaks thumb fetch.
    return still_sized + still_full + gif_sized


def _pick_preview_url(media_items: List[dict], urls: Optional[List[str]] = None) -> str:
    collected = urls if urls is not None else _collect_preview_urls(media_items)
    return collected[0] if collected else ""


def _pick_download_url(media_items: List[dict]) -> str:
    """Prefer mesh archives (stl.zip / obj.zip / ply.zip) over loose images.

    WordPress media is newest-first; posts often have many preview images before
    the mesh attachment — callers must fetch enough pages (``per_page`` ≥ 100).
    """
    best_url = ""
    best_rank = 99
    for item in media_items or []:
        if not isinstance(item, dict):
            continue
        source_url = str(item.get("source_url") or "").strip()
        mime_type = str(item.get("mime_type") or "").lower()
        if not source_url:
            continue
        rank = _mesh_download_rank(source_url, mime_type)
        if rank >= 99:
            continue
        if rank < best_rank:
            best_rank = rank
            best_url = source_url
    return best_url


def _fetch_post_media(post_id: str) -> Tuple[str, str, List[str]]:
    # Goethe Lifemask and similar older posts attach 15–20+ media items; the mesh
    # ZIP is often past the first dozen image/GIF attachments.
    payload, _, _ = _http_json_with_meta(
        "{}/media".format(API_BASE),
        {"parent": post_id, "per_page": "100", "_fields": "source_url,mime_type,media_details"},
    )
    if isinstance(payload, list):
        thumbs = _collect_preview_urls(payload)
        return _pick_download_url(payload), _pick_preview_url(payload, urls=thumbs), thumbs
    return "", "", []


_MESH_IMPORT_EXTS = (".stl", ".obj", ".ply", ".fbx", ".gltf", ".glb")


def _find_mesh(extract_dir: str) -> str:
    """Pick the largest importable mesh under *extract_dir* (STL or OBJ packs)."""
    found: List[Tuple[int, int, str]] = []
    priority = {".stl": 0, ".obj": 1, ".ply": 2, ".fbx": 3, ".glb": 4, ".gltf": 5}
    for dirpath, _, filenames in os.walk(extract_dir):
        for filename in filenames:
            if filename.startswith(".") or filename.startswith("._"):
                continue
            lowered = filename.lower()
            # Skip macOS resource forks inside ZIPs
            if "/__macosx/" in (dirpath.replace("\\", "/") + "/").lower():
                continue
            ext = os.path.splitext(lowered)[1]
            if ext not in _MESH_IMPORT_EXTS:
                continue
            path = os.path.join(dirpath, filename)
            try:
                size = int(os.path.getsize(path))
            except OSError:
                size = 0
            found.append((size, priority.get(ext, 9), path))
    if not found:
        return ""
    found.sort(key=lambda row: (-row[0], row[1]))
    return found[0][2]


class ThreeDScansSource(AssetSource):
    source_id = "threedscans"
    display_name = "Three D Scans"

    def __init__(self) -> None:
        self._media_cache: "OrderedDict[str, Dict[str, object]]" = OrderedDict()
        self._categories: Optional[Dict[str, str]] = None

    def get_type_filters(self) -> List[tuple]:
        filters = [("All scans", "")]
        for cat_id, label in sorted(self._load_categories().items(), key=lambda item: item[1].lower()):
            filters.append((label, cat_id))
        return filters

    def _load_categories(self) -> Dict[str, str]:
        if self._categories is not None:
            return self._categories
        mapping: Dict[str, str] = {}
        try:
            payload, _, _ = _http_json_with_meta("{}/categories".format(API_BASE), {"per_page": "100"})
            if isinstance(payload, list):
                for item in payload:
                    if isinstance(item, dict):
                        cat_id = str(item.get("id") or "").strip()
                        raw_name = item.get("name")
                        if isinstance(raw_name, dict):
                            name = _strip_html(raw_name.get("rendered", ""))
                        else:
                            name = _strip_html(str(raw_name or ""))
                        if cat_id and name:
                            mapping[cat_id] = name
        except Exception:
            pass
        self._categories = mapping
        return mapping

    def _enrich(self, post_id: str) -> Dict[str, object]:
        """Lazy media enrich (download + thumb). Refetch if thumb missing (poisoned cache)."""
        post_id = str(post_id or "").strip()
        cached = self._media_cache.get(post_id)
        if (
            isinstance(cached, dict)
            and str(cached.get("download_url") or "").strip()
            and str(cached.get("thumb_url") or "").strip()
        ):
            self._media_cache.move_to_end(post_id)
            return cached
        download_url, thumb_url, thumb_urls = _fetch_post_media(post_id)
        # Keep prior download_url if re-fetch only recovered thumbs
        if isinstance(cached, dict) and not download_url:
            download_url = str(cached.get("download_url") or "")
        entry: Dict[str, object] = {
            "download_url": download_url,
            "thumb_url": thumb_url,
            "thumb_urls": list(thumb_urls or ([] if not thumb_url else [thumb_url])),
        }
        lru_put(self._media_cache, post_id, entry, SOURCE_DETAIL_CACHE_MAX)
        return entry

    def resolve_thumb_url(self, asset_id: str) -> str:
        """Public lazy thumb for Online grid (search leaves thumb_url empty on purpose)."""
        media = self._enrich(asset_id)
        return str(media.get("thumb_url") or "").strip()

    def search(
        self,
        query: str,
        category: Optional[str] = None,
        page: int = 1,
        page_size: int = 50,
        **kwargs,
    ) -> SearchResults:
        params = {
            "per_page": str(max(1, min(page_size, 100))),
            "page": str(max(1, page)),
            "status": "publish",
            "_fields": POST_FIELDS,
        }
        if query.strip():
            params["search"] = query.strip()
        if category:
            params["categories"] = str(category).strip()

        payload, total, total_pages = _http_json_with_meta("{}/posts".format(API_BASE), params)
        if not isinstance(payload, list):
            return SearchResults()

        # Do not block first Search on /categories/ (~1.4s). Labels fill once warm.
        categories = self._categories if isinstance(self._categories, dict) else {}
        results: List[AssetResult] = []
        for post in payload:
            if not isinstance(post, dict):
                continue
            post_id = str(post.get("id") or "")
            if not post_id:
                continue
            # Search stays fast: no per-post media fetch (Houdini lazy enrich).
            # Grid fills thumbs via resolve_thumb_url during thumb worker.
            title = _strip_html((post.get("title") or {}).get("rendered", "")) or post_id
            cat_labels = [
                categories.get(str(cat_id), "")
                for cat_id in (post.get("categories") or [])
                if categories.get(str(cat_id))
            ]
            results.append(
                AssetResult(
                    source_id=self.source_id,
                    asset_id=post_id,
                    name=title,
                    asset_type="mesh",
                    thumb_url="",
                    preview_url=str(post.get("link") or SITE_URL),
                    license_name="Free use",
                    attribution="Three D Scans — {}".format(SITE_URL),
                    categories=cat_labels,
                    tags=["scan", "stl", "obj", "threedscans"],
                    formats=["stl", "obj", "zip"],
                    metadata={"download_url": "", "lazy_thumb": "1"},
                )
            )
        if total <= 0:
            total = len(results)
        has_more = page < total_pages if total_pages > 0 else len(results) >= page_size
        return SearchResults(items=results, has_more=has_more, total_count=total)

    def get_formats(self, asset: AssetResult) -> List[str]:
        media = self._enrich(asset.asset_id)
        url = str(media.get("download_url") or "").lower()
        if ".obj" in url:
            return ["obj", "zip"]
        if ".stl" in url:
            return ["stl", "zip"]
        if ".ply" in url:
            return ["ply", "zip"]
        return ["stl", "obj", "zip"]

    def download(
        self,
        asset: AssetResult,
        resolution: Optional[str],
        fmt: Optional[str],
        dest_folder: str,
        **kwargs,
    ) -> str:
        progress_callback: Optional[Callable[[int, int], None]] = kwargs.get("progress_callback")
        media = self._enrich(asset.asset_id)
        download_url = str(media.get("download_url") or asset.metadata.get("download_url") or "").strip()
        if not download_url:
            raise RuntimeError("Three D Scans download URL is missing for {}.".format(asset.name))

        asset_dir = os.path.join(dest_folder, asset.asset_id)
        os.makedirs(asset_dir, exist_ok=True)
        filename = os.path.basename(urllib.parse.urlparse(download_url).path) or "{}.zip".format(asset.asset_id)
        output_path = os.path.join(asset_dir, filename)
        stream_download_file(
            download_url,
            output_path,
            timeout=300,
            progress_callback=progress_callback,
            source_id="threedscans",
        )

        local_path = output_path
        lowered = output_path.lower()
        if lowered.endswith((".stl", ".obj", ".ply", ".fbx", ".glb", ".gltf")):
            return local_path
        try:
            zip_path = ensure_zip_archive_path(output_path)
            extract_dir = os.path.join(asset_dir, "extracted")
            extract_zip_members(zip_path, extract_dir, progress_callback=progress_callback)
            mesh_path = _find_mesh(extract_dir)
            if mesh_path:
                local_path = mesh_path
            else:
                raise RuntimeError(
                    "Downloaded ZIP for {} but no importable STL/OBJ mesh was found inside.".format(
                        asset.name
                    )
                )
        except Exception as exc:
            if local_path == output_path:
                raise RuntimeError(
                    "Three D Scans download/extract failed for {}: {}".format(asset.name, exc)
                ) from exc
        return local_path
