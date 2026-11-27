"""Differ for modules and module items (CONTRACTS.md sections 4 and 9).

Operations, in apply order: module creates/updates/reorders (target order), then
module item creates/updates/reorders, then deletes (``core.diff`` moves those last).

Operation shapes (the writers in ``remote/modules.py`` rely on them):

- Module ops: key ``modules.yaml#<name>`` (target key; base key for deletes);
  ``before`` is the base ``Module``, ``after`` a ``PlacedModule``.
- Item ops: key ``modules.yaml#<name>/items[<j>]`` (``j`` = index in the target
  module; base index for deletes), noun "item"; ``after`` is a ``PlacedItem``
  (``before`` is the base ``ModuleItem``, a ``PlacedItem`` for deletes).
- Order is enforced relative to a predecessor (``pred_id`` / ``pred_path``), not by
  absolute position, because deletes run last and Canvas positions shift. Only items
  outside the longest already-ordered run are moved, so a reorder emits the minimum
  number of position updates.

Never written (CONTRACTS.md section 4): prerequisites, require_sequential_progress,
completion_requirements, and ``unmanaged`` items. Local edits to them are ignored
with a one-line warning. Titles of page/assignment items are never sent (the reader
drops Canvas's custom titles), and ``published: None`` means no opinion.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel
from rich.console import Console

from canvas_sync.errors import ValidationError
from canvas_sync.model.base import CanvasObject, Kind, ObjectKey
from canvas_sync.model.course import Course
from canvas_sync.model.modules import FILENAME, Module, ModuleItem
from canvas_sync.plan.core import Action, Operation

_REF_KIND = {"page": Kind.PAGE, "assignment": Kind.ASSIGNMENT, "file": Kind.FILE}
_REF_PATH = {"page": "pages/{}.md", "assignment": "assignments/{}.md", "file": "files/{}"}


class PlacedModule(Module):
    """A module as an operation's ``after``: where it goes among the modules.

    ``place`` False means "leave position alone / append". Otherwise it goes right after
    the module with Canvas id ``pred_id`` or (created in this push) key path ``pred_path``;
    with neither, first.
    """

    place: bool = False
    pred_id: int | None = None
    pred_path: str | None = None


class PlacedItem(ModuleItem, CanvasObject):  # pyright: ignore[reportIncompatibleVariableOverride]
    """A module item as an operation's ``after`` (or ``before`` of a delete).

    ``module_path`` / ``module_id``: the module it belongs to (id None when that module is
    created in the same push: use ``ctx.ids``). ``from_module_id``: where it is now, for
    moves. ``content_path`` / ``content_id``: the referenced page/assignment/file (tree key
    path, and its Canvas id if known; else ``ctx.ids[ObjectKey(kind, content_path)]``).
    Placement as for ``PlacedModule``, among the module's items.
    """

    module_path: str = ""
    module_id: int | None = None
    from_module_id: int | None = None
    content_path: str | None = None
    content_id: int | str | None = None
    place: bool = False
    pred_id: int | None = None
    pred_path: str | None = None


def _warn(message: str) -> None:
    Console(stderr=True).print(f"warning: {message}", markup=False, highlight=False, soft_wrap=True)


def module_keys(mods: list[Module]) -> list[str]:
    """``modules.yaml#<name>``, ``#<name>#2`` for repeats (the ``dump_tree`` scheme)."""
    taken: set[str] = set()
    out: list[str] = []
    for m in mods:
        key, n = f"{FILENAME}#{m.name}", 2
        while key in taken:
            key = f"{FILENAME}#{m.name}#{n}"
            n += 1
        taken.add(key)
        out.append(key)
    return out


def _lis(seq: list[int]) -> set[int]:
    """Indexes (into ``seq``) of a longest strictly increasing subsequence."""
    n = len(seq)
    best = [1] * n
    prev = [-1] * n
    for i in range(n):
        for j in range(i):
            if seq[j] < seq[i] and best[j] + 1 > best[i]:
                best[i], prev[i] = best[j] + 1, j
    if not n:
        return set()
    i = max(range(n), key=lambda k: (best[k], -k))
    out: set[int] = set()
    while i >= 0:
        out.add(i)
        i = prev[i]
    return out


def _fields(obj: Any, cls: type[BaseModel]) -> dict[str, Any]:
    return {name: getattr(obj, name) for name in cls.model_fields}


def _placed(it: ModuleItem, **extra: Any) -> PlacedItem:
    data: dict[str, Any] = {**_fields(it, ModuleItem), **extra}
    return PlacedItem(**data)


def _is_assignment(item: ModuleItem) -> bool:
    return item.assignment is not None


def _content(course: Course, item: ModuleItem) -> tuple[str | None, int | str | None]:
    """(tree key path, canvas id) of what a page/assignment/file item points at."""
    typ = item.item_type
    if typ in _REF_KIND:
        path = _REF_PATH[typ].format(getattr(item, typ))
        obj: CanvasObject | None = course.objects.get(ObjectKey(_REF_KIND[typ], path))
        return path, (obj.canvas_id if obj is not None else None)
    if typ == "quiz" and item.quiz is not None:
        return None, item.quiz.canvas_id
    if typ == "discussion" and item.discussion is not None:
        return None, item.discussion.canvas_id
    return None, None


def _same_target(base: Course, b: ModuleItem, target: Course, t: ModuleItem) -> bool:
    """Same Canvas item can be kept (Canvas cannot change an item's type or target)."""
    if b.item_type != t.item_type:
        return False
    typ = b.item_type
    if typ in ("header", "url"):
        return True
    if typ in _REF_KIND:
        if getattr(b, typ) == getattr(t, typ):
            return True
        bid, tid = _content(base, b)[1], _content(target, t)[1]
        return bid is not None and bid == tid
    return _content(base, b)[1] == _content(target, t)[1]


@dataclass(frozen=True)
class _BaseItem:
    mod: int
    idx: int
    item: ModuleItem


def _item_changes(b: ModuleItem, t: ModuleItem) -> dict[str, tuple[Any, Any]]:
    """Managed-field changes of a kept item. Page/assignment titles are never compared."""
    out: dict[str, tuple[Any, Any]] = {}
    typ = t.item_type
    if typ == "header" and t.header != b.header:
        out["header"] = (b.header, t.header)
    if typ == "url":
        if t.url != b.url:
            out["url"] = (b.url, t.url)
        if t.new_tab != b.new_tab:
            out["new_tab"] = (b.new_tab, t.new_tab)
    if typ in ("file", "url", "quiz", "discussion") and t.title is not None and t.title != b.title:
        out["title"] = (b.title, t.title)
    if t.indent != b.indent:
        out["indent"] = (b.indent, t.indent)
    if t.published is not None and t.published != b.published:
        out["published"] = (b.published, t.published)
    return out


def _unmanaged_edits(b: ModuleItem, t: ModuleItem) -> list[str]:
    return [
        f
        for f in ("title", "indent", "new_tab", "published")
        if getattr(t, f) != getattr(b, f)
        and not (f in ("title", "published") and getattr(t, f) is None)
    ]


def _describe(item: ModuleItem) -> tuple[str, Any]:
    typ = item.item_type
    value = getattr(item, typ)
    if typ in ("quiz", "discussion"):
        value = value.canvas_id
    return typ, value


def diff_type(base: Course, target: Course) -> list[Operation]:
    bkeys, tkeys = module_keys(base.modules), module_keys(target.modules)
    match = _match_modules(base.modules, bkeys, target.modules, tkeys)
    matched_base = set(match.values())

    base_items: dict[int, _BaseItem] = {}
    for bi, bm in enumerate(base.modules):
        for ii, it in enumerate(bm.items):
            if it.canvas_id is not None:
                base_items[it.canvas_id] = _BaseItem(bi, ii, it)

    ops: list[Operation] = []
    ops.extend(_module_ops(base, bkeys, target, tkeys, match))

    claimed: set[int] = set()
    for ti, tm in enumerate(target.modules):
        ops.extend(_item_ops(base, bkeys, target, tkeys, ti, tm, match, base_items, claimed))

    # Deletes: items first (the module's own delete comes later in the list; core reverses).
    for bi, bm in enumerate(base.modules):
        if bi not in matched_base:
            continue  # the module itself is deleted below
        for ii, it in enumerate(bm.items):
            if it.canvas_id is None or it.canvas_id in claimed:
                continue
            if it.unmanaged is not None:
                _warn(
                    f"{bkeys[bi]}: items[{ii}]: deleting an unmanaged item is "
                    "not supported, local deletion ignored"
                )
                continue
            ops.append(_delete_item(bm, bkeys[bi], ii, it))
    for bi, bm in enumerate(base.modules):
        if bi in matched_base:
            continue
        touches = any(_is_assignment(i) for i in bm.items)
        ops.append(
            Operation(
                ObjectKey(Kind.MODULE, bkeys[bi]),
                Action.DELETE,
                bm,
                None,
                {},
                touches_assignment=touches,
            )
        )
    return ops


def _match_modules(
    bmods: list[Module], bkeys: list[str], tmods: list[Module], tkeys: list[str]
) -> dict[int, int]:
    """target index -> base index: by canvas_id first, then by key for id-less modules."""
    by_id = {m.canvas_id: i for i, m in enumerate(bmods) if m.canvas_id is not None}
    match: dict[int, int] = {}
    used: set[int] = set()
    for ti, m in enumerate(tmods):
        bi = by_id.get(m.canvas_id) if m.canvas_id is not None else None
        if bi is not None and bi not in used:
            match[ti] = bi
            used.add(bi)
    by_key = {k: i for i, k in enumerate(bkeys) if i not in used}
    for ti, m in enumerate(tmods):
        if ti in match or m.canvas_id is not None:
            continue
        bi = by_key.get(tkeys[ti])
        if bi is not None and bi not in used:
            match[ti] = bi
            used.add(bi)
    return match


_SETTINGS = ("prerequisites", "require_sequential_progress", "completion_requirements")


def _read_only_module_settings(path: str, b: Module | None, t: Module) -> None:
    """Warn (one line each) about local edits to settings canvas-sync never writes."""
    for name in _SETTINGS:
        new = getattr(t, name)
        if b is None:
            edited = new not in (None, False, [])
        else:
            edited = new != getattr(b, name) and new is not None
        if edited:
            verb = "is" if name == "require_sequential_progress" else "are"
            _warn(f"{path}: {name} {verb} read-only, local change ignored")


def _module_ops(
    base: Course,
    bkeys: list[str],
    target: Course,
    tkeys: list[str],
    match: dict[int, int],
) -> list[Operation]:
    ops: list[Operation] = []
    order = [(ti, bi) for ti, bi in sorted(match.items())]
    keep_pos = _lis([bi for _, bi in order])
    keep = {ti for k, (ti, _) in enumerate(order) if k in keep_pos}
    n = len(target.modules)
    for ti, tm in enumerate(target.modules):
        path = tkeys[ti]
        bi = match.get(ti)
        bm = base.modules[bi] if bi is not None else None
        # predecessor in the target order
        pred_id: int | None = None
        pred_path: str | None = None
        if ti > 0:
            pbi = match.get(ti - 1)
            if pbi is not None and base.modules[pbi].canvas_id is not None:
                pred_id = base.modules[pbi].canvas_id
            else:
                pred_path = tkeys[ti - 1]
        later_existing = any(t2 in match for t2 in range(ti + 1, n))
        _read_only_module_settings(path, bm, tm)
        key = ObjectKey(Kind.MODULE, path)
        has_assignment = any(_is_assignment(i) for i in tm.items) or (
            bm is not None and any(_is_assignment(i) for i in bm.items)
        )
        if bm is None:
            after = PlacedModule(
                **_fields(tm, Module),
                place=later_existing,
                pred_id=pred_id,
                pred_path=pred_path,
            )
            changes: dict[str, tuple[Any, Any]] = {"name": (None, tm.name)}
            if tm.published is not None:
                changes["published"] = (None, tm.published)
            if tm.unlock is not None:
                changes["unlock"] = (None, tm.unlock)
            ops.append(
                Operation(
                    key,
                    Action.CREATE,
                    None,
                    after,
                    changes,
                    touches_assignment=has_assignment and tm.published is True,
                )
            )
            continue
        changes = {}
        if tm.name != bm.name:
            changes["name"] = (bm.name, tm.name)
        if tm.published is not None and tm.published != bm.published:
            changes["published"] = (bm.published, tm.published)
        if tm.unlock != bm.unlock:
            changes["unlock"] = (bm.unlock, tm.unlock)
        moved = ti not in keep
        if not changes and not moved:
            continue
        if moved:
            changes["position"] = (bi + 1 if bi is not None else None, ti + 1)
        after = PlacedModule(
            **_fields(tm, Module),
            place=moved,
            pred_id=pred_id,
            pred_path=pred_path,
        )
        ops.append(
            Operation(
                key,
                Action.REORDER if set(changes) == {"position"} else Action.UPDATE,
                bm,
                after,
                changes,
                touches_assignment=has_assignment and "published" in changes,
            )
        )
    return ops


def _delete_item(bm: Module, bpath: str, ii: int, it: ModuleItem) -> Operation:
    before = _placed(it, module_path=bpath, module_id=bm.canvas_id)
    return Operation(
        ObjectKey(Kind.MODULE, f"{bpath}/items[{ii}]"),
        Action.DELETE,
        before,
        None,
        {},
        touches_assignment=_is_assignment(it),
    )


@dataclass
class _Entry:
    j: int
    item: ModuleItem
    hit: _BaseItem | None  # base item kept (None: create)
    unmanaged: bool = False


def _item_ops(
    base: Course,
    bkeys: list[str],
    target: Course,
    tkeys: list[str],
    ti: int,
    tm: Module,
    match: dict[int, int],
    base_items: dict[int, _BaseItem],
    claimed: set[int],
) -> list[Operation]:
    tpath = tkeys[ti]
    bi = match.get(ti)
    bm = base.modules[bi] if bi is not None else None
    mid = bm.canvas_id if bm is not None else None

    entries: list[_Entry] = []
    for j, it in enumerate(tm.items):
        cid = it.canvas_id
        hit = base_items.get(cid) if cid is not None and cid not in claimed else None
        if it.unmanaged is not None:
            if hit is None or hit.item.unmanaged is None:
                _warn(f"{tpath}: items[{j}]: adding an unmanaged item is not supported, ignored")
                continue
            claimed.add(hit.item.canvas_id or 0)
            entries.append(_Entry(j, it, hit, True))
            continue
        if (
            hit is not None
            and hit.item.unmanaged is None
            and _same_target(base, hit.item, target, it)
        ):
            claimed.add(hit.item.canvas_id or 0)
            entries.append(_Entry(j, it, hit))
        else:
            entries.append(_Entry(j, it, None))

    # warnings for unmanaged items, and order checks
    same = [e for e in entries if e.hit is not None and bi is not None and e.hit.mod == bi]
    here = {e.hit.item.canvas_id for e in same if e.hit is not None and not e.unmanaged}
    for k, e in enumerate(same):
        if not e.unmanaged or e.hit is None or bm is None:
            continue
        # An unmanaged item stays after the same managed neighbour as on Canvas.
        base_anchor = next(
            (
                x.canvas_id
                for x in reversed(bm.items[: e.hit.idx])
                if x.unmanaged is None and x.canvas_id in here
            ),
            None,
        )
        target_anchor = next(
            (
                x.hit.item.canvas_id
                for x in reversed(same[:k])
                if not x.unmanaged and x.hit is not None
            ),
            None,
        )
        if base_anchor != target_anchor:
            _warn(
                f"{tpath}: items[{e.j}]: reordering an unmanaged item is read-only, "
                "local move ignored"
            )
    for e in entries:
        if not e.unmanaged or e.hit is None:
            continue
        if bi is None or e.hit.mod != bi:
            _warn(
                f"{tpath}: items[{e.j}]: moving an unmanaged item is read-only, local move ignored"
            )
        edits = _unmanaged_edits(e.hit.item, e.item)
        if edits:
            _warn(
                f"{tpath}: items[{e.j}]: {', '.join(edits)} of an unmanaged item "
                "are read-only, local change ignored"
            )

    managed_same = [e for e in same if not e.unmanaged]
    keep_idx = _lis([e.hit.idx for e in managed_same if e.hit is not None])
    keep = {managed_same[k].j for k in keep_idx}

    ops: list[Operation] = []
    pred_id: int | None = None
    pred_path: str | None = None
    for k, e in enumerate(entries):
        it = e.item
        path = f"{tpath}/items[{e.j}]"
        later_existing = any(x.hit is not None for x in entries[k + 1 :])
        key = ObjectKey(Kind.MODULE, path)
        if e.hit is None:
            cpath, cid = _content(target, it)
            if (
                cpath is not None
                and it.item_type in _REF_KIND
                and ObjectKey(_REF_KIND[it.item_type], cpath) not in target.objects
            ):
                raise ValidationError(
                    f"{tpath}: items[{e.j}]: {it.item_type} "
                    f"{getattr(it, it.item_type)!r} is not in the course tree"
                )
            after = _placed(
                it,
                canvas_id=None,
                module_path=tpath,
                module_id=mid,
                content_path=cpath,
                content_id=cid,
                place=later_existing,
                pred_id=pred_id,
                pred_path=pred_path,
            )
            typ, value = _describe(it)
            ops.append(
                Operation(
                    key,
                    Action.CREATE,
                    None,
                    after,
                    {typ: (None, value)},
                    touches_assignment=_is_assignment(it),
                )
            )
            pred_id, pred_path = None, path
            continue
        hit = e.hit
        bid = hit.item.canvas_id
        pred_now = (pred_id, pred_path)
        pred_id, pred_path = bid, None
        if e.unmanaged:
            continue
        changes = _item_changes(hit.item, it)
        moved_module = bi is None or hit.mod != bi
        if moved_module:
            changes["module"] = (base.modules[hit.mod].name, tm.name)
        needs_place = moved_module or e.j not in keep
        if needs_place:
            changes["position"] = (hit.idx + 1, e.j + 1)
        if not changes:
            continue
        cpath, cid = _content(target, it)
        after = _placed(
            it,
            module_path=tpath,
            module_id=mid,
            from_module_id=base.modules[hit.mod].canvas_id,
            content_path=cpath,
            content_id=cid,
            place=needs_place,
            pred_id=pred_now[0],
            pred_path=pred_now[1],
        )
        only_order = set(changes) == {"position"}
        ops.append(
            Operation(
                key,
                Action.REORDER if only_order else Action.UPDATE,
                _placed(
                    hit.item,
                    module_path=bkeys[hit.mod],
                    module_id=base.modules[hit.mod].canvas_id,
                ),
                after,
                changes,
                touches_assignment=_is_assignment(it)
                and ("published" in changes or "module" in changes),
            )
        )
    return ops
