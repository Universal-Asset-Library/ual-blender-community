# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""Per-source Online Type filter — each platform has its own type taxonomy.

The N-panel enum is always ALL / mesh / material_set / texture / hdri.
Connectors do **not** share that enum. Do not exact-match ``asset_type == filter``
and do not assume Material ≡ Texture globally.

Policy lives here so search, Load More, empty-state, and tests stay in sync.
``category=`` is only the connector's *asset-type* token. Never send UI types
into Poly Pizza / Three D Scans (those ``category`` params are site taxonomies).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple

from .ui.icons import normalize_asset_type

UI_TYPE_KEYS = ("mesh", "material_set", "texture", "hdri")


@dataclass(frozen=True)
class SourceTypePolicy:
    """What one website actually catalogs, and how to query it."""

    # UI types that can return hits (ALL is always allowed)
    has: FrozenSet[str]
    # UI type → search(category=) token. Omit key to pass None.
    category: Dict[str, str] = field(default_factory=dict)
    # UI type → extra canonical asset_types that count as hits on THIS source
    extra_match: Dict[str, FrozenSet[str]] = field(default_factory=dict)
    # search(category=) is a *site* taxonomy (animals, WP ids) — never UI types
    category_is_not_asset_type: bool = False
    note: str = ""


# --- Per-platform memory (keep in docs/online-sources.md) -------------------
#
# polyhaven   type 0=hdri 1=texture 2=mesh. No "material" — PBR packs are textures.
# ambientcg   dataType Material | 3DModel | HDRI. No Texture type; Texture→Material.
# gpuopen     MaterialX materials only (Static/Parametric are not UI types).
# lazytextures  separate API: materials / textures / models / hdris / other.
# threedscans scans only; category= WordPress category id, not mesh/material.
# pbrpx       categorys= Textures | 3D_Models | HDRI. No Materials folder.
# polypizza   meshes only; category= Nature/Animals/… — not asset type.
# sketchfab   type=models (downloadable). No materials/HDRI search.
# fab         listingType material | 3d-model | plant | decal. No HDRI.
#             Megascans often ignores listing_type — connector overfetches.

SOURCE_TYPE_POLICY: Dict[str, SourceTypePolicy] = {
    "polyhaven": SourceTypePolicy(
        has=frozenset({"mesh", "material_set", "texture", "hdri"}),
        category={
            "mesh": "models",
            "material_set": "textures",
            "texture": "textures",
            "hdri": "hdris",
        },
        extra_match={"material_set": frozenset({"texture"})},
        note="PBR packs are type=texture; Material filter shows those.",
    ),
    "ambientcg": SourceTypePolicy(
        has=frozenset({"mesh", "material_set", "texture", "hdri"}),
        category={
            "mesh": "3DModel",
            "material_set": "Material",
            "texture": "Material",
            "hdri": "HDRI",
        },
        extra_match={
            "material_set": frozenset({"texture"}),
            "texture": frozenset({"material_set"}),
        },
        note="API type= Material/HDRI; 3DModel is client-filtered.",
    ),
    "gpuopen": SourceTypePolicy(
        has=frozenset({"material_set"}),
        category={},
        extra_match={},
        note="MaterialX library only. Mesh/Texture/HDRI are empty.",
    ),
    "lazytextures": SourceTypePolicy(
        has=frozenset({"mesh", "material_set", "texture", "hdri"}),
        category={
            "mesh": "models",
            "material_set": "materials",
            "texture": "textures",
            "hdri": "hdris",
        },
        extra_match={},
        note="materials vs textures are different API trees — do not merge.",
    ),
    "threedscans": SourceTypePolicy(
        has=frozenset({"mesh"}),
        category={},
        extra_match={},
        category_is_not_asset_type=True,
        note="Photogrammetry scans. category= WP id, never UI Type.",
    ),
    "pbrpx": SourceTypePolicy(
        has=frozenset({"mesh", "material_set", "texture", "hdri"}),
        category={
            "mesh": "3D_Models",
            "material_set": "Textures",
            "texture": "Textures",
            "hdri": "HDRI",
        },
        extra_match={"material_set": frozenset({"texture"})},
        note="Catalog folders Textures/3D_Models/HDRI — no Materials.",
    ),
    "polypizza": SourceTypePolicy(
        has=frozenset({"mesh"}),
        category={},
        extra_match={},
        category_is_not_asset_type=True,
        note="GLB models. category= Nature/Animals/… not mesh/material.",
    ),
    "sketchfab": SourceTypePolicy(
        has=frozenset({"mesh"}),
        category={"mesh": "models"},
        extra_match={},
        note="Downloadable models only (type=models).",
    ),
    "fab": SourceTypePolicy(
        has=frozenset({"mesh", "material_set", "texture"}),
        category={
            "mesh": "3d-model",
            "material_set": "material",
            "texture": "material",
        },
        extra_match={"texture": frozenset({"material_set"})},
        note="Megascans listingType; Texture→material maps. No HDRI.",
    ),
}


def _policy(source_id: str) -> Optional[SourceTypePolicy]:
    return SOURCE_TYPE_POLICY.get(str(source_id or "").strip().lower())


def _filter_key(type_filter: str) -> str:
    raw = str(type_filter or "").strip()
    if not raw or raw.upper() == "ALL":
        return ""
    return normalize_asset_type(raw)


def should_skip_search(source_id: str, type_filter: str) -> bool:
    """True when this site has no catalog for the UI Type — skip HTTP."""
    key = _filter_key(type_filter)
    if not key:
        return False
    policy = _policy(source_id)
    if policy is None:
        return False
    return key not in policy.has


def unsupported_type_empty_hint(source_id: str, type_filter: str) -> str:
    """Second empty-state line after Search with a Type filter."""
    if should_skip_search(source_id, type_filter):
        key = _filter_key(type_filter)
        noun = {
            "mesh": "meshes",
            "material_set": "materials",
            "texture": "textures",
            "hdri": "HDRIs",
        }.get(key, "that type")
        return f"This site has no {noun}"
    return "Try Type = All"


def search_category_for_filter(source_id: str, type_filter: str) -> Optional[str]:
    """Connector ``search(category=…)`` token, or None.

    Never returns a UI type for taxonomy-category sources (Poly Pizza, 3D Scans).
    """
    if should_skip_search(source_id, type_filter):
        return None
    key = _filter_key(type_filter)
    if not key:
        return None
    policy = _policy(source_id)
    if policy is None or policy.category_is_not_asset_type:
        return None
    token = policy.category.get(key)
    return str(token) if token else None


def asset_type_matches_filter(
    asset_type: str,
    type_filter: str,
    source_id: str = "",
) -> bool:
    """True if this hit belongs under the N-panel Type filter for *source_id*."""
    key = _filter_key(type_filter)
    if not key:
        return True
    asset_key = normalize_asset_type(asset_type)
    if asset_key == key:
        return True
    policy = _policy(source_id)
    if policy is None:
        return False
    extra = policy.extra_match.get(key) or frozenset()
    return asset_key in extra


def filter_search_items(items: Iterable, type_filter: str) -> List:
    """Keep search hits using each item's ``source_id`` policy."""
    return [
        asset
        for asset in items
        if asset_type_matches_filter(
            getattr(asset, "asset_type", "") or "",
            type_filter,
            getattr(asset, "source_id", "") or "",
        )
    ]


def apply_type_filter(
    items: Sequence,
    type_filter: str,
    *,
    has_more: bool,
    api_total: int,
) -> Tuple[List, bool, int]:
    """Client-filter a page. Drop unfiltered API totals when rows were removed."""
    raw = list(items or [])
    filtered = filter_search_items(raw, type_filter)
    dropped = len(filtered) != len(raw)
    if dropped:
        total = len(filtered)
    else:
        total = int(api_total or 0)
    return filtered, bool(has_more), total


def type_capability_line(source_id: str) -> str:
    """Narrow N-panel line: which UI types this site actually catalogs."""
    policy = _policy(source_id)
    short = {
        "mesh": "Mesh",
        "material_set": "Material",
        "texture": "Tex",
        "hdri": "HDRI",
    }
    if policy is None:
        return "Types: unknown"
    yes = [short[key] for key in UI_TYPE_KEYS if key in policy.has]
    if len(yes) == 4:
        return "Mesh · Mat · Tex · HDRI"
    if len(yes) == 1:
        return f"{yes[0]} only"
    return " · ".join(yes)
