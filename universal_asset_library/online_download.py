# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""Stage online downloads from cache into library Online Downloads/.

Also resolves **already-downloaded** packages (BlenderKit-style) so Download &
Import / Drop skip CDN when a matching library (or cache) copy exists.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import paths
from .download_utils import ensure_zip_archive_path, extract_zip_members, looks_like_zip

# Sidecar folders commonly shipped next to a main mesh download
_PACKAGE_DIR_NAMES = {
    "textures", "texture", "tex", "maps", "map", "materials", "images", "img",
    "preview", "previews", "thumbnails", "03_preview", "extracted",
    # PBRPX (and similar CDNs) store maps in resolution folders, not textures/
    "1k", "2k", "4k", "6k", "8k", "16k",
}
_PACKAGE_RES_DIR = re.compile(r"^(1|2|4|6|8|16)k$", re.IGNORECASE)
_PACKAGE_FILE_EXT = {
    ".bin", ".gltf", ".glb", ".fbx", ".obj", ".mtl", ".usd", ".usda", ".usdc", ".usdz",
    ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".exr", ".hdr", ".webp", ".tga", ".bmp",
    ".json", ".txt", ".md", ".zip",
}

_TYPE_FOLDERS = {
    "mesh": "Models",
    "material_set": "Materials",
    "texture": "Textures",
    "hdri": "HDRIs",
    "vdb": "Volumes",
}
_MESH_EXTS = {".gltf", ".glb", ".fbx", ".obj", ".usd", ".usda", ".usdc", ".usdz", ".stl", ".ply"}
_HDRI_EXTS = {".hdr", ".exr"}
_MAP_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".exr", ".tga", ".webp", ".bmp"}
_BUNDLE_NAME = ".assetlibrary.bundle.json"
# Last ``_token`` of ambientCG 2023 names (Metal049A_2K-JPG_Color vs _Roughness)
_MAP_CHANNEL_TOKENS = frozenset({
    "color", "col", "albedo", "diffuse", "diff", "basecolor",
    "metalness", "metallic", "metal",
    "roughness", "rough", "glossiness", "gloss",
    "normalgl", "normaldx", "normal", "nrm", "nor",
    "ao", "ambientocclusion", "occlusion", "cavity",
    "displacement", "disp", "height",
    "opacity", "alpha",
    "specular", "spec",
    "emissive", "emission",
    "translucency", "transmission",
})


def type_folder_for_asset(asset_type: str) -> str:
    """Library subfolder for a download. Connector aliases (ambientCG ``Material``) map first."""
    from .ui.icons import canonical_import_type

    key = canonical_import_type(asset_type) or str(asset_type or "").strip().lower()
    return _TYPE_FOLDERS.get(key, "Models")


def safe_asset_folder_name(asset_name: str) -> str:
    return (
        "".join(c if c.isalnum() or c in "._- " else "_" for c in (asset_name or "")).strip()
        or "asset"
    )


def write_bundle_manifest(folder: str, data: Dict[str, Any]) -> str:
    path = os.path.join(folder, _BUNDLE_NAME)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
    return path


def read_bundle_manifest(folder: str) -> Dict[str, Any]:
    path = os.path.join(folder or "", _BUNDLE_NAME)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            return {}
        if data.get("name") and not data.get("display_name"):
            data["display_name"] = data["name"]
        if data.get("display_name") and not data.get("name"):
            data["name"] = data["display_name"]
        return data
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {}


def _norm_opt(value: str) -> str:
    return str(value or "").strip().lower()


def package_options_compatible(
    manifest: Dict[str, Any],
    *,
    resolution: str = "",
    format: str = "",
) -> bool:
    """True when stored download options match the request (legacy = accept).

    Empty request or empty stored field → compatible. Mismatch → force CDN.
    """
    want_res = _norm_opt(resolution)
    want_fmt = _norm_opt(format)
    if want_res in ("", "default", "auto"):
        want_res = ""
    if want_fmt in ("", "auto", "default"):
        want_fmt = ""
    have_res = _norm_opt(
        manifest.get("download_resolution") or manifest.get("resolution") or ""
    )
    have_fmt = _norm_opt(
        manifest.get("download_format") or manifest.get("format") or ""
    )
    if want_res and have_res and want_res != have_res:
        return False
    if want_fmt and have_fmt and want_fmt != have_fmt:
        return False
    return True


def package_has_content(folder: str, asset_type: str = "") -> bool:
    """True when ``folder`` looks like a usable downloaded package (not empty)."""
    if not folder or not os.path.isdir(folder):
        return False
    atype = str(asset_type or "").strip().lower()
    try:
        names = [n for n in os.listdir(folder) if n and not n.startswith(".")]
    except OSError:
        return False
    if not names:
        return False
    # Ignore preview-only leftovers
    content = [n for n in names if n.lower() not in ("preview", "previews", "thumbnails")]
    if not content:
        return False
    lower_files: List[str] = []
    for name in content:
        path = os.path.join(folder, name)
        if os.path.isfile(path):
            lower_files.append(name.lower())
        elif os.path.isdir(path):
            # Nested package (textures/, extracted/, glTF folder)
            try:
                for root, _dirs, files in os.walk(path):
                    for fn in files:
                        if not fn.startswith("."):
                            lower_files.append(fn.lower())
                            if len(lower_files) > 40:
                                break
                    if len(lower_files) > 40:
                        break
            except OSError:
                pass
    if not lower_files:
        return False
    exts = {os.path.splitext(n)[1] for n in lower_files}
    if atype == "hdri":
        # Require real environment maps — JPG/PNG material packs must not count
        # as a valid HDRI package (wrong ambientCG download used to pass this).
        return bool(exts & _HDRI_EXTS)
    if atype in ("material_set", "texture"):
        return bool(exts & _MAP_EXTS) or bool(exts & {".zip"})
    if atype == "mesh" or not atype:
        if exts & _MESH_EXTS:
            return True
        if exts & _MAP_EXTS:
            return True
        if exts & {".zip", ".bin"}:
            return True
    return bool(lower_files)


def expected_library_package_path(
    library_root: str,
    source_id: str,
    asset_type: str,
    asset_name: str,
) -> str:
    return os.path.join(
        paths.online_downloads_dir(library_root),
        str(source_id or "").strip(),
        type_folder_for_asset(asset_type),
        safe_asset_folder_name(asset_name),
    )


def _scan_source_for_asset_id(
    source_root: str,
    asset_id: str,
    *,
    resolution: str = "",
    format: str = "",
) -> str:
    """Walk ``Online Downloads/<source>/…`` for a bundle with matching ``asset_id``."""
    aid = str(asset_id or "").strip()
    if not aid or not os.path.isdir(source_root):
        return ""
    aid_l = aid.lower()
    try:
        type_dirs = os.listdir(source_root)
    except OSError:
        return ""
    for type_name in type_dirs:
        type_path = os.path.join(source_root, type_name)
        if not os.path.isdir(type_path):
            continue
        try:
            packages = os.listdir(type_path)
        except OSError:
            continue
        for pkg_name in packages:
            pkg = os.path.join(type_path, pkg_name)
            if not os.path.isdir(pkg):
                continue
            manifest = read_bundle_manifest(pkg)
            mid = str(manifest.get("asset_id") or "").strip()
            if mid.lower() != aid_l:
                continue
            if not package_options_compatible(
                manifest, resolution=resolution, format=format
            ):
                continue
            if package_has_content(pkg, str(manifest.get("asset_type") or "")):
                return os.path.normpath(pkg)
    return ""


def find_existing_library_package(
    cfg: Dict[str, Any],
    *,
    source_id: str,
    asset_id: str = "",
    asset_name: str = "",
    asset_type: str = "mesh",
    resolution: str = "",
    format: str = "",
) -> str:
    """Return an Online Downloads package path if already saved, else ``\"\"``.

    Prefer deterministic ``…/<source>/<Type>/<Name>/``, then scan by ``asset_id``.
    Resolution/format mismatch (when both request and manifest set them) → miss.
    """
    sid = str(source_id or "").strip()
    if not sid:
        return ""
    roots: Sequence[str] = list(cfg.get("library_roots") or [])
    if not roots:
        roots = [paths.default_library_root()]
    atype = str(asset_type or "mesh").strip().lower() or "mesh"
    for root in roots:
        if not root:
            continue
        # 1) Deterministic name path
        if asset_name:
            candidate = expected_library_package_path(root, sid, atype, asset_name)
            if package_has_content(candidate, atype):
                manifest = read_bundle_manifest(candidate)
                if package_options_compatible(
                    manifest, resolution=resolution, format=format
                ):
                    # Prefer matching asset_id when both present
                    mid = str(manifest.get("asset_id") or "").strip()
                    if not asset_id or not mid or mid.lower() == str(asset_id).strip().lower():
                        return os.path.normpath(candidate)
        # 2) Scan by asset_id (renamed folders / older layouts)
        if asset_id:
            source_root = os.path.join(paths.online_downloads_dir(root), sid)
            hit = _scan_source_for_asset_id(
                source_root,
                asset_id,
                resolution=resolution,
                format=format,
            )
            if hit:
                return hit
    return ""


def find_existing_cache_package(
    cfg: Dict[str, Any],
    *,
    source_id: str,
    asset_id: str,
    asset_type: str = "mesh",
) -> str:
    """Return cache staging folder when Keep-cache left files behind, else ``\"\"``."""
    cache_dir = str(cfg.get("cache_dir") or "").strip()
    sid = str(source_id or "").strip()
    aid = str(asset_id or "").strip()
    if not cache_dir or not sid or not aid:
        return ""
    base = paths.source_cache_dir(cache_dir, sid, aid)
    for candidate in (base, os.path.join(base, "extracted")):
        if package_has_content(candidate, asset_type):
            return os.path.normpath(candidate)
    return ""


def find_reusable_online_asset(
    cfg: Dict[str, Any],
    *,
    source_id: str,
    asset_id: str = "",
    asset_name: str = "",
    asset_type: str = "mesh",
    resolution: str = "",
    format: str = "",
) -> Tuple[str, str]:
    """BlenderKit-style local reuse.

    Returns ``(path, origin)`` where origin is ``\"library\"``, ``\"cache\"``, or
    ``\"\"`` when nothing reusable was found.
    """
    lib = find_existing_library_package(
        cfg,
        source_id=source_id,
        asset_id=asset_id,
        asset_name=asset_name,
        asset_type=asset_type,
        resolution=resolution,
        format=format,
    )
    if lib:
        return lib, "library"
    # Cache has no resolution/format metadata — only reuse when options are unset
    # (explicit 1k→2k must hit CDN or a matching library package).
    want_res = _norm_opt(resolution)
    want_fmt = _norm_opt(format)
    if want_res in ("", "default", "auto"):
        want_res = ""
    if want_fmt in ("", "auto", "default"):
        want_fmt = ""
    if want_res or want_fmt:
        return "", ""
    cache = find_existing_cache_package(
        cfg,
        source_id=source_id,
        asset_id=asset_id,
        asset_type=asset_type,
    )
    if cache:
        return cache, "cache"
    return "", ""


def embed_online_preview(dest_folder: str, preview_src: str) -> str:
    """Copy the Online CDN thumb into ``Preview/thumb_preview.png`` for Library parity."""
    if not dest_folder or not preview_src or not os.path.isfile(preview_src):
        return ""
    if not os.path.isdir(dest_folder):
        return ""
    preview_dir = paths.ensure_dir(os.path.join(dest_folder, "Preview"))
    dest = os.path.join(preview_dir, "thumb_preview.png")
    try:
        shutil.copy2(preview_src, dest)
    except OSError:
        return ""
    return dest


def _stem_map_family(stem: str) -> str:
    """Strip a trailing PBR channel token so Color/Roughness/NormalGL share a family."""
    lower = (stem or "").lower()
    if "_" not in lower:
        return lower
    prefix, token = lower.rsplit("_", 1)
    if token in _MAP_CHANNEL_TOKENS:
        return prefix
    return lower


def _is_package_sibling(name: str, main_stem: str) -> bool:
    """True when a sibling file/dir belongs to the same downloaded asset package."""
    lower = (name or "").lower()
    if not lower or lower.startswith("."):
        return False
    if lower in _PACKAGE_DIR_NAMES or lower.startswith("03_preview"):
        return True
    if _PACKAGE_RES_DIR.match(lower):
        return True
    stem, ext = os.path.splitext(lower)
    if ext in _PACKAGE_FILE_EXT:
        # Same basename family (Barrel_01.gltf + Barrel_01.bin + Barrel_01_diff.jpg)
        if stem == main_stem or stem.startswith(main_stem + "_") or main_stem.startswith(stem + "_"):
            return True
        if main_stem and main_stem in stem:
            return True
        # ambientCG: Metal049A_2K-JPG_Color.jpg + _Roughness.jpg (stems do not contain each other)
        fam_main = _stem_map_family(main_stem)
        fam_other = _stem_map_family(stem)
        if fam_main and fam_other and fam_main == fam_other:
            return True
        # Shared glTF extras: scene.bin, buffer files
        if ext == ".bin":
            return True
    return False


def staging_asset_dir(staged_path: str, cache_dir: str) -> str:
    """Return ``sources/<sid>/<aid>`` for a path under the online staging tree, else ``\"\"``."""
    if not staged_path or not cache_dir:
        return ""
    sources = os.path.normpath(os.path.join(cache_dir, "sources"))
    path = os.path.normpath(staged_path)
    candidate = path if os.path.isdir(path) else os.path.dirname(path)
    if os.path.basename(candidate).lower() == "extracted":
        candidate = os.path.dirname(candidate)
    try:
        common = os.path.commonpath([sources, candidate])
    except ValueError:
        return ""
    if os.path.normcase(common) != os.path.normcase(sources):
        return ""
    rel = os.path.relpath(candidate, sources)
    parts = [p for p in rel.split(os.sep) if p and p not in (".", "..")]
    if len(parts) < 2:
        return ""
    return os.path.normpath(os.path.join(sources, parts[0], parts[1]))


def remove_staging_after_library_copy(staged_path: str, cache_dir: str) -> bool:
    """Delete per-asset ``sources/<sid>/<aid>/`` after a successful library copy.

    Used when ``keep_cache`` is off but the save had to *copy* (cross-drive) so
    the staging tree would otherwise remain forever.
    """
    asset_dir = staging_asset_dir(staged_path, cache_dir)
    if not asset_dir or not os.path.isdir(asset_dir):
        return False
    try:
        shutil.rmtree(asset_dir)
    except OSError:
        return False
    parent = os.path.dirname(asset_dir)
    try:
        if os.path.isdir(parent) and not os.listdir(parent):
            os.rmdir(parent)
    except OSError:
        pass
    return True


def stage_to_library(
    staged_path: str,
    *,
    library_root: str,
    source_id: str,
    asset_name: str,
    asset_type: str,
    keep_cache: bool = False,
    cache_dir: str = "",
    progress: Optional[Callable[[str], None]] = None,
    extra_manifest: Optional[Dict[str, Any]] = None,
    preview_image: str = "",
) -> str:
    """Copy/move staged files into Online Downloads/<source>/<Type>/<Name>/."""
    type_folder = type_folder_for_asset(asset_type)
    safe_name = safe_asset_folder_name(asset_name)
    dest = paths.online_downloads_dir(library_root)
    dest = os.path.join(dest, source_id, type_folder, safe_name)
    paths.ensure_dir(os.path.dirname(dest))
    if os.path.exists(dest):
        shutil.rmtree(dest, ignore_errors=True)
    if progress:
        progress(f"Saving to library: {safe_name}")

    original_staged = os.path.normpath(staged_path)
    src = staged_path
    # Sniff misnamed ZIPs — never gate on extension alone
    if os.path.isfile(src):
        try:
            src = ensure_zip_archive_path(src)
        except Exception:
            pass
    if os.path.isfile(src) and (src.lower().endswith(".zip") or looks_like_zip(src)):
        extracted = os.path.join(os.path.dirname(src), "extracted")
        if os.path.isdir(extracted):
            shutil.rmtree(extracted, ignore_errors=True)
        extract_zip_members(src, extracted)
        src = extracted

    if os.path.isdir(src):
        if keep_cache or (cache_dir and not paths.same_volume(src, dest)):
            shutil.copytree(src, dest)
        else:
            shutil.move(src, dest)
    elif os.path.isfile(src):
        # Mesh packages (e.g. Poly Haven glTF) often have sibling bins/textures in
        # the same per-asset cache folder — stage related package files only.
        parent = os.path.dirname(src)
        main_name = os.path.basename(src)
        main_stem = os.path.splitext(main_name)[0].lower()
        try:
            names = [n for n in os.listdir(parent) if n not in (".", "..")]
        except OSError:
            names = []
        related = [n for n in names if n == main_name or _is_package_sibling(n, main_stem)]
        # Match folder staging: copy when keep_cache OR cross-drive (move fails across volumes).
        copy_mode = bool(keep_cache) or (
            bool(cache_dir) and not paths.same_volume(parent, dest)
        )
        if related and len(related) < 500:
            paths.ensure_dir(dest)
            for name in related:
                s_path = os.path.join(parent, name)
                d_path = os.path.join(dest, name)
                if os.path.isdir(s_path):
                    if copy_mode:
                        shutil.copytree(s_path, d_path)
                    else:
                        shutil.move(s_path, d_path)
                elif os.path.isfile(s_path):
                    if copy_mode:
                        shutil.copy2(s_path, d_path)
                    else:
                        shutil.move(s_path, d_path)
        else:
            paths.ensure_dir(dest)
            target = os.path.join(dest, os.path.basename(src))
            if copy_mode:
                shutil.copy2(src, target)
            else:
                shutil.move(src, target)
    else:
        raise FileNotFoundError(staged_path)

    # Keep cache OFF: remove per-asset staging (covers cross-drive copy residue
    # and leftovers after partial same-volume moves).
    if not keep_cache and cache_dir:
        remove_staging_after_library_copy(original_staged, cache_dir)

    manifest = {
        "source_id": source_id,
        "name": asset_name,
        "display_name": asset_name,
        "asset_type": asset_type,
        "version": 1,
    }
    if extra_manifest:
        manifest.update(extra_manifest)
    # Keep identity fields even when extra_manifest omitted keys
    if preview_image and not manifest.get("thumb_path"):
        manifest["has_embedded_preview"] = True
    write_bundle_manifest(dest, manifest)
    if preview_image:
        embed_online_preview(dest, preview_image)
    if source_id == "fab":
        from .fab_import_sidecar import write_fab_sidecar_for_bundle

        asset_id = str(manifest.get("asset_id") or (extra_manifest or {}).get("asset_id") or "").strip()
        write_fab_sidecar_for_bundle(
            dest,
            asset_name=asset_name,
            asset_id=asset_id or safe_name,
            asset_type=asset_type,
            quality=str(
                (extra_manifest or {}).get("download_resolution")
                or manifest.get("download_resolution")
                or ""
            ),
            fmt=str(
                (extra_manifest or {}).get("download_format")
                or manifest.get("download_format")
                or ""
            ),
            mesh_lod=str((extra_manifest or {}).get("fab_mesh_lod") or ""),
        )
    return dest


def register_and_stage_online_download(
    staged_path: str,
    cfg: Dict[str, Any],
    *,
    source_id: str,
    asset_name: str,
    asset_type: str,
    progress: Optional[Callable[[str], None]] = None,
    extra_manifest: Optional[Dict[str, Any]] = None,
    preview_image: str = "",
    asset_id: str = "",
    thumb_url: str = "",
    resolution: str = "",
    format: str = "",
    library_root: str = "",
) -> str:
    roots = list(cfg.get("library_roots") or []) or [paths.default_library_root()]
    chosen = (library_root or "").strip() or str(roots[0] or "")
    keep = bool((cfg.get("online") or {}).get("keep_cache"))
    meta = dict(extra_manifest or {})
    if asset_id and not meta.get("asset_id"):
        meta["asset_id"] = asset_id
    if thumb_url and not meta.get("thumb_url"):
        meta["thumb_url"] = thumb_url
    if resolution and not meta.get("download_resolution"):
        meta["download_resolution"] = str(resolution)
    if format and not meta.get("download_format"):
        meta["download_format"] = str(format)
    return stage_to_library(
        staged_path,
        library_root=chosen,
        source_id=source_id,
        asset_name=asset_name,
        asset_type=asset_type,
        keep_cache=keep,
        cache_dir=str(cfg.get("cache_dir") or ""),
        progress=progress,
        extra_manifest=meta,
        preview_image=preview_image,
    )


def prepare_reusable_asset_for_import(
    path: str,
    origin: str,
    cfg: Dict[str, Any],
    *,
    source_id: str,
    asset_name: str,
    asset_type: str,
    asset_id: str = "",
    thumb_url: str = "",
    resolution: str = "",
    format: str = "",
    preview_image: str = "",
) -> str:
    """Return a path ready for import; stage cache hits into Online Downloads when enabled."""
    if not path:
        return ""
    if origin == "library":
        return path
    if origin == "cache" and bool((cfg.get("online") or {}).get("copy_to_library", True)):
        return register_and_stage_online_download(
            path,
            cfg,
            source_id=source_id,
            asset_name=asset_name,
            asset_type=asset_type or "mesh",
            asset_id=asset_id,
            thumb_url=thumb_url,
            resolution=resolution,
            format=format,
            preview_image=preview_image,
        )
    return path
