# Universal Asset Library (Blender Community)

Free Blender extension for browsing a local asset library and eight public online sources.

**This repository is Community-only.** It never contains Fab / Megascans browse-download, the GPU asset bar, or cloud backup. Those ship only in the private Pro product ([`ual-blender`](https://github.com/Universal-Asset-Library/ual-blender)).

| | |
|---|---|
| **License** | GPL-3.0-or-later |
| **Blender** | 4.2+ (tested on 5.2) |
| **Current release** | **0.3.97** |
| **Extension id** | `universal_asset_library` |
| **Website / Pro** | https://universalassetlibrary.com |

## Included

- Local library (index, favorites, import, health check)
- Hover preview
- Material Blend
- Online sources: Poly Haven, ambientCG, GPUOpen MaterialX, LazyTextures, Three D Scans, PBRPX, Poly Pizza, Sketchfab
- In-addon **Check for Updates** (Superhive / public build does not offer in-app ZIP install)

## Not included (Pro)

- Fab / Quixel Megascans online browse & download
- GPU asset bar
- Cloud metadata backup

Get Pro at the website. Do not expect Pro modules in this repo or its ZIPs.

**Same extension id as Pro** — uninstall one before installing the other.

## Install

1. Download `UniversalAssetLibrary-Blender-*-Community.zip` from [Releases](../../releases) or Superhive Market.
2. Install as a Blender extension (Edit → Preferences → Get Extensions / Install from Disk).

## Release (maintainers)

Product development happens in private **`ual-blender`**. This tree is an export:

```bash
# from ual-blender
python build/export_community_repo.py --out-dir ../ual-blender-community --clean
```

Then in this repo: fill `.github/RELEASE_NOTES.md`, bump version files, commit, tag `vX.Y.Z`, push the tag. GitHub Actions packages the Community ZIP, publishes the Release, and notifies ual-api (`flavor=community`).

Repo secrets (once): `UAL_API_BASE_URL`, `UAL_API_ADMIN_TOKEN`.

## Development

Do not add Pro connectors here. CI (`ci.yml`) fails if Fab / asset bar / cloud backup / Pro UI modules appear.
