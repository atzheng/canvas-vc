"""Course aggregate, load_tree and dump_tree (CONTRACTS.md section 4)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from canvas_sync.config import CourseConfig
from canvas_sync.model.base import CanvasObject, ObjectKey
from canvas_sync.model.files import FilesMeta
from canvas_sync.model.modules import Module
from canvas_sync.model.roster import Roster


@dataclass(frozen=True)
class Course:
    config: CourseConfig
    objects: dict[ObjectKey, CanvasObject]  # pages, assignments, announcements, files (FileEntry)
    files_meta: FilesMeta
    modules: list[Module]  # order matters
    roster: Roster


def load_tree(root: Path) -> Course:
    raise NotImplementedError


def dump_tree(course: Course) -> dict[str, bytes]:
    """Path -> exact file contents."""
    raise NotImplementedError
