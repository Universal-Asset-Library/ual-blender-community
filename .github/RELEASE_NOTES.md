# UAL Blender 0.3.97

Write this file **before** you tag `v0.3.97`. The release job copies it to the GitHub Release and to the website changelog.

## Highlights

Community and Pro are separate products and repos. In-app updates refuse a ZIP of the other flavor so Superhive Community installs cannot be overwritten by a Pro package (or the reverse).

## New

- Public Superhive distribution ships from `ual-blender-community` (Community-only tree, no Pro modules).
- Update installer checks `package_flavor.FLAVOR` and aborts on Community↔Pro mismatch.

## Improvements

- Community installs strip Fab from enabled online sources so Preferences stay aligned with shipped connectors.
- Website / API catalog sync reads Community ZIPs from `ual-blender-community` and Pro ZIPs from private `ual-blender`.

## Compatibility

Blender 4.2+, tested on 5.2

**Note:** Community and Pro use the same Blender extension id (`universal_asset_library`). Uninstall one before installing the other.
