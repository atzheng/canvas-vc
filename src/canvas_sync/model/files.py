"""File metadata model (CONTRACTS.md section 4). Stub: load and dump nothing yet."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from canvas_sync.config import CourseConfig
from canvas_sync.dates import LocalDT
from canvas_sync.model.base import CanvasObject, Kind

KEY_ORDER = ["canvas_id", "path", "sha256", "published", "hidden", "unlock", "lock", "stable_link"]


class FileEntry(CanvasObject):
    path: str = ""
    sha256: str = ""
    published: bool | None = None
    hidden: bool | None = None
    unlock: LocalDT | None = None
    lock: LocalDT | None = None
    stable_link: bool = False


class FileSettings(BaseModel):
    """Effective settings for one file after defaults, rules and its own entry."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    published: bool | None = None
    hidden: bool | None = None
    unlock: LocalDT | None = None
    lock: LocalDT | None = None


class FileRule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    match: str = ""
    settings: FileSettings = FileSettings()


class FilesMeta(BaseModel):
    """Contents of files/_meta.yaml."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    defaults: FileSettings = FileSettings()
    rules: list[FileRule] = []
    files: dict[str, FileEntry] = {}

    def effective(self, path: str) -> FileSettings:
        raise NotImplementedError


class _Spec:
    kind = Kind.FILE
    folder = "files"

    def load(self, root: Path, cfg: CourseConfig) -> dict[str, CanvasObject]:
        return {}

    def dump(self, objs: dict[str, CanvasObject], cfg: CourseConfig) -> dict[str, bytes]:
        return {}


FILE_SPEC = _Spec()
