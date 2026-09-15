# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""Sketchfab source connector (search + download via personal API token)."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from ..download_utils import ensure_zip_archive_path, extract_zip_members, stream_download_file
from ..http_ssl import urlopen
from .base import AssetResult, AssetSource, SearchResults

API_BASE = "https://api.sketchfab.com/v3"
USER_AGENT = "HoudiniAssetLibrary/1.0 (+https://sidefx.com)"
MESH_SUFFIXES = (".glb", ".gltf", ".fbx", ".obj", ".usd", ".usdz", ".stl")
SKETCHFAB_DOWNLOAD_FORMATS = ("glb", "gltf", "usdz", "source")
SKETCHFAB_VIRTUAL_FORMATS = ("fbx", "obj")
SKETCHFAB_UI_FORMAT_ORDER = ("glb", "gltf", "usdz", "fbx", "obj", "source")
# Auto must prefer GLB — Source ZIP → FBX extract is often huge and looks stuck.
SKETCHFAB_AUTO_PRIORITY_WITH_SOURCE = ("glb", "gltf", "usdz", "fbx", "obj", "source")
SKETCHFAB_AUTO_PRIORITY_NO_SOURCE = ("glb", "gltf", "usdz")

_SKETCHFAB_FORMAT_TITLES = {
    "glb": "GLB",
    "gltf": "glTF (ZIP)",
    "usdz": "USDZ",
    "source": "Source ZIP (original)",
    "fbx": "FBX (from Source ZIP)",
    "obj": "OBJ (from Source ZIP)",
}


def _http_json(url: str, token: str, params: Optional[Dict[str, str]] = None) -> Dict:
    query = urllib.parse.urlencode(
        {k: v for k, v in (params or {}).items() if v is not None and v != ""}
    )
    full_url = "{}?{}".format(url, query) if query else url
    request = urllib.request.Request(
        full_url,
        headers={
            "User-Agent": USER_AGENT,
            "Authorization": "Token {}".format(token.strip()),
        },
    )
    with urlopen(request, timeout=45) as response:
        raw = response.read().decode("utf-8", errors="ignore")
    return json.loads(raw) if raw else {}


def _fetch_download_payload(token: str, asset_id: str) -> Dict:
    # No trailing slash — keep Houdini path shape (`/models/{id}/download`)
    return _http_json("{}/models/{}/download".format(API_BASE, asset_id), token)


def _download_cdn_file(
    url: str,
    output_path: str,
    *,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> bool:
    """Stream a Sketchfab presigned CDN URL.

    Do **not** send the API Token — S3 rejects extra Authorization headers.
    """
    return stream_download_file(
        url, output_path, timeout=180, progress_callback=progress_callback
    )


def _pick_thumb_url(thumbnails: Dict) -> str:
    images = thumbnails.get("images") if isinstance(thumbnails, dict) else None
    if not isinstance(images, list) or not images:
        return ""
    scored = []
    for item in images:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        width = int(item.get("width") or 0)
        size = int(item.get("size") or 0)
        # Prefer ~512px; skip oversized gallery masters when smaller exist
        if size and size > 512_000:
            scored.append((abs(width - 512) + 5000 if width else 9999, size, url))
        else:
            scored.append((abs(width - 512) if width else 9999, size, url))
    if not scored:
        return ""
    scored.sort(key=lambda entry: (entry[0], entry[1] or 0))
    return scored[0][2]


def parse_sketchfab_api_formats(payload: Dict) -> List[str]:
    """Return mesh download keys that expose a signed URL for this model."""
    found: List[str] = []
    if not isinstance(payload, dict):
        return found
    for key, section in payload.items():
        lowered = str(key or "").strip().lower()
        if lowered not in SKETCHFAB_DOWNLOAD_FORMATS:
            continue
        if not isinstance(section, dict):
            continue
        if not str(section.get("url") or "").strip():
            continue
        found.append(lowered)
    ordered: List[str] = []
    for key in SKETCHFAB_DOWNLOAD_FORMATS:
        if key in found and key not in ordered:
            ordered.append(key)
    return ordered


def expand_sketchfab_ui_formats(api_formats: Sequence[str]) -> List[str]:
    """Format combo values — add virtual FBX/OBJ when ``source`` is present."""
    api = [
        str(item).lower()
        for item in api_formats
        if str(item).strip() and str(item).lower() in SKETCHFAB_DOWNLOAD_FORMATS
    ]
    expanded = list(api)
    if "source" in expanded:
        for virtual_fmt in SKETCHFAB_VIRTUAL_FORMATS:
            if virtual_fmt not in expanded:
                expanded.append(virtual_fmt)
    ordered: List[str] = []
    for fmt in SKETCHFAB_UI_FORMAT_ORDER:
        if fmt in expanded and fmt not in ordered:
            ordered.append(fmt)
    for fmt in expanded:
        if fmt not in ordered:
            ordered.append(fmt)
    return ordered


def pick_best_sketchfab_format(
    api_formats: Sequence[str],
    preferred: Optional[str] = None,
) -> str:
    """Auto mode: prefer GLB so downloads stay small and reliable."""
    ui_formats = expand_sketchfab_ui_formats(api_formats)
    if not ui_formats:
        return preferred or "glb"
    if preferred and str(preferred).lower() not in ("", "auto"):
        chosen = str(preferred).lower()
        if chosen in ui_formats:
            return chosen
    api = [str(item).lower() for item in api_formats if item]
    priority = (
        SKETCHFAB_AUTO_PRIORITY_WITH_SOURCE
        if "source" in api or "fbx" in ui_formats
        else SKETCHFAB_AUTO_PRIORITY_NO_SOURCE
    )
    for candidate in priority:
        if candidate in ui_formats:
            return candidate
    return ui_formats[0]


def resolve_sketchfab_download_api_key(requested_fmt: str, api_formats: Sequence[str]) -> str:
    """Map UI format (including virtual FBX/OBJ) to a Sketchfab Download API key."""
    fmt = str(requested_fmt or "").strip().lower()
    api = [str(item).lower() for item in api_formats if item]
    if fmt in api:
        return fmt
    if fmt in SKETCHFAB_VIRTUAL_FORMATS and "source" in api:
        return "source"
    return ""


def sketchfab_format_label(fmt: str) -> str:
    lowered = str(fmt or "").strip().lower()
    return _SKETCHFAB_FORMAT_TITLES.get(lowered, lowered.upper() if lowered else "Format")


def _pick_download_url(payload: Dict, candidates: List[str]) -> Tuple[str, str]:
    seen = set()
    for key in candidates:
        if key in seen:
            continue
        seen.add(key)
        section = payload.get(key) if isinstance(payload, dict) else None
        if isinstance(section, dict):
            url = str(section.get("url") or "")
            if url:
                return url, key
    return "", ""


def _dest_ext_for_api_format(api_fmt: str) -> str:
    # glTF package is always a ZIP from Sketchfab
    if api_fmt in ("gltf", "source"):
        return ".zip"
    return ".{}".format(api_fmt or "glb")


def _download_with_presign_retry(
    token: str,
    asset_id: str,
    download_url: str,
    dest_path: str,
    *,
    chosen_fmt: str,
    candidates: List[str],
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> Tuple[str, str]:
    """Stream *download_url*; on CDN failure re-fetch ``/download`` once and retry.

    Presigned S3 URLs expire quickly — a stale link looks like HTTP 403/404.
    """
    try:
        _download_cdn_file(download_url, dest_path, progress_callback=progress_callback)
        return download_url, chosen_fmt
    except Exception as first_exc:
        try:
            payload = _fetch_download_payload(token, asset_id)
        except Exception as refresh_exc:
            raise RuntimeError(
                "Sketchfab download failed ({}). Refreshing the download link also failed: {}.".format(
                    first_exc, refresh_exc
                )
            ) from first_exc
        refreshed_url, refreshed_fmt = _pick_download_url(payload, candidates)
        if not refreshed_url:
            raise RuntimeError(
                "Sketchfab download failed ({}). No fresh download URL was returned — "
                "links expire after a few minutes; try again.".format(first_exc)
            ) from first_exc
        try:
            if os.path.isfile(dest_path):
                os.remove(dest_path)
        except OSError:
            pass
        out_path = dest_path
        if refreshed_fmt and refreshed_fmt != chosen_fmt:
            out_path = os.path.join(
                os.path.dirname(dest_path),
                "{}{}".format(refreshed_fmt, _dest_ext_for_api_format(refreshed_fmt)),
            )
        try:
            _download_cdn_file(refreshed_url, out_path, progress_callback=progress_callback)
        except Exception as second_exc:
            raise RuntimeError(
                "Sketchfab download failed after refreshing the link: {}. "
                "Original error: {}.".format(second_exc, first_exc)
            ) from second_exc
        return refreshed_url, refreshed_fmt or chosen_fmt


def _find_mesh(
    path: str,
    extract_dir: str,
    *,
    prefer_ext: str = "",
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> str:
    lower = path.lower()
    prefer = str(prefer_ext or "").strip().lower().lstrip(".")
    if lower.endswith(MESH_SUFFIXES) and not lower.endswith(".zip"):
        if prefer and not lower.endswith(".{}".format(prefer)):
            pass
        else:
            return path
    try:
        zip_path = ensure_zip_archive_path(path)
        extract_zip_members(zip_path, extract_dir, progress_callback=progress_callback)
        preferred = ""
        fallback = ""
        for dirpath, _, filenames in os.walk(extract_dir):
            for filename in filenames:
                full = os.path.join(dirpath, filename)
                name_l = filename.lower()
                if not name_l.endswith(MESH_SUFFIXES):
                    continue
                if prefer and name_l.endswith(".{}".format(prefer)):
                    preferred = preferred or full
                else:
                    fallback = fallback or full
        return preferred or fallback
    except Exception:
        pass
    return ""


def _http_error_message(exc: urllib.error.HTTPError, *, what: str) -> str:
    if exc.code == 429:
        return (
            "Sketchfab rate-limited this {} (HTTP 429). Wait about a minute, then try again. "
            "Prefer Format GLB (Auto) over FBX/Source ZIP.".format(what)
        )
    if exc.code in (401, 403):
        return (
            "Sketchfab {} denied (HTTP {}). Check the API token in Preferences."
            .format(what, exc.code)
        )
    if exc.code == 404:
        return (
            "Sketchfab {} returned HTTP 404 — model may be private, removed, or not "
            "downloadable with this token.".format(what)
        )
    return "Sketchfab {} failed (HTTP {}).".format(what, exc.code)


class SketchfabSource(AssetSource):
    source_id = "sketchfab"
    display_name = "Sketchfab"
    requires_token = True

    TYPE_FILTERS = (("All types", ""), ("Models", "models"))

    def __init__(self) -> None:
        self._api_token = ""
        self._cursors: List[Optional[str]] = [None]
        self._cursor_query_key = ""

    def configure(self, source_cfg: Optional[Dict[str, str]] = None) -> None:
        cfg = source_cfg or {}
        self._api_token = str(cfg.get("api_token", "") or "").strip()

    def _token_from_kwargs(self, kwargs: Dict) -> str:
        return str(kwargs.get("token") or kwargs.get("api_token") or self._api_token or "").strip()

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
        token = self._token_from_kwargs(kwargs)
        if not token:
            raise RuntimeError(
                "Sketchfab requires a personal API token. Pass token=… or configure it in addon preferences."
            )

        query_key = ((query or "").strip().lower(), (category or "").strip().lower())
        if page <= 1 or query_key != self._cursor_query_key:
            self._cursor_query_key = query_key
            self._cursors = [None]

        cursor = None
        if page > 1:
            if page - 1 >= len(self._cursors) or not self._cursors[page - 1]:
                return SearchResults()
            cursor = self._cursors[page - 1]

        params = {
            "type": (category or "models").strip() or "models",
            "q": (query or "").strip(),
            "count": str(max(1, min(page_size, 50))),
            "downloadable": "true",
        }
        if cursor:
            params["cursor"] = cursor

        try:
            # No trailing slash on /search (Fab-style slash bugs do not apply here,
            # but keep the documented path shape).
            payload = _http_json("{}/search".format(API_BASE), token, params=params)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(_http_error_message(exc, what="search")) from exc

        next_cursor = ""
        cursors = payload.get("cursors")
        if isinstance(cursors, dict):
            next_cursor = str(cursors.get("next") or "")
        while len(self._cursors) < page:
            self._cursors.append(None)
        if next_cursor:
            if len(self._cursors) <= page:
                self._cursors.append(next_cursor)
            else:
                self._cursors[page] = next_cursor

        items: List[AssetResult] = []
        for entry in payload.get("results", []) or []:
            if not isinstance(entry, dict) or not bool(entry.get("isDownloadable", False)):
                continue
            uid = str(entry.get("uid") or "")
            if not uid:
                continue
            user_info = entry.get("user") if isinstance(entry.get("user"), dict) else {}
            username = str(user_info.get("username") or "")
            license_info = entry.get("license") if isinstance(entry.get("license"), dict) else {}
            items.append(
                AssetResult(
                    source_id=self.source_id,
                    asset_id=uid,
                    name=str(entry.get("name") or uid),
                    asset_type="mesh",
                    thumb_url=_pick_thumb_url(entry.get("thumbnails") or {}),
                    preview_url=str(entry.get("viewerUrl") or ""),
                    license_name=str(license_info.get("label") or "Sketchfab"),
                    attribution="by {}".format(username) if username else "",
                    formats=["glb", "gltf", "usdz", "source"],
                    metadata={"downloadable": "1"},
                )
            )
        total = int(payload.get("count") or len(items))
        return SearchResults(items=items, has_more=bool(next_cursor), total_count=total)

    def get_formats(self, asset: AssetResult) -> List[str]:
        cached = asset.metadata.get("_cached_download_formats") if asset.metadata else None
        if isinstance(cached, list) and cached:
            return [str(x) for x in cached]
        return ["glb", "gltf", "usdz", "source"]

    def download(
        self,
        asset: AssetResult,
        resolution: Optional[str],
        fmt: Optional[str],
        dest_folder: str,
        **kwargs,
    ) -> str:
        token = self._token_from_kwargs(kwargs)
        if not token:
            raise RuntimeError("Sketchfab API token is required for downloads (token kwarg).")
        progress_callback: Optional[Callable[[int, int], None]] = kwargs.get("progress_callback")

        asset_dir = os.path.join(dest_folder, asset.asset_id)
        os.makedirs(asset_dir, exist_ok=True)

        try:
            payload = _fetch_download_payload(token, asset.asset_id)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(_http_error_message(exc, what="download")) from exc

        api_formats = parse_sketchfab_api_formats(payload)
        if not api_formats:
            api_formats = list(SKETCHFAB_DOWNLOAD_FORMATS)

        ui_formats = expand_sketchfab_ui_formats(api_formats) or ["glb", "gltf"]
        if isinstance(asset.metadata, dict):
            asset.metadata["_cached_download_formats"] = ui_formats
            asset.formats = list(ui_formats)

        requested = str(fmt or "").strip().lower()
        if requested in ("", "auto"):
            requested = pick_best_sketchfab_format(api_formats)
        elif requested not in ui_formats:
            requested = pick_best_sketchfab_format(api_formats, preferred=requested)

        api_key = resolve_sketchfab_download_api_key(requested, api_formats)
        candidates: List[str] = []
        if api_key:
            candidates.append(api_key)
        for fallback in ("glb", "gltf", "usdz", "source"):
            if fallback not in candidates:
                candidates.append(fallback)

        download_url, chosen_fmt = _pick_download_url(payload, candidates)
        if not download_url:
            raise RuntimeError("No downloadable format returned for this Sketchfab model.")

        extract_prefer = requested if requested in SKETCHFAB_VIRTUAL_FORMATS else ""
        dest_path = os.path.join(
            asset_dir, "{}{}".format(chosen_fmt, _dest_ext_for_api_format(chosen_fmt))
        )
        _url, chosen_fmt = _download_with_presign_retry(
            token,
            asset.asset_id,
            download_url,
            dest_path,
            chosen_fmt=chosen_fmt,
            candidates=candidates,
            progress_callback=progress_callback,
        )
        dest_path = os.path.join(
            asset_dir, "{}{}".format(chosen_fmt, _dest_ext_for_api_format(chosen_fmt))
        )

        local_path = _find_mesh(
            dest_path,
            os.path.join(asset_dir, "extracted"),
            prefer_ext=extract_prefer,
            progress_callback=progress_callback,
        )
        if not local_path:
            if extract_prefer:
                raise RuntimeError(
                    "Sketchfab source archive downloaded but no .{} file could be extracted. "
                    "Try GLB or another format.".format(extract_prefer)
                )
            raise RuntimeError(
                "Sketchfab archive downloaded but no glTF/GLB/OBJ/FBX file could be extracted."
            )
        return local_path
