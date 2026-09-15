# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""Pure helpers for hover preview detail rows (no bpy — unit-testable)."""

from __future__ import annotations

import os
from typing import List, Sequence, Tuple

HoverDetailRows = List[Tuple[str, str]]

_MAX_DETAIL_ROWS = 4

# Compact source labels for the big preview (License Source)
_SOURCE_LABELS = {
    "polyhaven": "Poly Haven",
    "ambientcg": "ambientCG",
    "gpuopen": "GPUOpen",
    "lazytextures": "LazyTextures",
    "threedscans": "Three D Scans",
    "pbrpx": "PBRPX",
    "polypizza": "Poly Pizza",
    "sketchfab": "Sketchfab",
    "fab": "Fab",
}


def source_display_name(source_id: str) -> str:
    sid = (source_id or "").strip().lower()
    if not sid:
        return ""
    return _SOURCE_LABELS.get(sid, sid)


def _pretty_res(token: str) -> str:
    t = (token or "").strip()
    if not t:
        return ""
    low = t.lower().replace(" ", "")
    if low.endswith("k") and low[:-1].replace(".", "", 1).isdigit():
        return low[:-1].upper() + "K" if "." not in low[:-1] else t.upper()
    if low.isdigit():
        try:
            n = int(low)
        except ValueError:
            return t
        if n >= 1024 and n % 1024 == 0:
            return f"{n // 1024}K"
        if n >= 1000:
            return f"{max(1, round(n / 1000))}K"
    return t.upper() if len(t) <= 6 else t


def format_size_span(resolutions: str = "", *, items: Sequence[str] = ()) -> str:
    """Compact size line: ``2K`` or ``1K–8K`` from CSV / list."""
    toks: List[str] = []
    if items:
        toks = [str(x).strip() for x in items if str(x).strip()]
    else:
        raw = (resolutions or "").replace(";", ",")
        toks = [t.strip() for t in raw.split(",") if t.strip()]
    if not toks:
        return ""
    pretty = [_pretty_res(t) for t in toks]
    pretty = [p for p in pretty if p]
    if not pretty:
        return ""
    if len(pretty) == 1:
        return pretty[0]
    return f"{pretty[0]}–{pretty[-1]}"


def format_formats_short(formats: str = "", *, items: Sequence[str] = (), limit: int = 2) -> str:
    """``GLTF · FBX`` (capped) from CSV / list."""
    toks: List[str] = []
    if items:
        toks = [str(x).strip() for x in items if str(x).strip()]
    else:
        raw = (formats or "").replace(";", ",")
        toks = [t.strip() for t in raw.split(",") if t.strip()]
    if not toks:
        return ""
    shown = [t.upper() for t in toks[: max(1, int(limit))]]
    line = " · ".join(shown)
    if len(toks) > limit:
        line += "…"
    return line


def compact_product_line(
    *,
    mapped_type: str = "",
    resolutions: str = "",
    formats: str = "",
    file_size_label: str = "",
    in_library: bool = False,
) -> str:
    """One scannable product row: ``mesh · 1K–8K · GLTF`` or ``mesh · 12 MB``."""
    parts: List[str] = []
    atype = (mapped_type or "").strip() or "unknown"
    parts.append(atype)
    size = format_size_span(resolutions)
    if size:
        parts.append(size)
    elif (file_size_label or "").strip():
        parts.append(file_size_label.strip())
    fmt = format_formats_short(formats)
    if fmt:
        parts.append(fmt)
    if in_library:
        parts.append("In library")
    return " · ".join(parts)


def hover_tooltip_chrome_height(detail_count: int = 1) -> float:
    """Extra px below the square thumb for name + labeled meta rows + pads."""
    n = max(1, min(_MAX_DETAIL_ROWS, int(detail_count or 1)))
    # name 16 + n×13 lines + bottom pad 8 + top pad under image 6
    return 16.0 + 13.0 * float(n) + 14.0


def build_library_hover_details(
    *,
    asset_type: str,
    file_size_label: str = "",
    tags_line: str = "",
    source_id: str = "",
) -> HoverDetailRows:
    """Compact meta — product line + optional source (saved Online packs)."""
    rows: HoverDetailRows = [
        (
            "",
            compact_product_line(
                mapped_type=asset_type or "unknown",
                file_size_label=file_size_label,
            ),
        )
    ]
    src = source_display_name(source_id)
    if src and (source_id or "").strip().lower() not in ("", "local"):
        rows.append(("Source", src))
    if tags_line:
        rows.append(("", tags_line))
    return rows[:_MAX_DETAIL_ROWS]


def build_online_hover_details(
    *,
    source_id: str,
    mapped_type: str,
    license_name: str = "",
    pricing: str = "",
    in_library: bool = False,
    resolutions: str = "",
    formats: str = "",
) -> HoverDetailRows:
    """Compact big-preview meta: product · License · Source (easy to scan)."""
    rows: HoverDetailRows = [
        (
            "",
            compact_product_line(
                mapped_type=mapped_type,
                resolutions=resolutions,
                formats=formats,
                in_library=in_library,
            ),
        )
    ]
    license = (license_name or "").strip()
    if license:
        rows.append(("License", license))
    src = source_display_name(source_id)
    if src:
        rows.append(("Source", src))
    if pricing:
        # Keep pricing as a trailing note on the product line when present
        prod = rows[0][1]
        rows[0] = ("", f"{prod} · {pricing}" if prod else pricing)
    return rows[:_MAX_DETAIL_ROWS]


def resolve_card_target_index(
    *,
    hover_index: int,
    hover_online: bool,
    hover_card_ready: bool,
    pinned: bool,
    scope: str,
    selected_index: int,
    online_selected_index: int,
) -> Tuple[int, bool]:
    """Index the visible card represents — never writes WM selection.

    Dwelling / pinned-with-hover → hover index.
    Pinned without hover index → WM selection for current scope.
    """
    if int(hover_index) >= 0 and (bool(hover_card_ready) or bool(pinned)):
        return int(hover_index), bool(hover_online)
    online = (scope or "") == "online"
    if online:
        return int(online_selected_index or 0), True
    return int(selected_index or 0), False


def _norm_dir(path: str) -> str:
    return os.path.normcase(os.path.normpath(path or ""))


def is_shared_thumbs_dir(path: str, thumbs_dir: str) -> bool:
    """True when ``path`` is the global UAL thumbs cache (not an asset package)."""
    if not path or not thumbs_dir:
        return False
    return _norm_dir(path) == _norm_dir(thumbs_dir)


def hover_thumb_upgrade_roots(
    *,
    thumb_path: str = "",
    thumbs_dir: str = "",
    source_package_dir: str = "",
) -> List[str]:
    """Directories safe to ``discover_preview_image`` for a better hover thumb.

    Never includes the shared ``{cache}/thumbs`` folder — discovering there
    scans every ``online_*.png`` / local fingerprint and can return another
    asset's image (looks like a random big preview).
    """
    roots: List[str] = []
    seen = set()

    def _add(p: str) -> None:
        n = _norm_dir(p)
        if not n or n in seen:
            return
        if is_shared_thumbs_dir(p, thumbs_dir):
            return
        seen.add(n)
        roots.append(os.path.normpath(p))

    if source_package_dir:
        _add(source_package_dir)
    parent = os.path.dirname(thumb_path or "")
    if parent:
        _add(parent)
    return roots


def online_hover_upgrade_candidates(
    *,
    thumb_path: str,
    asset_type: str,
    upgrade_roots: List[str],
    discover_fn,
) -> List[str]:
    """Package preview paths that may replace ``thumb_path`` (size / missing).

    ``upgrade_roots`` must already exclude shared ``{cache}/thumbs/`` — see
    ``hover_thumb_upgrade_roots`` (0.3.2 random big-preview fix). Order is
    preserved; the first candidate that yields a GPU icon wins in wiring.
    ``discover_fn(root, asset_type)`` is injected so this stays bpy-free.
    """
    current = (thumb_path or "").strip()
    current_n = os.path.normpath(current) if current else ""
    out: List[str] = []
    for root in upgrade_roots:
        if not root or not os.path.isdir(root):
            continue
        try:
            discovered = discover_fn(root, asset_type) or ""
        except Exception:
            discovered = ""
        if not discovered:
            continue
        if current_n and os.path.normpath(discovered) == current_n:
            continue
        try:
            better = not current or not os.path.isfile(current)
            if not better:
                better = os.path.getsize(discovered) >= os.path.getsize(current)
        except Exception:
            better = True
        if better:
            out.append(os.path.normpath(discovered))
    return out


def resolve_library_hover_thumb_path(
    *,
    item_thumb_path: str = "",
    discovered: str = "",
) -> str:
    """Prefer the row's own cache thumb; discover only when missing/invalid."""
    tp = (item_thumb_path or "").strip()
    if tp and os.path.isfile(tp):
        return os.path.normpath(tp)
    disc = (discovered or "").strip()
    if disc and os.path.isfile(disc):
        return os.path.normpath(disc)
    return tp or disc or ""
