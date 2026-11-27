"""Differ for assignment groups and assignments (CONTRACTS.md sections 9 and 10).

``diff_type(base, target)`` compares the assignments of the last Canvas snapshot
(``base``) with the working tree (``target``). Objects are matched by ``canvas_id``
first; an id-less target file matches a base object at the same path.

Operations emitted, in apply order:

- ``CREATE`` of an :class:`AssignmentGroup` for each group name that no assignment of
  ``base`` uses (the writer reuses an existing Canvas group of that name instead of
  duplicating it). Groups are **never deleted or renamed** by this tool.
- ``CREATE`` of an assignment without a ``canvas_id``.
- ``UPDATE`` when a matched pair differs; ``Operation.changes`` lists the fields.
- ``DELETE`` of a base assignment that no target file carries. The differ cannot
  know whether it has submissions; the guard (``ApplyContext.guard``) refuses the
  delete at apply time if it does, before anything is written.

A target ``canvas_id`` missing from ``base`` produces **no** operation: the object was
deleted on Canvas, or Canvas turned it into a quiz, discussion or external tool, which
the reader does not mirror. It must never be re-created.

Static risks: ``GRADING_CHANGE`` when ``points`` or ``grading_type`` change. The
``LATE_POLICY_REGRADE`` and ``ACCESS_REMOVED`` risks depend on live state (the late
policy, graded submissions, who has submitted), which a pure differ over two trees
cannot see, so the guard derives them at apply time.

Override hook (orx.9): override changes are carried on the assignment's own operation
under ``Operation.changes["overrides"]`` (and ``["assign_to_overrides_only"]``), never
as separate ``Override`` operations, because the guard reads the assignment id from
``op.before``/``op.after``. Overrides compare by value, so an unchanged file whose
overrides contain ``unknown:<id>`` refs produces no change.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from canvas_sync.model.assignment import Assignment
from canvas_sync.model.base import CanvasObject, Kind, ObjectKey
from canvas_sync.plan.core import Action, Operation, Risk, RiskKind, changes_between

if TYPE_CHECKING:
    from canvas_sync.model.course import Course

GROUP_PREFIX = "assignments#group:"
CONFIRM_GRADING = "--confirm-grading-change"
OVERRIDE_FIELDS = ("overrides", "assign_to_overrides_only")


class AssignmentGroup(CanvasObject):
    """An assignment group. The class name is load-bearing: the guard recognises group
    operations by it (CONTRACTS.md section 10)."""

    name: str = ""


def group_key(name: str) -> ObjectKey:
    return ObjectKey(Kind.ASSIGNMENT, f"{GROUP_PREFIX}{name}")


def _normalised(a: Assignment) -> Assignment:
    """Canvas reports ``["none"]`` for no submission types and ``0`` for no points."""
    return a.model_copy(
        update={
            "submission_types": [t for t in a.submission_types if t != "none"],
            "points": a.points or None,
        }
    )


def assignment_changes(before: Assignment, after: Assignment) -> dict[str, tuple[Any, Any]]:
    """``{field: (old, new)}``, ignoring differences Canvas itself normalises away."""
    changes = changes_between(before, after)
    for name, (old, new) in list(changes.items()):
        if name in ("submission_types", "points"):
            if getattr(_normalised(before), name) == getattr(_normalised(after), name):
                del changes[name]
        elif old == new:
            del changes[name]
    return changes


def _risks(path: str, changes: dict[str, tuple[Any, Any]]) -> tuple[Risk, ...]:
    if "points" in changes or "grading_type" in changes:
        msg = f"{path}: points/grading_type change reinterprets or rescales existing scores"
        return (Risk(RiskKind.GRADING_CHANGE, msg, CONFIRM_GRADING),)
    return ()


def _assignments(course: Course) -> dict[str, Assignment]:
    return {
        k.path: cast(Assignment, o) for k, o in course.objects.items() if k.kind is Kind.ASSIGNMENT
    }


def diff_type(base: Course, target: Course) -> list[Operation]:
    old, new = _assignments(base), _assignments(target)
    old_by_id = {o.canvas_id: p for p, o in old.items() if o.canvas_id is not None}

    matched: dict[str, str] = {}  # target path -> base path
    for path, obj in new.items():
        if obj.canvas_id is not None and obj.canvas_id in old_by_id:
            matched[path] = old_by_id[obj.canvas_id]
    claimed = set(matched.values())
    creates: list[str] = []
    for path, obj in sorted(new.items()):
        if path in matched:
            continue
        if obj.canvas_id is not None:
            continue  # gone from Canvas (deleted, or not mirrored): never re-create it
        if path in old and path not in claimed:
            matched[path] = path  # the id was stripped from the file: same assignment
            claimed.add(path)
        else:
            creates.append(path)

    ops: list[Operation] = []
    updates: list[Operation] = []
    for path in sorted(matched):
        before, after = old[matched[path]], new[path]
        changes = assignment_changes(before, after)
        if changes:
            key = ObjectKey(Kind.ASSIGNMENT, path)
            updates.append(
                Operation(key, Action.UPDATE, before, after, changes, _risks(path, changes))
            )

    known_groups = {o.group for o in old.values() if o.group is not None}
    wanted: list[str] = []
    for path in creates:
        wanted.append(new[path].group or "")
    for op in updates:
        if "group" in op.changes:
            wanted.append(op.changes["group"][1] or "")
    seen: set[str] = set()
    for name in wanted:
        if name and name not in known_groups and name not in seen:
            seen.add(name)
            ops.append(
                Operation(
                    group_key(name),
                    Action.CREATE,
                    None,
                    AssignmentGroup(name=name),
                    {"name": (None, name)},
                )
            )

    ops.extend(
        Operation(ObjectKey(Kind.ASSIGNMENT, p), Action.CREATE, None, new[p], {}) for p in creates
    )
    ops.extend(updates)
    ops.extend(
        Operation(ObjectKey(Kind.ASSIGNMENT, p), Action.DELETE, old[p], None, {})
        for p in sorted(set(old) - claimed)
    )
    return ops
