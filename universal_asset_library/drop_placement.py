# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""Pure drop-placement math (no bpy) — unit-testable.

These helpers never write Blender's 3D cursor; callers must not mutate
``scene.cursor`` from drop preview either.
"""

from __future__ import annotations

from typing import Optional, Sequence, Tuple

Vec3 = Tuple[float, float, float]

# Reject edge-on hits: |dot(view, normal)| near 0 → unstable snap
_GRAZE_DOT_MAX = 0.15


def ground_plane_intersection(
    ray_origin: Sequence[float],
    ray_direction: Sequence[float],
    *,
    z: float = 0.0,
) -> Optional[Vec3]:
    """Intersect a view ray with the horizontal plane at ``z`` (world).

    Returns ``(x, y, z)`` when the hit is in front of the ray origin, else None.
    """
    ox, oy, oz = float(ray_origin[0]), float(ray_origin[1]), float(ray_origin[2])
    dx, dy, dz = float(ray_direction[0]), float(ray_direction[1]), float(ray_direction[2])
    if abs(dz) < 1e-8:
        return None
    t = (z - oz) / dz
    if t <= 0.0:
        return None
    return (ox + dx * t, oy + dy * t, oz + dz * t)


def is_grazing_hit(
    view_direction: Sequence[float],
    hit_normal: Sequence[float],
    *,
    max_abs_dot: float = _GRAZE_DOT_MAX,
) -> bool:
    """True when the view ray is nearly edge-on to the surface (unstable snap).

    Face-on hits have |dot(view, normal)| near 1; grazing hits are near 0.
    """
    vx, vy, vz = float(view_direction[0]), float(view_direction[1]), float(view_direction[2])
    nx, ny, nz = float(hit_normal[0]), float(hit_normal[1]), float(hit_normal[2])
    vlen = (vx * vx + vy * vy + vz * vz) ** 0.5
    nlen = (nx * nx + ny * ny + nz * nz) ** 0.5
    if vlen < 1e-8 or nlen < 1e-8:
        return True
    dot = abs((vx * nx + vy * ny + vz * nz) / (vlen * nlen))
    return dot < max_abs_dot


# Public API — never includes cursor writes
__all__ = (
    "ground_plane_intersection",
    "is_grazing_hit",
)
