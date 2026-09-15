# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""World HDRI node graph spec (pure — no bpy).

Matches Node Wrangler Ctrl+T on an Environment Texture, using **built-in**
shader nodes only. Node Wrangler is not required (and must not be called).

Chain: Texture Coordinate (Generated) → Mapping → Environment Texture
     → Background → World Output (Surface)
"""

from __future__ import annotations

import os
from typing import Dict, List, Sequence, Tuple

HDRI_EXTS = frozenset({".hdr", ".exr"})

# Built-in shader node types — never ShaderNodeTexImage for world HDRI
HDRI_NODE_TYPES: Dict[str, str] = {
    "tex_coord": "ShaderNodeTexCoord",
    "mapping": "ShaderNodeMapping",
    "env": "ShaderNodeTexEnvironment",
    "bg": "ShaderNodeBackground",
    "output": "ShaderNodeOutputWorld",
}

# Generated (not UV) — UV is for mesh materials; world needs Generated
HDRI_LINKS: Tuple[Tuple[str, str, str, str], ...] = (
    ("tex_coord", "Generated", "mapping", "Vector"),
    ("mapping", "Vector", "env", "Vector"),
    ("env", "Color", "bg", "Color"),
    ("bg", "Background", "output", "Surface"),
)

# Left → right, similar density to Node Wrangler Ctrl+T
HDRI_LOCATIONS: Dict[str, Tuple[float, float]] = {
    "tex_coord": (-920.0, 80.0),
    "mapping": (-700.0, 80.0),
    "env": (-420.0, 80.0),
    "bg": (-80.0, 80.0),
    "output": (180.0, 80.0),
}

# Blender 4.x / 5.x first; older Linear / Non-Color as fallbacks
_COLORSPACE_PREFS = (
    "Linear Rec.709",
    "Linear Rec.2020",
    "Linear",
    "Non-Color",
)


def pick_hdri_colorspace(available: Sequence[str]) -> str:
    """Pick a linear HDR color space from names Blender actually lists."""
    names = [str(n).strip() for n in (available or []) if str(n).strip()]
    if not names:
        return "Linear Rec.709"
    lower = {n.lower(): n for n in names}
    for pref in _COLORSPACE_PREFS:
        hit = lower.get(pref.lower())
        if hit:
            return hit
    for n in names:
        if n.lower().startswith("linear"):
            return n
    return names[0]


def hdri_graph_uses_environment_texture() -> bool:
    return HDRI_NODE_TYPES["env"] == "ShaderNodeTexEnvironment"


def hdri_graph_uses_generated_not_uv() -> bool:
    sockets = {src_sock for _src, src_sock, _dst, _dst_sock in HDRI_LINKS}
    return "Generated" in sockets and "UV" not in sockets


def resolve_hdri_file(path: str) -> str:
    """Return an HDR/EXR file. Directories pick the largest .hdr/.exr (skip tiny thumbs)."""
    raw = os.path.normpath(path or "")
    if os.path.isfile(raw):
        return raw
    if not os.path.isdir(raw):
        raise FileNotFoundError(raw)
    found: List[Tuple[int, int, str]] = []
    for root, _dirs, files in os.walk(raw):
        for name in files:
            ext = os.path.splitext(name)[1].lower()
            if ext not in HDRI_EXTS:
                continue
            fp = os.path.join(root, name)
            try:
                size = int(os.path.getsize(fp))
            except OSError:
                continue
            found.append((size, 0 if ext == ".hdr" else 1, fp))
    if not found:
        raise FileNotFoundError("No .hdr/.exr in {}".format(raw))
    found.sort(key=lambda row: (-row[0], row[1]))
    return found[0][2]
