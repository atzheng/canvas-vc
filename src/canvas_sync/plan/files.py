"""Differ for files (CONTRACTS.md section 9).

STOP-GAP until bead orx.5 (files differ and writers): this differ emits no
operations. When base and target agree it returns ``[]``; when anything about
a file differs it raises one ``ValidationError`` listing the paths, so file
changes are never silently dropped and no file op (least of all a DELETE) is
ever planned. orx.5 replaces the body of ``diff_type``; keep its signature.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from canvas_sync.errors import ValidationError

if TYPE_CHECKING:
    from canvas_sync.model.course import Course
    from canvas_sync.plan.core import Operation


def _changed_paths(base: Course, target: Course) -> list[str]:
    """Tree paths whose hash, effective settings or stable_link differ, or that
    exist on one side only. canvas_id and settings-neutral edits to
    ``_meta.yaml`` do not count."""
    b, t = base.files_meta, target.files_meta
    changed: list[str] = []
    for path in sorted(set(b.files) | set(t.files)):
        old, new = b.files.get(path), t.files.get(path)
        if (
            old is None
            or new is None
            or old.sha256 != new.sha256
            or old.stable_link != new.stable_link
            or b.effective(path) != t.effective(path)
        ):
            changed.append(path)
    return changed


def diff_type(base: Course, target: Course) -> list[Operation]:
    changed = _changed_paths(base, target)
    if changed:
        raise ValidationError(
            "\n".join(f"{p}: pushing file changes is not implemented yet" for p in changed)
        )
    return []
