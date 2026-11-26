"""Kind, ObjectKey, CanvasObject (CONTRACTS.md section 4)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from canvas_sync.config import CourseConfig


class Kind(StrEnum):
    PAGE = "page"
    ASSIGNMENT = "assignment"
    ANNOUNCEMENT = "announcement"
    FILE = "file"
    MODULE = "module"


@dataclass(frozen=True, order=True)
class ObjectKey:
    kind: Kind
    path: str  # tree path, e.g. "assignments/hw1.md"; modules: "modules.yaml#<name>"


class CanvasObject(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    canvas_id: int | str | None = None  # pages use their URL slug (str)

    def key(self, path: str) -> ObjectKey:
        raise NotImplementedError


class TypeSpec(Protocol):
    """One per type, registered in model/__init__.py."""

    kind: Kind
    folder: str  # "pages", "assignments", ...

    def load(self, root: Path, cfg: CourseConfig) -> dict[str, CanvasObject]: ...

    def dump(self, objs: dict[str, CanvasObject], cfg: CourseConfig) -> dict[str, bytes]: ...
