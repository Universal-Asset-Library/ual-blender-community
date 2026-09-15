# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""Icon helpers — native Blender icons for asset types + view-mode toggles.

Type icons follow Blender Asset Browser / Outliner conventions:

* Mesh / 3D model → ``MESH_DATA``
* Material / shader → ``MATERIAL_DATA`` (fallback ``MATERIAL``)
* Texture maps → ``TEXTURE`` (fallback ``IMAGE_DATA``)
* HDRI / environment → ``WORLD_DATA`` (fallback ``WORLD``)
* Volume / VDB → ``VOLUME_DATA``

View toggle matches Houdini ``ViewModeToggle``: one icon shows the *current*
mode (grid or list); click flips to the other.

Blender 5.2 note: SHORTDISPLAY / LONGDISPLAY can render as garbage ("B:B") in
some builds — prefer IMGDISPLAY / ALIGN_JUSTIFY (probed).
"""

from __future__ import annotations

from typing import FrozenSet, Optional, Tuple

# Icons that must work on Blender 4.2–5.2 N-panel (avoid SHORTDISPLAY/LONGDISPLAY)
_SAFE_GRID = ("IMGDISPLAY", "IMAGE_DATA", "FILE_IMAGE", "TEXTURE")
_SAFE_LIST = ("ALIGN_JUSTIFY", "LINENUMBERS_ON", "SORTSIZE", "LONGDISPLAY", "TEXT")

# Candidate chains — first available RNA icon wins (Blender version variance)
_TYPE_ICON_CANDIDATES = {
    "mesh": ("MESH_DATA", "OUTLINER_OB_MESH", "FILE_3D"),
    "material_set": ("MATERIAL_DATA", "MATERIAL", "NODE_MATERIAL"),
    "texture": ("TEXTURE", "IMAGE_DATA", "FILE_IMAGE"),
    "hdri": ("WORLD_DATA", "WORLD", "LIGHT_SUN"),
    "vdb": ("VOLUME_DATA", "OUTLINER_OB_VOLUME", "FILE_VOLUME"),
}
_DEFAULT_TYPE_ICONS = ("FILE_3D", "MESH_DATA", "QUESTION")

# Friendly labels for list footers / hover (not raw keys like material_set)
_TYPE_LABELS = {
    "mesh": "Mesh",
    "material_set": "Material",
    "texture": "Texture",
    "hdri": "HDRI",
    "vdb": "Volume",
}

_VALID_CACHE: Optional[FrozenSet[str]] = None


def _blender_icon_names() -> FrozenSet[str]:
    global _VALID_CACHE
    if _VALID_CACHE is not None:
        return _VALID_CACHE
    names: set = set()
    try:
        import bpy

        enum = getattr(bpy.types, "UILayout", None)
        # bpy.types.UILayout.bl_rna not always listing icons; use enum items helper
        items = getattr(bpy.types.UILayout, "bl_rna", None)
        del enum, items
        # Fall through to known-good list if RNA probe unavailable
    except Exception:
        pass
    # Curated safe set used for probing
    names.update(_SAFE_GRID)
    names.update(_SAFE_LIST)
    for chain in _TYPE_ICON_CANDIDATES.values():
        names.update(chain)
    names.update(_DEFAULT_TYPE_ICONS)
    names.update(
        {
            "FILTER",
            "WORLD",
            "WORLD_DATA",
            "LOCKED",
            "SOLO_ON",
            "SOLO_OFF",
            "TRASH",
            "VIEWZOOM",
            "MATERIAL",
            "MATERIAL_DATA",
            "MESH_DATA",
            "TEXTURE",
            "IMAGE_DATA",
            "VOLUME_DATA",
            "FILE_3D",
        }
    )
    _VALID_CACHE = frozenset(names)
    return _VALID_CACHE


def _first_available(candidates: tuple) -> str:
    """Return first icon name; probe bpy.types when available."""
    try:
        import bpy

        enum_items = bpy.types.UILayout.bl_rna.functions["prop"].parameters["icon"].enum_items
        valid = {item.identifier for item in enum_items}
        for name in candidates:
            if name in valid:
                return name
    except Exception:
        pass
    return candidates[0]


def normalize_asset_type(asset_type: str) -> str:
    """Map synonyms → canonical UAL type keys used by ``icon_for_type``."""
    token = str(asset_type or "").strip().lower().replace("-", "_").replace(" ", "_")
    if token in ("", "all"):
        return ""
    if token in (
        "mesh",
        "model",
        "models",
        "geometry",
        "geo",
        "3d",
        "object",
        "3dmodel",
        "3d_model",
        "3dmodels",
        "plant",
        "plants",
        "brush",
    ):
        return "mesh"
    if token in (
        "material_set",
        "material",
        "materials",
        "shader",
        "shaders",
        "shading",
        "mat",
        "decal",
        "decals",
        "atlas",
        "atlases",
    ):
        return "material_set"
    if token in ("texture", "textures", "maps", "pbr", "image"):
        return "texture"
    if token in ("hdri", "hdr", "environment", "env", "world", "sky"):
        return "hdri"
    if token in ("vdb", "volume", "volumes", "openvdb"):
        return "vdb"
    return token


# Types ``import_routing`` can actually import. Connectors often send aliases
# (ambientCG ``Material``, GPUOpen/Fab ``material``) that must map here first.
IMPORT_ASSET_TYPES = frozenset({"mesh", "material_set", "texture", "hdri"})
MATERIAL_DROP_TYPES = frozenset({"material_set", "texture"})


def canonical_import_type(asset_type: str) -> str:
    """Map a source/library type hint onto an import-router key, or ``\"\"``."""
    key = normalize_asset_type(asset_type)
    return key if key in IMPORT_ASSET_TYPES else ""


def icon_for_type(asset_type: str) -> str:
    """Native Blender icon identifier for an asset type (Asset Browser aligned)."""
    key = normalize_asset_type(asset_type)
    candidates = _TYPE_ICON_CANDIDATES.get(key) or _DEFAULT_TYPE_ICONS
    return _first_available(candidates)


def label_for_type(asset_type: str) -> str:
    """Short human label for list/hover (Mesh, Material, HDRI…)."""
    key = normalize_asset_type(asset_type)
    if not key:
        return "Asset"
    return _TYPE_LABELS.get(key, key.replace("_", " ").title())


def filter_type_enum_items() -> Tuple[tuple, ...]:
    """Type filter enum with native icons (identifier, name, desc, icon, number)."""
    return (
        ("ALL", "All", "All asset types", "FILTER", 0),
        ("mesh", "Mesh", "3D meshes / models", icon_for_type("mesh"), 1),
        (
            "material_set",
            "Material",
            "PBR materials / shaders",
            icon_for_type("material_set"),
            2,
        ),
        ("texture", "Texture", "Texture / map sets", icon_for_type("texture"), 3),
        ("hdri", "HDRI", "Environment / world HDRIs", icon_for_type("hdri"), 4),
    )


def icon_value_or_type(preview_icon_id: int, asset_type: str) -> tuple:
    """Return (icon_value, icon_name) for operator/label drawing."""
    value = int(preview_icon_id or 0)
    if value:
        return value, "NONE"
    return 0, icon_for_type(asset_type)


def view_mode_icon(mode: str) -> str:
    """Blender icon for the *current* view mode (Houdini ViewModeToggle parity).

    Grid → IMGDISPLAY; list → ALIGN_JUSTIFY (never SHORTDISPLAY/LONGDISPLAY —
    those render as "B:B" on some Blender 5.2 builds).
    """
    if (mode or "grid") == "list":
        return _first_available(_SAFE_LIST)
    return _first_available(_SAFE_GRID)


def view_mode_tooltip(mode: str) -> str:
    if (mode or "grid") == "list":
        return "List with large previews — click for preview grid"
    return "Preview grid view — click for list with large previews"


# Pure export for tests (no bpy needed)
SAFE_VIEW_MODE_ICONS = frozenset(_SAFE_GRID + _SAFE_LIST)
TYPE_ICON_DEFAULTS = {
    key: candidates[0] for key, candidates in _TYPE_ICON_CANDIDATES.items()
}

# N-panel / pie / prefs action icons — native identifiers that match the verb.
# Refresh list = reload from index; rebuild = rescan folders (must stay distinct).
ACTION_ICONS = {
    "search": "VIEWZOOM",
    "import": "IMPORT",
    "drop": "MOUSE_LMB_DRAG",
    "drop_from_disk": "FILEBROWSER",
    "asset_bar": "SEQ_PREVIEW",
    "refresh_list": "FILE_REFRESH",
    "rebuild_index": "MOD_BUILD",
    "favorite_on": "SOLO_ON",
    "favorite_off": "SOLO_OFF",
    "hide": "HIDE_ON",
    "delete": "TRASH",
    "download": "IMPORT",
    "cancel": "CANCEL",
    "refresh_thumbs": "FILE_REFRESH",
    "load_more": "ADD",
    "auth": "LOCKED",
    "preferences": "PREFERENCES",
    "type_filter": "FILTER",
    "source": "WORLD",
    "open_url": "URL",
    "library": "FILE_FOLDER",
    "cache": "TEMP",
    "accounts": "USER",
    "indexing": "OUTLINER",
    "material_blend": "NODE_MATERIAL",
}
