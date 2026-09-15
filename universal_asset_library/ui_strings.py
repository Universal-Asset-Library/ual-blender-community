# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""N-panel copy helpers (pure — no bpy). Narrow-width safe strings + status lines."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

# Canonical product terminology (align with Houdini strings.py where concepts match):
# - My Library — local indexed scope (Blender scope tab may show "Library" when narrow)
# - Online Sources — online catalog scope (Blender: "Online")
# - Previews — Preferences tab for thumb size / hover preview
# - Save to Library / Download & Import — same as Houdini online footer

# Soft target for one label line in a ~180–220px sidebar
_MAX_LABEL = 28

ADDON_VERSION = "0.3.99"
CHANGELOG_BLURB = "ambientCG thumbs fixed"
SCOPE_MY_LIBRARY = "My Library"
SCOPE_ONLINE = "Online Sources"
PREFERENCES_TAB_PREVIEWS = "Previews"
MATERIAL_BLEND_SETS_PICK_TIP = "Pick, Collection, or + — not drag into list"

# Shared operator / toast copy (ASCII-safe; Blender info bar truncates ~160 chars)
MSG_ONLINE_ACCESS = "Turn on Allow Online Access in Blender Preferences → System"
MSG_SELECT_ASSET = "Select an asset first — click the name under the preview"
MSG_EMPTY_DOWNLOAD = "Download finished but no file was saved. Try again."
MSG_GENERIC_ERROR = "Something went wrong. Try again."
MSG_NETWORK = "Couldn't reach the site. Check your internet and try again."
MSG_SSL = "Secure connection failed. Check your internet or firewall."
MSG_RNA_REMOVED = "That object was already removed. Undo or import again."
MSG_PERMISSION = "Can't write there. Close the file if it's open, or pick another folder."
MSG_NOT_FOUND = "File not found. It may have been moved or deleted."
MSG_ZIP = "The download isn't a valid zip file. Try again."
MSG_USE_DOWNLOAD = "Online assets: use Download & Import, not Import Selected"
MSG_SEARCH_FIRST = "Search first — press Enter or Search"
MSG_NO_ASSETS = "No assets yet — add a folder, then Rebuild Index"
MSG_CACHE_MISSING = "Cache folder missing — set it in Preferences → Library"
MSG_FAB_SIGNIN = "Fab downloads need Epic sign-in. Preferences → Online → Sign In with Epic…"


def pluralize(count: int, singular: str, plural: str = "") -> str:
    """Return ``\"1 asset\"`` / ``\"2 assets\"`` (count included)."""
    n = int(count)
    word = singular if n == 1 else (plural or f"{singular}s")
    return f"{n} {word}"


def empty_state_lines(
    scope: str,
    *,
    did_search: bool = False,
    type_filter: str = "ALL",
    source_id: str = "",
    auth_needed: bool = False,
    searching: bool = False,
) -> Tuple[str, str]:
    """Two short lines for empty Assets (avoids UILayout clip at ~200px)."""
    if scope == "favorites":
        return ("No favorites yet", "Favorite one in My Library")
    if scope == "online":
        if auth_needed and not did_search:
            return ("Sign in to search", "Open Preferences")
        if searching:
            return ("Searching…", "UI stays interactive")
        if did_search:
            filt = str(type_filter or "ALL").strip()
            if filt and filt.upper() != "ALL":
                try:
                    from .online_type_filter import unsupported_type_empty_hint

                    hint = unsupported_type_empty_hint(source_id, filt)
                except Exception:
                    hint = "Try Type = All"
                return ("No matching assets", hint)
            return ("No matching assets", "Try another query or source")
        return ("No online results", "Press Enter or Search")
    return ("Empty library", "Add a folder, then Rebuild Index")


def empty_state_cta(
    scope: str,
    *,
    did_search: bool = False,
    type_filter: str = "ALL",
    auth_needed: bool = False,
    searching: bool = False,
) -> str:
    """Which empty-state button to draw: search | preferences | type_all | library | ''."""
    if scope == "favorites":
        return ""
    if scope == "online":
        if auth_needed and not did_search:
            return "preferences"
        if searching and not did_search:
            return ""
        if not did_search:
            return "search"
        filt = str(type_filter or "ALL").strip()
        if filt and filt.upper() != "ALL":
            return "type_all"
        return ""
    return "library"


def onboarding_banner_should_show(
    *,
    dismissed: bool,
    scope: str = "library",
    library_empty: bool = False,
) -> bool:
    """Skip the first-run banner when Library empty state already has CTAs."""
    if dismissed:
        return False
    if (scope or "library") == "library" and library_empty:
        return False
    return True


def source_needs_search_auth(source_id: str) -> bool:
    """True when Search itself requires credentials (not Fab — browse is public)."""
    return (source_id or "").strip().lower() in {"sketchfab", "polypizza"}


def viewport_range_label(start: int, count: int, total: int) -> str:
    """Narrow Assets footer: ``1-12 of 40`` (empty when nothing to window)."""
    total = max(0, int(total or 0))
    count = max(0, int(count or 0))
    start = max(0, int(start or 0))
    if total <= 0 or count <= 0:
        return ""
    first = start + 1
    last = min(total, start + count)
    return f"{first}-{last} of {total}"[:_MAX_LABEL]


def format_online_result_status(
    shown: int,
    *,
    total_count: int = 0,
    has_more: bool = False,
    type_filter: str = "ALL",
    loading_thumbs: bool = False,
) -> str:
    """Human live-count line for Online search results (not operation status)."""
    del loading_thumbs  # operation chrome lives in status_message now
    n = max(0, int(shown))
    total = max(0, int(total_count or 0))
    if total > n or has_more:
        total_label = str(total) if total else "?"
        word = "result" if total == 1 else "results"
        base = f"{n} of {total_label} {word}"
    elif n:
        base = f"{pluralize(n, 'result')} (showing all)"
    else:
        base = "0 results"
    if type_filter and type_filter != "ALL":
        type_label = {
            "material_set": "Material",
            "mesh": "Mesh",
            "texture": "Texture",
            "hdri": "HDRI",
        }.get(str(type_filter), str(type_filter))
        base = f"{base} · {type_label}"
    return base


def live_scope_count_label(ui: Any) -> str:
    """Human live count for the active scope (used by status helpers / tests)."""
    scope = getattr(ui, "scope", "library") or "library"
    if scope == "online":
        try:
            shown = len(ui.online_results)
        except Exception:
            shown = 0
        return format_online_result_status(
            shown,
            total_count=int(getattr(ui, "online_total_count", 0) or 0),
            has_more=bool(getattr(ui, "online_has_more", False)),
            type_filter=getattr(ui, "filter_type", "ALL") or "ALL",
        )
    try:
        n = len(ui.assets)
    except Exception:
        n = 0
    if scope == "favorites":
        return pluralize(n, "favorite")
    return pluralize(n, "asset")


def assets_header_label(ui: Any) -> str:
    """Assets subpanel title with embedded count: ``Assets (40 of 2290)``."""
    if ui is None:
        return "Assets"
    scope = getattr(ui, "scope", "library") or "library"
    if scope == "online":
        try:
            results = getattr(ui, "online_results", None)
            shown = len(results) if results is not None else 0
        except Exception:
            shown = 0
        total = max(0, int(getattr(ui, "online_total_count", 0) or 0))
        has_more = bool(getattr(ui, "online_has_more", False))
        if total > shown or has_more:
            total_label = str(total) if total else "?"
            return f"Assets ({shown} of {total_label})"
        return f"Assets ({shown})"
    try:
        assets = getattr(ui, "assets", None)
        n = len(assets) if assets is not None else 0
    except Exception:
        n = 0
    return f"Assets ({n})"


def assets_toolbar_row_count(scope: str, region_width: float = 320.0) -> int:
    """How many toolbar rows Assets draws (for hover chrome estimate)."""
    if (scope or "") == "online":
        return 1
    try:
        from .ui.layout_breakpoints import library_toolbar_wraps

        return 2 if library_toolbar_wraps(region_width) else 1
    except Exception:
        return 2 if float(region_width or 320) < 220 else 1


# Soft cap: Online Load More stops appending past this (memory / Asset Bar still browse)
ONLINE_RESULTS_SOFT_CAP = 120


def estimate_hover_card_ui_units(preview_size: int = 320) -> float:
    """UI-units for the pinned inline details card (thumb + meta + actions)."""
    size = max(160, min(int(preview_size or 320), 512))
    scale = max(4.0, min(size / 32.0, 10.0))
    # template_icon(scale) + title + meta + action row + box padding
    return float(scale) + 3.5


def estimate_assets_chrome_ui_units(
    scope: str,
    *,
    changelog_visible: bool = False,
    onboarding_visible: bool = False,
    download_active: bool = False,
    toolbar_rows: int = 1,
    hover_card_visible: bool = False,
    hover_preview_size: int = 320,
    download_option_rows: int = 0,
    capability_lines: int = 0,
) -> float:
    """UI-units from N-panel top to Assets grid content (hover hit-test).

    Keep aligned with panel draw:
    - UI region category tabs (Item / Tool / View / UAL) above all panels
    - Main: header → scope → gap → filter row (Search+Type+View) → Source (online)
      → optional capability line (WIDE only)
    - Assets: header → toolbar → Size/Format ``layout.box()`` (online) → grid

    Download progress lives in the Downloads *child* panel below Assets, so
    ``download_active`` does not add chrome above the grid.

    ``hover_card_visible`` / ``hover_preview_size`` are accepted for API
    compatibility but ignored — the large preview is a GPU overlay over the
    3D View and does not insert UILayout chrome into the N-panel.
    """
    del hover_card_visible, hover_preview_size, download_active
    y = 0.0
    # VIEW_3D UI region draws category tabs above every panel header. Omitting
    # this shifts first_row_top up onto Assets toolbar / Size-Format chrome.
    y += 1.15  # N-panel category tabs
    y += 1.15  # main panel header
    if changelog_visible:
        y += 1.7
    if onboarding_visible:
        y += 1.7
    y += 1.15  # scope tabs
    y += 0.35  # gap before filter block
    y += 1.15  # Search + Type + View
    if (scope or "") == "online":
        y += 1.15  # source dropdown
        caps = max(0, min(1, int(capability_lines or 0)))
        y += 0.85 * caps  # one compact line at WIDE
    y += 1.15  # Assets subpanel header (includes count)
    rows = max(1, min(2, int(toolbar_rows or 1)))
    y += 1.15 * rows
    if rows >= 2:
        y += 0.15  # separator between wrapped toolbar rows
    # Online Size/Format is ``layout.box()`` + prop row — taller than a bare row.
    opt = max(0, min(2, int(download_option_rows or 0)))
    y += 2.15 * opt
    y += 0.35
    return y


def source_needs_auth(source_id: str) -> bool:
    return (source_id or "").strip().lower() in {"sketchfab", "polypizza", "fab"}


def source_auth_configured(source_id: str, cfg_or_online: Optional[Dict] = None) -> bool:
    """True when gated source has credentials (or source is free).

    ``cfg_or_online`` may be the full config dict or just the ``online`` block.
    """
    sid = (source_id or "").strip().lower()
    if not source_needs_auth(sid):
        return True
    raw = cfg_or_online or {}
    online = raw["online"] if isinstance(raw.get("online"), dict) else raw
    if sid == "sketchfab":
        return bool((online.get("sketchfab_token") or "").strip())
    if sid == "polypizza":
        return bool((online.get("polypizza_api_key") or "").strip())
    if sid == "fab":
        if (online.get("fab_sessionid") or "").strip():
            return True
        try:
            from . import epic_oauth

            return bool(epic_oauth.epic_session_configured(raw))
        except Exception:
            return False
    return True


def item_is_auth_gated(source_id: str, cfg_or_online: Optional[Dict] = None) -> bool:
    """Show lock badge when source needs auth the user has not configured."""
    return source_needs_auth(source_id) and not source_auth_configured(source_id, cfg_or_online)


def split_status_for_narrow(message: str, max_len: int = _MAX_LABEL) -> List[str]:
    """Split a long status into ≤2 short lines for narrow sidebars."""
    text = (message or "").strip()
    if not text:
        return ["Ready"]
    if len(text) <= max_len:
        return [text]
    # Prefer split at em-dash / dash / middle
    for sep in (" — ", " - ", " · ", "; "):
        if sep in text:
            left, right = text.split(sep, 1)
            return [left.strip()[: max_len + 8], right.strip()[: max_len + 12]]
    mid = max_len
    return [text[:mid].rstrip() + "…", text[mid:].lstrip()[: max_len + 8]]


def changelog_seen(cfg: Optional[Dict], version: str = ADDON_VERSION) -> bool:
    ui = (cfg or {}).get("ui") or {}
    return str(ui.get("changelog_seen_version") or "") == str(version)


_MOJIBAKE_DASH_RE = re.compile("â€[\u201c\u201d\"']")
_MAX_TOAST = 160
_TECH_MARKERS = (
    "traceback",
    'file "',
    "http error",
    "urlerror",
    "sslerror",
    "winerror",
    "errno",
    "<urlopen",
    "structrna",
    "cache_dir",
    "empty path",
    "exception:",
    "error 0x",
    "zipfile.",
    "runtimeerror",
    "valueerror",
    "filenotfound",
    "permissionerror",
    "probing platform",
    "kwargs",
    "ssl.ssl",
    "certificate_verify",
)


def _fix_mojibake(text: str) -> str:
    out = _MOJIBAKE_DASH_RE.sub(" — ", text or "")
    return (
        out.replace("â€“", "-")
        .replace("â€¦", "...")
        .replace("â€™", "'")
        .replace("â€˜", "'")
    )


def _looks_technical(text: str) -> bool:
    lower = (text or "").lower()
    if any(marker in lower for marker in _TECH_MARKERS):
        return True
    if "http " in lower and any(code in lower for code in (" 40", " 50", " 429")):
        return True
    return False


def _strip_exception_prefix(text: str) -> str:
    raw = (text or "").strip()
    if "Traceback (most recent call last)" in raw or ('File "' in raw and "\n" in raw):
        raw = raw.strip().splitlines()[-1].strip()
    for prefix in (
        "RuntimeError: ",
        "ValueError: ",
        "OSError: ",
        "FileNotFoundError: ",
        "PermissionError: ",
        "URLError: ",
        "HTTPError: ",
        "ssl.SSLError: ",
        "urllib.error.URLError: ",
        "urllib.error.HTTPError: ",
        "Exception: ",
        "zipfile.BadZipFile: ",
        "BadZipFile: ",
    ):
        if raw.startswith(prefix):
            raw = raw[len(prefix) :].strip()
    return raw


def friendly_error(exc_or_text: Any) -> str:
    """Map exceptions and jargon to a short artist-facing sentence.

    Idempotent for already-friendly copy. Technical detail belongs in the
    Blender console, not the info bar or N-panel status footer.
    """
    if isinstance(exc_or_text, BaseException):
        code = getattr(exc_or_text, "code", None)
        raw = str(exc_or_text or "").strip()
        if code and f" {code}" not in raw and str(code) not in raw:
            raw = f"HTTP {code}: {raw}"
    else:
        raw = str(exc_or_text or "").strip()
    raw = _fix_mojibake(raw)
    if not raw:
        return MSG_GENERIC_ERROR
    raw = _strip_exception_prefix(raw)
    lower = raw.lower()

    rules: Tuple[Tuple[Tuple[str, ...], str], ...] = (
        (("invalid_addon_key",), "API key must start with ualak_. Copy a new key from Account → Profile."),
        (("missing_addon_key",), "Paste your UAL API key from Account → Profile, then click Connect."),
        (("missing_api_base",), "Set Updates API base URL first (website origin, e.g. https://universalassetlibrary.com)."),
        (("could not resolve account from api key",), "Could not resolve that API key. Rotate the key on Account → Profile and try Connect again."),
        (("sign in or paste your add-on key", "paste your add-on key from account"), "That API key was rejected. Create or rotate a key on Account → Profile, then Connect."),
        (("structrna", "has been removed"), MSG_RNA_REMOVED),
        (("allow online access",), MSG_ONLINE_ACCESS),
        (("blender online access is disabled",), MSG_ONLINE_ACCESS),
        (("online access is off",), MSG_ONLINE_ACCESS),
        (("paste the epic", "authorizationcode"), "Paste the Epic code from your browser, then try again."),
        (("epic session expired",), "Epic sign-in expired. Preferences → Online → Sign In with Epic…"),
        (("epic account not signed in",), MSG_FAB_SIGNIN),
        (("epic access token",), MSG_FAB_SIGNIN),
        (("fab downloads require",), MSG_FAB_SIGNIN),
        (("fab cookies not configured",), MSG_FAB_SIGNIN),
        (("epic / fab cookies",), MSG_FAB_SIGNIN),
        (("sketchfab api token is required",), "Sketchfab needs an API token. Preferences → Online → Get Sketchfab Token."),
        (("sketchfab token not set",), "Sketchfab needs an API token. Preferences → Online → Get Sketchfab Token."),
        (("poly pizza api key is required",), "Poly Pizza needs an API key. Preferences → Online → Get Poly Pizza API Key."),
        (("poly pizza api key not set",), "Poly Pizza needs an API key. Preferences → Online → Get Poly Pizza API Key."),
        (("rate-limited", "rate limited", "http 429", " 429"), "This site asked us to slow down. Wait about a minute, then try again."),
        (
            ("http 401", "http error 401", "unauthorized"),
            "Sign-in rejected. UAL account: rotate API key on Profile and Connect. Fab/Epic: Preferences → Online.",
        ),
        (("http 403", "http error 403", "forbidden"), "This site blocked the request. Sign in (Preferences → Online) or try another asset."),
        (("http 404", "http error 404"), "This asset is no longer available on the site."),
        (("certificate_verify", "sslerror", "ssl cert", "ssl:"), MSG_SSL),
        (("timed out", "timeout", "temporarily unavailable"), MSG_NETWORK),
        (("connection refused", "urlopen error", "getaddrinfo", "name or service not known", "network is unreachable", "failed to establish"), MSG_NETWORK),
        (("no such file", "filenotfound", "[errno 2]"), MSG_NOT_FOUND),
        (("permission denied", "[errno 13]", "winerror 32", "being used by another"), MSG_PERMISSION),
        (("empty path", "produced no files", "primary file is missing"), MSG_EMPTY_DOWNLOAD),
        (("download returned empty",), MSG_EMPTY_DOWNLOAD),
        (("badzipfile", "not a zip", "zip slip", "illegal path"), MSG_ZIP),
        (("unsupported mesh format",), "Can't import this file type. Use FBX, glTF, OBJ, or USD."),
        (("unsupported source",), "This online source isn't available."),
        (("failed to load hdri",), "Couldn't load this HDRI. Try another file or format."),
        (("world node tree was not created",), "Couldn't set up World lighting for this HDRI."),
        (("download limit reached",), "Too many downloads at once (max 3). Wait or cancel one."),
        (("no downloadable", "download url is missing", "no download package", "no hdri download", "no mesh download", "no matching lazytextures download"), "This asset has no download for the selected size or format. Try another option."),
        (("cache collides", "inside the library"), "Cache folder is inside the library. Health Check will move it to UAL CACHE."),
        (("cache must not equal", "can't be the same folder"), "Cache and library can't be the same folder."),
        (("cache_dir",), "Cache folder needs a fix. Run Health Check in Preferences."),
        (("use download & import",), MSG_USE_DOWNLOAD),
        (("nothing to import",), "Nothing to drop — select an asset first."),
        (("asset disappeared", "missing on disk", "has no path"), "This file is missing. Rebuild Index or download it again."),
        (("protected path",), "Can't delete that — it's a library or cache folder, not an asset."),
        (("cache not writable",), "Can't write to the cache folder. Check folder permissions."),
        (("cache path is empty",), "Cache folder isn't set. Health Check will use UAL CACHE."),
        (("no library roots", "no library folder"), "No library folder set — pick one in Preferences → Library."),
        (("kwargs",), MSG_FAB_SIGNIN),
    )
    for needles, message in rules:
        if any(needle in lower for needle in needles):
            return message

    if _looks_technical(raw):
        return MSG_GENERIC_ERROR
    if len(raw) > _MAX_TOAST:
        return raw[: _MAX_TOAST - 1].rstrip() + "…"
    return raw


def friendly_status(message: str) -> str:
    """N-panel status line: keep progress copy, rewrite technical errors."""
    text = _fix_mojibake((message or "").strip()) or "Ready"
    replacements = (
        ("Download returned empty path", MSG_EMPTY_DOWNLOAD),
        ("Use Download & Import for online assets", MSG_USE_DOWNLOAD),
        ("Health check: paths OK — probing platforms…", "Folders look good — checking online sites…"),
        ("Health check: paths OK - probing platforms…", "Folders look good — checking online sites…"),
        ("Paths OK — checking platforms…", "Folders look good — checking online sites…"),
        ("Paths OK - checking platforms…", "Folders look good — checking online sites…"),
        ("Cache collides with library — Save will repair", "Cache folder is inside the library — Health Check will move it"),
        ("Cache must not equal library root", "Cache and library can't be the same folder"),
        ("Using library copy —", "Already in your library —"),
        ("Using library copy -", "Already in your library —"),
        ("Using cache copy —", "Using a cached download —"),
        ("Using cache copy -", "Using a cached download —"),
        ("Platform check already running…", "Already checking online sites…"),
        ("Platform check failed:", "Couldn't check online sites:"),
        (" need auth", " need sign-in"),
        ("Cache repaired →", "Moved cache to"),
        ("Pruned ", "Cleared "),
        ("old online thumb(s)", "old thumbnails"),
        ("Empty cache_dir → will use", "Empty cache folder — will use"),
        ("Cache collides with library → will use", "Cache was inside the library — now using"),
        ("No library roots configured", "No library folder set — pick one in Preferences → Library"),
        ("Blender online access is disabled", MSG_ONLINE_ACCESS),
        ("Cache and library on different drives (Save will be slower)", "Cache and library are on different drives — saving downloads will be slower"),
        ("probing platforms", "checking online sites"),
    )
    out = text
    for old, new in replacements:
        if old in out:
            out = out.replace(old, new)
    if _looks_technical(out):
        return friendly_error(out)
    if len(out) > _MAX_TOAST:
        return out[: _MAX_TOAST - 1].rstrip() + "…"
    return out
