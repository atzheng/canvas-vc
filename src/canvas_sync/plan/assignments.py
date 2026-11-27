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

Static risks: ``GRADING_CHANGE`` when ``points`` or ``grading_type`` change, and
``ACCESS_REMOVED`` when an override audience drops out or ``assign_to_overrides_only``
turns on (conservative: the guard refines it live with who has submitted). The
``LATE_POLICY_REGRADE`` risk depends on live state, so the guard derives it at apply time.

Overrides: changes are carried on the assignment's own operation
under ``Operation.changes["overrides"]`` (and ``["assign_to_overrides_only"]``), never
as separate ``Override`` operations, because the guard reads the assignment id from
``op.before``/``op.after``. Overrides compare by value, so an unchanged file whose
overrides contain ``unknown:<id>`` refs produces no change.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast

from canvas_sync.errors import ValidationError
from canvas_sync.model.assignment import Assignment
from canvas_sync.model.base import CanvasObject, Kind, ObjectKey
from canvas_sync.model.overrides import Override
from canvas_sync.plan.core import Action, Operation, Risk, RiskKind, changes_between

if TYPE_CHECKING:
    from canvas_sync.model.course import Course

GROUP_PREFIX = "assignments#group:"
CONFIRM_GRADING = "--confirm-grading-change"
CONFIRM_ACCESS = "--confirm-access-removal"
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


@dataclass(frozen=True)
class OverrideChange:
    """One override API call: ``add`` (POST), ``update`` (PUT) or ``remove`` (DELETE)."""

    kind: Literal["add", "update", "remove"]
    before: Override | None
    after: Override | None


AUTO_TITLE = re.compile(r"\d+ students?")  # Canvas's own title for an ad hoc student set


def custom_title(o: Override) -> str | None:
    """The title Canvas would keep: Canvas's auto-generated "N students" is not one."""
    if not o.title or AUTO_TITLE.fullmatch(o.title):
        return None
    return o.title


def _same(a: Override, b: Override) -> bool:
    """Equal as far as Canvas can store it (a section/group override has no custom title)."""
    if a.target() != b.target() or (a.unlock, a.due, a.lock) != (b.unlock, b.due, b.lock):
        return False
    return a.students is None or custom_title(a) == custom_title(b)


def _loses_students(before: Override, after: Override) -> bool:
    return not set(before.students or []) <= set(after.students or [])


def diff_overrides(before: Sequence[Override], after: Sequence[Override]) -> list[OverrideChange]:
    """Minimal override changes, keyed by target (CONTRACTS.md section 4).

    Sections and groups match by name, student sets by their exact membership. A student set
    whose membership changed is paired with the old one by a unique custom ``title``, then by
    largest overlap of students (deterministic: ties broken by the sorted student lists), so
    that a student who stays is always in an override: one PUT with the full ``student_ids``.
    Only sets with no student in common are remove plus add.

    Order (Canvas allows a student in only one override per assignment, so a moving student
    has a brief gap, never two overrides): updates that drop students; adds; other updates;
    removes. Adds come before removes, so no kept audience is ever left without an override.
    """
    old = {o.target(): o for o in before}
    new = {o.target(): o for o in after}
    pairs: list[tuple[Override, Override]] = [(old[k], new[k]) for k in old if k in new]
    gone = [o for k, o in old.items() if k not in new]
    fresh = [o for k, o in new.items() if k not in old]
    for o in list(gone):
        title = custom_title(o)
        if o.students is None or title is None:
            continue
        same_old = [g for g in gone if g.students is not None and custom_title(g) == title]
        cands = [f for f in fresh if f.students is not None and custom_title(f) == title]
        if len(same_old) == 1 and len(cands) == 1:
            pairs.append((o, cands[0]))
            gone.remove(o)
            fresh.remove(cands[0])
    overlaps = sorted(
        (
            (-len(set(g.students or []) & set(f.students or [])), g.students, f.students, g, f)
            for g in gone
            for f in fresh
            if g.students is not None
            and f.students is not None
            and set(g.students) & set(f.students)
        ),
        key=lambda t: t[:3],
    )
    for _, _, _, g, f in overlaps:
        if g in gone and f in fresh:
            pairs.append((g, f))
            gone.remove(g)
            fresh.remove(f)
    updates = [OverrideChange("update", b, a) for b, a in pairs if not _same(b, a)]
    shrinking = [c for c in updates if c.before and c.after and _loses_students(c.before, c.after)]
    others = [c for c in updates if c not in shrinking]
    return [
        *shrinking,
        *(OverrideChange("add", None, o) for o in fresh),
        *others,
        *(OverrideChange("remove", o, None) for o in gone),
    ]


def access_narrowed(
    before: Sequence[Override],
    after: Sequence[Override],
    only_before: bool,
    only_after: bool,
) -> bool:
    """True if some audience may lose access: a section, group or student drops out of the
    overrides, or ``assign_to_overrides_only`` turns on. Static and conservative; the guard
    refines it live (who has actually submitted)."""
    if only_after and not only_before:
        return True
    kept_students = {s for o in after for s in (o.students or [])}
    kept = {o.target() for o in after}
    for o in before:
        kind, names = o.target()
        if kind == "students":
            if any(s not in kept_students for s in names):
                return True
        elif (kind, names) not in kept:
            return True
    return False


def moved_students(before: Sequence[Override], after: Sequence[Override]) -> int:
    """Students who leave one student-set override and join another. They are written as two
    PUTs, so each is in no override between them."""
    changes = diff_overrides(before, after)
    left = {s for c in changes if c.before and c.after for s in _lost(c.before, c.after)}
    joined = {
        s
        for c in changes
        if c.after is not None
        for s in set(c.after.students or []) - set((c.before.students if c.before else None) or [])
    }
    return len(left & joined)


def _lost(before: Override, after: Override) -> set[str]:
    return set(before.students or []) - set(after.students or [])


def _risks(path: str, changes: dict[str, tuple[Any, Any]], only: bool = False) -> tuple[Risk, ...]:
    risks: list[Risk] = []
    if "points" in changes or "grading_type" in changes:
        msg = f"{path}: points/grading_type change reinterprets or rescales existing scores"
        risks.append(Risk(RiskKind.GRADING_CHANGE, msg, CONFIRM_GRADING))
    if "overrides" in changes or "assign_to_overrides_only" in changes:
        ov_old, ov_new = changes.get("overrides", ([], []))
        only_old, only_new = changes.get("assign_to_overrides_only", (False, False))
        if access_narrowed(ov_old, ov_new, bool(only_old), bool(only_new)):
            msg = f"{path}: removes an override audience; students who submitted lose access"
            risks.append(Risk(RiskKind.ACCESS_REMOVED, msg, CONFIRM_ACCESS))
        moved = moved_students(ov_old, ov_new) if only else 0
        if moved:
            msg = (
                f"{path}: {moved} student(s) move between override sets; each is in no override "
                "between the two writes (transient unless the push fails, then they lose access)"
            )
            risks.append(Risk(RiskKind.ACCESS_REMOVED, msg, CONFIRM_ACCESS))
    return tuple(risks)


def _assignments(course: Course) -> dict[str, Assignment]:
    return {
        k.path: cast(Assignment, o) for k, o in course.objects.items() if k.kind is Kind.ASSIGNMENT
    }


def diff_type(base: Course, target: Course) -> list[Operation]:
    old, new = _assignments(base), _assignments(target)
    seen_ids: dict[int | str, str] = {}
    for path, obj in sorted(new.items()):
        if obj.canvas_id is not None:
            if obj.canvas_id in seen_ids:
                raise ValidationError(
                    f"{path}: canvas_id {obj.canvas_id} is also in {seen_ids[obj.canvas_id]}"
                )
            seen_ids[obj.canvas_id] = path
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
                Operation(
                    key,
                    Action.UPDATE,
                    before,
                    after,
                    changes,
                    _risks(path, changes, after.assign_to_overrides_only),
                )
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
