# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""Online asset source interface."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class AssetResult:
    source_id: str
    asset_id: str
    name: str
    asset_type: str
    thumb_url: str = ""
    preview_url: str = ""
    license_name: str = ""
    attribution: str = ""
    categories: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    resolutions: List[str] = field(default_factory=list)
    formats: List[str] = field(default_factory=list)
    metadata: Dict[str, str] = field(default_factory=dict)


@dataclass
class SearchResults:
    items: List[AssetResult] = field(default_factory=list)
    has_more: bool = False
    total_count: int = 0


class AssetSource:
    source_id: str = "base"
    display_name: str = "Base Source"
    requires_token: bool = False

    def search(
        self,
        query: str,
        category: Optional[str] = None,
        page: int = 1,
        page_size: int = 50,
        **kwargs,
    ) -> SearchResults:
        raise NotImplementedError

    def get_resolutions(self, asset: AssetResult, fmt: Optional[str] = None) -> List[str]:
        return list(asset.resolutions)

    def get_formats(self, asset: AssetResult) -> List[str]:
        return list(asset.formats)

    def download(
        self,
        asset: AssetResult,
        resolution: Optional[str],
        fmt: Optional[str],
        dest_folder: str,
        **kwargs,
    ) -> str:
        raise NotImplementedError

    def get_type_filters(self) -> List[tuple]:
        return [("All types", "")]
