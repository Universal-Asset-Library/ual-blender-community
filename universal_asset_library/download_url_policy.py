# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""HTTPS download URL allowlists for online connectors and release updates."""

from __future__ import annotations

import os
import urllib.error
import urllib.parse
import urllib.request

MAX_REDIRECT_HOPS = 5

# Fab / Megascans EGL and legacy CDN hosts (see docs/fab-integration.md).
_FAB_DOWNLOAD_HOST_SUFFIXES = (
    "fab.com",
    "blob.core.windows.net",
    "quixel.com",
    "epicgamescdn.com",
    "on.epicgames.com",
)

SOURCE_HOST_SUFFIXES = {
    "polyhaven": ("polyhaven.com", "polyhaven.org"),
    # Thumbnails + ZIP CDN live on struffelproductions.com (ambientcg.com redirects).
    "ambientcg": ("ambientcg.com", "struffelproductions.com"),
    "gpuopen": ("gpuopen.com",),
    "lazytextures": ("lazytextures.net", "lazytextures.com"),
    "threedscans": ("threedscans.com",),
    "pbrpx": ("pbrpx.com",),
    "polypizza": ("poly.pizza",),
    "sketchfab": ("sketchfab.com", "amazonaws.com"),
    "fab": _FAB_DOWNLOAD_HOST_SUFFIXES,
    "megascans": _FAB_DOWNLOAD_HOST_SUFFIXES,
    "quixel": _FAB_DOWNLOAD_HOST_SUFFIXES,
}

UPDATE_HOST_SUFFIXES = (
    "github.com",
    "githubusercontent.com",
    "universalassetlibrary.com",
)


def _extra_suffixes() -> tuple[str, ...]:
    raw = os.environ.get("UAL_DOWNLOAD_ALLOWLIST_EXTRA", "").strip()
    if not raw:
        return ()
    return tuple(
        part.strip().lower()
        for part in raw.replace(";", ",").split(",")
        if part.strip()
    )


def _host_matches(host: str, suffixes: tuple[str, ...]) -> bool:
    host = host.strip().lower()
    if not host:
        return False
    for suffix in suffixes:
        token = suffix.strip().lower()
        if not token:
            continue
        if host == token or host.endswith("." + token):
            return True
    return False


def allowed_host_suffixes(
    source_id: str | None = None,
    *,
    download_class: str | None = None,
) -> tuple[str, ...]:
    extra = _extra_suffixes()
    if download_class == "update":
        return UPDATE_HOST_SUFFIXES + extra
    if source_id:
        key = str(source_id or "").strip().lower()
        specific = SOURCE_HOST_SUFFIXES.get(key)
        if specific:
            return tuple(specific) + extra
    union = set(UPDATE_HOST_SUFFIXES)
    for suffixes in SOURCE_HOST_SUFFIXES.values():
        union.update(suffixes)
    union.update(extra)
    return tuple(sorted(union))


def is_allowed_download_url(
    url: str,
    source_id: str | None = None,
    *,
    download_class: str | None = None,
) -> bool:
    try:
        parsed = urllib.parse.urlparse(str(url or "").strip())
    except (ValueError, AttributeError, TypeError):
        return False
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return False
    if parsed.username or parsed.password:
        return False
    if download_class == "update" and host in ("localhost", "127.0.0.1"):
        return parsed.scheme in ("http", "https")
    if host in ("localhost", "127.0.0.1", "::1"):
        if os.environ.get("PYTEST_CURRENT_TEST"):
            return parsed.scheme in ("http", "https")
    if parsed.scheme != "https":
        return False
    suffixes = allowed_host_suffixes(source_id, download_class=download_class)
    return _host_matches(host, suffixes)


def assert_allowed_download_url(
    url: str,
    source_id: str | None = None,
    *,
    download_class: str | None = None,
) -> None:
    if is_allowed_download_url(url, source_id, download_class=download_class):
        return
    host = ""
    try:
        host = urllib.parse.urlparse(url).hostname or ""
    except (ValueError, AttributeError, TypeError):
        pass
    where = " ({})".format(host) if host else ""
    raise RuntimeError("Download blocked: URL host is not allowlisted{}.".format(where))


def resolve_download_max_bytes(
    download_class: str | None = None,
    max_bytes: int | None = None,
) -> int | None:
    if max_bytes is not None:
        return int(max_bytes) if int(max_bytes) > 0 else None
    if download_class == "thumb":
        return 32 * 1024 * 1024
    if download_class == "update":
        return 2 * 1024 * 1024 * 1024
    if download_class in (None, "asset"):
        return 6 * 1024 * 1024 * 1024
    return 6 * 1024 * 1024 * 1024


class _AllowlistedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-check download allowlist on every HTTP redirect hop."""

    def __init__(
        self,
        source_id: str | None,
        download_class: str | None,
        hop_state: list[int],
    ) -> None:
        super().__init__()
        self._source_id = source_id
        self._download_class = download_class
        self._hop_state = hop_state

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        self._hop_state[0] += 1
        if self._hop_state[0] > MAX_REDIRECT_HOPS:
            raise urllib.error.HTTPError(
                req.full_url,
                code,
                "Too many redirects",
                headers,
                fp,
            )
        assert_allowed_download_url(
            newurl,
            self._source_id,
            download_class=self._download_class,
        )
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None:
            old_host = (urllib.parse.urlparse(req.full_url).hostname or "").lower()
            new_host = (urllib.parse.urlparse(newurl).hostname or "").lower()
            if old_host and new_host and old_host != new_host:
                for header in ("Authorization", "Cookie", "X-Auth-Token"):
                    if header in redirected.headers:
                        del redirected.headers[header]
                    unredirected = getattr(redirected, "unredirected_hdrs", None)
                    if isinstance(unredirected, dict) and header in unredirected:
                        del unredirected[header]
        return redirected


def open_allowed_download(
    request: urllib.request.Request,
    *,
    source_id: str | None = None,
    download_class: str | None = None,
    timeout: float | int = 30,
):
    """Open *request* with TLS and allowlist checks on the initial URL and redirects."""
    from .http_ssl import create_ssl_context

    assert_allowed_download_url(
        request.full_url,
        source_id,
        download_class=download_class,
    )
    hop_state = [0]
    handler = _AllowlistedRedirectHandler(source_id, download_class, hop_state)
    https_handler = urllib.request.HTTPSHandler(context=create_ssl_context())
    http_handler = urllib.request.HTTPHandler()
    opener = urllib.request.build_opener(handler, https_handler, http_handler)
    return opener.open(request, timeout=timeout)
