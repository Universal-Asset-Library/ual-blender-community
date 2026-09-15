#!/usr/bin/env python3
# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""POST this Community GitHub tag to ual-api (Downloads + changelog).

Skip (exit 0) when UAL_API_BASE_URL or UAL_API_ADMIN_TOKEN is missing.
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _safe_print(text: str) -> None:
    try:
        print(text)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        sys.stdout.buffer.write((text + "\n").encode(enc, errors="replace"))


def _read_sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    token = path.read_text(encoding="utf-8").strip().split()[0]
    return token or None


def _local_body() -> str:
    notes = ROOT / ".github" / "RELEASE_NOTES.md"
    if notes.is_file():
        return notes.read_text(encoding="utf-8-sig").strip()
    return ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--sha256-file", required=True)
    parser.add_argument("--filename", default="")
    parser.add_argument("--download-url", default="")
    args = parser.parse_args()

    base = (os.environ.get("UAL_API_BASE_URL") or "").strip().rstrip("/")
    token = (
        os.environ.get("UAL_API_ADMIN_TOKEN") or os.environ.get("ADMIN_API_TOKEN") or ""
    ).strip()
    if not base or not token:
        print("Skipping ual-api notify (UAL_API_BASE_URL / UAL_API_ADMIN_TOKEN not set)")
        return 0

    version = args.version.lstrip("v")
    tag = f"v{version}"
    filename = args.filename.strip() or f"UniversalAssetLibrary-Blender-{version}-Community.zip"
    download_url = args.download_url.strip()
    if not download_url:
        repo = (
            os.environ.get("RELEASE_GITHUB_REPO_BLENDER_COMMUNITY")
            or os.environ.get("GITHUB_REPOSITORY")
            or "Universal-Asset-Library/ual-blender-community"
        )
        download_url = f"https://github.com/{repo}/releases/download/{tag}/{filename}"

    payload = {
        "productId": "blender",
        "version": version,
        "channel": "stable",
        "flavor": "community",
        "downloadUrl": download_url,
        "sha256": _read_sha256(Path(args.sha256_file)),
        "filename": filename,
        "releaseNotes": _local_body(),
    }
    req = urllib.request.Request(
        f"{base}/api/v1/admin/releases/from-github",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "ual-community-release-notify",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=45, context=ssl.create_default_context()) as resp:
            _safe_print(
                f"ual-api notify {resp.status}: {resp.read().decode('utf-8', errors='ignore')[:500]}"
            )
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        _safe_print(f"ual-api notify failed {exc.code}: {detail[:800]}")
        return 1
    except (OSError, urllib.error.URLError) as exc:
        _safe_print(f"ual-api notify failed: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
