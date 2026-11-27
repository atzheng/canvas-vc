"""Per-request view of the repo (CONTRACTS.md section 13). Read-only."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from canvas_sync import gitops
from canvas_sync.errors import CanvasSyncError
from canvas_sync.model.course import Course, load_tree


@dataclass(frozen=True)
class State:
    root: Path
    course: Course | None
    load_error: str | None
    head: str | None
    remote: str | None  # canvas/main commit; None = never fetched

    @classmethod
    def load(cls, root: Path) -> State:
        course: Course | None = None
        err: str | None = None
        try:
            course = load_tree(root)
        except (CanvasSyncError, OSError, UnicodeError, ValueError) as e:
            err = " ".join(str(e).split()) or type(e).__name__
        return cls(root, course, err, gitops.head_commit(root), gitops.ref_commit(root))

    def remote_course(self) -> Course | None:
        if self.remote is None:
            return None
        return gitops.load_course_at(self.root, self.remote)
