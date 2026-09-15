"""Redact sensitive values from opt-in support / diagnostics payloads."""

from __future__ import annotations

import re
from typing import Any

_PATH_RE = re.compile(r"(?:[A-Za-z]:\\|/)[^\s\"']+")
_UALAK_RE = re.compile(r"\bualak_[A-Za-z0-9_-]+\b")
_BEARER_RE = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+\b", re.IGNORECASE)
_TOKEN_KV_RE = re.compile(
    r"(?i)(api[_-]?token|access[_-]?token|refresh[_-]?token|session[_-]?id|"
    r"csrf[_-]?token|fab_sessionid|fab_csrftoken|sketchfab[_-]?token|"
    r"polypizza[_-]?token)\s*[:=]\s*['\"]?[^'\"\s,}]+"
)
_QUERY_SECRET_RE = re.compile(r"(?i)([?&](?:key|token|api_key|apikey)=)[^&\s]+")


def redact_support_text(text: str, *, max_len: int = 40_000) -> str:
    t = str(text or "")
    t = _UALAK_RE.sub("ualak_<redacted>", t)
    t = _BEARER_RE.sub("Bearer <redacted>", t)
    t = _TOKEN_KV_RE.sub(r"\1<redacted>", t)
    t = _QUERY_SECRET_RE.sub(r"\1<redacted>", t)
    t = _PATH_RE.sub("<path>", t)
    if len(t) > max_len:
        return t[:max_len]
    return t


def redact_diagnostics_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): redact_diagnostics_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_diagnostics_value(v) for v in value]
    if isinstance(value, str):
        return redact_support_text(value)
    return value


def format_logged_error(exc: BaseException, *, label: str) -> str:
    """Redact secrets/paths from worker/timer exceptions before console print."""
    message = redact_support_text(str(exc))
    token = str(label or "error").strip() or "error"
    return "[UAL] {}: {}".format(token, message)
