"""Assignments reader and writer (CONTRACTS.md section 8).

Reader (GET only): assignments with their group name, overrides resolved through the
``RosterCache`` to section/group names and student refs, and bodies as for pages
(``Bodies.matches`` reuses the previous Markdown, else ``html_to_md``). Quiz, discussion and
external-tool assignments are not mirrored (they are managed elsewhere in Canvas).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from typing import Any, Literal, cast

from pydantic import ValidationError as PydanticValidationError
from rich.console import Console

from canvas_sync.dates import from_canvas
from canvas_sync.errors import ValidationError
from canvas_sync.model.assignment import Assignment
from canvas_sync.model.base import CanvasObject, Kind, ObjectKey
from canvas_sync.model.overrides import Override, validate_overrides
from canvas_sync.remote.context import ApplyContext, FetchContext
from canvas_sync.remote.http import CanvasClient
from canvas_sync.render.html2md import html_to_md
from canvas_sync.render.links import LinkContext

FOLDER = "assignments"
_PER_PAGE = {"per_page": 100}
_NOT_MIRRORED_TYPES = {"online_quiz", "discussion_topic", "external_tool", "attendance"}
_AUTO_TITLE = re.compile(r"\d+ students?")  # Canvas's own title for an ad hoc student set
PathMap = Mapping[tuple[Kind, int | str], str]


def _warn(message: str) -> None:
    """One-line warning on stderr. Never put a Canvas user name in ``message``."""
    Console(stderr=True).print(f"warning: {message}", markup=False, highlight=False, soft_wrap=True)


def _slugify(title: str) -> str:
    """Lower-case ASCII slug of ``title`` (``Homework 1: Intro!`` -> ``homework-1-intro``)."""
    text = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:80].strip("-")
    return slug or "untitled"


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []  # pyright: ignore[reportUnknownArgumentType]


def _is_mirrored(raw: dict[str, Any]) -> bool:
    kinds = {str(t) for t in _list(raw.get("submission_types"))}
    return not (
        raw.get("is_quiz_assignment")
        or raw.get("quiz_id")
        or raw.get("discussion_topic")
        or kinds & _NOT_MIRRORED_TYPES
    )


def _assign_paths(rows: list[dict[str, Any]], ctx: FetchContext) -> dict[int, str]:
    """Canvas id -> tree path: the previous path (matched by id), else a fresh slug."""
    taken: set[str] = set()
    out: dict[int, str] = {}
    if ctx.previous is not None:
        by_id = {
            o.canvas_id: key.path
            for key, o in ctx.previous.objects.items()
            if key.kind == Kind.ASSIGNMENT and o.canvas_id is not None
        }
        taken = {k.path.casefold() for k in ctx.previous.objects if k.kind == Kind.ASSIGNMENT}
        for row in rows:
            path = by_id.get(int(row["id"]))
            if path is not None:
                out[int(row["id"])] = path
    for row in sorted(rows, key=lambda r: int(r["id"])):
        aid = int(row["id"])
        if aid in out:
            continue
        base = _slugify(str(row.get("name") or ""))
        path, n = f"{FOLDER}/{base}.md", 2
        while path.casefold() in taken:
            path, n = f"{FOLDER}/{base}-{n}.md", n + 1
        taken.add(path.casefold())
        out[aid] = path
    return out


def _known_paths(
    ctx: FetchContext, assignments: Mapping[int, str], given: PathMap | None
) -> PathMap:
    """(kind, canvas id) -> tree path: ``given`` or else the previous tree, plus this read's
    assignments (``given`` wins where it names one)."""
    known: dict[tuple[Kind, int | str], str] = dict(given) if given is not None else {}
    if given is None and ctx.previous is not None:
        for key, obj in ctx.previous.objects.items():
            if obj.canvas_id is not None:
                known[(key.kind, obj.canvas_id)] = key.path
    for aid, path in assignments.items():
        if given is None:
            known[(Kind.ASSIGNMENT, aid)] = path
        else:
            known.setdefault((Kind.ASSIGNMENT, aid), path)
    return known


def _link_ctx(path: str, ctx: FetchContext, known: PathMap) -> LinkContext:
    by_key = {ObjectKey(k, p): i for (k, i), p in known.items()}
    return LinkContext(
        course_id=ctx.config.course_id,
        from_path=path,
        ids=lambda key: by_key.get(key),
        paths=lambda kind, cid: known.get((kind, cid)),
    )


def _body(
    html: str, path: str, prev: CanvasObject | None, ctx: FetchContext, known: PathMap
) -> tuple[str, Literal["md", "html"]]:
    """(body, body_format) for the remote ``html``."""
    if not html.strip():
        return "", "md"
    if isinstance(prev, Assignment):
        if ctx.bodies.matches(path, html):
            return prev.body, prev.body_format
        if prev.body_format == "html":
            return html, "html"
    return html_to_md(html, _link_ctx(path, ctx, known)), "md"


def _override(raw: dict[str, Any], ctx: FetchContext, where: str) -> Override | None:
    unlock = from_canvas(raw.get("unlock_at"), ctx.tz)
    due = from_canvas(raw.get("due_at"), ctx.tz)
    lock = from_canvas(raw.get("lock_at"), ctx.tz)
    title = raw.get("title")
    student_ids = _list(raw.get("student_ids"))
    if student_ids:
        refs: list[str] = []
        unknown = 0
        for uid in student_ids:
            n = _int(uid)
            if n is None:
                continue
            ref = ctx.roster.ref_for(n)
            if ref is None:
                unknown += 1
                ref = f"unknown:{n}"
            refs.append(ref)
        if not refs:
            _warn(f"{where}: override with no usable student ids is not mirrored")
            return None
        if unknown:
            _warn(f"{where}: override names {unknown} student(s) not in the roster cache")
        keep = isinstance(title, str) and title and not _AUTO_TITLE.fullmatch(title)
        return Override(
            students=refs, title=title if keep else None, unlock=unlock, due=due, lock=lock
        )
    group_id = _int(raw.get("group_id"))
    if group_id is not None:
        gname = next((n for n, i in ctx.roster.groups.items() if i == group_id), None)
        if gname is None:
            _warn(f"{where}: override names a group not in the roster cache")
        return Override(group=gname or f"unknown:{group_id}", unlock=unlock, due=due, lock=lock)
    section_id = _int(raw.get("course_section_id"))
    if section_id is not None:
        sname = next((n for n, i in ctx.roster.sections.items() if i == section_id), None)
        if sname is None:
            _warn(f"{where}: override names a section not in the roster cache")
        return Override(section=sname or f"unknown:{section_id}", unlock=unlock, due=due, lock=lock)
    _warn(f"{where}: override with no section, group or students is not mirrored")
    return None


def _group_names(client: CanvasClient, course: int) -> dict[int, str]:
    rows: list[dict[str, Any]] = client.get(f"/courses/{course}/assignment_groups", _PER_PAGE)
    out: dict[int, str] = {}
    seen: set[str] = set()
    for row in rows:
        name = str(row.get("name") or "")
        if name in seen:
            _warn(f"assignment group {name!r} exists more than once in Canvas")
        seen.add(name)
        out[int(row["id"])] = name
    return out


def read(
    client: CanvasClient, ctx: FetchContext, *, paths: PathMap | None = None
) -> dict[str, CanvasObject]:
    """Assignments keyed by tree path (``assignments/<slug>.md``).

    Existing assignments keep the path they have in ``ctx.previous`` (matched by
    ``canvas_id``); new ones get ``_slugify(title)`` with ``-2``, ``-3`` on collision.
    Assignments matched by ``.canvasignore`` are left out. ``paths`` maps (kind, id) to tree
    paths (pages by url slug, others by id) for rewriting links in bodies; default: the
    previous tree.
    """
    course = ctx.config.course_id
    groups = _group_names(client, course)
    rows: list[dict[str, Any]] = client.get(
        f"/courses/{course}/assignments",
        {**_PER_PAGE, "include[]": "overrides", "override_assignment_dates": "false"},
    )
    rows = [r for r in rows if _is_mirrored(r)]
    tree_paths = _assign_paths(rows, ctx)
    known = _known_paths(ctx, tree_paths, paths)
    previous = ctx.previous.objects if ctx.previous is not None else {}
    out: dict[str, CanvasObject] = {}
    for raw in rows:
        path = tree_paths[int(raw["id"])]
        if ctx.ignore.ignored(path):
            continue
        prev = previous.get(ObjectKey(Kind.ASSIGNMENT, path))
        out[path] = _assignment(raw, path, groups, ctx, prev, known)
    return dict(sorted(out.items()))


def _assignment(
    raw: dict[str, Any],
    path: str,
    groups: Mapping[int, str],
    ctx: FetchContext,
    prev: CanvasObject | None,
    known: PathMap,
) -> Assignment:
    only = bool(raw.get("only_visible_to_overrides"))
    found: list[Override | None] = []
    for r in _list(raw.get("overrides")):
        if isinstance(r, dict) and not r.get("noop_id"):  # pyright: ignore[reportUnknownMemberType]
            found.append(_override(cast("dict[str, Any]", r), ctx, path))
    overrides = [o for o in found if o is not None]
    body, fmt = _body(str(raw.get("description") or ""), path, prev, ctx, known)
    group_id = _int(raw.get("assignment_group_id"))
    points = raw.get("points_possible")
    published = raw.get("published")
    # With "only visible to overrides" the base dates are meaningless (no "Everyone else").
    base = (
        {}
        if only
        else {
            "unlock": from_canvas(raw.get("unlock_at"), ctx.tz),
            "due": from_canvas(raw.get("due_at"), ctx.tz),
            "lock": from_canvas(raw.get("lock_at"), ctx.tz),
        }
    )
    try:
        validate_overrides(overrides)
    except ValidationError:
        raise ValidationError(
            f"{path}: Canvas overrides cannot be mirrored (a target is in more than one override)"
        ) from None
    try:
        return Assignment(
            canvas_id=int(raw["id"]),
            title=str(raw.get("name") or ""),
            points=float(points) if isinstance(points, (int, float)) else None,
            group=groups.get(group_id) if group_id is not None else None,
            submission_types=[str(t) for t in _list(raw.get("submission_types"))],
            allowed_extensions=[str(e) for e in _list(raw.get("allowed_extensions"))],
            grading_type=str(raw.get("grading_type") or "points"),
            omit_from_final_grade=bool(raw.get("omit_from_final_grade")),
            published=published if isinstance(published, bool) else None,
            assign_to_overrides_only=only,
            overrides=overrides,
            body_format=fmt,
            body=body,
            **base,
        )
    except (ValidationError, PydanticValidationError):
        # Not echoed: the messages can name students.
        raise ValidationError(
            f"{path}: Canvas data cannot be mirrored (invalid assignment)"
        ) from None


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
