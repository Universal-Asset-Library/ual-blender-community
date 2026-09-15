# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""HTTPS helpers with optional vendored CA bundle."""

from __future__ import annotations

import os
import ssl
import urllib.request
from typing import Optional


def create_ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ca = os.path.join(os.path.dirname(__file__), "data", "cacert.pem")
    if os.path.isfile(ca):
        try:
            ctx.load_verify_locations(ca)
        except Exception:
            pass
    return ctx


def urlopen(request, timeout: Optional[float] = 30):
    return urllib.request.urlopen(request, timeout=timeout, context=create_ssl_context())
