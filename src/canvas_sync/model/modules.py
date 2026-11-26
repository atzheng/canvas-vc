"""Module model (CONTRACTS.md section 4). Stub: load and dump nothing yet."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from canvas_sync.config import CourseConfig
from canvas_sync.dates import LocalDT
from canvas_sync.model.base import CanvasObject, Kind
from canvas_sync.model.overrides import Override

KEY_ORDER = ["canvas_id", "name", "published", "unlock", "overrides", "items"]
ITEM_KEY_ORDER = [
    "canvas_id",
    "page",
    "assignment",
    "file",
    "header",
    "url",
    "quiz",
    "discussion",
    "title",
    "indent",
    "new_tab",
    "published",
]


class UnmanagedRef(BaseModel):
    """Quiz or discussion item: an object canvas-sync does not manage."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    canvas_id: int


class ModuleItem(BaseModel):
    """Exactly one of page, assignment, file, header, url, quiz, discussion is set."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    page: str | None = None
    assignment: str | None = None
    file: str | None = None
    header: str | None = None
    url: str | None = None
    quiz: UnmanagedRef | None = None
    discussion: UnmanagedRef | None = None
    title: str | None = None
    indent: int = 0
    new_tab: bool = False
    published: bool | None = None
    canvas_id: int | None = None


class Module(CanvasObject):
    name: str = ""
    published: bool | None = None
    unlock: LocalDT | None = None
    overrides: list[Override] = []  # unlock only
    items: list[ModuleItem] = []


class _Spec:
    kind = Kind.MODULE
    folder = "modules"

    def load(self, root: Path, cfg: CourseConfig) -> dict[str, CanvasObject]:
        return {}

    def dump(self, objs: dict[str, CanvasObject], cfg: CourseConfig) -> dict[str, bytes]:
        return {}


MODULE_SPEC = _Spec()
