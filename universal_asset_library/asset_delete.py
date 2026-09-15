# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""Delete library assets from disk and/or the SQLite index (Houdini parity).

Scopes:
- ``files`` — remove package folder / file on disk + index + favorites
- ``index_only`` — hide from Library index; files stay on disk

Never deletes cache roots, thumbs roots, or library root folders themselves.
Online Downloads packages delete the whole package folder (bundle-aware).
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from . import config as ual_config
from . import db
from . import paths
from . import thumbnails

DELETE_INDEX_ONLY = "index_only"
DELETE_FILES = "files"

_BUNDLE_NAME = ".assetlibrary.bundle.json"


def _default_cfg() -> Dict[str, Any]:
    try:
        from . import preferences

        return preferences.load_active_config()
    except Exception:
        return ual_config.load_config()


@dataclass
class DeletePlan:
    scope: str
    selection_count: int = 0
    db_paths: List[str] = field(default_factory=list)
    file_paths: List[str] = field(default_factory=list)
    folder_paths: List[str] = field(default_factory=list)
    thumb_paths: List[str] = field(default_factory=list)
    config_paths: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    labels: List[str] = field(default_factory=list)


@dataclass
class DeleteResult:
    success: bool
    message: str
    db_removed: int = 0
    files_removed: int = 0
    folders_removed: int = 0


def _norm(path: str) -> str:
    return os.path.normpath(path) if path else ""


def _is_protected_root(path: str, cfg: Dict[str, Any]) -> bool:
    normalized = _norm(path)
    if not normalized:
        return True
    cache = _norm(str(cfg.get("cache_dir") or ""))
    protected: Set[str] = set()
    if cache:
        protected.add(cache)
        protected.add(_norm(os.path.join(cache, "sources")))
        protected.add(_norm(os.path.join(cache, "thumbs")))
        protected.add(_norm(os.path.join(cache, "config")))
    for root in cfg.get("library_roots") or []:
        if root:
            protected.add(_norm(root))
    key = ual_config.norm_asset_path_key(normalized)
    return key in {ual_config.norm_asset_path_key(p) for p in protected}


def _has_bundle(folder: str) -> bool:
    return bool(folder) and os.path.isfile(os.path.join(folder, _BUNDLE_NAME))


def resolve_package_folder(path: str) -> str:
    """Climb to Online Downloads package / bundle folder when present."""
    normalized = _norm(path)
    if not normalized:
        return ""
    cur = normalized if os.path.isdir(normalized) else os.path.dirname(normalized)
    found = ""
    for _ in range(8):
        if not cur:
            break
        if _has_bundle(cur):
            found = cur
            break
        # Prefer …/Online Downloads/<source>/<Type>/<Name>/
        parts = [p.lower() for p in cur.replace("\\", "/").split("/") if p]
        if "online downloads" in parts:
            try:
                idx = parts.index("online downloads")
                # source / Type / Name → at least 3 segments after
                if len(parts) >= idx + 4:
                    found = cur
                    # keep walking up until Name folder (one under Type)
                    parent = os.path.dirname(cur)
                    parent_parts = [p.lower() for p in parent.replace("\\", "/").split("/") if p]
                    if "online downloads" in parent_parts and len(parent_parts) >= idx + 4:
                        cur = parent
                        continue
                    break
            except ValueError:
                pass
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    if found:
        return _norm(found)
    if os.path.isdir(normalized):
        return normalized
    return _norm(os.path.dirname(normalized))


def plan_delete(
    asset_paths: Sequence[str],
    scope: str = DELETE_FILES,
    *,
    cfg: Optional[Dict[str, Any]] = None,
) -> DeletePlan:
    cfg = ual_config.coerce_config(cfg or _default_cfg())
    scope = scope if scope in (DELETE_FILES, DELETE_INDEX_ONLY) else DELETE_FILES
    plan = DeletePlan(scope=scope)

    seen_folders: Set[str] = set()
    seen_files: Set[str] = set()
    seen_db: Set[str] = set()

    for raw in asset_paths:
        path = _norm(raw)
        if not path:
            continue
        plan.selection_count += 1
        label = os.path.basename(path.rstrip("\\/")) or path
        plan.labels.append(label)

        if _is_protected_root(path, cfg):
            plan.warnings.append(
                "Can't delete that — it's a library or cache folder, not an asset."
            )
            continue

        package = resolve_package_folder(path)
        if scope == DELETE_FILES and package and not _is_protected_root(package, cfg):
            key = ual_config.norm_asset_path_key(package)
            if key not in seen_folders:
                seen_folders.add(key)
                if os.path.isdir(package):
                    plan.folder_paths.append(package)
                plan.config_paths.append(package)
                plan.db_paths.append(package)
                # Also remove indexed children / the original path
                if path != package:
                    plan.db_paths.append(path)
                    plan.config_paths.append(path)
        elif scope == DELETE_FILES and os.path.isfile(path):
            key = ual_config.norm_asset_path_key(path)
            if key not in seen_files:
                seen_files.add(key)
                plan.file_paths.append(path)
                plan.config_paths.append(path)
                plan.db_paths.append(path)
                fp = thumbnails.fingerprint_for_path(path)
                thumb = thumbnails.local_thumb_path(str(cfg.get("cache_dir") or ""), fp)
                if thumb and os.path.isfile(thumb):
                    plan.thumb_paths.append(thumb)
        else:
            # Index-only, or missing on disk but still indexed
            plan.db_paths.append(path)
            plan.config_paths.append(path)
            if package and package != path:
                plan.db_paths.append(package)

        for p in list(plan.db_paths):
            k = ual_config.norm_asset_path_key(p)
            if k not in seen_db:
                seen_db.add(k)

    # Deduplicate lists while preserving order
    def _dedupe(items: List[str]) -> List[str]:
        out: List[str] = []
        seen: Set[str] = set()
        for item in items:
            key = ual_config.norm_asset_path_key(item)
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
        return out

    plan.db_paths = _dedupe(plan.db_paths)
    plan.file_paths = _dedupe(plan.file_paths)
    plan.folder_paths = _dedupe(plan.folder_paths)
    plan.thumb_paths = _dedupe(plan.thumb_paths)
    plan.config_paths = _dedupe(plan.config_paths)

    if plan.selection_count and not (plan.db_paths or plan.file_paths or plan.folder_paths):
        if not plan.warnings:
            plan.warnings.append("Nothing to delete — this asset isn't on disk.")
    return plan


def confirm_message(plan: DeletePlan) -> str:
    if plan.warnings and not (plan.db_paths or plan.file_paths or plan.folder_paths):
        return "\n".join(plan.warnings)
    names = ", ".join(plan.labels[:3])
    if len(plan.labels) > 3:
        names += f", +{len(plan.labels) - 3} more"
    if plan.scope == DELETE_INDEX_ONLY:
        return (
            f"Hide {plan.selection_count} asset(s) from the library index?\n"
            f"{names}\n\nFiles on disk are kept."
        )
    n_folders = len(plan.folder_paths)
    return (
        f"Delete {plan.selection_count} asset(s) from disk and remove from the index?\n"
        f"{names}\n\n"
        f"Removes {n_folders} folder(s) and {len(plan.file_paths)} file(s). "
        "Online packages delete the full download folder. This cannot be undone."
    )


def execute_delete(plan: DeletePlan, *, cfg: Optional[Dict[str, Any]] = None) -> DeleteResult:
    cfg = ual_config.coerce_config(cfg or _default_cfg())
    if plan.warnings and not (plan.db_paths or plan.file_paths or plan.folder_paths):
        return DeleteResult(False, plan.warnings[0] if plan.warnings else "Nothing to delete.")

    folders_removed = 0
    files_removed = 0
    if plan.scope == DELETE_FILES:
        for folder in plan.folder_paths:
            if _is_protected_root(folder, cfg):
                continue
            if os.path.isdir(folder):
                try:
                    shutil.rmtree(folder)
                    folders_removed += 1
                except OSError as exc:
                    return DeleteResult(
                        False,
                        "Couldn't delete the folder. Close it in Explorer if it's open.",
                    )
        for file_path in plan.file_paths:
            if _is_protected_root(file_path, cfg):
                continue
            if os.path.isfile(file_path):
                try:
                    os.remove(file_path)
                    files_removed += 1
                except OSError as exc:
                    return DeleteResult(
                        False,
                        "Couldn't delete the file. Close it if another program is using it.",
                    )
        for thumb in plan.thumb_paths:
            try:
                if os.path.isfile(thumb):
                    os.remove(thumb)
            except OSError:
                pass

    cache = str(cfg.get("cache_dir") or "")
    db_removed = 0
    if cache and plan.db_paths:
        conn = db.connect(cache)
        try:
            db_removed = db.delete_assets_by_paths(conn, plan.db_paths)
            # Also drop indexed children under deleted folders
            for folder in plan.folder_paths:
                db_removed += db.delete_assets_under_prefix(conn, folder)
            conn.commit()
        finally:
            conn.close()
        # Invalidate fingerprint short-circuit so a later Rebuild sees the change
        try:
            from . import indexer

            for root in cfg.get("library_roots") or []:
                indexer._FINGERPRINT_CACHE.pop(_norm(root), None)
        except Exception:
            pass

    for path in plan.config_paths:
        ual_config.remove_path_list_references(cfg, path)
    try:
        ual_config.save_config(cfg, cache or None)
    except Exception:
        pass

    if plan.scope == DELETE_INDEX_ONLY:
        msg = (
            f"Hidden {db_removed} from the library (files kept on disk)"
            if db_removed != 1
            else "Hidden from the library (files kept on disk)"
        )
    else:
        parts = []
        if folders_removed:
            parts.append(f"{folders_removed} folder" + ("s" if folders_removed != 1 else ""))
        if files_removed:
            parts.append(f"{files_removed} file" + ("s" if files_removed != 1 else ""))
        if not parts:
            msg = "Removed from the library index"
        else:
            msg = "Deleted " + " and ".join(parts) + " from disk"
    return DeleteResult(
        True,
        msg,
        db_removed=db_removed,
        files_removed=files_removed,
        folders_removed=folders_removed,
    )


def delete_asset_paths(
    asset_paths: Sequence[str],
    scope: str = DELETE_FILES,
    *,
    cfg: Optional[Dict[str, Any]] = None,
) -> Tuple[DeletePlan, DeleteResult]:
    """Plan + execute (caller handles Blender confirm dialog)."""
    cfg = ual_config.coerce_config(cfg or _default_cfg())
    plan = plan_delete(asset_paths, scope, cfg=cfg)
    result = execute_delete(plan, cfg=cfg)
    return plan, result
