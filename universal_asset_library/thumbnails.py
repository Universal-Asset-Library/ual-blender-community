# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""Thumbnail cache, preview discovery, and Blender PreviewCollection icons.

Mirrors Houdini UAL rules:
- Disk cache under ``{cache}/thumbs/`` (local fingerprint + ``online_*.png``)
- Background threads may only sniff magic bytes / write files
- ``bpy.utils.previews`` load happens on the main thread only
"""

from __future__ import annotations

import hashlib
import os
import urllib.request
from collections import OrderedDict
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from . import paths
from .download_url_policy import (
    assert_allowed_download_url,
    open_allowed_download,
    resolve_download_max_bytes,
)

ONLINE_THUMB_DISK_MAX_FILES = 1500
# CDN browse thumbs (Poly Haven / Fab) often exceed 1 MiB at full size.
# Cap high enough for those, still rejecting multi‑MB WP full GIFs (~5–6 MiB).
ONLINE_THUMB_MAX_DOWNLOAD_BYTES = 4_194_304  # 4 MiB
ONLINE_THUMB_MAX_DISK_BYTES = 4_194_304  # 4 MiB — keep cache usable for PreviewCollection
# Soft prune only every N successful writes (Houdini parity — not every batch)
ONLINE_THUMB_PRUNE_EVERY_WRITES = 24
# Parallel Online browse fetch (Houdini source_tabs parity)
ONLINE_THUMB_FETCH_WORKERS = 6
ONLINE_THUMB_BATCH_SIZE = 12
THUMB_SIZE_DEFAULT = 96
THUMB_SIZE_MIN = 64
THUMB_SIZE_MAX = 160
PREVIEW_SIZE_DEFAULT = 320
# Soft cap on bpy.utils.previews entries (LRU eviction)
PREVIEW_COLLECTION_MAX = 256
_ONLINE_THUMB_WRITES = 0

_COVER_TYPES = frozenset({"texture", "hdri", "material_set"})
# PreviewCollection loads these reliably; EXR/HDR often show as black squares in the N-panel.
_SAFE_PREVIEW_EXT = {".png", ".jpg", ".jpeg", ".webp", ".tga", ".bmp", ".tif", ".tiff"}
_HDR_PREVIEW_EXT = {".exr", ".hdr"}
_IMAGE_EXT = _SAFE_PREVIEW_EXT | _HDR_PREVIEW_EXT
_PREVIEW_SUBDIRS = (
    "03_Preview",
    "Preview",
    "preview",
    "previews",
    "thumbnails",
    "Thumbnails",
)
_TEXTURE_SUBDIRS = (
    "textures",
    "Textures",
    "maps",
    "Maps",
    "materials",
    "Materials",
)
_PREVIEW_KEYWORDS = ("thumbnail", "preview", "thumb", "_preview")
_ALBEDO_KEYWORDS = (
    "albedo", "basecolor", "base_color", "diffuse", "color", "_col", "_diff", "diff_",
)
# Never use these as the Library mesh tile preview (they showed as purple/orange map tiles)
_MAP_ONLY_KEYWORDS = (
    "normal", "nor_gl", "nor_dx", "_nor", "_nrm", "rough", "metal", "orm", "arm",
    "disp", "height", "occlusion", "_ao", "opacity", "alpha", "specular", "bump",
    "explosive",  # Poly Haven multi-slot stems still look like UV layouts in the grid
)
_BUNDLE_NAME = ".assetlibrary.bundle.json"
# Sources where we can rebuild the Online CDN thumb URL without a stored thumb_url.
# Poly Haven Bunny optimizer: sized thumbs stay under the download cap.
_THUMB_URL_TEMPLATES = {
    "polyhaven": (
        "https://cdn.polyhaven.com/asset_img/thumbs/{asset_id}.png"
        "?width=512&height=512"
    ),
}

# Lazily created on register (requires bpy)
_previews = None
_preview_keys: Dict[str, str] = {}
_preview_lru: "OrderedDict[str, None]" = OrderedDict()


def clamp_thumb_size(value: int) -> int:
    try:
        return max(THUMB_SIZE_MIN, min(int(value), THUMB_SIZE_MAX))
    except (TypeError, ValueError):
        return THUMB_SIZE_DEFAULT


def thumbnail_fit_mode(asset_type: str) -> str:
    """cover for textures/HDRIs/materials; contain for meshes (Houdini parity)."""
    if (asset_type or "").lower() in _COVER_TYPES:
        return "cover"
    return "contain"


def looks_like_image_bytes(data: bytes) -> bool:
    if not data or len(data) < 8:
        return False
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return True
    if data[:3] == b"\xff\xd8\xff":
        return True
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return True
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return True
    return False


def looks_like_blender_safe_thumb_bytes(data: bytes) -> bool:
    """PNG/JPEG/GIF only — WebP/AVIF often poison PreviewCollection (black tiles)."""
    if not data or len(data) < 8:
        return False
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return True
    if data[:3] == b"\xff\xd8\xff":
        return True
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return True
    return False


def local_thumb_path(cache_dir: str, fingerprint: str) -> str:
    folder = paths.ensure_dir(paths.thumbs_dir(cache_dir))
    safe = "".join(c if c.isalnum() else "_" for c in (fingerprint or "x"))[:40]
    return os.path.join(folder, f"{safe}.png")


def online_thumb_cache_key(source_id: str, asset_id: str) -> str:
    """Stable disk key (Houdini parity) — URL must not be part of the hash.

    Including the CDN URL broke second-search reuse (esp. Three D Scans where
    search leaves ``thumb_url`` empty until lazy enrich).
    """
    sid = str(source_id or "").strip().lower()
    aid = str(asset_id or "").strip()
    if sid == "threedscans":
        return "threedscans:preview_v2:{}".format(aid)
    return "{}:{}".format(sid, aid)


def thumb_cache_path(cache_dir: str, key: str, ext: str = ".png") -> str:
    digest = hashlib.sha1(key.encode("utf-8", errors="ignore")).hexdigest()[:16]
    folder = paths.ensure_dir(paths.thumbs_dir(cache_dir))
    return os.path.join(folder, f"online_{digest}{ext}")


def online_thumb_disk_path(cache_dir: str, source_id: str, asset_id: str) -> str:
    return thumb_cache_path(cache_dir, online_thumb_cache_key(source_id, asset_id))


def compact_online_thumb_url_candidates(
    thumb_url: str,
    source_id: str = "",
    asset_id: str = "",
) -> List[str]:
    """Prefer CDN-resized URLs so browse thumbs stay under the download cap.

    Poly Haven full ``/thumbs/{id}.png`` often exceeds 1–2 MiB; ``?width=&height=``
    uses their image optimizer (same pattern as polyhaven.com / Public API docs).
    """
    sid = (source_id or "").strip().lower()
    aid = (asset_id or "").strip()
    url = (thumb_url or "").strip()
    out: List[str] = []

    def _add(u: str) -> None:
        u = (u or "").strip()
        if u and u not in out:
            out.append(u)

    if sid == "polyhaven" and aid:
        base = f"https://cdn.polyhaven.com/asset_img/thumbs/{aid}.png"
        _add(f"{base}?width=512&height=512")
        _add(f"{base}?width=256&height=256")
        _add(base)
    if url:
        lower = url.lower()
        if "cdn.polyhaven.com/asset_img/thumbs/" in lower:
            base = url.split("?", 1)[0]
            _add(f"{base}?width=512&height=512")
            _add(f"{base}?width=256&height=256")
            _add(base)
        else:
            # PreviewCollection: PNG/JPEG/GIF only (``looks_like_blender_safe_thumb_bytes``).
            # Poly Pizza (and some other CDNs) list ``.webp``; sibling ``.jpg`` is usually
            # available and loads — try those before the WebP URL (which would be skipped).
            path_part, sep, query = url.partition("?")
            if path_part.lower().endswith(".webp"):
                stem = path_part[: -len(".webp")]
                q = (sep + query) if sep else ""
                _add(stem + ".jpg" + q)
                _add(stem + ".jpeg" + q)
                _add(stem + ".png" + q)
            _add(url)
    if not out and sid in _THUMB_URL_TEMPLATES and aid:
        _add(_THUMB_URL_TEMPLATES[sid].format(asset_id=aid))
    return out


def _touch_mtime(path: str) -> None:
    try:
        os.utime(path, None)
    except OSError:
        pass


def _online_thumb_cache_is_usable(path: str) -> bool:
    if not path or not os.path.isfile(path):
        return False
    try:
        size = os.path.getsize(path)
    except OSError:
        return False
    return 32 < size <= ONLINE_THUMB_MAX_DISK_BYTES


def fingerprint_for_path(path: str) -> str:
    return hashlib.sha1(os.path.normcase(os.path.normpath(path or "")).encode("utf-8")).hexdigest()[:20]


def prune_unusable_online_thumbs(cache_dir: str) -> int:
    """Delete ``online_*`` files that fail size/usability checks (corrupt / oversized)."""
    folder = paths.thumbs_dir(cache_dir)
    if not os.path.isdir(folder):
        return 0
    removed = 0
    for name in os.listdir(folder):
        if not name.startswith("online_"):
            continue
        full = os.path.join(folder, name)
        if _online_thumb_cache_is_usable(full):
            continue
        try:
            if os.path.isfile(full):
                os.remove(full)
                removed += 1
        except OSError:
            pass
    return removed


def soft_prune_thumbs(
    cache_dir: str,
    max_files: int = ONLINE_THUMB_DISK_MAX_FILES,
    *,
    force: bool = False,
) -> int:
    """Delete oldest ``online_*.png`` when over *max_files*.

    By default only runs every ``ONLINE_THUMB_PRUNE_EVERY_WRITES`` writes so
    visible thumbs are not thrashed after every search batch. Always drops
    unusable/oversized online thumbs when a prune pass runs.
    """
    global _ONLINE_THUMB_WRITES
    if not force:
        if _ONLINE_THUMB_WRITES <= 0 or (_ONLINE_THUMB_WRITES % ONLINE_THUMB_PRUNE_EVERY_WRITES) != 0:
            return 0
    folder = paths.thumbs_dir(cache_dir)
    if not os.path.isdir(folder):
        return 0
    removed = prune_unusable_online_thumbs(cache_dir)
    files = []
    for name in os.listdir(folder):
        if not name.startswith("online_"):
            continue
        full = os.path.join(folder, name)
        try:
            files.append((os.path.getmtime(full), full))
        except OSError:
            continue
    if len(files) <= max_files:
        return removed
    files.sort()
    for _mtime, full in files[: len(files) - max_files]:
        try:
            os.remove(full)
            removed += 1
        except OSError:
            pass
    return removed


def package_preview_path(folder: str) -> str:
    """Canonical Online-style thumb path inside a library package folder."""
    return os.path.join(folder, "Preview", "thumb_preview.png")


def read_bundle_manifest(folder: str) -> Dict:
    path = os.path.join(folder or "", _BUNDLE_NAME)
    if not os.path.isfile(path):
        return {}
    try:
        import json

        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def write_bundle_fields(folder: str, updates: Dict) -> None:
    """Merge fields into ``.assetlibrary.bundle.json`` (best-effort)."""
    if not folder or not updates or not os.path.isdir(folder):
        return
    import json

    data = read_bundle_manifest(folder)
    data.update({k: v for k, v in updates.items() if v})
    path = os.path.join(folder, _BUNDLE_NAME)
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
    except OSError:
        pass


def infer_online_asset_id(folder: str, bundle: Optional[Dict] = None) -> str:
    """Best-effort asset_id for CDN thumb repair (Poly Haven stems, etc.)."""
    bundle = bundle or read_bundle_manifest(folder)
    aid = str(bundle.get("asset_id") or "").strip()
    if aid:
        return aid
    try:
        names = os.listdir(folder)
    except OSError:
        names = []
    import re

    for name in names:
        lower = name.lower()
        if lower.endswith((".gltf", ".glb", ".fbx", ".obj", ".usd", ".usdc", ".usda")):
            stem = os.path.splitext(name)[0]
            stem = re.sub(r"_[1248]k$", "", stem, flags=re.IGNORECASE)
            return stem
    name = str(bundle.get("name") or os.path.basename(folder) or "").strip()
    return name.replace(" ", "") if name else ""


def reconstruct_thumb_url(source_id: str, asset_id: str) -> str:
    """Rebuild Online CDN thumb URL when the bundle omitted thumb_url."""
    sid = (source_id or "").strip().lower()
    aid = (asset_id or "").strip()
    if not sid or not aid:
        return ""
    tmpl = _THUMB_URL_TEMPLATES.get(sid)
    if not tmpl:
        return ""
    return tmpl.format(asset_id=aid)


def ensure_online_style_preview(
    asset_path: str,
    cache_dir: str,
    *,
    asset_type: str = "",
    fetch: bool = True,
) -> str:
    """Make Library use the same CDN thumb Online shows.

    Writes ``Preview/thumb_preview.png`` beside the package (safe on worker threads —
    HTTP + file I/O only, no bpy).
    """
    path = os.path.normpath(asset_path or "")
    if not path:
        return ""
    folder = path if os.path.isdir(path) else os.path.dirname(path)
    if not folder or not os.path.isdir(folder):
        return ""

    embedded = package_preview_path(folder)
    if _is_displayable_preview(embedded):
        return embedded

    bundle = read_bundle_manifest(folder)
    source_id = str(bundle.get("source_id") or "").strip()
    asset_id = infer_online_asset_id(folder, bundle)
    thumb_url = str(bundle.get("thumb_url") or "").strip()
    if not thumb_url:
        thumb_url = reconstruct_thumb_url(source_id, asset_id)

    # Already cached from an Online search session?
    # Prefer stable source:asset key (current writer); fall back to legacy URL-keyed path.
    if cache_dir and source_id and asset_id:
        candidates = [online_thumb_disk_path(cache_dir, source_id, asset_id)]
        if thumb_url:
            candidates.append(
                thumb_cache_path(cache_dir, f"{source_id}:{asset_id}:{thumb_url}")
            )
        for disk in candidates:
            if not _is_displayable_preview(disk):
                continue
            from .online_download import embed_online_preview

            embed_online_preview(folder, disk)
            write_bundle_fields(
                folder,
                {
                    "asset_id": asset_id,
                    "thumb_url": thumb_url,
                    "has_embedded_preview": True,
                },
            )
            if _is_displayable_preview(embedded):
                return embedded
            return disk

    if not fetch or not thumb_url or not cache_dir:
        return ""

    disk = fetch_online_thumb_to_disk(cache_dir, source_id or "local", asset_id or "asset", thumb_url)
    if not disk:
        return ""
    from .online_download import embed_online_preview

    embed_online_preview(folder, disk)
    write_bundle_fields(
        folder,
        {"asset_id": asset_id, "thumb_url": thumb_url, "has_embedded_preview": True},
    )
    if _is_displayable_preview(embedded):
        return embedded
    return disk if _is_displayable_preview(disk) else ""


def _score_preview_file(path: str, *, asset_type: str = "") -> int:
    name = os.path.basename(path).lower()
    ext = os.path.splitext(name)[1].lower()
    score = 0
    # Embedded Online CDN thumb wins everything
    if name == "thumb_preview.png" or name.startswith("thumb_preview"):
        score += 200
    for index, keyword in enumerate(_PREVIEW_KEYWORDS):
        if keyword in name:
            score += 20 - index
    if "thumbnail" in name:
        score += 10
    if name.endswith("_preview.png"):
        score += 8
    if any(k in name for k in _ALBEDO_KEYWORDS):
        score += 25
    # Data maps / Poly Haven UV layouts must not win mesh thumbs
    if any(k in name for k in _MAP_ONLY_KEYWORDS):
        if not any(k in name for k in _ALBEDO_KEYWORDS):
            score -= 80
        else:
            # e.g. explosive_diff — still a flat map, not the Online render
            at = (asset_type or "").lower()
            if at in {"mesh", "hdri"}:
                score -= 60
    at = (asset_type or "").lower()
    if at == "mesh" and any(k in name for k in _MAP_ONLY_KEYWORDS):
        score -= 40
    if ext in _SAFE_PREVIEW_EXT:
        score += 10
    if ext in _HDR_PREVIEW_EXT:
        score -= 50
    try:
        size = os.path.getsize(path)
        if size < 500_000:
            score += 5
        elif size > 8_000_000:
            score -= 5
    except OSError:
        pass
    return score


def _iter_images(directory: str, *, safe_only: bool = False) -> Iterable[str]:
    if not directory or not os.path.isdir(directory):
        return
    try:
        names = os.listdir(directory)
    except OSError:
        return
    allow = _SAFE_PREVIEW_EXT if safe_only else _IMAGE_EXT
    for name in names:
        full = os.path.join(directory, name)
        if not os.path.isfile(full):
            continue
        if os.path.splitext(name)[1].lower() in allow:
            yield full


def discover_preview_image(asset_path: str, asset_type: str = "") -> str:
    """Find a preview image beside an asset.

    Prefer ``Preview/thumb_preview.png`` (Online CDN embed). For meshes/HDRIs do
    **not** fall back to PBR maps (those looked like UV/normal tiles in Library).
    Material/texture packs may still use albedo as a last resort.
    """
    path = os.path.normpath(asset_path or "")
    if not path:
        return ""
    at = (asset_type or "").lower()
    allow_map_fallback = at in {"material_set", "texture"}

    # Fast path: embedded Online thumb
    folder = path if os.path.isdir(path) else os.path.dirname(path)
    if folder:
        embedded = package_preview_path(folder)
        if _is_displayable_preview(embedded):
            return embedded

    if os.path.isfile(path):
        ext = os.path.splitext(path)[1].lower()
        if ext in _SAFE_PREVIEW_EXT:
            if at in {"texture", "hdri"} or not at:
                return path
            stem = os.path.splitext(os.path.basename(path))[0].lower()
            if allow_map_fallback and not any(k in stem for k in _MAP_ONLY_KEYWORDS):
                return path
        elif ext in _HDR_PREVIEW_EXT:
            return ""
        roots = [os.path.dirname(path)]
    elif os.path.isdir(path):
        roots = [path]
    else:
        return ""

    extracted = os.path.join(roots[0], "extracted")
    if os.path.isdir(extracted):
        roots.insert(0, extracted)

    candidates: List[Tuple[int, str]] = []
    for root in roots:
        for sub in _PREVIEW_SUBDIRS:
            subdir = os.path.join(root, sub)
            for img in _iter_images(subdir, safe_only=True):
                candidates.append((_score_preview_file(img, asset_type=at) + 100, img))
        if allow_map_fallback:
            for sub in _TEXTURE_SUBDIRS:
                subdir = os.path.join(root, sub)
                for img in _iter_images(subdir, safe_only=True):
                    stem = os.path.splitext(os.path.basename(img))[0].lower()
                    bonus = 20 if any(k in stem for k in _ALBEDO_KEYWORDS) else 5
                    candidates.append((_score_preview_file(img, asset_type=at) + bonus, img))
            for img in _iter_images(root, safe_only=True):
                candidates.append((_score_preview_file(img, asset_type=at), img))
            for img in _iter_images(root, safe_only=True):
                stem = os.path.splitext(os.path.basename(img))[0].lower()
                if any(k in stem for k in _ALBEDO_KEYWORDS):
                    candidates.append((_score_preview_file(img, asset_type=at) + 18, img))
    if not candidates:
        return ""
    positive = [c for c in candidates if c[0] > 0]
    pool = positive or candidates
    pool.sort(key=lambda t: (-t[0], t[1].lower()))
    return pool[0][1]


def _is_displayable_preview(path: str) -> bool:
    """True when PreviewCollection can show this file (not EXR/HDR / corrupt)."""
    if not path or not os.path.isfile(path):
        return False
    ext = os.path.splitext(path)[1].lower()
    if ext in _HDR_PREVIEW_EXT:
        return False
    try:
        with open(path, "rb") as handle:
            head = handle.read(16)
    except OSError:
        return False
    if not head:
        return False
    # Reject EXR/HDR bytes stored under a .png cache name
    if head[:4] == b"\x76\x2f\x31\x01":  # OpenEXR
        return False
    if head[:10] == b"#?RADIANCE" or head[:7] == b"#?RGBE":
        return False
    if ext in {".png", ".jpg", ".jpeg", ".webp", ".bmp"}:
        return looks_like_image_bytes(head)
    return ext in _SAFE_PREVIEW_EXT


def resolve_local_thumb_file(
    cache_dir: str,
    asset_path: str,
    asset_type: str = "",
    db_thumb: str = "",
    *,
    fetch_online: bool = False,
) -> str:
    """Resolve Library thumb the Online way when possible.

    Order: embedded ``Preview/thumb_preview.png`` → optional CDN fetch → DB/cache
    → discover (maps only for materials/textures).
    """
    # Prefer Online-style embedded / fetched CDN thumb for every type
    online_style = ensure_online_style_preview(
        asset_path, cache_dir, asset_type=asset_type, fetch=fetch_online
    )
    if online_style and _is_displayable_preview(online_style):
        return online_style

    if db_thumb and _is_displayable_preview(db_thumb):
        # Ignore stale DB thumbs that point at PBR maps for meshes
        at = (asset_type or "").lower()
        stem = os.path.splitext(os.path.basename(db_thumb))[0].lower()
        if at in {"mesh", "hdri"} and any(k in stem for k in _MAP_ONLY_KEYWORDS):
            pass
        else:
            return db_thumb

    fp = fingerprint_for_path(asset_path)
    cached = local_thumb_path(cache_dir, fp)
    if _is_displayable_preview(cached):
        return cached

    discovered = discover_preview_image(asset_path, asset_type)
    if discovered and _is_displayable_preview(discovered):
        try:
            import shutil

            paths.ensure_dir(os.path.dirname(cached))
            shutil.copy2(discovered, cached)
            return cached
        except OSError:
            return discovered
    return ""


def register_previews() -> None:
    global _previews
    import bpy.utils.previews

    if _previews is None:
        _previews = bpy.utils.previews.new()
        _preview_keys.clear()
        _preview_lru.clear()


def unregister_previews() -> None:
    global _previews
    import bpy.utils.previews

    if _previews is not None:
        bpy.utils.previews.remove(_previews)
        _previews = None
    _preview_keys.clear()
    _preview_lru.clear()


def clear_preview_cache() -> None:
    global _previews
    import bpy.utils.previews

    if _previews is not None:
        bpy.utils.previews.remove(_previews)
        _previews = bpy.utils.previews.new()
    _preview_keys.clear()
    _preview_lru.clear()


def _preview_key(kind: str, identity: str) -> str:
    digest = hashlib.sha1(f"{kind}:{identity}".encode("utf-8", errors="ignore")).hexdigest()[:24]
    return f"ual_{digest}"


def _touch_preview_lru(key: str) -> None:
    """Mark key as recently used; soft-evict oldest beyond PREVIEW_COLLECTION_MAX."""
    if key in _preview_lru:
        _preview_lru.move_to_end(key)
    else:
        _preview_lru[key] = None
    while len(_preview_lru) > PREVIEW_COLLECTION_MAX:
        old, _unused = _preview_lru.popitem(last=False)
        _preview_keys.pop(old, None)
        try:
            if _previews is not None and old in _previews:
                del _previews[old]
        except Exception:
            pass


def _placeholder_png_path() -> str:
    return os.path.join(os.path.dirname(__file__), "data", "placeholder_thumb.png")


def _in_library_dot_png_path() -> str:
    return os.path.join(os.path.dirname(__file__), "data", "in_library_dot.png")


def placeholder_icon_id() -> int:
    """Dark square placeholder so grid cells stay uniform when no thumb exists."""
    return icon_id_for_file(_placeholder_png_path(), kind="placeholder")


def in_library_dot_icon_id() -> int:
    """Small green status dot for Online tiles already saved (N-panel badge).

    Loads the packaged PNG without display-pad rewrite (already square RGBA).
    """
    path = _in_library_dot_png_path()
    if not path or not os.path.isfile(path):
        return 0
    if _previews is None:
        register_previews()
    assert _previews is not None
    key = _preview_key("in_library_dot", os.path.normcase(os.path.normpath(path)))
    if key in _preview_keys:
        try:
            icon = _previews[_preview_keys[key]].icon_id
            _touch_preview_lru(key)
            return int(icon or 0)
        except KeyError:
            pass
    try:
        preview = _previews.load(key, path, "IMAGE")
    except Exception:
        return 0
    _preview_keys[key] = key
    _touch_preview_lru(key)
    return int(getattr(preview, "icon_id", 0) or 0)


def resolve_grid_icon_id(preview_icon_id: int) -> int:
    """Real thumb icon_id, or square placeholder (never 0 for grid layout)."""
    value = int(preview_icon_id or 0)
    if value:
        return value
    return placeholder_icon_id() or 0


def icon_id_for_file(path: str, *, kind: str = "file") -> int:
    """Load image into PreviewCollection on main thread; return icon_id or 0.

    Square display normalize (``imbuf``) runs here — never from worker threads.
    """
    if not path or not os.path.isfile(path):
        return 0
    if _previews is None:
        register_previews()
    assert _previews is not None
    key = _preview_key(kind, os.path.normcase(os.path.normpath(path)))
    if key in _preview_keys:
        try:
            icon = _previews[_preview_keys[key]].icon_id
            _touch_preview_lru(key)
            return icon
        except KeyError:
            pass
    # Main-thread only: letterbox before PreviewCollection load
    path = pad_display_thumb_to_square(path)
    try:
        preview = _previews.load(key, path, "IMAGE")
    except Exception:
        return 0
    _preview_keys[key] = key
    _touch_preview_lru(key)
    return int(getattr(preview, "icon_id", 0) or 0)


def icon_id_for_online(cache_dir: str, source_id: str, asset_id: str, thumb_url: str = "") -> int:
    """Load PreviewCollection icon from stable disk cache (instant second search)."""
    legacy_url = str(thumb_url or "").strip()
    disk = online_thumb_disk_path(cache_dir, source_id, asset_id)
    if not _online_thumb_cache_is_usable(disk):
        # Legacy URL-keyed path (pre-stable-key builds) — one-time migrate
        if legacy_url:
            legacy = thumb_cache_path(
                cache_dir, f"{source_id}:{asset_id}:{legacy_url}"
            )
            if _online_thumb_cache_is_usable(legacy):
                try:
                    if not os.path.isfile(disk):
                        import shutil

                        shutil.copy2(legacy, disk)
                    # Drop URL-keyed duplicate after migrate (or when stable already exists)
                    if (
                        os.path.normcase(os.path.normpath(legacy))
                        != os.path.normcase(os.path.normpath(disk))
                        and os.path.isfile(legacy)
                    ):
                        try:
                            os.remove(legacy)
                        except OSError:
                            pass
                    if not _online_thumb_cache_is_usable(disk):
                        disk = legacy
                except OSError:
                    disk = legacy
            else:
                return 0
        else:
            return 0
    if not _online_thumb_cache_is_usable(disk):
        return 0
    _touch_mtime(disk)
    return icon_id_for_file(disk, kind="online")


def square_pad_layout(width: int, height: int) -> Tuple[int, int, int]:
    """Return ``(side, offset_x, offset_y)`` to center a rect in a dark square."""
    w = max(0, int(width or 0))
    h = max(0, int(height or 0))
    if w <= 0 or h <= 0:
        return 0, 0, 0
    side = max(w, h)
    return side, (side - w) // 2, (side - h) // 2


def aspect_fit_rect(
    dest_x: float,
    dest_y: float,
    dest_w: float,
    dest_h: float,
    img_w: float,
    img_h: float,
    *,
    fit: str = "contain",
) -> Tuple[float, float, float, float]:
    """Map an image into ``dest`` preserving aspect. Returns ``(x, y, w, h)``.

    - ``contain`` — letterbox/pillarbox (mesh previews; no stretch)
    - ``cover`` — fill dest, crop overflow (textures / HDRIs)
    - ``stretch`` — fill dest ignoring aspect (legacy)
    """
    dx, dy = float(dest_x), float(dest_y)
    dw, dh = max(1.0, float(dest_w)), max(1.0, float(dest_h))
    iw, ih = max(1.0, float(img_w)), max(1.0, float(img_h))
    mode = (fit or "contain").strip().lower()
    if mode == "stretch":
        return dx, dy, dw, dh
    img_aspect = iw / ih
    dest_aspect = dw / dh
    if mode == "cover":
        if img_aspect > dest_aspect:
            # Image wider than dest — match height, crop sides
            h = dh
            w = h * img_aspect
        else:
            w = dw
            h = w / img_aspect
        return dx + (dw - w) * 0.5, dy + (dh - h) * 0.5, w, h
    # contain (default)
    if img_aspect > dest_aspect:
        w = dw
        h = w / img_aspect
    else:
        h = dh
        w = h * img_aspect
    return dx + (dw - w) * 0.5, dy + (dh - h) * 0.5, w, h


def pad_display_thumb_to_square(path: str, fill_rgb=(28, 28, 28)) -> str:
    """Letterbox/pillarbox image onto a dark square (display normalize only).

    Uses Blender's ``imbuf`` when available (addon runtime). Pure tests / missing
    imbuf → no-op (returns the original path). Never touches import routers.
    """
    del fill_rgb  # reserved for imbuf rect fill when API allows
    if not path or not os.path.isfile(path):
        return path or ""
    try:
        import imbuf  # type: ignore  # Blender built-in
    except Exception:
        return path
    try:
        img = imbuf.load(path)
    except Exception:
        return path
    if img is None:
        return path
    try:
        size = getattr(img, "size", None)
        if not size or len(size) < 2:
            return path
        w, h = int(size[0]), int(size[1])
        side, _ox, _oy = square_pad_layout(w, h)
        if side <= 0 or w == h:
            return path
        # Best-effort: resize-to-contain via imbuf when paste APIs are absent.
        # Prefer keeping aspect by resizing the long edge to ``side`` then writing
        # through a square canvas if ``crop`` / pixel APIs exist.
        if hasattr(img, "resize"):
            scale = side / float(max(w, h))
            nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
            try:
                img.resize((nw, nh))
            except Exception:
                return path
        canvas = None
        try:
            canvas = imbuf.new((side, side))
        except Exception:
            canvas = None
        if canvas is None:
            try:
                imbuf.write(img, filepath=path)
            except Exception:
                return path
            return path
        # Without a reliable blit, write the resized image as-is (still square-ish)
        try:
            imbuf.write(img, filepath=path)
        except TypeError:
            try:
                imbuf.write(img, path)
            except Exception:
                return path
        except Exception:
            return path
        return path
    except Exception:
        return path


def fetch_online_thumb_url_bytes(
    url: str,
    source_id: str,
    *,
    timeout: float = 25,
    max_bytes: Optional[int] = None,
) -> bytes:
    """Download browse-thumb bytes with HTTPS allowlist + redirect checks."""
    candidate_url = str(url or "").strip()
    sid = str(source_id or "").strip()
    if not candidate_url or not sid:
        return b""
    byte_cap = int(max_bytes if max_bytes is not None else ONLINE_THUMB_MAX_DOWNLOAD_BYTES)
    policy_cap = resolve_download_max_bytes("thumb")
    if policy_cap:
        byte_cap = min(byte_cap, int(policy_cap))
    try:
        assert_allowed_download_url(candidate_url, sid, download_class="thumb")
    except RuntimeError:
        return b""
    headers = {
        "User-Agent": "HoudiniAssetLibrary/1.0 (+https://sidefx.com)",
        "Accept": "image/png,image/jpeg,image/gif,image/*;q=0.5,*/*;q=0.1",
    }
    joined = candidate_url.lower()
    if "fab.com" in joined or "quixel" in joined:
        headers["Referer"] = "https://www.fab.com/"
        headers["Origin"] = "https://www.fab.com"
    request = urllib.request.Request(candidate_url, headers=headers)
    try:
        with open_allowed_download(
            request,
            source_id=sid,
            download_class="thumb",
            timeout=timeout,
        ) as response:
            content_length = int(response.headers.get("Content-Length") or 0)
            if content_length and content_length > byte_cap:
                return b""
            data = response.read(byte_cap + 1)
    except Exception:
        return b""
    if len(data) > byte_cap:
        return b""
    return data


def fetch_online_thumb_to_disk(
    cache_dir: str,
    source_id: str,
    asset_id: str,
    thumb_url: str,
) -> str:
    """Worker-safe: download bytes, sniff magic, write ``online_*.png``.

    HTTP + file I/O only — **no** ``bpy`` / ``imbuf``. Square normalize runs on
    the main thread via ``icon_id_for_file`` → ``pad_display_thumb_to_square``.

    Disk key is ``source_id:asset_id`` (stable) so a second Search reuses the
    file even when ``thumb_url`` was empty at append time.

    Tries CDN-compact URL candidates (Poly Haven sized thumbs) before the
    bare full-resolution URL so browse tiles are not stuck on oversize rejects.
    """
    global _ONLINE_THUMB_WRITES
    disk = online_thumb_disk_path(cache_dir, source_id, asset_id)
    if _online_thumb_cache_is_usable(disk):
        _touch_mtime(disk)
        return disk

    candidates = compact_online_thumb_url_candidates(thumb_url, source_id, asset_id)
    if not candidates:
        return ""

    data = b""
    for candidate in candidates:
        chunk = fetch_online_thumb_url_bytes(candidate, source_id, timeout=25)
        if len(chunk) > ONLINE_THUMB_MAX_DOWNLOAD_BYTES:
            continue
        if not looks_like_blender_safe_thumb_bytes(chunk):
            # WebP/AVIF / non-image — do not poison the stable cache path
            continue
        data = chunk
        break
    if not data:
        return ""
    paths.ensure_dir(os.path.dirname(disk))
    try:
        with open(disk, "wb") as handle:
            handle.write(data)
    except OSError:
        return ""
    _ONLINE_THUMB_WRITES += 1
    soft_prune_thumbs(cache_dir)
    return disk
