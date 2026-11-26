"""Page model (CONTRACTS.md section 4). Stub: load and dump nothing yet."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from canvas_sync.config import CourseConfig
from canvas_sync.dates import LocalDT
from canvas_sync.model.base import CanvasObject, Kind

KEY_ORDER = ["canvas_id", "title", "published", "publish_at", "front_page", "body_format"]


class Page(CanvasObject):
    title: str = ""
    published: bool | None = None
    publish_at: LocalDT | None = None
    front_page: bool = False
    body_format: Literal["md", "html"] = "md"
    body: str = ""


class _Spec:
    kind = Kind.PAGE
    folder = "pages"

    def load(self, root: Path, cfg: CourseConfig) -> dict[str, CanvasObject]:
        return {}

    def dump(self, objs: dict[str, CanvasObject], cfg: CourseConfig) -> dict[str, bytes]:
        return {}


PAGE_SPEC = _Spec()
