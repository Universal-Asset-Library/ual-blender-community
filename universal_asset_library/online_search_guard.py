# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""Guards against stale Online Search callbacks after a source switch.

Search runs on a background thread. If the user switches Poly Haven ← Three D Scans
while a search is in flight, the old ``done()`` must not refill the grid.
"""

from __future__ import annotations

from typing import Any


def online_search_still_current(ui: Any, search_gen: int, source_id: str) -> bool:
    """True if a background Search/Load More still matches the live Online source."""
    try:
        if int(getattr(ui, "online_search_gen", 0) or 0) != int(search_gen):
            return False
        if str(getattr(ui, "online_source", "") or "") != str(source_id or ""):
            return False
    except Exception:
        return False
    return True
