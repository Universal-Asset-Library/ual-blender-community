# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""SQLite asset index."""

from __future__ import annotations

import os
import shutil
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from . import paths

INCOMPATIBLE_DB_SUFFIX = ".incompatible-backup"
SHARED_CACHE_SCHEMA_ERROR = (
    "This cache folder's assets.db was created by Houdini UAL and cannot be opened "
    "by Blender UAL. Share only the library folder (Online Downloads); give Houdini "
    "and Blender separate cache directories (e.g. E:/UAL CACHE HOUDINI and "
    "E:/UAL CACHE BLENDER), then run Rebuild Index in each app."
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    asset_type TEXT NOT NULL,
    root TEXT NOT NULL,
    thumb_path TEXT DEFAULT '',
    mtime REAL DEFAULT 0,
    size INTEGER DEFAULT 0,
    source_id TEXT DEFAULT 'local'
);
CREATE INDEX IF NOT EXISTS idx_assets_type ON assets(asset_type);
CREATE INDEX IF NOT EXISTS idx_assets_name ON assets(name);
"""


def _table_columns(conn: sqlite3.Connection, table: str) -> Set[str]:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return set()
    return {str(row[1]) for row in rows if row and row[1]}


def _is_blender_assets_schema(columns: Set[str]) -> bool:
    return bool(columns) and "root" in columns and "root_path" not in columns


def _is_foreign_assets_schema(columns: Set[str]) -> bool:
    if not columns:
        return False
    if _is_blender_assets_schema(columns):
        return False
    return "root_path" in columns


def _backup_foreign_assets_db(db_path: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = f"{db_path}.{stamp}{INCOMPATIBLE_DB_SUFFIX}"
    if not os.path.isfile(db_path):
        return backup_path
    try:
        shutil.move(db_path, backup_path)
    except OSError as exc:
        # Typical when Houdini (or another Blender) still has assets.db open.
        raise RuntimeError(SHARED_CACHE_SCHEMA_ERROR) from exc
    return backup_path


def _ensure_compatible_assets_db(db_path: str) -> None:
    if not os.path.isfile(db_path):
        return
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='assets'"
        ).fetchone()
        if not row:
            return
        columns = _table_columns(conn, "assets")
        if _is_blender_assets_schema(columns):
            return
        if not _is_foreign_assets_schema(columns):
            return
    finally:
        conn.close()
    _backup_foreign_assets_db(db_path)


def connect(cache_dir: str) -> sqlite3.Connection:
    paths.ensure_dir(cache_dir)
    db = paths.db_path(cache_dir)
    try:
        _ensure_compatible_assets_db(db)
    except RuntimeError:
        raise
    except OSError as exc:
        raise RuntimeError(SHARED_CACHE_SCHEMA_ERROR) from exc
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


_UPSERT_SQL = """
INSERT INTO assets (path, name, asset_type, root, thumb_path, mtime, size, source_id)
VALUES (:path, :name, :asset_type, :root, :thumb_path, :mtime, :size, :source_id)
ON CONFLICT(path) DO UPDATE SET
    name=excluded.name,
    asset_type=excluded.asset_type,
    root=excluded.root,
    thumb_path=excluded.thumb_path,
    mtime=excluded.mtime,
    size=excluded.size,
    source_id=excluded.source_id
"""


def upsert_asset(conn: sqlite3.Connection, record: Dict[str, Any]) -> None:
    conn.execute(_UPSERT_SQL, record)


def upsert_assets_batch(conn: sqlite3.Connection, records: List[Dict[str, Any]]) -> int:
    """Batched upsert inside the caller's transaction. Returns number of rows written."""
    if not records:
        return 0
    conn.executemany(_UPSERT_SQL, records)
    return len(records)


def clear_root(conn: sqlite3.Connection, root: str) -> None:
    conn.execute("DELETE FROM assets WHERE root = ?", (os.path.normpath(root),))


def delete_assets_by_paths(conn: sqlite3.Connection, paths: List[str]) -> int:
    """Delete exact path matches. Returns rows removed."""
    removed = 0
    for path in paths:
        normalized = os.path.normpath(path or "")
        if not normalized:
            continue
        cur = conn.execute("DELETE FROM assets WHERE path = ?", (normalized,))
        removed += int(cur.rowcount or 0)
        # Case-insensitive retry on Windows
        if os.name == "nt":
            cur = conn.execute(
                "DELETE FROM assets WHERE lower(path) = lower(?)",
                (normalized,),
            )
            removed += int(cur.rowcount or 0)
    return removed


def delete_assets_under_prefix(conn: sqlite3.Connection, folder: str) -> int:
    """Delete indexed rows whose path is the folder or a child of it."""
    normalized = os.path.normpath(folder or "")
    if not normalized:
        return 0
    prefix = normalized.rstrip("\\/") + os.sep
    cur = conn.execute(
        "DELETE FROM assets WHERE path = ? OR path LIKE ?",
        (normalized, prefix + "%"),
    )
    removed = int(cur.rowcount or 0)
    # Also match forward-slash children (paths written with /)
    alt = normalized.replace("\\", "/").rstrip("/") + "/"
    cur = conn.execute(
        "DELETE FROM assets WHERE path LIKE ?",
        (alt + "%",),
    )
    removed += int(cur.rowcount or 0)
    return removed


def query_assets(
    conn: sqlite3.Connection,
    *,
    query: str = "",
    asset_type: str = "ALL",
    favorites: Optional[List[str]] = None,
    limit: int = 500,
) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM assets WHERE 1=1"
    params: List[Any] = []
    if asset_type and asset_type != "ALL":
        sql += " AND asset_type = ?"
        params.append(asset_type)
    from .search_text import (
        compact_alnum,
        escape_like_token,
        normalize_search_query,
        query_tokens,
        rank_search_hit,
    )

    raw_q = normalize_search_query(query)
    if raw_q:
        tokens = query_tokens(raw_q)
        compact = compact_alnum(raw_q)
        token_sql = []
        for tok in tokens:
            like = f"%{escape_like_token(tok.lower())}%"
            token_sql.append(
                "(lower(name) LIKE ? ESCAPE '\\' OR lower(path) LIKE ? ESCAPE '\\')"
            )
            params.extend([like, like])
        compact_sql = ""
        if compact:
            compact_sql = (
                "replace(replace(replace(lower(name),' ',''),'_',''),'-','') LIKE ? "
                "OR replace(replace(replace(lower(path),' ',''),'_',''),'-','') LIKE ?"
            )
        if token_sql and compact_sql:
            sql += " AND ((" + " AND ".join(token_sql) + ") OR (" + compact_sql + "))"
            params.extend([f"%{compact}%", f"%{compact}%"])
        elif token_sql:
            sql += " AND " + " AND ".join(token_sql)
        elif compact_sql:
            sql += " AND (" + compact_sql + ")"
            params.extend([f"%{compact}%", f"%{compact}%"])
    sql += " ORDER BY name COLLATE NOCASE"
    # Favorites use Python norm-key match (Windows case / slash-safe).
    # Avoid SQL IN + early LIMIT which can miss favorites sorted later by name.
    if favorites is None:
        sql += " LIMIT ?"
        params.append(limit)
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    if favorites is not None:
        if not favorites:
            return []
        try:
            from .config import norm_asset_path_key
        except Exception:
            def norm_asset_path_key(path: str) -> str:  # type: ignore
                return os.path.normcase(os.path.normpath(path or ""))

        keys = {norm_asset_path_key(p) for p in favorites if p}
        rows = [r for r in rows if norm_asset_path_key(r.get("path") or "") in keys]
        rows = rows[:limit]
    if raw_q:
        rows.sort(
            key=lambda r: rank_search_hit(
                r.get("name") or "", r.get("path") or "", raw_q
            )
        )
    return rows


def fingerprint_root(root: str) -> str:
    """Cheap fingerprint: file count + max mtime under root."""
    count = 0
    max_mtime = 0.0
    if not os.path.isdir(root):
        return "missing"
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            count += 1
            try:
                max_mtime = max(max_mtime, os.path.getmtime(os.path.join(dirpath, name)))
            except OSError:
                pass
            if count > 50000:
                break
    return f"{count}:{max_mtime:.3f}"


def sync_thumb_paths_after_cache_clear(conn: sqlite3.Connection, thumbs_dir: str) -> int:
    """Null or reattach ``thumb_path`` after ``thumbs/`` was cleared.

    Rows pointing at missing files or at files under the cache thumbs folder are
    updated: prefer ``Preview/thumb_preview.png`` beside the asset, else empty.
    Returns number of rows updated.
    """
    thumbs_norm = os.path.normcase(os.path.normpath(thumbs_dir or ""))
    if not thumbs_norm:
        return 0
    sep = os.sep
    updated = 0
    rows = conn.execute("SELECT path, thumb_path FROM assets").fetchall()
    for row in rows:
        tp = str(row["thumb_path"] or "").strip()
        if not tp:
            continue
        tp_norm = os.path.normpath(tp)
        tp_key = os.path.normcase(tp_norm)
        under_thumbs = tp_key == thumbs_norm or tp_key.startswith(thumbs_norm + sep)
        missing = not os.path.isfile(tp_norm)
        if not under_thumbs and not missing:
            continue
        asset_path = str(row["path"] or "")
        folder = asset_path if os.path.isdir(asset_path) else os.path.dirname(asset_path)
        embedded = os.path.join(folder, "Preview", "thumb_preview.png") if folder else ""
        new_tp = embedded if embedded and os.path.isfile(embedded) else ""
        if new_tp == tp:
            continue
        conn.execute(
            "UPDATE assets SET thumb_path = ? WHERE path = ?",
            (new_tp, row["path"]),
        )
        updated += 1
    return updated
