# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""Texture-set detection from filename conventions (Blender-portable, standalone)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple, Union

TextureSet = Dict[str, str]

TEXTURE_EXTENSIONS = frozenset(
    {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".tga", ".exr", ".hdr", ".webp", ".bmp"}
)

CORE_CHANNELS = (
    "albedo", "normal", "roughness", "metalness", "ao",
    "displacement", "opacity", "emissive", "specular",
)

CHANNELS = (
    "albedo", "normal", "roughness", "metalness", "ao", "displacement",
    "opacity", "emissive", "specular", "translucency", "transmission",
    "fuzz", "cavity", "curvature",
)

RAW_CHANNELS = frozenset(
    {"normal", "roughness", "metalness", "ao", "displacement", "opacity",
     "cavity", "curvature", "transmission"}
)

MEGASCANS_PACKED_MAPS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    (r"(?i)_ORM(?:$|_\d|[._])", ("ao", "roughness", "metalness")),
    (r"(?i)_ORDp(?:$|_\d|[._])", ("ao", "roughness", "displacement")),
    (r"(?i)_ORD(?:$|_\d|[._])", ("ao", "roughness", "displacement")),
    (r"(?i)_ORT(?:$|_\d|[._])", ("ao", "roughness", "transmission")),
    (r"(?i)_ARM(?:$|_\d|[._])", ("ao", "roughness", "metalness")),
)

ORM_PACKED = {"ao": "r", "roughness": "g", "metalness": "b"}
ORD_PACKED = {"ao": "r", "roughness": "g", "displacement": "b"}
ORT_PACKED = {"ao": "r", "roughness": "g", "transmission": "b"}

PACKED_MAP_SUFFIX_LAYOUTS: Tuple[Tuple[str, Dict[str, str]], ...] = (
    (r"(?i)_ORM(?:$|_\d|[._])", ORM_PACKED),
    (r"(?i)_ARM(?:$|_\d|[._])", ORM_PACKED),
    (r"(?i)_ORDp(?:$|_\d|[._])", ORD_PACKED),
    (r"(?i)_ORD(?:$|_\d|[._])", ORD_PACKED),
    (r"(?i)_ORT(?:$|_\d|[._])", ORT_PACKED),
)

_GENERIC_MESH_STEMS = frozenset({"scene", "model", "mesh", "asset", "main", "root", "geometry", "default"})
_UDIM_TILE_RE = re.compile(r"(?:^|[._-])(1\d{3})(?:$|[._-])")


def file_extension(path: str) -> str:
    lower = path.lower()
    return ".bgeo.sc" if lower.endswith(".bgeo.sc") else os.path.splitext(lower)[1]


def empty_texture_set() -> TextureSet:
    return {ch: "" for ch in CHANNELS}


def packed_map_layout(file_path: str) -> Optional[Dict[str, str]]:
    if not file_path:
        return None
    stem = os.path.splitext(os.path.basename(file_path))[0]
    for pattern, layout in PACKED_MAP_SUFFIX_LAYOUTS:
        if re.search(pattern, stem, re.IGNORECASE):
            return dict(layout)
    return None


def _same_file(left: str, right: str) -> bool:
    return bool(left and right and os.path.normcase(os.path.normpath(left)) == os.path.normcase(os.path.normpath(right)))


def packed_channel_letter(texture_set: TextureSet, channel: str, file_path: str) -> Optional[str]:
    layout = packed_map_layout(file_path)
    if layout:
        return layout.get(channel)
    rough, metal = texture_set.get("roughness") or "", texture_set.get("metalness") or ""
    if rough and metal and _same_file(rough, metal) and _same_file(file_path, rough):
        return ORM_PACKED.get(channel)
    return None


def _apply_packed_maps(texture_set: TextureSet, files: Sequence[str]) -> None:
    for full_path in files:
        stem = os.path.splitext(os.path.basename(full_path))[0]
        for pattern, channels in MEGASCANS_PACKED_MAPS:
            if not re.search(pattern, stem, re.IGNORECASE):
                continue
            path = normalize_texture_path(full_path)
            for channel in channels:
                if not texture_set.get(channel):
                    texture_set[channel] = path
            break


def expand_packed_maps_in_texture_set(texture_set: TextureSet) -> None:
    if not texture_set:
        return
    seen: List[str] = []
    for channel in CHANNELS:
        path = texture_set.get(channel) or ""
        if not path:
            continue
        key = os.path.normcase(os.path.normpath(path))
        if key in seen:
            continue
        seen.append(key)
        _apply_packed_maps(texture_set, [path])


def normalize_texture_path(path: str) -> str:
    normalized = os.path.normpath(path)
    stem = os.path.splitext(normalized)[0]
    match = _UDIM_TILE_RE.search(os.path.basename(stem))
    return normalized.replace(match.group(1), "<UDIM>", 1) if match else normalized


@dataclass
class NamingProfile:
    name: str
    patterns: Dict[str, Sequence[str]]

    def __post_init__(self) -> None:
        self.patterns = {
            ch: (p,) if isinstance(p, str) else tuple(p)
            for ch, p in self.patterns.items()
        }


# Quixel / Fab abbreviated suffixes: T_*_{1,2,4,8,16}K_B / _N / _H / _R / _M / _D
# (older patterns only matched 4K/8K — mid/low packs like Forest Terrain used 2K and
# failed to detect albedo/normal, leaving ORM-only Principled graphs).
_MS_RES = r"_\d+[kK]"

PROFILES: Dict[str, NamingProfile] = {
    "megascans": NamingProfile("megascans", {
        "albedo": (
            r"_BaseColor",
            r"_Basecolor",
            r"_Albedo",
            r"_Diffuse",
            r"_Color",
            r"_Alb(?:$|[._])",
            r"_B-O",
            _MS_RES + r"_B(?:-O)?$",
            r"(?<![a-zA-Z])_B(?:-O)?$",
        ),
        "normal": (
            r"_Normal",
            r"_NormalMap",
            r"_NormalDX",
            r"_NormalGL",
            r"_N-T$",
            _MS_RES + r"_N(?:-T)?$",
            r"(?<![a-zA-Z])_N$",
        ),
        "roughness": (
            r"_Roughness",
            r"_Rough$",
            _MS_RES + r"_R$",
            r"(?<![a-zA-Z])_R$",
        ),
        "metalness": (
            r"_Metalness",
            r"_Metallic",
            r"_Metal$",
            _MS_RES + r"_M$",
            r"(?<![a-zA-Z])_M$",
        ),
        "ao": (r"_AO", r"_AmbientOcclusion", r"_Occlusion"),
        "displacement": (
            r"_Displacement",
            r"_Height",
            r"_Disp$",
            _MS_RES + r"_H$",
            _MS_RES + r"_D$",
            r"(?<![a-zA-Z])_H$",
            r"(?<![a-zA-Z])_D$",
        ),
        "opacity": (r"_Opacity", r"_Alpha"),
        "emissive": (r"_Emissive", r"_Emission"),
        "specular": (r"_Specular", r"_SpecularMap"),
        "translucency": (r"_Translucency",),
        "transmission": (r"_Transmission",),
        "fuzz": (r"_Fuzz",),
        "cavity": (r"_Cavity",),
        "curvature": (r"_Curvature",),
    }),
    "polyhaven": NamingProfile("polyhaven", {
        # Poly Haven: Asset_diff_2k.jpg, Asset_nor_gl_1k.jpg, Asset_rough_4k.png, …
        "albedo": (r"(?i)_(diff|diffuse|albedo|colou?r|col)(?:_|$|\.)",),
        "normal": (r"(?i)_(nor_gl|nor_dx|normal|nor|nrm)(?:_|$|\.)",),
        "roughness": (r"(?i)_(rough|roughness)(?:_|$|\.)",),
        "metalness": (r"(?i)_(metal|metallic|metalness)(?:_|$|\.)",),
        "ao": (r"(?i)_(ao|occlusion|ambientocclusion)(?:_|$|\.)",),
        "displacement": (r"(?i)_(disp|displacement|bump|height)(?:_|$|\.)",),
        "opacity": (r"(?i)_(alpha|opacity)(?:_|$|\.)",),
        "emissive": (r"(?i)_(emit|emissive|emission)(?:_|$|\.)",),
        "specular": (r"(?i)_(spec|specular)(?:_|$|\.)",),
    }),
    "ambientcg": NamingProfile("ambientcg", {
        # ambientCG: Paving003_Color.jpg or Paving003_8K-PNG_Color.png (2023+ naming)
        "albedo": (r"(?i)_?(color|col|albedo|diffuse|basecolor|base_color)(?=[._]|$)",),
        "normal": (r"(?i)_?(normalgl|normaldx|normal|nrm|nor)(?=[._]|$)",),
        "roughness": (r"(?i)_?(roughness|rough)(?=[._]|$)",),
        "metalness": (r"(?i)_?(metalness|metallic|metal)(?=[._]|$)",),
        # Do not match bare "orm" here — packed ORM is handled by MEGASCANS_PACKED_MAPS
        "ao": (r"(?i)_?(ao|ambientocclusion|ambient_occlusion|occlusion)(?=[._]|$)",),
        "displacement": (r"(?i)_?(displacement|disp|height)(?=[._]|$)",),
        "opacity": (r"(?i)_?(opacity|alpha)(?=[._]|$)",),
        "emissive": (r"(?i)_?(emissive|emission)(?=[._]|$)",),
        "transmission": (r"(?i)_?(transmission)(?=[._]|$)",),
        "specular": (r"(?i)_?(specular|spec)(?=[._]|$)",),
    }),
    "generic": NamingProfile("generic", {
        "albedo": (r"(?i)_?(diffuse|albedo|basecolor|base_color|col|color|diff)(?=[._]|$)",),
        "normal": (r"(?i)_?(normal|nrm|norm|nor_gl|nor_dx|nor)(?=[._]|$)",),
        "roughness": (r"(?i)_?(roughness|rough)(?=[._]|$)",),
        "metalness": (r"(?i)_?(metalness|metallic|metal)(?=[._]|$)",),
        "ao": (r"(?i)_?(ao|ambientocclusion|ambient_occlusion|occlusion)(?=[._]|$)",),
        "displacement": (r"(?i)_?(displacement|height|disp)(?=[._]|$)",),
        # Never match bare "trans" — that steals Transmission maps into Opacity
        "opacity": (r"(?i)_?(opacity|alpha)(?=[._]|$)",),
        "emissive": (r"(?i)_?(emissive|emission)(?=[._]|$)",),
        "specular": (r"(?i)_?(specular|spec)(?=[._]|$)",),
        "translucency": (r"(?i)_?(translucency)(?=[._]|$)",),
        "transmission": (r"(?i)_?(transmission)(?=[._]|$)",),
    }),
}
PROFILES["standard"] = PROFILES["generic"]


def _is_texture(path: str) -> bool:
    return file_extension(path) in TEXTURE_EXTENSIONS


def _mesh_stem(mesh_path: Optional[str]) -> str:
    if not mesh_path:
        return ""
    stem = os.path.splitext(os.path.basename(mesh_path))[0].lower()
    return "" if stem in _GENERIC_MESH_STEMS else stem


def _iter_texture_files(directory: str, mesh_path: Optional[str] = None, *, max_depth: int = 3) -> List[str]:
    if not os.path.isdir(directory):
        return []
    mesh_stem = _mesh_stem(mesh_path)
    directory = os.path.normpath(directory)
    out: List[str] = []
    for root, dirnames, filenames in os.walk(directory):
        rel = os.path.relpath(root, directory)
        if (0 if rel == "." else rel.count(os.sep) + 1) > max_depth:
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if d and not d.startswith(".") and d.lower() != "__macosx"]
        for name in filenames:
            if name.startswith("."):
                continue
            full = os.path.join(root, name)
            if not os.path.isfile(full) or not _is_texture(full):
                continue
            if mesh_stem:
                stem = os.path.splitext(name)[0].lower()
                prefix = mesh_stem.rsplit("_", 1)[0]
                if not (stem.startswith(mesh_stem) or mesh_stem in stem or (prefix and len(prefix) > 3 and prefix in stem)):
                    continue
            out.append(full)
    return out


def _profile_hint(directory: str) -> str:
    """Infer naming profile from Online Downloads / cache path segments."""
    lower = directory.replace("\\", "/").lower()
    if any(t in lower for t in ("/fab/", "/megascans/", "/quixel/")):
        return "megascans"
    if "/ambientcg/" in lower or lower.endswith("/ambientcg"):
        return "ambientcg"
    if "/polyhaven/" in lower or "/poly_haven/" in lower:
        return "polyhaven"
    if "/pbrpx/" in lower or "/lazytextures/" in lower or "/gpuopen/" in lower:
        return "generic"
    return ""


def texture_set_ready_for_principled(maps: Dict[str, str]) -> bool:
    """True when detected maps are enough to build a useful Principled graph."""
    if not maps:
        return False
    # Any core / wired channel from principled_plan
    for key in (
        "albedo",
        "normal",
        "roughness",
        "metalness",
        "ao",
        "opacity",
        "emissive",
        "displacement",
        "transmission",
        "specular",
    ):
        if maps.get(key):
            return True
    return False


def _score_profile(profile: NamingProfile, filenames: Sequence[str]) -> int:
    score, matched = 0, set()
    for channel, patterns in profile.patterns.items():
        regexes = [re.compile(p, re.I) for p in patterns]
        for filename in filenames:
            stem = os.path.splitext(os.path.basename(filename))[0]
            if any(r.search(stem) for r in regexes):
                if channel not in matched:
                    matched.add(channel)
                    score += 2
                break
    return score


def detect_profile(filenames: Sequence[str], directory: str = "") -> str:
    if not filenames:
        hint = _profile_hint(directory)
        return hint if hint in PROFILES else "generic"
    hint = _profile_hint(directory)
    # Fab / Megascans path always wins — abbreviated packs may score 0 until
    # patterns match, and ORM-only folders still need the megascans packed pass.
    if hint == "megascans":
        return "megascans"
    if hint in PROFILES and _score_profile(PROFILES[hint], filenames) > 0:
        return hint
    scores = {n: _score_profile(p, filenames) for n, p in PROFILES.items() if n not in ("generic", "standard")}
    best, best_score = max(scores.items(), key=lambda x: x[1])
    return best if best_score > 0 else "generic"


def _resolve_profile(name: Optional[str]) -> NamingProfile:
    key = (name or "generic").strip().lower()
    return PROFILES.get(key, PROFILES["generic"]) if key not in ("auto", "") else PROFILES["generic"]


def _match_channel(stem: str, patterns: Sequence[str]) -> bool:
    return any(re.search(p, stem, re.I) for p in patterns)


def _prefer_albedo_bo(texture_set: TextureSet, files: Sequence[str]) -> None:
    current = texture_set.get("albedo") or ""
    if not current or re.search(r"_B-O$", os.path.splitext(os.path.basename(current))[0], re.I):
        return
    for full_path in files:
        if re.search(r"_B-O$", os.path.splitext(os.path.basename(full_path))[0], re.I):
            texture_set["albedo"] = normalize_texture_path(full_path)
            return


_NORMAL_STEM_RE = re.compile(
    r"(?i)(?:normalgl|normaldx|nor_gl|nor_dx|normal|nrm|nor)(?:_|$|\.)|_N$"
)
_NORMAL_GL_RE = re.compile(r"(?i)(?:nor_gl|normalgl|normal_gl)")
_NORMAL_DX_RE = re.compile(r"(?i)(?:nor_dx|normaldx|normal_dx)")


def prefer_normal_map_variant(
    texture_set: TextureSet,
    files: Sequence[str],
    *,
    prefer_gl: bool = True,
) -> None:
    """Prefer OpenGL (nor_gl / NormalGL) or DirectX normals when both exist.

    Blender Principled + Normal Map node expects OpenGL by default; DirectX
    maps need a green-channel flip (``import.normal_map_space=DIRECTX``).
    Walk order previously picked DX first and broke ambientCG / Poly Haven.
    """
    if not texture_set.get("normal"):
        return
    gl_files: List[str] = []
    dx_files: List[str] = []
    other: List[str] = []
    for full_path in files:
        stem = os.path.splitext(os.path.basename(full_path))[0]
        if not _NORMAL_STEM_RE.search(stem):
            continue
        path = normalize_texture_path(full_path)
        if _NORMAL_GL_RE.search(stem):
            gl_files.append(path)
        elif _NORMAL_DX_RE.search(stem):
            dx_files.append(path)
        else:
            other.append(path)
    if prefer_gl:
        pick = (gl_files or other or dx_files or [texture_set["normal"]])[0]
    else:
        pick = (dx_files or other or gl_files or [texture_set["normal"]])[0]
    texture_set["normal"] = pick


def _resolve_input(
    paths_or_folder: Union[str, Sequence[str]],
    mesh_path: Optional[str] = None,
) -> Tuple[str, List[str]]:
    if isinstance(paths_or_folder, str):
        path = os.path.normpath(paths_or_folder)
        if os.path.isdir(path):
            return path, _iter_texture_files(path, mesh_path)
        if os.path.isfile(path):
            directory = os.path.dirname(path)
            return directory, [path] if _is_texture(path) else _iter_texture_files(directory, mesh_path)
        return path, []
    files = [os.path.normpath(str(e)) for e in paths_or_folder if e and os.path.isfile(str(e)) and _is_texture(str(e))]
    if not files:
        return "", []
    dirs = [os.path.dirname(os.path.abspath(f)) for f in files]
    return (os.path.commonpath(dirs) if len(dirs) > 1 else dirs[0]), files


def _match_files(files: Sequence[str], profile_name: Optional[str], directory: str = "") -> Tuple[TextureSet, str]:
    if not files:
        return empty_texture_set(), (profile_name if profile_name not in (None, "auto", "") else "generic")

    resolved = profile_name or "auto"
    if resolved in ("auto", ""):
        basenames = [os.path.basename(f) for f in files]
        resolved = detect_profile(basenames, directory)

    profile = _resolve_profile(resolved)
    texture_set = empty_texture_set()
    for full_path in files:
        stem = os.path.splitext(os.path.basename(full_path))[0]
        for channel, patterns in profile.patterns.items():
            if not texture_set[channel] and _match_channel(stem, patterns):
                texture_set[channel] = normalize_texture_path(full_path)

    if resolved == "megascans":
        _prefer_albedo_bo(texture_set, files)
    _apply_packed_maps(texture_set, files)
    expand_packed_maps_in_texture_set(texture_set)
    return texture_set, resolved


def detect_texture_set(
    paths_or_folder: Union[str, Sequence[str]],
    profile: Optional[str] = None,
    *,
    mesh_path: Optional[str] = None,
    prefer_gl_normal: bool = True,
) -> Tuple[TextureSet, str]:
    """Detect PBR maps from a folder path or explicit file list."""
    directory, files = _resolve_input(paths_or_folder, mesh_path)
    if not files and directory and os.path.isdir(directory):
        files = _iter_texture_files(directory)
    texture_set, resolved = _match_files(files, profile, directory)
    # Prefer OpenGL normals for Blender default (nor_gl / NormalGL over DX)
    prefer_normal_map_variant(texture_set, files, prefer_gl=prefer_gl_normal)
    return texture_set, resolved


def detect_maps_in_folder(folder: str) -> Dict[str, str]:
    texture_set, _ = detect_texture_set(folder)
    return {ch: p for ch, p in texture_set.items() if p}


def _resolve_on_disk(path: str, roots: Sequence[str]) -> str:
    if not path:
        return ""
    normalized = str(path).replace("\\", "/").strip()
    candidates = [normalized, os.path.normpath(normalized)]
    if os.path.isabs(normalized):
        candidates.append(os.path.abspath(normalized))
    else:
        for root in roots:
            candidates.extend([
                os.path.normpath(os.path.join(root, normalized)),
                os.path.normpath(os.path.join(root, os.path.basename(normalized))),
            ])
    seen = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        check = candidate.replace("<UDIM>", "1001")
        if os.path.isfile(check):
            return os.path.abspath(os.path.normpath(check))
    return ""


def _audit(texture_set: TextureSet, profile: str, roots: Sequence[str]) -> Tuple[TextureSet, Dict[str, object]]:
    cleaned: TextureSet = {}
    dropped: List[str] = []
    warnings: List[str] = []
    for channel, path in texture_set.items():
        if not path:
            continue
        resolved = _resolve_on_disk(path, roots)
        if resolved:
            cleaned[channel] = normalize_texture_path(resolved)
        else:
            dropped.append(channel)
            warnings.append("{}: texture file not found ({})".format(channel, os.path.basename(path)))
    found = [ch for ch in CORE_CHANNELS if cleaned.get(ch)]
    missing = [ch for ch in CORE_CHANNELS if not cleaned.get(ch)]
    return cleaned, {
        "profile": profile,
        "found": found,
        "missing_core": missing,
        "dropped_channels": dropped,
        "warnings": warnings,
    }


def find_sidecar_texture_folders(mesh_path: str) -> List[str]:
    """Folders to probe for PBR maps next to an imported mesh.

    Includes ``textures/`` / ``maps/``, ZIP ``extracted/``, and PBRPX-style
    resolution folders (``1K`` … ``16K``).
    """
    path = os.path.normpath(mesh_path or "")
    parent = path if os.path.isdir(path) else os.path.dirname(path)
    names = (
        "textures",
        "Textures",
        "texture",
        "Texture",
        "maps",
        "Maps",
        "materials",
        "Materials",
        "tex",
        "Tex",
        "images",
        "Images",
        "extracted",
        "Extracted",
    )
    res_folder = re.compile(r"(?i)^(1|2|4|6|8|16)k$")
    out: List[str] = []

    def _add(candidate: str) -> None:
        if candidate and os.path.isdir(candidate) and candidate not in out:
            out.append(candidate)

    def _add_named_and_res(base: str) -> None:
        for name in names:
            _add(os.path.join(base, name))
        try:
            for entry in os.listdir(base):
                if res_folder.match(entry or ""):
                    _add(os.path.join(base, entry))
        except OSError:
            pass

    _add_named_and_res(parent)
    _add(parent)
    grand = os.path.dirname(parent)
    if grand and os.path.isdir(grand):
        _add_named_and_res(grand)
        _add(grand)
    return out


def prepare_import_texture_set(
    paths_or_folder: Union[str, Sequence[str]],
    profile: Optional[str] = None,
    *,
    mesh_path: Optional[str] = None,
    search_roots: Optional[Sequence[str]] = None,
    normal_map_space: str = "OPENGL",
) -> Tuple[TextureSet, Dict[str, object]]:
    """Detect, resolve on disk, validate, and audit PBR maps before material build."""
    directory, files = _resolve_input(paths_or_folder, mesh_path)
    prefer_gl = str(normal_map_space or "OPENGL").upper() != "DIRECTX"
    texture_set, resolved_profile = detect_texture_set(
        paths_or_folder,
        profile=profile,
        mesh_path=mesh_path,
        prefer_gl_normal=prefer_gl,
    )
    # Re-apply in case detect's internal match already preferred GL — honor cfg space
    if files:
        prefer_normal_map_variant(texture_set, files, prefer_gl=prefer_gl)
    roots: List[str] = []
    for root in (directory, os.path.dirname(mesh_path or ""), *(search_roots or ())):
        if root and os.path.isdir(root):
            norm = os.path.normpath(root)
            if norm not in roots:
                roots.append(norm)
    return _audit(texture_set, resolved_profile, roots)
