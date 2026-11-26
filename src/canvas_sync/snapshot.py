"""Course -> commit on canvas/main (CONTRACTS.md section 11)."""

from __future__ import annotations

from pathlib import Path

from canvas_sync.model.course import Course
from canvas_sync.render.bodies import Bodies


class BlobStore:
    """Content-addressed file store in .git/canvas-blobs/."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def put(self, content: bytes) -> str:
        raise NotImplementedError

    def get(self, sha256: str) -> bytes:
        raise NotImplementedError

    def path_for(self, sha256: str) -> Path:
        raise NotImplementedError


def snapshot(
    root: Path,
    course: Course,
    bodies: Bodies,
    blobs: BlobStore,
    message: str,
    extra_parent: str | None = None,
) -> str | None:
    raise NotImplementedError
