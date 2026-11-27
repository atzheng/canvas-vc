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

from canvas_sync.dates import from_canvas
from canvas_sync.model.base import CanvasObject, Kind
from canvas_sync.model.modules import (
    CompletionRequirement,
    Module,
    ModuleItem,
    UnmanagedItem,
    UnmanagedRef,
)
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


def create(client: CanvasClient, obj: CanvasObject, ctx: ApplyContext) -> int | str:
    """Returns the new canvas_id."""
    raise NotImplementedError


def update(
    client: CanvasClient, before: CanvasObject, after: CanvasObject, ctx: ApplyContext
) -> None:
    """Only changed fields are sent."""
    raise NotImplementedError


def delete(client: CanvasClient, obj: CanvasObject, ctx: ApplyContext) -> None:
    """Goes through the submission guard."""
    raise NotImplementedError
