# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""Download, verify, and apply Blender extension updates (no bpy)."""

from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping, Optional

from .download_utils import DownloadCancelled, extract_zip_members, stream_download_file
from .release_manifest import verify_file_sha256

ProgressCb = Optional[Callable[[int, int], None]]
CancelCb = Optional[Callable[[], bool]]


@dataclass(frozen=True)
class UpdateInstallResult:
    ok: bool
    message: str
    zip_path: str = ""
    staging_dir: str = ""
    applied: bool = False
    files_copied: int = 0
    files_failed: int = 0
    error: str = ""


def extract_artifact_info(payload: Mapping[str, Any]) -> Dict[str, Optional[str]]:
    url: Optional[str] = None
    sha256: Optional[str] = None
    filename: Optional[str] = None

    release = payload.get("release")
    if isinstance(release, dict):
        arts = release.get("artifacts") or []
        if arts and isinstance(arts[0], dict):
            art = arts[0]
            url = str(art.get("publicUrl") or "").strip() or None
            sha256 = str(art.get("sha256") or "").strip() or None
            filename = str(art.get("filename") or "").strip() or None
        manifest = release.get("manifest")
        if isinstance(manifest, dict):
            if not url:
                url = str(manifest.get("downloadUrl") or "").strip() or None
            if not sha256:
                sha256 = str(manifest.get("sha256") or "").strip() or None
            if not filename:
                filename = str(manifest.get("filename") or "").strip() or None

    if isinstance(payload.get("manifest"), dict):
        manifest = payload["manifest"]
        if not url:
            url = str(manifest.get("downloadUrl") or "").strip() or None
        if not sha256:
            sha256 = str(manifest.get("sha256") or "").strip() or None
        if not filename:
            filename = str(manifest.get("filename") or "").strip() or None

    if url and not filename:
        filename = os.path.basename(url.split("?", 1)[0]) or (
            "UniversalAssetLibrary-Blender-update.zip"
        )
    return {"url": url, "sha256": sha256, "filename": filename}


def updates_cache_dir(cache_dir: str) -> str:
    root = os.path.normpath(os.path.join(str(cache_dir or "").strip() or ".", "updates"))
    os.makedirs(root, exist_ok=True)
    return root


def resolve_installed_addon_dir() -> str:
    """Directory of the running ``universal_asset_library`` package."""
    return os.path.dirname(os.path.abspath(__file__))


def find_blender_addon_root(extracted_dir: str) -> str:
    extracted_dir = os.path.normpath(extracted_dir)
    direct = os.path.join(extracted_dir, "universal_asset_library")
    if os.path.isdir(direct) and os.path.isfile(os.path.join(direct, "__init__.py")):
        return direct
    if os.path.isfile(os.path.join(extracted_dir, "__init__.py")) and os.path.isfile(
        os.path.join(extracted_dir, "blender_manifest.toml")
    ):
        return extracted_dir
    try:
        names = os.listdir(extracted_dir)
    except OSError as exc:
        raise RuntimeError(f"Cannot read extracted update: {exc}") from exc
    for name in names:
        cand = os.path.join(extracted_dir, name)
        if os.path.isdir(cand) and os.path.isfile(os.path.join(cand, "__init__.py")):
            if os.path.isfile(os.path.join(cand, "blender_manifest.toml")) or name == (
                "universal_asset_library"
            ):
                return cand
    raise RuntimeError("ZIP is not a Universal Asset Library Blender package.")


# Marker files/packages that Community ZIPs omit. asset_bar is a package
# (ui/asset_bar/__init__.py), not a flat ui/asset_bar.py.
_PRO_TREE_MARKERS = (
    "package_flavor.py",
    os.path.join("sources", "fab.py"),
    os.path.join("ui", "asset_bar", "__init__.py"),
    "cloud_backup.py",
)


def _path_exists_as_module(root: str, rel: str) -> bool:
    """True for a .py file or a package dir with __init__.py under root."""
    path = os.path.join(root, rel)
    if os.path.isfile(path):
        return True
    # Accept legacy flat ui/asset_bar.py if a future layout ships it.
    if rel.endswith(os.path.join("asset_bar", "__init__.py")):
        flat = os.path.join(root, "ui", "asset_bar.py")
        if os.path.isfile(flat):
            return True
    return False


def read_tree_flavor(root: str) -> str:
    """Read FLAVOR from an extracted/installed addon tree's package_flavor.py."""
    flavor_path = os.path.join(os.path.normpath(str(root or "")), "package_flavor.py")
    try:
        with open(flavor_path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return ""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("FLAVOR") or "=" not in stripped:
            continue
        val = stripped.split("=", 1)[1].strip().strip("\"'")
        if val in ("community", "pro"):
            return val
    return ""


def running_package_flavor() -> str:
    try:
        from . import package_flavor as flavor_mod

        val = str(getattr(flavor_mod, "FLAVOR", "pro") or "pro").strip().lower()
        return val if val in ("community", "pro") else "pro"
    except Exception:
        return "pro"


def is_pro_addon_tree(root: str) -> bool:
    """True when extracted/installed tree is a Pro ZIP (flavor marker + Pro modules)."""
    root = os.path.normpath(str(root or "").strip())
    if not root or not os.path.isdir(root):
        return False
    if read_tree_flavor(root) != "pro":
        return False
    for rel in _PRO_TREE_MARKERS:
        if not _path_exists_as_module(root, rel):
            return False
    return True


def apply_addon_tree(source_root: str, addon_root: str) -> tuple[int, int]:
    source_root = os.path.normpath(source_root)
    addon_root = os.path.normpath(addon_root)
    if not os.path.isdir(source_root):
        raise RuntimeError("Extracted addon root missing.")
    if not os.path.isdir(addon_root):
        raise RuntimeError(f"Installed addon path missing: {addon_root}")

    copied = 0
    failed = 0
    for dirpath, _dirnames, filenames in os.walk(source_root):
        rel_dir = os.path.relpath(dirpath, source_root)
        for name in filenames:
            if name == ".DS_Store":
                continue
            src = os.path.join(dirpath, name)
            if rel_dir in (".", ""):
                dest = os.path.join(addon_root, name)
            else:
                dest = os.path.join(addon_root, rel_dir, name)
            try:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.copy2(src, dest)
                copied += 1
            except OSError:
                failed += 1
    return copied, failed


def download_verify_and_apply(
    *,
    artifact_url: str,
    expected_sha256: Optional[str],
    filename: Optional[str],
    cache_dir: str,
    addon_root: Optional[str] = None,
    version: Optional[str] = None,
    progress_callback: ProgressCb = None,
    should_cancel: CancelCb = None,
    apply: bool = True,
    require_pro: bool = False,
) -> UpdateInstallResult:
    url = str(artifact_url or "").strip()
    if not url:
        return UpdateInstallResult(
            ok=False,
            message="No download URL for this release. Open the website Download page.",
            error="no_url",
        )
    target = os.path.normpath(str(addon_root or resolve_installed_addon_dir()))
    if apply and not os.path.isdir(target):
        return UpdateInstallResult(
            ok=False,
            message=f"Installed addon folder missing: {target}",
            error="no_addon",
        )

    cache = updates_cache_dir(cache_dir)
    safe_name = os.path.basename(str(filename or "").strip()) or (
        "UniversalAssetLibrary-Blender-update.zip"
    )
    if not safe_name.lower().endswith(".zip"):
        safe_name = f"{safe_name}.zip"
    zip_path = os.path.join(cache, safe_name)
    staging = os.path.join(
        cache,
        f"stage-{str(version or 'pending').replace('/', '_')}",
    )

    try:
        stream_download_file(
            url,
            zip_path,
            timeout=300,
            progress_callback=progress_callback,
            should_cancel=should_cancel,
            download_class="update",
        )
    except DownloadCancelled:
        return UpdateInstallResult(
            ok=False,
            message="Update download cancelled.",
            zip_path=zip_path,
            error="cancelled",
        )
    except Exception as exc:  # noqa: BLE001
        return UpdateInstallResult(
            ok=False,
            message=f"Update download failed: {exc}",
            zip_path=zip_path,
            error=f"download:{exc}",
        )

    expected = str(expected_sha256 or "").strip()
    if not expected:
        try:
            os.remove(zip_path)
        except OSError:
            pass
        return UpdateInstallResult(
            ok=False,
            message="Release metadata is missing sha256. Update aborted for safety.",
            zip_path=zip_path,
            error="sha256_missing",
        )
    if not verify_file_sha256(zip_path, expected):
        try:
            os.remove(zip_path)
        except OSError:
            pass
        return UpdateInstallResult(
            ok=False,
            message="Downloaded ZIP failed sha256 verification. Update aborted.",
            zip_path=zip_path,
            error="sha256_mismatch",
        )

    if os.path.isdir(staging):
        shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging, exist_ok=True)

    try:
        extract_zip_members(
            zip_path,
            staging,
            progress_callback=progress_callback,
            should_cancel=should_cancel,
        )
        package_root = find_blender_addon_root(staging)
        expected_flavor = running_package_flavor()
        got_flavor = read_tree_flavor(package_root)
        if got_flavor and got_flavor != expected_flavor:
            return UpdateInstallResult(
                ok=False,
                message=(
                    f"Downloaded ZIP is {got_flavor}, but this install is {expected_flavor}. "
                    "Install aborted to avoid mixing Community and Pro."
                ),
                zip_path=zip_path,
                staging_dir=package_root,
                error="flavor_mismatch",
            )
        if require_pro and not is_pro_addon_tree(package_root):
            return UpdateInstallResult(
                ok=False,
                message="Downloaded ZIP is not a Pro package. Install aborted.",
                zip_path=zip_path,
                staging_dir=package_root,
                error="not_pro_package",
            )
    except DownloadCancelled:
        return UpdateInstallResult(
            ok=False,
            message="Update extract cancelled.",
            zip_path=zip_path,
            staging_dir=staging,
            error="cancelled",
        )
    except Exception as exc:  # noqa: BLE001
        return UpdateInstallResult(
            ok=False,
            message=f"Update extract failed: {exc}",
            zip_path=zip_path,
            staging_dir=staging,
            error=f"extract:{exc}",
        )

    if not apply:
        return UpdateInstallResult(
            ok=True,
            message="Update downloaded and verified.",
            zip_path=zip_path,
            staging_dir=package_root,
            applied=False,
        )

    try:
        copied, failed = apply_addon_tree(package_root, target)
        if require_pro and not is_pro_addon_tree(target):
            return UpdateInstallResult(
                ok=False,
                message=(
                    "Pro files did not land (folder may be locked). "
                    "Close Blender and use Install from Disk with the Pro ZIP."
                ),
                zip_path=zip_path,
                staging_dir=package_root,
                applied=False,
                files_copied=copied,
                files_failed=failed,
                error="incomplete_pro_apply",
            )
    except Exception as exc:  # noqa: BLE001
        return UpdateInstallResult(
            ok=False,
            message=f"Update apply failed: {exc}",
            zip_path=zip_path,
            staging_dir=package_root,
            error=f"apply:{exc}",
        )

    meta_path = os.path.join(cache, "last_applied.json")
    try:
        with open(meta_path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "version": version,
                    "sha256": expected or None,
                    "applied_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "files_copied": copied,
                    "files_failed": failed,
                    "addon_root": target,
                },
                handle,
                indent=2,
            )
            handle.write("\n")
    except OSError:
        pass

    if failed and not copied:
        return UpdateInstallResult(
            ok=False,
            message=(
                "Could not write any addon files (may be locked). "
                "Close Blender and use Install from Disk."
            ),
            zip_path=zip_path,
            staging_dir=package_root,
            applied=False,
            files_copied=copied,
            files_failed=failed,
            error="apply_locked",
        )

    msg = f"Update applied ({copied} files)."
    if failed:
        msg = f"{msg} {failed} file(s) were locked and skipped."
    msg = f"{msg} Restart Blender (or disable/enable the extension) to load it."
    return UpdateInstallResult(
        ok=True,
        message=msg,
        zip_path=zip_path,
        staging_dir=package_root,
        applied=True,
        files_copied=copied,
        files_failed=failed,
    )
