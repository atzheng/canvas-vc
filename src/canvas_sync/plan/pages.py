"""Differ for pages (CONTRACTS.md sections 4 and 9; SPEC.md "Pages").

Objects are paired by ``page_id`` when both sides have one (the slug can be reused by
another page after a rename), else by ``canvas_id`` (the url slug) unless both sides have
different ``page_id``s, then by tree path for a target that has no ``canvas_id``.
A paired object whose fields
differ becomes an UPDATE (never
DELETE + CREATE, which would lose the page's identity); an unpaired target is a CREATE;
an unpaired base object is a DELETE. A page moved to another path with the same slug and
no other change produces nothing.

``after`` is a ``PlacedPage`` (a ``Page`` plus ``key_path``, the operation's key path) so
the writer can record the body and the new slug under the right key. ``published: None``
in the target means "no opinion": it never produces a change.

Deleting the current front page when no target page has ``front_page`` raises a
``ValidationError`` at plan time (Canvas refuses it with a 400 mid-push).
"""

from __future__ import annotations

from canvas_sync.errors import ValidationError
from canvas_sync.model.base import Kind, ObjectKey
from canvas_sync.model.course import Course
from canvas_sync.model.page import Page
from canvas_sync.plan.core import Action, Operation, changes_between


class PlacedPage(Page):
    """A page as an operation's ``after``; ``key_path`` is its tree path."""

    key_path: str = ""


def _pages(course: Course) -> dict[str, Page]:
    return {
        k.path: o for k, o in course.objects.items() if k.kind is Kind.PAGE and isinstance(o, Page)
    }


def _placed(
    page: Page, path: str, canvas_id: str | int | None, page_id: int | None = None
) -> PlacedPage:
    data = page.model_dump()
    data["canvas_id"] = canvas_id
    data["page_id"] = page_id
    return PlacedPage(key_path=path, **data)


def _pair(base: dict[str, Page], target: dict[str, Page]) -> dict[str, str]:
    """Target path -> base path for objects that exist on both sides."""
    by_id = {str(o.canvas_id): p for p, o in base.items() if o.canvas_id is not None}
    by_pid = {o.page_id: p for p, o in base.items() if o.page_id is not None}
    seen: dict[object, str] = {}
    seen_pid: dict[int, str] = {}
    for path, obj in sorted(target.items()):
        if obj.canvas_id is not None:
            if obj.canvas_id in seen:
                raise ValidationError(
                    f"{path}: duplicate canvas_id {obj.canvas_id!r} (also in {seen[obj.canvas_id]})"  # noqa: E501
                )
            seen[obj.canvas_id] = path
        if obj.page_id is not None:
            if obj.page_id in seen_pid:
                raise ValidationError(
                    f"{path}: duplicate page_id {obj.page_id} (also in {seen_pid[obj.page_id]})"
                )
            seen_pid[obj.page_id] = path
    pairs: dict[str, str] = {}
    used: set[str] = set()
    for path, obj in sorted(target.items()):  # stable identity first
        if obj.page_id is not None and obj.page_id in by_pid and by_pid[obj.page_id] not in used:
            pairs[path] = by_pid[obj.page_id]
            used.add(pairs[path])
    for path, obj in sorted(target.items()):
        if path in pairs or obj.canvas_id is None or str(obj.canvas_id) not in by_id:
            continue
        bp = by_id[str(obj.canvas_id)]
        if bp in used:
            continue
        other = base[bp].page_id
        if obj.page_id is not None and other is not None and obj.page_id != other:
            continue  # same slug, different page: the slug was reused
        pairs[path] = bp
        used.add(bp)
    for path, obj in sorted(target.items()):
        if path in pairs or obj.canvas_id is not None or obj.page_id is not None:
            continue
        if path in base and path not in used:  # the id was removed from the file
            pairs[path] = path
            used.add(path)
    return pairs


def diff_type(base: Course, target: Course) -> list[Operation]:
    b, t = _pages(base), _pages(target)
    pairs = _pair(b, t)
    ops: list[Operation] = []
    for path, new in sorted(t.items()):
        key = ObjectKey(Kind.PAGE, path)
        if path not in pairs:
            created = {
                f: (None, v)
                for f, v in new.model_dump(
                    exclude={"canvas_id", "page_id"}, exclude_defaults=True
                ).items()
            }
            ops.append(Operation(key, Action.CREATE, None, _placed(new, path, None), created))
            continue
        old = b[pairs[path]]
        changes = changes_between(old, new, exclude=("canvas_id", "page_id"))
        if new.published is None:
            changes.pop("published", None)
        if not changes:
            continue
        placed = _placed(new, path, old.canvas_id, old.page_id or new.page_id)
        ops.append(Operation(key, Action.UPDATE, old, placed, changes))
    paired = set(pairs.values())
    for path, old in sorted(b.items()):
        if path not in paired:
            if old.front_page and not any(o.front_page for o in t.values()):
                raise ValidationError(
                    f"{path}: is the course front page; set front_page on another page first"
                )
            ops.append(Operation(ObjectKey(Kind.PAGE, path), Action.DELETE, old, None, {}))
    return ops
