# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""Online download resolution / quality helpers.

Not every source has resolution tiers:

* **Texture / HDRI size (1K–8K):** Poly Haven, ambientCG, GPUOpen, LazyTextures, PBRPX
* **Fab quality (raw/high/mid/low ≈ 8K/4K/2K/1K):** Fab / Megascans only
* **No tiers (ignore prefs):** Sketchfab, Poly Pizza, Three D Scans (single file)

GPUOpen formats are bit depths (8b/16b). Poly Pizza is GLB-only. Sketchfab is
format-only. ``SOURCE_DOWNLOAD_CAPS`` drives the N-panel capability strip and
Size/Format columns (always drawn when a result is selected).

Preferences ``default_download_resolution`` is a *max preference* for sources that
expose multiple tiers — mesh-only sources never use it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Optional, Sequence, Tuple

# Pixel / k-token texture & HDRI catalogs
SOURCES_WITH_TEXTURE_RESOLUTION = frozenset(
    {
        "polyhaven",
        "ambientcg",
        "gpuopen",
        "lazytextures",
        "pbrpx",
    }
)

# Megascans quality codes (not pixels)
SOURCES_WITH_FAB_QUALITY = frozenset({"fab"})

# Single-file / format-only — prefs resolution is N/A
SOURCES_WITHOUT_RESOLUTION = frozenset(
    {
        "threedscans",
        "polypizza",
        "sketchfab",
    }
)


@dataclass(frozen=True)
class SourceDownloadCaps:
    """What download pickers this website actually exposes."""

    size_kind: str  # none | resolution | quality
    size_hint: str
    format_kind: str  # none | single | choice | bit_depth
    format_hint: str
    format_fixed: str = ""


# Per-platform download memory — keep in sync with docs/online-sources.md
SOURCE_DOWNLOAD_CAPS = {
    "polyhaven": SourceDownloadCaps("resolution", "1K–8K", "choice", "HDR/glTF"),
    "ambientcg": SourceDownloadCaps("resolution", "1K–8K", "choice", "ZIP/maps"),
    "gpuopen": SourceDownloadCaps("resolution", "1K–8K", "bit_depth", "8/16-bit"),
    "lazytextures": SourceDownloadCaps("resolution", "1K–8K", "choice", "ZIP/maps"),
    "pbrpx": SourceDownloadCaps("resolution", "1K–8K", "choice", "maps/FBX"),
    "threedscans": SourceDownloadCaps("none", "Not offered", "choice", "STL/OBJ ZIP"),
    "polypizza": SourceDownloadCaps("none", "Not offered", "single", "GLB only", "GLB"),
    "sketchfab": SourceDownloadCaps("none", "Not offered", "choice", "GLB/glTF"),
    "fab": SourceDownloadCaps("quality", "Raw–Low", "choice", "glTF/FBX"),
}


def source_download_caps(source_id: str) -> SourceDownloadCaps:
    sid = str(source_id or "").strip().lower()
    return SOURCE_DOWNLOAD_CAPS.get(
        sid,
        SourceDownloadCaps("none", "Not offered", "none", "Per asset"),
    )


def size_ui_label(source_id: str) -> str:
    if source_download_caps(source_id).size_kind == "quality":
        return "Quality"
    return "Resolution"


def format_ui_label(source_id: str) -> str:
    if source_download_caps(source_id).format_kind == "bit_depth":
        return "Bit depth"
    return "Format"


def format_fixed_label(source_id: str, formats: Sequence[str] = ()) -> str:
    """Disabled Format column text when there is no dropdown."""
    items = [str(f).strip() for f in (formats or []) if str(f).strip()]
    if len(items) == 1:
        return items[0].upper()
    caps = source_download_caps(source_id)
    return caps.format_fixed or caps.format_hint or "Per asset"


def source_capability_lines(source_id: str) -> Tuple[str, str]:
    """Two narrow lines under the Online source dropdown."""
    from .online_type_filter import type_capability_line

    types = type_capability_line(source_id)
    caps = source_download_caps(source_id)
    if caps.size_kind == "none":
        size = "No size tiers"
    elif caps.size_kind == "quality":
        size = f"Quality: {caps.size_hint}"
    else:
        size = f"Size: {caps.size_hint}"
    if caps.format_kind == "bit_depth":
        fmt = f"Bit: {caps.format_hint}"
    elif caps.format_kind == "single":
        fmt = caps.format_hint
    else:
        fmt = f"Fmt: {caps.format_hint}"
    return types, f"{size} · {fmt}"


def source_capability_inline(source_id: str) -> str:
    """Single WIDE-tier line under Source (types + size/format)."""
    types, size_fmt = source_capability_lines(source_id)
    text = f"{types} · {size_fmt}".strip(" ·")
    if len(text) <= 42:
        return text
    return text[:40] + "…"

# When search left resolutions empty, still apply prefs against these candidates
_FALLBACK_TEXTURE_RES = ("1k", "2k", "4k", "8k")
_FAB_QUALITY_CODES = ("raw", "high", "mid", "low")

# Prefs pixel / ORIGINAL → Fab quality (max-tier intent)
_FAB_FROM_PREF = (
    (512, "low"),
    (1024, "low"),
    (2048, "mid"),
    (4096, "high"),
    (8192, "raw"),
)


def source_uses_download_resolution(source_id: str) -> bool:
    sid = (source_id or "").strip().lower()
    return sid in SOURCES_WITH_TEXTURE_RESOLUTION or sid in SOURCES_WITH_FAB_QUALITY


def source_resolution_help(source_id: str) -> str:
    sid = (source_id or "").strip().lower()
    return {
        "polyhaven": "Textures & HDRIs (meshes often ignore tier)",
        "ambientcg": "Material / HDRI / model ZIP size",
        "gpuopen": "MaterialX package size (1k–8k)",
        "lazytextures": "Archive / map tier",
        "pbrpx": "Map folder tier (mesh file is single)",
        "fab": "Quality Raw/High/Mid/Low (≈8K–1K), not pixels",
        "sketchfab": "No resolution tiers (format only)",
        "polypizza": "No resolution tiers (single GLB)",
        "threedscans": "No resolution tiers (STL or OBJ ZIP per asset)",
    }.get(sid, "")


def _norm_res(value: str) -> str:
    text = (value or "").strip().lower().replace(" ", "")
    if text in ("original", "source", "max", "auto"):
        return text
    if text.endswith("k") and text[:-1].isdigit():
        return str(int(text[:-1]) * 1024)
    digits = "".join(c for c in text if c.isdigit())
    return digits or text


def _is_original_pref(preferred: Optional[str]) -> bool:
    return _norm_res(preferred or "") in ("original", "source", "max")


def pick_highest_resolution(available: Sequence[str]) -> Optional[str]:
    items = [str(r) for r in (available or []) if r]
    if not items:
        return None
    numeric = []
    for raw in items:
        try:
            numeric.append((int(_norm_res(raw)), raw))
        except ValueError:
            continue
    if numeric:
        return max(numeric, key=lambda p: p[0])[1]
    return items[-1]


def pick_resolution(
    available: Sequence[str],
    preferred: Optional[str] = None,
) -> Optional[str]:
    """Pick best resolution from asset list using prefs default (e.g. ``2048``).

    Prefers exact match, then nearest lower-or-equal, then nearest overall.
    ``ORIGINAL`` / empty preferred → highest available.
    """
    items = [str(r) for r in (available or []) if r]
    if not items:
        return None
    if not preferred or _is_original_pref(preferred):
        return pick_highest_resolution(items) if _is_original_pref(preferred) else items[0]
    pref = _norm_res(preferred)
    norms = [(_norm_res(r), r) for r in items]
    for norm, raw in norms:
        if norm == pref:
            return raw
    try:
        pref_n = int(pref)
    except ValueError:
        return items[0]
    numeric = []
    for norm, raw in norms:
        try:
            numeric.append((int(norm), raw))
        except ValueError:
            continue
    if not numeric:
        return items[0]
    lower_or_eq = [pair for pair in numeric if pair[0] <= pref_n]
    if lower_or_eq:
        return max(lower_or_eq, key=lambda p: p[0])[1]
    return min(numeric, key=lambda p: abs(p[0] - pref_n))[1]


def pick_fab_quality(
    available: Sequence[str],
    preferred: Optional[str] = None,
) -> str:
    """Map prefs pixel/ORIGINAL → Megascans quality; prefer high over raw for auto."""
    pool = [str(q).strip().lower() for q in (available or []) if str(q).strip()]
    if not pool:
        pool = list(_FAB_QUALITY_CODES)
    # Already a quality code
    pref_raw = str(preferred or "").strip().lower()
    if pref_raw in _FAB_QUALITY_CODES and pref_raw in pool:
        return pref_raw
    # ORIGINAL / highest → Raw when available
    if _is_original_pref(preferred):
        for code in ("raw", "high", "mid", "low"):
            if code in pool:
                return code
        return pool[0]
    # Empty / auto → High (safer default than full Raw)
    if not preferred:
        for code in ("high", "raw", "mid", "low"):
            if code in pool:
                return code
        return pool[0]
    # Pixel / Nk → quality (max-tier intent)
    try:
        pref_n = int(_norm_res(preferred))
    except ValueError:
        for code in ("high", "mid", "low", "raw"):
            if code in pool:
                return code
        return pool[0]
    mapped = "mid"
    for threshold, code in _FAB_FROM_PREF:
        if pref_n <= threshold:
            mapped = code
            break
    else:
        mapped = "raw"
    if mapped in pool:
        return mapped
    # Nearest available by rank
    order = {c: i for i, c in enumerate(_FAB_QUALITY_CODES)}
    target = order.get(mapped, 1)
    return min(pool, key=lambda c: abs(order.get(c, 99) - target))


def preferred_resolution_from_cfg(cfg: Optional[dict]) -> str:
    return str((cfg or {}).get("online", {}).get("default_download_resolution") or "2048")


def _available_for_asset(
    source_id: str,
    asset: Any,
    *,
    source: Any = None,
    fmt: Optional[str] = None,
) -> List[str]:
    sid = (source_id or "").strip().lower()
    available: List[str] = []
    try:
        available = [str(r) for r in (getattr(asset, "resolutions", None) or []) if r]
    except Exception:
        available = []
    # Lazy API refresh when search left the list empty/incomplete
    if source is not None and hasattr(source, "get_resolutions"):
        try:
            lazy = source.get_resolutions(asset, fmt)
            if lazy:
                available = [str(r) for r in lazy if r]
        except Exception:
            pass
    if available:
        return available
    if sid in SOURCES_WITH_FAB_QUALITY:
        return list(_FAB_QUALITY_CODES)
    if sid in SOURCES_WITH_TEXTURE_RESOLUTION:
        return list(_FALLBACK_TEXTURE_RES)
    return []


def resolve_download_resolution(
    source_id: str,
    asset: Any,
    preferred: Optional[str] = None,
    *,
    source: Any = None,
    fmt: Optional[str] = None,
) -> Optional[str]:
    """Source-aware resolution/quality for ``download(asset, resolution, …)``.

    Returns ``None`` for sources with no tiers (caller should pass through).
    """
    sid = (source_id or "").strip().lower()
    if sid in SOURCES_WITHOUT_RESOLUTION:
        return None
    available = _available_for_asset(sid, asset, source=source, fmt=fmt)
    if sid in SOURCES_WITH_FAB_QUALITY:
        return pick_fab_quality(available, preferred)
    if not available:
        return None
    return pick_resolution(available, preferred)


# --- N-panel / per-asset override helpers ---------------------------------

_FAB_QUALITY_LABELS = {
    "raw": "Raw (8K)",
    "high": "High (4K)",
    "mid": "Mid (2K)",
    "low": "Low (1K)",
}


def fab_quality_label(tier: str) -> str:
    code = str(tier or "").strip().lower()
    return _FAB_QUALITY_LABELS.get(code, code.upper() if code else "")


def split_option_csv(value: str) -> List[str]:
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


def resolution_label(source_id: str, token: str) -> str:
    sid = (source_id or "").strip().lower()
    raw = str(token or "").strip()
    if sid in SOURCES_WITH_FAB_QUALITY:
        return fab_quality_label(raw)
    return raw


def show_resolution_ui(source_id: str, resolutions: Sequence[str]) -> bool:
    """Whether the N-panel Resolution/Quality enum should be visible."""
    sid = (source_id or "").strip().lower()
    if sid in SOURCES_WITHOUT_RESOLUTION:
        return False
    if not source_uses_download_resolution(sid):
        return False
    items = [str(r).strip() for r in (resolutions or []) if str(r).strip()]
    if not items:
        # Still show with fallback tiers so user can override Settings
        return sid in SOURCES_WITH_TEXTURE_RESOLUTION or sid in SOURCES_WITH_FAB_QUALITY
    if len(items) == 1 and items[0].lower() in ("source", "original", "auto"):
        return False
    return True


def show_format_ui(formats: Sequence[str], source_id: str = "") -> bool:
    """Whether the N-panel Format enum should be an interactive dropdown."""
    items = [str(f).strip() for f in (formats or []) if str(f).strip()]
    if len(items) > 1:
        return True
    sid = str(source_id or "").strip().lower()
    caps = source_download_caps(sid)
    return caps.format_kind in ("choice", "bit_depth") and len(items) > 1


def build_resolution_enum_items(
    source_id: str,
    resolutions_csv: str,
) -> List[tuple]:
    """RNA enum items: DEFAULT first, then asset tiers (or Fab/texture fallbacks)."""
    sid = (source_id or "").strip().lower()
    items: List[tuple] = [
        ("DEFAULT", "Default (from Settings)", "Use Preferences → Default Max Texture Quality"),
    ]
    tokens = split_option_csv(resolutions_csv)
    if not tokens:
        if sid in SOURCES_WITH_FAB_QUALITY:
            tokens = list(_FAB_QUALITY_CODES)
        elif sid in SOURCES_WITH_TEXTURE_RESOLUTION:
            tokens = list(_FALLBACK_TEXTURE_RES)
    seen = {"DEFAULT"}
    for token in tokens:
        key = str(token).strip()
        if not key or key.upper() in seen or key in seen:
            continue
        seen.add(key)
        items.append((key, resolution_label(sid, key), ""))
    return items


def build_format_enum_items(formats_csv: str, source_id: str = "") -> List[tuple]:
    items: List[tuple] = [
        ("AUTO", "Auto (first available)", "Use the first format from the asset listing"),
    ]
    seen = {"AUTO"}
    sid = (source_id or "").strip().lower()
    format_label = None
    if sid == "fab":
        try:
            from .sources.fab import fab_format_label

            format_label = fab_format_label
        except Exception:
            format_label = None
    elif sid == "sketchfab":
        try:
            from .sources.sketchfab import sketchfab_format_label

            format_label = sketchfab_format_label
        except Exception:
            format_label = None
    for token in split_option_csv(formats_csv):
        key = str(token).strip()
        # Skip corrupted Fab dict-literal leftovers from older builds
        if not key or key.startswith("{") or "groupname" in key.lower() or "groupName" in key:
            continue
        if key.upper() in seen or key in seen:
            continue
        seen.add(key)
        if format_label is not None:
            label = format_label(key)
        else:
            label = key.upper()
        items.append((key, label, ""))
    return items


def preferred_resolution_from_ui(ui: Any, cfg: Optional[dict]) -> str:
    """N-panel override wins; DEFAULT/empty → Settings."""
    pick = ""
    if ui is not None:
        pick = str(getattr(ui, "online_dl_resolution", "") or "").strip()
    if pick and pick.upper() not in ("DEFAULT", "AUTO", ""):
        return pick
    return preferred_resolution_from_cfg(cfg)


def preferred_format_from_ui(ui: Any, formats: Sequence[str]) -> Optional[str]:
    pick = ""
    if ui is not None:
        pick = str(getattr(ui, "online_dl_format", "") or "").strip()
    pool = [str(f).strip() for f in (formats or []) if str(f).strip()]
    pool_l = {p.lower() for p in pool}
    if pick and pick.upper() not in ("AUTO", "DEFAULT", ""):
        # Stale EnumProperty can keep a format from the previous asset — clamp to pool
        if not pool or pick in pool or pick.lower() in pool_l:
            return pick
    return pool[0] if pool else None


# Guard against EnumProperty update → seed → format assign → update recursion
_SEEDING_DOWNLOAD_OPTIONS = False


def seed_download_option_caches(ui: Any, item: Any) -> None:
    """Copy selected asset format/resolution CSV into WM enum caches (main thread)."""
    global _SEEDING_DOWNLOAD_OPTIONS
    if ui is None or item is None:
        return
    res_csv = str(getattr(item, "resolutions", "") or "")
    fmt_csv = str(getattr(item, "formats", "") or "")
    sid = str(getattr(item, "source_id", "") or "")
    # Ensure Fab/texture sources have something to show before enrich returns
    if not split_option_csv(res_csv) and source_uses_download_resolution(sid):
        if sid.strip().lower() in SOURCES_WITH_FAB_QUALITY:
            res_csv = ",".join(_FAB_QUALITY_CODES)
        else:
            res_csv = ",".join(_FALLBACK_TEXTURE_RES)
    try:
        _SEEDING_DOWNLOAD_OPTIONS = True
        ui.online_dl_res_items = res_csv
        ui.online_dl_fmt_items = fmt_csv
        ui.online_dl_options_asset_id = str(getattr(item, "asset_id", "") or "")
        ui.online_dl_options_source_id = sid
        # Reset picks when asset identity changes
        prev = str(getattr(ui, "online_dl_bound_asset_id", "") or "")
        bound = "{}:{}".format(sid, getattr(item, "asset_id", "") or "")
        if prev != bound:
            ui.online_dl_bound_asset_id = bound
            try:
                ui.online_dl_resolution = "DEFAULT"
            except Exception:
                pass
            try:
                ui.online_dl_format = "AUTO"
            except Exception:
                pass
        ui.online_dl_options_ready = True
    except Exception:
        pass
    finally:
        _SEEDING_DOWNLOAD_OPTIONS = False


def is_seeding_download_options() -> bool:
    return bool(_SEEDING_DOWNLOAD_OPTIONS)
