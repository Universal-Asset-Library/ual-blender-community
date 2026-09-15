# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""Shared download + ZIP extract helpers (hardened; no blind extractall)."""

from __future__ import annotations

import os
import zipfile
from typing import Callable, Dict, List, Optional, Sequence

from .download_url_policy import (
    assert_allowed_download_url,
    open_allowed_download,
    resolve_download_max_bytes,
)
import urllib.request

USER_AGENT = "UniversalAssetLibrary-Blender/0.1"
DOWNLOAD_CHUNK_SIZE = 1024 * 1024
ZIP_MAX_MEMBER_COUNT = 10_000
ZIP_MAX_UNCOMPRESSED_BYTES = 8 * 1024 * 1024 * 1024


class DownloadCancelled(Exception):
    pass


def stream_download_file(
    url: str,
    output_path: str,
    *,
    timeout: float = 120,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
    raise_on_error: bool = True,
    extra_headers: Optional[Dict[str, str]] = None,
    max_retries: int = 3,
    source_id: Optional[str] = None,
    download_class: Optional[str] = None,
    max_bytes: Optional[int] = None,
) -> bool:
    """Download ``url`` to ``output_path`` with cancel, partial cleanup, and retries.

    Retries use exponential backoff (0.5s, 1s, 2s). Partial files are deleted
    on cancel/error so a corrupt half-file is never treated as valid cache.
    """
    from . import download_progress
    import time

    byte_cap = resolve_download_max_bytes(download_class, max_bytes)
    try:
        assert_allowed_download_url(url, source_id, download_class=download_class)
    except RuntimeError:
        if raise_on_error:
            raise
        return False

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    headers = {"User-Agent": USER_AGENT}
    if extra_headers:
        headers.update(extra_headers)

    def _cancelled() -> bool:
        if should_cancel and should_cancel():
            return True
        return download_progress.is_cancelled()

    def _emit(done: int, total: int) -> None:
        tid = download_progress.get_active_task_id()
        if tid:
            download_progress.update_bytes(tid, done, total)
        if progress_callback:
            progress_callback(done, total)

    def _cleanup() -> None:
        if os.path.isfile(output_path):
            try:
                os.remove(output_path)
            except OSError:
                pass

    last_exc: Optional[BaseException] = None
    attempts = max(1, int(max_retries))
    for attempt in range(attempts):
        if _cancelled():
            _cleanup()
            raise DownloadCancelled()
        request = urllib.request.Request(url, headers=headers)
        try:
            with open_allowed_download(
                request,
                source_id=source_id,
                download_class=download_class,
                timeout=timeout,
            ) as response:
                total = int(response.headers.get("Content-Length") or 0)
                if byte_cap and total > byte_cap:
                    raise RuntimeError(
                        "Download exceeds maximum size ({} bytes).".format(byte_cap)
                    )
                done = 0
                with open(output_path, "wb") as handle:
                    while True:
                        if _cancelled():
                            raise DownloadCancelled()
                        chunk = response.read(DOWNLOAD_CHUNK_SIZE)
                        if not chunk:
                            break
                        handle.write(chunk)
                        done += len(chunk)
                        if byte_cap and done > byte_cap:
                            raise RuntimeError(
                                "Download exceeds maximum size ({} bytes).".format(byte_cap)
                            )
                        _emit(done, total or -1)
            return True
        except DownloadCancelled:
            _cleanup()
            raise
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            _cleanup()
            if attempt + 1 >= attempts:
                break
            time.sleep(0.5 * (2 ** attempt))
    if raise_on_error and last_exc is not None:
        raise last_exc
    return False


def looks_like_zip(path: str) -> bool:
    if not os.path.isfile(path):
        return False
    try:
        with open(path, "rb") as handle:
            return handle.read(4) == b"PK\x03\x04"
    except OSError:
        return False


def ensure_zip_archive_path(path: str) -> str:
    """Return path usable as ZIP even if misnamed (magic-byte sniff)."""
    if path.lower().endswith(".zip") and zipfile.is_zipfile(path):
        return path
    if looks_like_zip(path) or zipfile.is_zipfile(path):
        return path
    raise zipfile.BadZipFile(f"Not a ZIP archive: {path}")


def _safe_member_path(dest_dir: str, member_name: str) -> Optional[str]:
    cleaned = member_name.replace("\\", "/")
    if cleaned.startswith("/") or cleaned.startswith("../") or "/../" in cleaned:
        return None
    target = os.path.normpath(os.path.join(dest_dir, cleaned))
    base = os.path.normpath(dest_dir)
    try:
        if os.path.commonpath([base, target]) != base:
            return None
    except ValueError:
        return None
    return target


def _is_symlink_member(info: zipfile.ZipInfo) -> bool:
    """True for Unix symlink / Windows reparse ZIP members (Zip-Slip vector)."""
    # Unix: upper 4 bits of external_attr >> 16; 0o120000 = symlink
    mode = (info.external_attr >> 16) & 0xFFFF
    if mode & 0o170000 == 0o120000:
        return True
    # Some archives mark links via create_system + MS-DOS attributes
    if info.external_attr & 0x40000000:  # FILE_ATTRIBUTE_REPARSE_POINT-ish
        return True
    name = (info.filename or "").replace("\\", "/")
    # Soft heuristic: empty payload with trailing slash already filtered; skip
    # members that claim to be links via known extensions used by malware samples
    if name.endswith(".lnk") and info.file_size == 0:
        return True
    return False


def extract_zip_members(
    archive_path: str,
    dest_dir: str,
    *,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> List[str]:
    """Native ZipFile.extract per member + Zip-Slip / symlink / cancel guards."""
    from . import download_progress

    archive_path = ensure_zip_archive_path(archive_path)
    os.makedirs(dest_dir, exist_ok=True)
    extracted: List[str] = []
    tid = download_progress.get_active_task_id()
    if tid:
        download_progress.set_phase(tid, "extract", progress=0.0)

    def _cancelled() -> bool:
        if should_cancel and should_cancel():
            return True
        return download_progress.is_cancelled()

    with zipfile.ZipFile(archive_path, "r") as zf:
        members = [m for m in zf.infolist() if not m.is_dir()]
        total = len(members) or 1
        if len(members) > ZIP_MAX_MEMBER_COUNT:
            raise RuntimeError(
                "ZIP archive has too many files ({}; limit {}).".format(
                    len(members),
                    ZIP_MAX_MEMBER_COUNT,
                )
            )
        total_uncompressed = sum(int(m.file_size or 0) for m in members)
        if total_uncompressed > ZIP_MAX_UNCOMPRESSED_BYTES:
            raise RuntimeError(
                "ZIP archive is too large to extract safely ({} bytes).".format(
                    total_uncompressed
                )
            )
        for index, info in enumerate(members):
            if _cancelled():
                raise DownloadCancelled()
            if _is_symlink_member(info):
                continue
            member_size = int(info.file_size or 0)
            if member_size > ZIP_MAX_UNCOMPRESSED_BYTES:
                raise RuntimeError("ZIP member is too large to extract safely.")
            safe = _safe_member_path(dest_dir, info.filename)
            if safe is None:
                continue
            # Never follow an existing symlink at the destination
            if os.path.islink(safe):
                try:
                    os.unlink(safe)
                except OSError:
                    continue
            os.makedirs(os.path.dirname(safe) or dest_dir, exist_ok=True)
            with zf.open(info) as src, open(safe, "wb") as dst:
                while True:
                    chunk = src.read(DOWNLOAD_CHUNK_SIZE)
                    if not chunk:
                        break
                    dst.write(chunk)
            extracted.append(safe)
            if tid:
                download_progress.update_fraction(tid, index + 1, total)
            if progress_callback:
                progress_callback(index + 1, total)
    return extracted


def download_urls_parallel(
    jobs: Sequence[tuple],
    *,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    max_workers: int = 6,
    timeout: float = 300,
    source_id: Optional[str] = None,
    download_class: Optional[str] = None,
    max_bytes: Optional[int] = None,
) -> List[str]:
    """Download (url, path) pairs using a thread pool for real parallelism."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    if not jobs:
        return []
    total = len(jobs)
    done_paths: List[str] = []
    done_count = 0

    def _fetch(url_path: tuple) -> str:
        url, path = url_path
        stream_download_file(
            url,
            path,
            timeout=timeout,
            source_id=source_id,
            download_class=download_class,
            max_bytes=max_bytes,
        )
        return path

    if total == 1:
        stream_download_file(
            jobs[0][0],
            jobs[0][1],
            timeout=timeout,
            source_id=source_id,
            download_class=download_class,
            max_bytes=max_bytes,
        )
        if progress_callback:
            progress_callback(1, 1)
        return [jobs[0][1]]

    with ThreadPoolExecutor(max_workers=min(max_workers, total)) as pool:
        futures = {pool.submit(_fetch, job): job for job in jobs}
        for future in as_completed(futures):
            path = future.result()
            done_paths.append(path)
            done_count += 1
            if progress_callback:
                progress_callback(done_count, total)
    return done_paths
