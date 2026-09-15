# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""Local Health Check diagnosis (pure — no bpy).

Important: callers must pass the **raw** ``cache_dir`` from Preferences, not the
value from ``prefs_to_config()`` (that helper already runs ``repair_cache_dir``
in memory and would hide empty/colliding prefs from Health Check).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from . import paths


@dataclass
class HealthDiagnosis:
    issues: List[str] = field(default_factory=list)
    fixed: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    roots: List[str] = field(default_factory=list)
    cache_raw: str = ""
    cache_effective: str = ""
    needs_cache_repair: bool = False
    ok: bool = True


# Last local health snapshot (stored after UAL_OT_health_check runs).
# Used by the opt-in “Send Diagnostics Report” operator.
_LAST_LOCAL_DIAG: HealthDiagnosis | None = None


def set_last_local_diagnostics(diag: HealthDiagnosis) -> None:
    global _LAST_LOCAL_DIAG
    # Store a shallow copy to avoid accidental mutation by callers.
    _LAST_LOCAL_DIAG = HealthDiagnosis(
        issues=list(diag.issues),
        fixed=list(diag.fixed),
        notes=list(diag.notes),
        roots=list(diag.roots),
        cache_raw=str(diag.cache_raw),
        cache_effective=str(diag.cache_effective),
        needs_cache_repair=bool(diag.needs_cache_repair),
        ok=bool(diag.ok),
    )


def get_last_local_diagnostics() -> HealthDiagnosis | None:
    return _LAST_LOCAL_DIAG


def diagnose_local_health(
    *,
    library_roots: Optional[List[str]] = None,
    cache_dir: str = "",
    library_root: str = "",
    online_access: bool = True,
    copy_to_library: bool = True,
    index_downloads: bool = True,
    keep_cache: bool = False,
) -> HealthDiagnosis:
    """Diagnose library/cache layout using raw preference paths."""
    result = HealthDiagnosis()
    roots = [os.path.normpath(r) for r in (library_roots or []) if (r or "").strip()]
    if not roots and (library_root or "").strip():
        roots = [os.path.normpath(library_root.strip())]
    result.roots = roots

    cache_raw = (cache_dir or "").strip()
    result.cache_raw = cache_raw

    if not roots:
        result.issues.append("No library folder set — pick one in Preferences → Library")
    for root in roots:
        if not os.path.isdir(root):
            result.issues.append(f"Library folder not found: {root}")

    repaired, migrate_notes = (
        paths.repair_cache_dir_with_notes(cache_raw, roots)
        if roots
        else (cache_raw or paths.default_cache_dir(None), [])
    )
    result.cache_effective = repaired
    for note in migrate_notes:
        if note not in result.fixed:
            result.fixed.append(note)

    if roots:
        raw_norm = os.path.normcase(os.path.normpath(cache_raw)) if cache_raw else ""
        rep_norm = os.path.normcase(os.path.normpath(repaired))
        if not cache_raw or raw_norm != rep_norm:
            result.needs_cache_repair = True
            if not cache_raw:
                result.fixed.append(f"Empty cache folder — will use {repaired}")
            elif paths.is_legacy_shared_cache_dir(cache_raw) or paths.is_foreign_dcc_cache_dir(
                cache_raw
            ):
                result.fixed.append(
                    f"Shared/Houdini cache detected — will use Blender-only {repaired}"
                )
            else:
                result.fixed.append(
                    f"Cache was inside the library — now using {repaired}"
                )
        else:
            # Exact equality with a root (should already be caught by repair)
            for root in roots:
                if rep_norm == os.path.normcase(os.path.normpath(root)):
                    result.issues.append("Cache and library can't be the same folder")

    if roots and repaired and not paths.same_volume(roots[0], repaired):
        result.issues.append(
            "Cache and library are on different drives — saving downloads will be slower"
        )

    if not copy_to_library:
        result.issues.append(
            "Copy Downloads to Library is OFF — files stay in cache and won't show in a "
            "shared Houdini library. Turn it ON in Preferences → Online → Downloads"
        )
    if not index_downloads:
        result.issues.append(
            "Index Downloads is OFF — files save to disk but My Library stays empty"
        )

    if copy_to_library and keep_cache:
        result.notes.append(
            "Keep cache copy is ON — staging folders remain after library copy. "
            "Recommended OFF for Fab/Megascans (Preferences → Online → Downloads)."
        )

    result.notes.append(
        "Share library folders with Houdini UAL (Online Downloads/). "
        f"Never share cache — Blender uses {paths.PRODUCT_CACHE_BASENAME}, "
        "Houdini uses UAL CACHE HOUDINI. After the other app saves, Rebuild Index."
    )

    if not online_access:
        result.issues.append(
            "Turn on Allow Online Access in Blender Preferences → System"
        )

    # Inline warnings parity (informational when already covered)
    for warn in paths.path_warnings(roots, cache_raw, library_root=library_root):
        if warn not in result.issues and not any(warn[:20] in f for f in result.fixed):
            low = warn.lower()
            if warn.startswith("No library"):
                continue
            if warn.startswith("Missing library") or warn.startswith(
                "Library folder not found"
            ):
                continue
            if result.needs_cache_repair and (
                "inside the library" in low
                or "collides" in low
                or "can't be the same" in low
                or "must not equal" in low
            ):
                continue
            result.notes.append(warn)

    result.ok = not result.issues and not result.needs_cache_repair
    return result


def apply_cache_repair(cache_path: str) -> str:
    """Create the repaired cache directory; return normalized path."""
    return paths.ensure_dir(os.path.normpath(cache_path))


def probe_cache_writable(cache_dir: str) -> Optional[str]:
    """Return None if writable, else an error message."""
    folder = (cache_dir or "").strip()
    if not folder:
        return "Cache folder isn't set. Health Check will use UAL CACHE."
    try:
        paths.ensure_dir(folder)
        probe = os.path.join(folder, "ual_healthcheck.tmp")
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write("ok")
        os.remove(probe)
    except OSError:
        return "Can't write to the cache folder. Check folder permissions."
    return None


def format_health_toast(
    issues: List[str],
    fixed: List[str],
    notes: Optional[List[str]] = None,
) -> Tuple[str, str]:
    """Short Blender info-bar line. Returns (WARNING|INFO, message)."""
    del notes  # details stay in Preferences; toasts stay one sentence
    if issues:
        first = issues[0]
        extra = len(issues) - 1
        if extra:
            return "WARNING", f"{first} (+{extra} more — see Preferences)"
        return "WARNING", first
    if fixed:
        return "INFO", fixed[0]
    return "INFO", "Folders look good — checking online sites…"
