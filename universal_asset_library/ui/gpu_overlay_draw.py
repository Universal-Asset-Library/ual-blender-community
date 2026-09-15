# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""Shared GPU POST_PIXEL helpers (N-panel hover tooltip + Pro asset bar).

Lives outside ``ui/asset_bar`` so Community packages (which omit the bar)
still draw GPU hover previews.
"""

from __future__ import annotations

import os
from collections import OrderedDict
from typing import Dict, Optional, Sequence, Tuple

import bpy
import gpu
from gpu_extras.batch import batch_for_shader

try:
    import blf
except ImportError:  # pragma: no cover
    blf = None

_shader_2d = None
# Soft cap — asset bar + hover tooltip share this module (session VRAM guard)
GPU_IMAGE_CACHE_MAX = 96
_textures: "OrderedDict[str, object]" = OrderedDict()
_images: "OrderedDict[str, object]" = OrderedDict()
_overlay_shader = None
_overlay_mode = ""  # preset | builtin | fallback | image


def overlay_image_draw_mode(
    *,
    preset_has_linear_flag: bool,
    has_linear_builtin: bool,
) -> str:
    """How to draw ``gpu.texture.from_image`` on a POST_PIXEL overlay.

    ``from_image`` samples **scene linear**. N-panel ``template_icon`` is UI
    color-managed. Without linear→sRGB, GPU thumbs (bar + big preview) look dark.
    """
    if preset_has_linear_flag:
        return "preset"
    if has_linear_builtin:
        return "builtin"
    return "fallback"


def _shader():
    global _shader_2d
    if _shader_2d is None:
        _shader_2d = gpu.shader.from_builtin("UNIFORM_COLOR")
    return _shader_2d


def draw_rect(x: float, y: float, w: float, h: float, color: Tuple[float, float, float, float]) -> None:
    shader = _shader()
    batch = batch_for_shader(
        shader,
        "TRI_FAN",
        {
            "pos": (
                (x, y),
                (x + w, y),
                (x + w, y + h),
                (x, y + h),
            )
        },
    )
    gpu.state.blend_set("ALPHA")
    shader.bind()
    shader.uniform_float("color", color)
    batch.draw(shader)
    gpu.state.blend_set("NONE")


def draw_rect_outline(
    x: float,
    y: float,
    w: float,
    h: float,
    color: Tuple[float, float, float, float],
    *,
    thickness: float = 2.0,
) -> None:
    """Draw a 1-cell rectangular border (four edge quads)."""
    t = max(1.0, float(thickness))
    ww = max(t, float(w))
    hh = max(t, float(h))
    draw_rect(x, y + hh - t, ww, t, color)  # top
    draw_rect(x, y, ww, t, color)  # bottom
    draw_rect(x, y, t, hh, color)  # left
    draw_rect(x + ww - t, y, t, hh, color)  # right


def draw_glow_border(
    x: float,
    y: float,
    w: float,
    h: float,
    *,
    color: Tuple[float, float, float, float] = (0.890, 0.435, 0.118, 1.0),
    thickness: float = 2.5,
    glow_layers: int = 4,
) -> None:
    """UAL accent ring + soft outer glow (selection / focus)."""
    r, g, b, a = color
    layers = max(0, int(glow_layers))
    for i in range(layers, 0, -1):
        pad = float(i) * 1.6
        alpha = a * (0.10 + 0.10 * (layers - i + 1) / max(1, layers))
        draw_rect_outline(
            float(x) - pad,
            float(y) - pad,
            float(w) + 2.0 * pad,
            float(h) + 2.0 * pad,
            (r, g, b, alpha),
            thickness=1.5,
        )
    draw_rect_outline(float(x), float(y), float(w), float(h), (r, g, b, a), thickness=thickness)


# Brand accent (same #e36f1e as N-panel primary).
ACCENT = (0.890, 0.435, 0.118, 1.0)
LIBRARY_GREEN = (0.25, 0.75, 0.35, 1.0)
CHIP_BG = (0.18, 0.18, 0.18, 1.0)
CHIP_BG_HOVER = (0.26, 0.26, 0.26, 1.0)
CHIP_BG_ACTIVE = (0.28, 0.16, 0.08, 1.0)
CHIP_ALERT = (0.55, 0.18, 0.12, 1.0)
MENU_BG = (0.14, 0.14, 0.14, 0.97)
MENU_HOVER = (0.24, 0.24, 0.24, 1.0)


def draw_text(text: str, x: float, y: float, *, size: int = 12, color=(1, 1, 1, 1)) -> None:
    if blf is None or not text:
        return
    font_id = 0
    if bpy.app.version < (4, 0, 0):
        blf.size(font_id, size, 72)
    else:
        blf.size(font_id, size)
    blf.color(font_id, *color)
    blf.position(font_id, x, y, 0)
    blf.draw(font_id, text)


def draw_chip(
    x: float,
    y: float,
    w: float,
    h: float,
    text: str,
    *,
    active: bool = False,
    hover: bool = False,
    alert: bool = False,
    dropdown: bool = False,
    muted: bool = False,
) -> None:
    """Chrome chip: inactive / hover / active (orange) / alert (unsupported Type)."""
    if w < 4.0 or h < 4.0:
        return
    if alert and not active:
        bg = CHIP_ALERT
    elif active:
        bg = CHIP_BG_ACTIVE
    elif hover:
        bg = CHIP_BG_HOVER
    elif muted:
        bg = (0.14, 0.14, 0.14, 0.85)
    else:
        bg = CHIP_BG
    draw_rect(x, y, w, h, bg)
    if active:
        draw_rect_outline(x, y, w, h, ACCENT, thickness=1.5)
    else:
        edge = (0.28, 0.28, 0.28, 0.7) if muted else (0.32, 0.32, 0.32, 0.9)
        draw_rect_outline(x, y, w, h, edge, thickness=1.0)
    font = max(8, min(14, int(round(h * 0.48))))
    caret_w = max(10.0, h * 0.45) if dropdown else 0.0
    shown = str(text or "")
    text_w = max(8.0, w - caret_w - 8.0)
    max_chars = max(2, int(text_w / max(5.5, font * 0.58)))
    if len(shown) > max_chars:
        shown = shown[: max(1, max_chars - 1)] + "…"
    color = ACCENT if active else ((0.72, 0.72, 0.72, 1) if muted else (0.95, 0.95, 0.95, 1))
    draw_text(
        shown,
        x + 6,
        y + max(2.0, (h - font) * 0.40),
        size=font,
        color=color,
    )
    if dropdown:
        draw_text(
            "▾",
            x + w - caret_w,
            y + max(1.0, (h - font) * 0.32),
            size=font,
            color=color,
        )


def draw_scope_bar(
    rects: Sequence[Tuple[str, float, float, float, float]],
    labels: Sequence[str],
    *,
    active_id: str,
    hover_id: str,
) -> None:
    """One segmented control: Library | Fav | Online (0 gap, shared outline)."""
    if not rects:
        return
    x0 = float(rects[0][1])
    y0 = float(rects[0][2])
    h = float(rects[0][4])
    x1 = float(rects[-1][1] + rects[-1][3])
    w = x1 - x0
    if w < 4.0 or h < 4.0:
        return
    draw_rect(x0, y0, w, h, CHIP_BG)
    draw_rect_outline(x0, y0, w, h, (0.32, 0.32, 0.32, 0.9), thickness=1.0)
    for i, (sid, x, y, sw, sh) in enumerate(rects):
        hover = hover_id == f"scope:{sid}"
        active = sid == active_id
        if active:
            draw_rect(x, y, sw, sh, CHIP_BG_ACTIVE)
            draw_rect_outline(x, y, sw, sh, ACCENT, thickness=1.5)
        elif hover:
            draw_rect(x, y, sw, sh, CHIP_BG_HOVER)
        if i > 0 and not active:
            prev_active = rects[i - 1][0] == active_id
            if not prev_active:
                draw_rect(x, y + 3.0, 1.0, max(2.0, sh - 6.0), (0.38, 0.38, 0.38, 0.65))
        lab = labels[i] if i < len(labels) else sid
        font = max(8, min(14, int(round(sh * 0.48))))
        shown = str(lab or "")
        max_chars = max(2, int((sw - 8) / max(5.5, font * 0.58)))
        if len(shown) > max_chars:
            shown = shown[: max(1, max_chars - 1)] + "…"
        draw_text(
            shown,
            x + 6,
            y + max(2.0, (sh - font) * 0.40),
            size=font,
            color=ACCENT if active else (0.95, 0.95, 0.95, 1),
        )


def draw_menu_panel(rows: Sequence[Tuple[int, float, float, float, float]]) -> None:
    """One popup behind Type/Source rows (no striped per-row boxes)."""
    if not rows:
        return
    x = min(float(r[1]) for r in rows)
    y = min(float(r[2]) for r in rows)
    right = max(float(r[1] + r[3]) for r in rows)
    top = max(float(r[2] + r[4]) for r in rows)
    w = right - x
    h = top - y
    if w < 4.0 or h < 4.0:
        return
    draw_rect(x, y, w, h, MENU_BG)
    draw_rect_outline(x, y, w, h, (0.38, 0.38, 0.38, 0.95), thickness=1.0)


def draw_menu_row(
    x: float,
    y: float,
    w: float,
    h: float,
    text: str,
    *,
    active: bool = False,
    hover: bool = False,
) -> None:
    if w < 4.0 or h < 4.0:
        return
    if hover:
        draw_rect(x, y, w, h, MENU_HOVER)
    elif active:
        draw_rect(x, y, w, h, CHIP_BG_ACTIVE)
    font = max(8, min(13, int(round(h * 0.50))))
    shown = str(text or "")
    text_x = x + 20.0
    max_chars = max(2, int((w - 28) / max(5.5, font * 0.58)))
    if len(shown) > max_chars:
        shown = shown[: max(1, max_chars - 1)] + "…"
    if active:
        draw_text(
            "✓",
            x + 6,
            y + max(2.0, (h - font) * 0.38),
            size=font,
            color=ACCENT,
        )
    draw_text(
        shown,
        text_x,
        y + max(2.0, (h - font) * 0.38),
        size=font,
        color=ACCENT if active else (0.95, 0.95, 0.95, 1),
    )


def _touch_gpu_cache(path: str) -> None:
    """LRU touch; drop oldest GPU textures when over ``GPU_IMAGE_CACHE_MAX``."""
    if path in _images:
        _images.move_to_end(path)
    if path in _textures:
        _textures.move_to_end(path)
    while len(_images) > GPU_IMAGE_CACHE_MAX:
        old, _img = _images.popitem(last=False)
        _textures.pop(old, None)
    while len(_textures) > GPU_IMAGE_CACHE_MAX:
        _textures.popitem(last=False)


def _load_image(path: str) -> Optional[bpy.types.Image]:
    path = os.path.normpath(path or "")
    if not path or not os.path.isfile(path):
        return None
    if path in _images:
        img = _images[path]
        try:
            if img.name:  # still valid
                _touch_gpu_cache(path)
                return img
        except ReferenceError:
            _images.pop(path, None)
            _textures.pop(path, None)
    try:
        # Do not change colorspace — this datablock may already be a material map.
        img = bpy.data.images.load(path, check_existing=True)
        _images[path] = img
        _touch_gpu_cache(path)
        return img
    except Exception:
        return None


def _fallback_linear_to_srgb_shader():
    """4.2 path when IMAGE_SCENE_LINEAR_TO_REC709_SRGB / preset flag are missing.

    ``gpu.types.GPUShader(vert, frag)`` was removed; use GPUShaderCreateInfo.
    """
    vert_body = """
void main()
{
  uvInterp = texCoord;
  gl_Position = ModelViewProjectionMatrix * vec4(pos, 0.0, 1.0);
}
"""
    frag_body = """
void main()
{
  vec4 tex = texture(image, uvInterp);
  vec3 c = max(tex.rgb, vec3(0.0));
  vec3 cutoff = step(vec3(0.0031308), c);
  vec3 lower = c * 12.92;
  vec3 higher = 1.055 * pow(c, vec3(1.0 / 2.4)) - 0.055;
  fragColor = vec4(mix(lower, higher, cutoff), tex.a);
}
"""
    try:
        iface = gpu.types.GPUStageInterfaceInfo("UALOverlayUV")
        iface.smooth("VEC2", "uvInterp")
        info = gpu.types.GPUShaderCreateInfo()
        info.vertex_in(0, "VEC2", "pos")
        info.vertex_in(1, "VEC2", "texCoord")
        info.sampler(0, "FLOAT_2D", "image")
        info.push_constant("MAT4", "ModelViewProjectionMatrix")
        info.vertex_out(iface)
        info.fragment_out(0, "VEC4", "fragColor")
        info.vertex_source(vert_body)
        info.fragment_source(frag_body)
        shader = gpu.shader.create_from_info(info)
        del info
        del iface
        return shader
    except Exception:
        pass
    try:
        vert = (
            "uniform mat4 ModelViewProjectionMatrix;\n"
            "in vec2 pos;\n"
            "in vec2 texCoord;\n"
            "out vec2 uvInterp;\n"
            "void main()\n"
            "{\n"
            "  uvInterp = texCoord;\n"
            "  gl_Position = ModelViewProjectionMatrix * vec4(pos.xy, 0.0, 1.0);\n"
            "}\n"
        )
        frag = (
            "in vec2 uvInterp;\n"
            "out vec4 fragColor;\n"
            "uniform sampler2D image;\n"
            "void main()\n"
            "{\n"
            "  vec4 tex = texture(image, uvInterp);\n"
            "  vec3 c = max(tex.rgb, vec3(0.0));\n"
            "  vec3 cutoff = step(vec3(0.0031308), c);\n"
            "  vec3 lower = c * 12.92;\n"
            "  vec3 higher = 1.055 * pow(c, vec3(1.0 / 2.4)) - 0.055;\n"
            "  fragColor = vec4(mix(lower, higher, cutoff), tex.a);\n"
            "}\n"
        )
        return gpu.types.GPUShader(vert, frag)
    except Exception:
        return None


def _resolve_overlay_mode() -> str:
    global _overlay_mode, _overlay_shader
    if _overlay_mode:
        return _overlay_mode
    preset_flag = False
    try:
        import inspect
        from gpu_extras.presets import draw_texture_2d

        preset_flag = (
            "is_scene_linear_with_rec709_srgb_target"
            in inspect.signature(draw_texture_2d).parameters
        )
    except Exception:
        preset_flag = False
    has_builtin = False
    try:
        _overlay_shader = gpu.shader.from_builtin("IMAGE_SCENE_LINEAR_TO_REC709_SRGB")
        has_builtin = True
    except Exception:
        has_builtin = False
        _overlay_shader = None
    _overlay_mode = overlay_image_draw_mode(
        preset_has_linear_flag=preset_flag,
        has_linear_builtin=has_builtin,
    )
    if _overlay_mode == "fallback":
        _overlay_shader = _fallback_linear_to_srgb_shader()
        if _overlay_shader is None:
            try:
                _overlay_shader = gpu.shader.from_builtin("IMAGE")
            except Exception:
                _overlay_shader = None
            _overlay_mode = "image"
    elif _overlay_mode == "builtin" and _overlay_shader is None:
        try:
            _overlay_shader = gpu.shader.from_builtin("IMAGE_SCENE_LINEAR_TO_REC709_SRGB")
        except Exception:
            _overlay_shader = _fallback_linear_to_srgb_shader()
            _overlay_mode = "fallback" if _overlay_shader is not None else "image"
    return _overlay_mode


def _overlay_quad_pos(x: float, y: float, w: float, h: float, *, vec3: bool):
    if vec3:
        return (
            (x, y, 0.0),
            (x + w, y, 0.0),
            (x + w, y + h, 0.0),
            (x, y + h, 0.0),
        )
    return (
        (x, y),
        (x + w, y),
        (x + w, y + h),
        (x, y + h),
    )


def _draw_textured_quad(shader, texture, x: float, y: float, w: float, h: float) -> bool:
    """Draw a textured overlay quad. Builtin IMAGE shaders take vec3 pos."""
    uvs = ((0, 0), (1, 0), (1, 1), (0, 1))
    for vec3 in (True, False):
        try:
            batch = batch_for_shader(
                shader,
                "TRI_FAN",
                {
                    "pos": _overlay_quad_pos(x, y, w, h, vec3=vec3),
                    "texCoord": uvs,
                },
            )
            shader.bind()
            shader.uniform_sampler("image", texture)
            batch.draw(shader)
            return True
        except Exception:
            continue
    return False


def _draw_overlay_texture(texture, x: float, y: float, w: float, h: float) -> bool:
    """Draw a scene-linear image texture onto the POST_PIXEL overlay."""
    mode = _resolve_overlay_mode()
    gpu.state.blend_set("ALPHA")
    ok = False
    try:
        if mode == "preset":
            from gpu_extras.presets import draw_texture_2d

            draw_texture_2d(
                texture,
                (x, y),
                w,
                h,
                is_scene_linear_with_rec709_srgb_target=True,
            )
            ok = True
        else:
            shader = _overlay_shader
            if shader is None:
                try:
                    shader = gpu.shader.from_builtin("IMAGE")
                except Exception:
                    shader = None
            if shader is not None:
                ok = _draw_textured_quad(shader, texture, x, y, w, h)
    except Exception:
        ok = False
    finally:
        gpu.state.blend_set("NONE")
    return ok


def draw_image(
    path: str,
    x: float,
    y: float,
    w: float,
    h: float,
    *,
    fit: str = "contain",
) -> bool:
    """Draw a disk image as a textured quad. Returns False if unavailable.

    ``fit``: ``contain`` (default, no stretch), ``cover``, or ``stretch``.
    """
    img = _load_image(path)
    if img is None:
        return False
    try:
        from .. import thumbnails

        try:
            size = getattr(img, "size", None)
            iw = float(size[0]) if size and len(size) >= 2 else float(w)
            ih = float(size[1]) if size and len(size) >= 2 else float(h)
        except Exception:
            iw, ih = float(w), float(h)
        dx, dy, dw, dh = thumbnails.aspect_fit_rect(
            x, y, w, h, iw, ih, fit=fit
        )
        texture = _textures.get(path)
        if texture is None:
            texture = gpu.texture.from_image(img)
            _textures[path] = texture
        else:
            _textures.move_to_end(path)
        _touch_gpu_cache(path)
        return bool(_draw_overlay_texture(texture, dx, dy, dw, dh))
    except Exception:
        return False


def clear_texture_cache() -> None:
    _textures.clear()
    # Leave bpy images; Blender owns them if check_existing loaded
    _images.clear()
