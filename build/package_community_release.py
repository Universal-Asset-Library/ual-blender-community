#!/usr/bin/env python3
# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""Build UniversalAssetLibrary-Blender-{version}-Community.zip from this public tree.

Preserves package_flavor.py (FLAVOR=community, PUBLIC_STORE=True).
Never ships Pro modules — this repo must not contain them.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "universal_asset_library"
DIST = ROOT / "dist"

EXCLUDE_DIR_NAMES = {
    "__pycache__",
    "tests",
    ".git",
    ".github",
    ".pytest_cache",
    ".cursor",
    ".claude",
    ".gemini",
}

FORBIDDEN_REL = frozenset(
    {
        "sources/fab.py",
        "epic_oauth.py",
        "ui/epic_signin.py",
        "cloud_backup.py",
        "ui/pro_operators.py",
        "preferences_pro_ui.py",
    }
)


def read_version() -> str:
    text = (PKG / "blender_manifest.toml").read_text(encoding="utf-8")
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
    if not match:
        raise SystemExit("Could not read version from blender_manifest.toml")
    return match.group(1)


def should_skip(rel: Path) -> bool:
    if any(part in EXCLUDE_DIR_NAMES for part in rel.parts):
        return True
    if rel.name.startswith(".") and rel.name not in {".gitkeep"}:
        return True
    posix = rel.as_posix()
    if posix in FORBIDDEN_REL:
        raise SystemExit(f"Pro path present in Community tree: {posix}")
    if posix == "ui/asset_bar" or posix.startswith("ui/asset_bar/"):
        raise SystemExit(f"Pro path present in Community tree: {posix}")
    return False


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default="")
    parser.add_argument("--out-dir", type=Path, default=DIST)
    args = parser.parse_args()
    version = (args.version or read_version()).lstrip("v")
    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_name = f"UniversalAssetLibrary-Blender-{version}-Community.zip"
    zip_path = out_dir / zip_name
    if zip_path.exists():
        zip_path.unlink()

    flavor = (PKG / "package_flavor.py").read_text(encoding="utf-8")
    if 'FLAVOR = "community"' not in flavor:
        raise SystemExit("package_flavor.py must set FLAVOR = \"community\"")
    if "PUBLIC_STORE = True" not in flavor:
        raise SystemExit("package_flavor.py must set PUBLIC_STORE = True")

    files: list[Path] = []
    for child in PKG.rglob("*"):
        if not child.is_file():
            continue
        rel = child.relative_to(PKG)
        if should_skip(rel):
            continue
        files.append(child)
    if not files:
        raise SystemExit("No files selected for package")

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for file_path in files:
            rel = file_path.relative_to(PKG)
            arc = Path("universal_asset_library") / rel
            zf.write(file_path, arcname=arc.as_posix())

    digest = sha256_file(zip_path)
    checksum_path = out_dir / f"{zip_path.stem}.sha256"
    checksum_path.write_text(f"{digest}  {zip_path.name}\n", encoding="utf-8")
    print(f"Built {zip_path} ({zip_path.stat().st_size} bytes) flavor=community")
    print(f"SHA256 {digest}")
    print(f"Wrote {checksum_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
