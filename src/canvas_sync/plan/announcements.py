"""Differ for announcements (CONTRACTS.md sections 4, 9 and 10; SPEC.md "Announcements").

Pairing is by ``canvas_id`` (the topic id), then by tree path for a target without one.

- CREATE or an UPDATE that makes the announcement posted now (published, no ``post_at`` or
  one that has passed): ``NOTIFIES_STUDENTS`` risk, needs ``--confirm-notify``. The guard
  re-derives the same risk at apply time.
- UPDATE of an announcement that is already posted: ``POSTED_ANNOUNCEMENT`` warning (no
  flag; students may already have been notified).
- A deleted file is never a DELETE (the allowlist refuses it): it becomes UNPUBLISH with a
  ``POSTED_ANNOUNCEMENT`` warning. One that is already unpublished produces nothing.

``after`` is a ``PlacedAnnouncement`` (an ``Announcement`` plus ``key_path``).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from rich.console import Console

from canvas_sync.errors import ValidationError
from canvas_sync.model.announcement import Announcement
from canvas_sync.model.base import Kind, ObjectKey
from canvas_sync.model.course import Course
from canvas_sync.plan.core import Action, Operation, Risk, RiskKind, changes_between

CONFIRM_NOTIFY = "--confirm-notify"


class PlacedAnnouncement(Announcement):
    """An announcement as an operation's ``after``; ``key_path`` is its tree path."""

    key_path: str = ""


def _anns(course: Course) -> dict[str, Announcement]:
    return {
        k.path: o
        for k, o in course.objects.items()
        if k.kind is Kind.ANNOUNCEMENT and isinstance(o, Announcement)
    }


def _placed(a: Announcement, path: str, canvas_id: int | str | None) -> PlacedAnnouncement:
    data = a.model_dump()
    data["canvas_id"] = canvas_id
    return PlacedAnnouncement(key_path=path, **data)


def posted_now(a: Announcement, now: datetime) -> bool:
    """Visible to students (and emailed) right now."""
    return a.published and (a.post_at is None or a.post_at <= now)


def _pair(base: dict[str, Announcement], target: dict[str, Announcement]) -> dict[str, str]:
    by_id = {o.canvas_id: p for p, o in base.items() if o.canvas_id is not None}
    seen: dict[object, str] = {}
    for path, obj in sorted(target.items()):
        if obj.canvas_id is None:
            continue
        if obj.canvas_id in seen:
            raise ValidationError(
                f"{path}: duplicate canvas_id {obj.canvas_id!r} (also in {seen[obj.canvas_id]})"
            )
        seen[obj.canvas_id] = path
    pairs: dict[str, str] = {}
    used: set[str] = set()
    for path, obj in sorted(target.items()):
        if obj.canvas_id is not None and obj.canvas_id in by_id:
            pairs[path] = by_id[obj.canvas_id]
            used.add(pairs[path])
    for path, obj in sorted(target.items()):
        if path in pairs or obj.canvas_id is not None:
            continue
        if path in base and path not in used:
            pairs[path] = path
            used.add(path)
    return pairs


def _notify(path: str) -> Risk:
    msg = f"{path}: posts now and emails students; cannot be taken back"
    return Risk(RiskKind.NOTIFIES_STUDENTS, msg, CONFIRM_NOTIFY)


def diff_type(
    base: Course, target: Course, *, now: Callable[[], datetime] = lambda: datetime.now(UTC)
) -> list[Operation]:
    b, t = _anns(base), _anns(target)
    pairs = _pair(b, t)
    clock = now()
    ops: list[Operation] = []
    for path, new in sorted(t.items()):
        key = ObjectKey(Kind.ANNOUNCEMENT, path)
        if path not in pairs:
            created = {
                f: (None, v)
                for f, v in new.model_dump(exclude={"canvas_id"}, exclude_defaults=True).items()
            }
            risks = (_notify(path),) if posted_now(new, clock) else ()
            ops.append(
                Operation(key, Action.CREATE, None, _placed(new, path, None), created, risks)
            )
            continue
        old = b[pairs[path]]
        changes = changes_between(old, new)
        if not changes:
            continue
        risks: tuple[Risk, ...] = ()
        if posted_now(new, clock) and not posted_now(old, clock):
            risks = (_notify(path),)
        elif posted_now(old, clock):
            msg = f"{path}: already posted; students may have seen or been emailed the original"
            risks = (Risk(RiskKind.POSTED_ANNOUNCEMENT, msg, None),)
        ops.append(
            Operation(key, Action.UPDATE, old, _placed(new, path, old.canvas_id), changes, risks)
        )
    paired = set(pairs.values())
    for path, old in sorted(b.items()):
        if path in paired:
            continue
        if not old.published:
            Console(stderr=True).print(
                f"warning: {path}: announcements are never deleted; it is already unpublished",
                markup=False,
                highlight=False,
                soft_wrap=True,
            )
            continue
        msg = f"{path}: announcements are never deleted; unpublishing instead"
        if posted_now(old, clock):
            msg += " (already posted, students may have seen it)"
        down = _placed(old.model_copy(update={"published": False}), path, old.canvas_id)
        ops.append(
            Operation(
                ObjectKey(Kind.ANNOUNCEMENT, path),
                Action.UNPUBLISH,
                old,
                down,
                {"published": (True, False)},
                (Risk(RiskKind.POSTED_ANNOUNCEMENT, msg, None),),
            )
        )
    return ops
