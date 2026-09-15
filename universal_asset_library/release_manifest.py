# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""Phase 10 — signed release manifest helpers (no bpy).

Canonical JSON must match ual-api ``src/releases/manifest.ts``.
Ed25519 verify uses the optional ``cryptography`` package when installed;
otherwise verification status is ``None`` (unknown) and update-check still works.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import urllib.parse
from typing import Any, Dict, List, Mapping, Optional, Tuple

# Pinned public key for ual-dev-1 (matches ual-api .env.example DEV keypair).
# Additional production pins + live fetch from GET /api/releases/manifest-key.
DEFAULT_MANIFEST_KEY_ID = "ual-dev-1"
DEFAULT_MANIFEST_PUBLIC_KEY_PEM = """-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEA85zxbXHYva/IwPNRS7zJ61JQ6bwiWE3XuoBsQMasLIY=
-----END PUBLIC KEY-----
"""

KNOWN_MANIFEST_PUBLIC_KEYS: Dict[str, str] = {
    DEFAULT_MANIFEST_KEY_ID: DEFAULT_MANIFEST_PUBLIC_KEY_PEM,
    "ual-2026-08": """-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEAAcIDMjchT/bGXRi0wZIZXXj/04vSq8u0bGkPK1j7+Y0=
-----END PUBLIC KEY-----
""",
}

_manifest_key_cache: Dict[str, Dict[str, str]] = {}

PRODUCTION_UPDATES_HOSTS = frozenset(
    {
        "universalassetlibrary.com",
        "www.universalassetlibrary.com",
    }
)
ALLOW_UNSIGNED_RELEASE_ENV = "ALLOW_UNSIGNED_RELEASE"


def _b64url_decode(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def cryptography_available() -> bool:
    try:
        from cryptography.hazmat.primitives.serialization import load_pem_public_key  # noqa: F401

        return True
    except ImportError:
        return False


def allow_unsigned_release_override() -> bool:
    return os.environ.get(ALLOW_UNSIGNED_RELEASE_ENV, "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def is_production_updates_api_base(api_base: str) -> bool:
    """True for the public website origin — not localhost or direct ual-api dev ports."""
    base = str(api_base or "").strip()
    if not base:
        return False
    parsed = urllib.parse.urlparse(base if "://" in base else "https://" + base)
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return False
    if host in ("localhost", "127.0.0.1", "::1"):
        return False
    if "ual-api" in host:
        return False
    if ":4000" in base or ":4010" in base:
        return False
    return host in PRODUCTION_UPDATES_HOSTS


def resolve_require_signed_update_check(
    *,
    api_base: str = "",
    channel: str = "stable",
) -> bool:
    """Fail closed on unsigned/invalid manifests for production update checks."""
    _ = channel
    if allow_unsigned_release_override():
        return False
    if not cryptography_available():
        return False
    return is_production_updates_api_base(api_base)


def resolve_require_release_sha256(*, api_base: str = "") -> bool:
    """Fail closed when release metadata omits sha256 (production installs)."""
    if allow_unsigned_release_override():
        return False
    return is_production_updates_api_base(api_base)


def normalize_api_base(raw: str) -> str:
    return str(raw or "").strip().rstrip("/")


def resolve_manifest_key_endpoint(api_base: str) -> Optional[str]:
    base = normalize_api_base(api_base)
    if not base:
        return None
    lower = base.lower()
    if lower.endswith("/api/v1"):
        return f"{base}/releases/manifest-key"
    if lower.endswith("/api"):
        return f"{base}/v1/releases/manifest-key"
    if "ual-api" in lower or ":4000" in lower or ":4010" in lower:
        return f"{base}/api/v1/releases/manifest-key"
    return f"{base}/api/releases/manifest-key"


def fetch_manifest_public_key(
    api_base: str,
    *,
    timeout: float = 10.0,
) -> Optional[Tuple[str, str]]:
    base = normalize_api_base(api_base)
    if not base:
        return None
    cached = _manifest_key_cache.get(base)
    if cached and cached.get("publicKey"):
        return cached.get("keyId", DEFAULT_MANIFEST_KEY_ID), cached["publicKey"]

    endpoint = resolve_manifest_key_endpoint(base)
    if not endpoint:
        return None
    try:
        import urllib.request

        from .http_ssl import urlopen as ssl_urlopen

        request = urllib.request.Request(
            endpoint,
            headers={"Accept": "application/json", "User-Agent": "UAL-Blender/ManifestKey"},
        )
        with ssl_urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="ignore")
        payload = json.loads(raw) if raw else {}
        if not isinstance(payload, dict):
            return None
        key_id = str(payload.get("keyId") or DEFAULT_MANIFEST_KEY_ID).strip()
        public_key = str(payload.get("publicKey") or "").strip()
        if not public_key:
            return None
        _manifest_key_cache[base] = {"keyId": key_id, "publicKey": public_key}
        return key_id, public_key
    except Exception:
        return None


def resolve_public_keys_for_verify(
    manifest: Optional[Mapping[str, Any]],
    *,
    api_base: str = "",
    public_key_pem: str = DEFAULT_MANIFEST_PUBLIC_KEY_PEM,
) -> List[str]:
    keys: List[str] = []
    key_id = ""
    if manifest:
        key_id = str(manifest.get("keyId") or "").strip()
    if key_id and key_id in KNOWN_MANIFEST_PUBLIC_KEYS:
        keys.append(KNOWN_MANIFEST_PUBLIC_KEYS[key_id])
    fetched = fetch_manifest_public_key(api_base) if api_base else None
    if fetched:
        _, pem = fetched
        keys.append(pem)
    keys.append(public_key_pem)
    for pem in KNOWN_MANIFEST_PUBLIC_KEYS.values():
        keys.append(pem)
    out: List[str] = []
    seen = set()
    for pem in keys:
        norm = pem.strip()
        if not norm or norm in seen:
            continue
        seen.add(norm)
        out.append(norm)
    return out


def verify_manifest_signature_any(
    manifest: Mapping[str, Any],
    signature: str,
    *,
    public_keys: List[str],
) -> Optional[bool]:
    if not signature or not manifest:
        return False
    if not cryptography_available():
        return None
    if not public_keys:
        return verify_manifest_signature(manifest, signature)
    saw_false = False
    for pem in public_keys:
        result = verify_manifest_signature(manifest, signature, public_key_pem=pem)
        if result is True:
            return True
        if result is False:
            saw_false = True
    return False if saw_false else None


def canonical_manifest_json(body: Mapping[str, Any]) -> str:
    """Sorted-key JSON, no spaces — must match Node canonicalManifestBytes."""
    ordered = {
        "channel": str(body.get("channel") or "stable"),
        "compatibility": str(body.get("compatibility") or ""),
        "downloadUrl": body.get("downloadUrl"),
        "filename": str(body.get("filename") or ""),
        "keyId": str(body.get("keyId") or DEFAULT_MANIFEST_KEY_ID),
        "platform": str(body.get("platform") or "any"),
        "product": str(body.get("product") or ""),
        "sha256": body.get("sha256"),
        "version": str(body.get("version") or ""),
    }
    return json.dumps(ordered, separators=(",", ":"), ensure_ascii=False)


def extract_manifest_from_payload(payload: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    release = payload.get("release")
    if isinstance(release, dict) and isinstance(release.get("manifest"), dict):
        return dict(release["manifest"])
    if isinstance(payload.get("manifest"), dict):
        return dict(payload["manifest"])
    return None


def extract_signature_from_payload(payload: Mapping[str, Any]) -> str:
    release = payload.get("release")
    if isinstance(release, dict) and release.get("signature"):
        return str(release.get("signature") or "").strip()
    return str(payload.get("signature") or "").strip()


def verify_manifest_signature(
    manifest: Mapping[str, Any],
    signature: str,
    *,
    public_key_pem: str = DEFAULT_MANIFEST_PUBLIC_KEY_PEM,
) -> Optional[bool]:
    """Return True/False when cryptography is available; None if verify unavailable."""
    if not signature or not manifest:
        return False
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        from cryptography.hazmat.primitives.serialization import load_pem_public_key
    except ImportError:
        return None

    try:
        key = load_pem_public_key(public_key_pem.encode("utf-8"))
        if not isinstance(key, Ed25519PublicKey):
            return False
        message = canonical_manifest_json(manifest).encode("utf-8")
        key.verify(_b64url_decode(signature), message)
        return True
    except Exception:
        return False


def evaluate_update_manifest(
    payload: Mapping[str, Any],
    *,
    public_key_pem: str = DEFAULT_MANIFEST_PUBLIC_KEY_PEM,
    require_signed: bool = False,
    api_base: str = "",
) -> Dict[str, Any]:
    """Inspect update-check JSON for Phase 10 signature trust.

    Returns keys: signed, verified (True/False/None), sha256, message_suffix.
    """
    manifest = extract_manifest_from_payload(payload)
    signature = extract_signature_from_payload(payload)
    release = payload.get("release") if isinstance(payload.get("release"), dict) else {}
    signed_flag = bool(
        (isinstance(release, dict) and release.get("signed"))
        or payload.get("signed")
        or signature
    )
    sha256 = None
    if manifest and manifest.get("sha256"):
        sha256 = str(manifest.get("sha256"))
    elif isinstance(release, dict):
        arts = release.get("artifacts") or []
        if arts and isinstance(arts[0], dict) and arts[0].get("sha256"):
            sha256 = str(arts[0]["sha256"])

    if not signature or not manifest:
        verified = False if require_signed else None
    else:
        public_keys = resolve_public_keys_for_verify(
            manifest,
            api_base=api_base,
            public_key_pem=public_key_pem,
        )
        verified = verify_manifest_signature_any(
            manifest,
            signature,
            public_keys=public_keys,
        )

    suffix = ""
    if verified is True:
        suffix = " Manifest signature OK."
    elif verified is False and signature:
        suffix = " Manifest signature invalid."
    elif verified is None and signed_flag:
        suffix = " Manifest signed (local verify unavailable)."
    elif require_signed and not signature:
        suffix = " Signed manifest required."

    return {
        "signed": signed_flag,
        "verified": verified,
        "sha256": sha256,
        "message_suffix": suffix,
        "reject": bool(require_signed and verified is not True),
    }


def sha256_file(path: str, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def verify_file_sha256(path: str, expected_hex: str) -> bool:
    expected = str(expected_hex or "").strip().lower()
    if not expected:
        return False
    return sha256_file(path).lower() == expected
