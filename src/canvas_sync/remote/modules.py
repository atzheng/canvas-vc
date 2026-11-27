"""Modules reader and writer (CONTRACTS.md sections 4 and 8).

Reader (GET only): modules and their items in Canvas order. Page, assignment and
file items are turned into local refs through ``paths`` (or, when absent, the
previous tree); a target that is not known (ignored, or not fetched) becomes an
``unmanaged`` item at its position, with a warning. Item types canvas-sync cannot
write (ExternalTool, ...) are ``unmanaged`` too and never dropped or reordered.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from rich.console import Console

from canvas_sync.dates import from_canvas, to_canvas
from canvas_sync.errors import CanvasSyncError
from canvas_sync.model.base import CanvasObject, Kind, ObjectKey
from canvas_sync.model.modules import (
    CompletionRequirement,
    Module,
    ModuleItem,
    UnmanagedItem,
    UnmanagedRef,
)
from canvas_sync.plan.modules import PlacedItem, PlacedModule
from canvas_sync.remote.context import ApplyContext, FetchContext
from canvas_sync.remote.http import CanvasClient

PathKey = tuple[Kind, int | str]
_PER_PAGE = {"per_page": 100}
_REF_TYPES = {"Page": Kind.PAGE, "Assignment": Kind.ASSIGNMENT, "File": Kind.FILE}


def _warn(message: str) -> None:
    Console(stderr=True).print(f"warning: {message}", markup=False, highlight=False, soft_wrap=True)


def paths_from_previous(ctx: FetchContext) -> dict[PathKey, str]:
    """(kind, canvas id or page slug) -> module ref, from the previous tree."""
    out: dict[PathKey, str] = {}
    if ctx.previous is None:
        return out
    for key, obj in ctx.previous.objects.items():
        if obj.canvas_id is None:
            continue
        if key.kind in (Kind.PAGE, Kind.ASSIGNMENT):
            _, _, rest = key.path.partition("/")
            out[(key.kind, obj.canvas_id)] = rest.removesuffix(".md")
        elif key.kind == Kind.FILE:
            out[(key.kind, obj.canvas_id)] = key.path.removeprefix("files/")
    return out


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _unmanaged(raw: dict[str, Any], where: str, *, warn: str | None = None) -> ModuleItem:
    if warn:
        _warn(f"{where}: {warn}")
    ext = raw.get("external_url")
    return ModuleItem(
        unmanaged=UnmanagedItem(
            type=str(raw.get("type") or "Unknown"),
            content_id=_int(raw.get("content_id")),
            external_url=ext if isinstance(ext, str) else None,
        ),
        title=raw.get("title") or None,
        indent=_int(raw.get("indent")) or 0,
        new_tab=bool(raw.get("new_tab")),
        published=raw.get("published") if isinstance(raw.get("published"), bool) else None,
        canvas_id=int(raw["id"]),
    )


def _item(raw: dict[str, Any], paths: Mapping[PathKey, str], where: str) -> ModuleItem:
    typ = raw.get("type")
    title = raw.get("title") or None
    common: dict[str, Any] = {
        "indent": max(_int(raw.get("indent")) or 0, 0),
        "published": raw["published"] if isinstance(raw.get("published"), bool) else None,
        "canvas_id": int(raw["id"]),
    }
    content_id = _int(raw.get("content_id"))
    if typ == "SubHeader" and title and title.strip():
        return ModuleItem(header=title, **common)
    if typ == "ExternalUrl":
        url = raw.get("external_url")
        if isinstance(url, str) and url.startswith(("http://", "https://")) and " " not in url:
            return ModuleItem(url=url, title=title, new_tab=bool(raw.get("new_tab")), **common)
    elif typ in _REF_TYPES:
        kind = _REF_TYPES[typ]
        ident: int | str | None = raw.get("page_url") if kind == Kind.PAGE else content_id
        ref = paths.get((kind, ident)) if ident is not None else None
        if ref is not None:
            if kind == Kind.PAGE:
                return ModuleItem(page=ref, **common)
            if kind == Kind.ASSIGNMENT:
                return ModuleItem(assignment=ref, **common)
            base = ref.rsplit("/", 1)[-1]
            return ModuleItem(file=ref, title=title if title != base else None, **common)
        what = f"{typ.lower()} {ident}" if ident is not None else typ.lower()
        return _unmanaged(raw, where, warn=f"{what} is not in the course tree, kept as unmanaged")
    elif typ in ("Quiz", "Discussion") and content_id is not None:
        ref_obj = UnmanagedRef(canvas_id=content_id)
        if typ == "Quiz":
            return ModuleItem(quiz=ref_obj, title=title, **common)
        return ModuleItem(discussion=ref_obj, title=title, **common)
    return _unmanaged(raw, where)


def _requirements(raw: Any) -> list[CompletionRequirement]:
    out: list[CompletionRequirement] = []
    seen: set[int] = set()
    if not isinstance(raw, list):
        return out
    for r in raw:  # pyright: ignore[reportUnknownVariableType]
        if not isinstance(r, dict):
            continue
        d: dict[str, Any] = r  # pyright: ignore[reportUnknownVariableType]
        item_id = _int(d.get("id"))
        typ = d.get("type")
        if item_id is None or item_id in seen or not isinstance(typ, str) or not typ.strip():
            continue
        seen.add(item_id)
        score = d.get("min_score") if typ == "min_score" else None
        out.append(
            CompletionRequirement(
                item_id=item_id,
                type=typ,
                min_score=float(score) if isinstance(score, (int, float)) else None,
            )
        )
    return out


def _prerequisites(raw: Any, own_id: int) -> list[int]:
    out: list[int] = []
    if isinstance(raw, list):
        for v in raw:  # pyright: ignore[reportUnknownVariableType]
            n = _int(v)
            if n is not None and n != own_id and n not in out:
                out.append(n)
    return out


def _position(raw: dict[str, Any]) -> int:
    return _int(raw.get("position")) or 0


def read(
    client: CanvasClient,
    ctx: FetchContext,
    *,
    paths: Mapping[PathKey, str] | None = None,
) -> list[Module]:
    """Modules in Canvas order. ``paths`` maps (Kind, id) to the ref a module item uses:
    page slug (``page_url``) -> ``syllabus``; assignment id -> ``hw1``; file id ->
    ``lectures/01.pdf`` (relative to ``files/``). Falls back to the previous tree."""
    known = paths if paths is not None else paths_from_previous(ctx)
    course = ctx.config.course_id
    rows: list[dict[str, Any]] = client.get(f"/courses/{course}/modules", _PER_PAGE)
    modules: list[Module] = []
    for raw in sorted(rows, key=_position):  # stable: ties keep Canvas order
        mid = int(raw["id"])
        name = str(raw.get("name") or "")
        items_raw: list[dict[str, Any]] = client.get(
            f"/courses/{course}/modules/{mid}/items", _PER_PAGE
        )
        items = [
            _item(it, known, f"modules.yaml: {name}: items[{j}]")
            for j, it in enumerate(sorted(items_raw, key=_position))
        ]
        seq = raw.get("require_sequential_progress")
        pub = raw.get("published")
        modules.append(
            Module(
                canvas_id=mid,
                name=name,
                published=pub if isinstance(pub, bool) else None,
                unlock=from_canvas(raw.get("unlock_at"), ctx.tz),
                prerequisites=_prerequisites(raw.get("prerequisite_module_ids"), mid),
                require_sequential_progress=seq if isinstance(seq, bool) else None,
                completion_requirements=_requirements(raw.get("completion_requirements")),
                items=items,
            )
        )
    return modules


# ---------------------------------------------------------------------------
# Writers (POST/PUT/DELETE). Only managed fields are ever sent: never
# prerequisites, completion requirements, sequential progress, or unmanaged items.
# Bodies are JSON so that a cleared date is an explicit null.
# ---------------------------------------------------------------------------

_TYPES = {
    "page": "Page",
    "assignment": "Assignment",
    "file": "File",
    "header": "SubHeader",
    "url": "ExternalUrl",
    "quiz": "Quiz",
    "discussion": "Discussion",
}
_KIND_BY_NAME = {"page": Kind.PAGE, "assignment": Kind.ASSIGNMENT, "file": Kind.FILE}


def _modules_path(ctx: ApplyContext) -> str:
    return f"/courses/{ctx.config.course_id}/modules"


def _items_path(ctx: ApplyContext, module_id: int) -> str:
    return f"{_modules_path(ctx)}/{module_id}/items"


def _module_id(ctx: ApplyContext, path: str, known: int | None) -> int:
    if known is not None:
        return known
    new = ctx.ids.get(ObjectKey(Kind.MODULE, path))
    if not isinstance(new, int):
        raise CanvasSyncError(f"{path}: the module this item belongs to has no Canvas id yet")
    return new


def _position_after(
    client: CanvasClient,
    ctx: ApplyContext,
    list_path: str,
    placed: PlacedModule | PlacedItem,
    moving: int | None,
) -> int | None:
    """1-based position that puts ``moving`` right after the predecessor in the live list.

    Only relative order matters: deletes run last, so the live list may still hold items
    that are about to go. ``None`` (append) when the predecessor cannot be found.
    """
    if not placed.place:
        return None
    pred = placed.pred_id
    if pred is None and placed.pred_path is not None:
        new = ctx.ids.get(ObjectKey(Kind.MODULE, placed.pred_path))
        pred = new if isinstance(new, int) else None
        if pred is None:
            return None
    if pred is None:
        return 1
    rows: list[dict[str, Any]] = client.get(list_path, _PER_PAGE)
    ids = [int(r["id"]) for r in sorted(rows, key=_position)]
    if pred not in ids:
        return None
    at = ids.index(pred) + 1
    if moving is not None and moving in ids and ids.index(moving) < ids.index(pred):
        return at
    return at + 1


def _module_body(before: Module | None, after: Module) -> dict[str, Any]:
    body: dict[str, Any] = {}
    if before is None or after.name != before.name:
        body["name"] = after.name
    if after.unlock != (before.unlock if before is not None else None):
        body["unlock_at"] = to_canvas(after.unlock)  # None -> explicit null
    return body


def _create_module(client: CanvasClient, obj: Module, ctx: ApplyContext) -> int:
    body = _module_body(None, obj)
    if isinstance(obj, PlacedModule):
        pos = _position_after(client, ctx, _modules_path(ctx), obj, None)
        if pos is not None:
            body["position"] = pos
    row = client.post(_modules_path(ctx), json={"module": body})
    mid = int(row["id"])
    if obj.published:  # new modules start unpublished; publishing is a separate update
        client.put(f"{_modules_path(ctx)}/{mid}", json={"module": {"published": True}})
    return mid


def _update_module(client: CanvasClient, before: Module, after: Module, ctx: ApplyContext) -> None:
    mid = before.canvas_id
    if mid is None:
        raise CanvasSyncError(f"modules.yaml#{before.name}: module has no Canvas id")
    body = _module_body(before, after)
    if after.published is not None and after.published != before.published:
        body["published"] = after.published
    if isinstance(after, PlacedModule):
        pos = _position_after(client, ctx, _modules_path(ctx), after, mid)
        if pos is not None:
            body["position"] = pos
    if body:
        client.put(f"{_modules_path(ctx)}/{mid}", json={"module": body})


def _content_id(ctx: ApplyContext, item: PlacedItem) -> int | str:
    if item.content_id is not None:
        return item.content_id
    if item.content_path is not None:
        new = ctx.ids.get(ObjectKey(_KIND_BY_NAME[item.item_type], item.content_path))
        if new is not None:
            return new
    raise CanvasSyncError(
        f"{item.module_path}: {item.item_type} {getattr(item, item.item_type)!r} "
        "has no Canvas id yet (was it created earlier in this push?)"
    )


def _create_item(client: CanvasClient, obj: PlacedItem, ctx: ApplyContext) -> int:
    mid = _module_id(ctx, obj.module_path, obj.module_id)
    typ = obj.item_type
    body: dict[str, Any] = {"type": _TYPES[typ], "indent": obj.indent}
    if typ == "page":
        body["page_url"] = str(_content_id(ctx, obj))
    elif typ == "header":
        body["title"] = obj.header
    elif typ == "url":
        body["external_url"] = obj.url
        body["title"] = obj.title or obj.url
        body["new_tab"] = obj.new_tab
    else:  # assignment, file, quiz, discussion
        body["content_id"] = _content_id(ctx, obj)
    if obj.title and typ in ("file", "quiz", "discussion"):
        body["title"] = obj.title
    pos = _position_after(client, ctx, _items_path(ctx, mid), obj, None)
    if pos is not None:
        body["position"] = pos
    row = client.post(_items_path(ctx, mid), json={"module_item": body})
    new_id = int(row["id"])
    if obj.published is not None:
        client.put(
            f"{_items_path(ctx, mid)}/{new_id}",
            json={"module_item": {"published": obj.published}},
        )
    return new_id


def _update_item(
    client: CanvasClient, before: PlacedItem, after: PlacedItem, ctx: ApplyContext
) -> None:
    item_id = before.canvas_id
    if item_id is None:
        raise CanvasSyncError(f"{after.module_path}: module item has no Canvas id")
    target = _module_id(ctx, after.module_path, after.module_id)
    current = after.from_module_id if after.from_module_id is not None else target
    typ = after.item_type
    body: dict[str, Any] = {}
    if typ == "header" and after.header != before.header:
        body["title"] = after.header
    if typ == "url":
        if after.url != before.url:
            body["external_url"] = after.url
        if after.new_tab != before.new_tab:
            body["new_tab"] = after.new_tab
    if typ in ("file", "url", "quiz", "discussion") and after.title not in (None, before.title):
        body["title"] = after.title
    if after.indent != before.indent:
        body["indent"] = after.indent
    if after.published is not None and after.published != before.published:
        body["published"] = after.published
    if target != current:
        body["module_id"] = target
    pos = _position_after(client, ctx, _items_path(ctx, target), after, item_id)
    if pos is not None:
        body["position"] = pos
    if body:
        client.put(f"{_items_path(ctx, current)}/{item_id}", json={"module_item": body})


def create(client: CanvasClient, obj: CanvasObject, ctx: ApplyContext) -> int | str:
    """Returns the new canvas_id (of the module, or of the module item)."""
    if isinstance(obj, PlacedItem):
        return _create_item(client, obj, ctx)
    if isinstance(obj, Module):
        return _create_module(client, obj, ctx)
    raise TypeError(f"modules writer got {type(obj).__name__}")


def update(
    client: CanvasClient, before: CanvasObject, after: CanvasObject, ctx: ApplyContext
) -> None:
    """Only changed fields are sent."""
    if isinstance(before, PlacedItem) and isinstance(after, PlacedItem):
        _update_item(client, before, after, ctx)
    elif isinstance(before, Module) and isinstance(after, Module):
        _update_module(client, before, after, ctx)
    else:
        raise TypeError(f"modules writer got {type(before).__name__} -> {type(after).__name__}")


def delete(client: CanvasClient, obj: CanvasObject, ctx: ApplyContext) -> None:
    """Module or item delete. Operations that touch an assignment item are routed through
    the submission guard by ``apply`` (``Operation.touches_assignment``)."""
    if isinstance(obj, PlacedItem):
        mid = _module_id(ctx, obj.module_path, obj.module_id)
        if obj.canvas_id is None:
            raise CanvasSyncError(f"{obj.module_path}: module item has no Canvas id")
        client.delete(f"{_items_path(ctx, mid)}/{obj.canvas_id}")
    elif isinstance(obj, Module):
        if obj.canvas_id is None:
            raise CanvasSyncError(f"modules.yaml#{obj.name}: module has no Canvas id")
        client.delete(f"{_modules_path(ctx)}/{obj.canvas_id}")
    else:
        raise TypeError(f"modules writer got {type(obj).__name__}")
