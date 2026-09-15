# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""Principled BSDF PBR wiring plan (pure data — no bpy).

Matches Blender 4.x Principled BSDF + Color Management:
- Base Color / Emission Color → sRGB
- Metallic, Roughness, Normal, Alpha, AO, Height → Non-Color (data)
- Normal → Normal Map node → Principled Normal
- Displacement → Displacement node → Material Output Displacement
- AO multiplies into Base Color (standard PBR; avoids double-darkening via AO socket)
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

PRINCIPLED_CHANNEL_PLAN: Tuple[Dict[str, Any], ...] = (
    {"channel": "albedo", "socket": "Base Color", "colorspace": "sRGB", "via": "color"},
    {"channel": "roughness", "socket": "Roughness", "colorspace": "Non-Color", "via": "scalar"},
    {"channel": "metalness", "socket": "Metallic", "colorspace": "Non-Color", "via": "scalar"},
    {"channel": "normal", "socket": "Normal", "colorspace": "Non-Color", "via": "normal_map"},
    {"channel": "ao", "socket": "Base Color", "colorspace": "Non-Color", "via": "ao_multiply"},
    {"channel": "opacity", "socket": "Alpha", "colorspace": "Non-Color", "via": "alpha"},
    {"channel": "emissive", "socket": "Emission Color", "colorspace": "sRGB", "via": "color"},
    {"channel": "transmission", "socket": "Transmission Weight", "colorspace": "Non-Color", "via": "scalar"},
    # Megascans foliage / cloth extras → Principled 4.x sockets
    {"channel": "translucency", "socket": "Transmission Weight", "colorspace": "Non-Color", "via": "scalar"},
    {"channel": "fuzz", "socket": "Sheen Weight", "colorspace": "Non-Color", "via": "scalar"},
    {"channel": "specular", "socket": "Specular IOR Level", "colorspace": "Non-Color", "via": "scalar"},
    {"channel": "displacement", "socket": "Displacement", "colorspace": "Non-Color", "via": "displacement"},
)


def principled_wire_plan() -> List[Dict[str, Any]]:
    return [dict(row) for row in PRINCIPLED_CHANNEL_PLAN]


def colorspace_for_channel(channel: str) -> str:
    for row in PRINCIPLED_CHANNEL_PLAN:
        if row["channel"] == channel:
            return str(row["colorspace"])
    return "Non-Color"


def socket_for_channel(channel: str) -> str:
    for row in PRINCIPLED_CHANNEL_PLAN:
        if row["channel"] == channel:
            return str(row["socket"])
    return ""
