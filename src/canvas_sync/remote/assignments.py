"""Assignments reader and writer (CONTRACTS.md section 8).

Writer: ``create`` / ``update`` / ``delete`` for assignments and (create only) assignment groups.
Updates send only the fields that changed; assignment POST/PUT bodies never contain
``assignment_overrides`` (Canvas replaces the whole override set with it). Overrides are
written one call each (``POST/PUT/DELETE .../overrides``) from ``plan.diff_overrides``, matched
to the live overrides by target; mastery-path (``noop_id``) overrides are never touched.
Names resolve to ids through ``ctx.roster`` (``unknown:<id>`` refs carry their id) and an
unresolvable name refuses the assignment's operation before its first write.

Reader (GET only): assignments with their group name, overrides resolved through the
``RosterCache`` to section/group names and student refs, and bodies as for pages
(``Bodies.matches`` reuses the previous Markdown, else ``html_to_md``). Quiz, discussion and
external-tool assignments are not mirrored (they are managed elsewhere in Canvas).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast

from pydantic import ValidationError as PydanticValidationError
from rich.console import Console

from canvas_sync.dates import from_canvas, to_canvas
from canvas_sync.errors import GuardBlocked, ValidationError
from canvas_sync.model.assignment import Assignment
from canvas_sync.model.base import CanvasObject, Kind, ObjectKey
from canvas_sync.model.overrides import Override, validate_overrides
from canvas_sync.plan.assignments import (
    AUTO_TITLE,
    AssignmentGroup,
    OverrideChange,
    assignment_changes,
    custom_title,
    diff_overrides,
    group_key,
)
from canvas_sync.plan.core import Action, Operation
from canvas_sync.remote.context import ApplyContext, FetchContext
from canvas_sync.remote.http import CanvasClient
from canvas_sync.remote.paths import unique_path
from canvas_sync.render.html2md import html_to_md
from canvas_sync.render.links import LinkContext
from canvas_sync.render.md2html import md_to_html

FOLDER = "assignments"
_PER_PAGE = {"per_page": 100}
_NOT_MIRRORED_TYPES = {"online_quiz", "discussion_topic", "external_tool", "attendance"}
PathMap = Mapping[tuple[Kind, int | str], str]


def _warn(message: str) -> None:
    """One-line warning on stderr. Never put a Canvas user name in ``message``."""
    Console(stderr=True).print(f"warning: {message}", markup=False, highlight=False, soft_wrap=True)


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
        taken = {k.path for k in ctx.previous.objects if k.kind == Kind.ASSIGNMENT}
        for row in rows:
            path = by_id.get(int(row["id"]))
            if path is not None:
                out[int(row["id"])] = path
    for row in sorted(rows, key=lambda r: int(r["id"])):
        aid = int(row["id"])
        if aid in out:
            continue
        path = unique_path(FOLDER, str(row.get("name") or ""), taken)
        taken.add(path)
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
        keep = isinstance(title, str) and title and not AUTO_TITLE.fullmatch(title)
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


def _mirrored_rows(client: CanvasClient, ctx: FetchContext) -> list[dict[str, Any]]:
    course = ctx.config.course_id
    rows: list[dict[str, Any]] = client.get(
        f"/courses/{course}/assignments",
        {**_PER_PAGE, "include[]": "overrides", "override_assignment_dates": "false"},
    )
    return [r for r in rows if _is_mirrored(r)]


def assign_paths(client: CanvasClient, ctx: FetchContext) -> dict[int, str]:
    """Assignment id -> tree path for every mirrored assignment (one list call).

    fetch_course uses it in its id/path pre-pass. ``read`` computes the same assignment.
    """
    return _assign_paths(_mirrored_rows(client, ctx), ctx)


def read(
    client: CanvasClient, ctx: FetchContext, *, paths: PathMap | None = None
) -> dict[str, CanvasObject]:
    """Assignments keyed by tree path (``assignments/<slug>.md``).

    Existing assignments keep the path they have in ``ctx.previous`` (matched by
    ``canvas_id``); new ones get ``slugify(title)`` (remote/paths.py) with ``-2``, ``-3``
    on collision.
    Assignments matched by ``.canvasignore`` are left out. ``paths`` maps (kind, id) to tree
    paths (pages by url slug, others by id) for rewriting links in bodies; default: the
    previous tree.
    """
    course = ctx.config.course_id
    groups = _group_names(client, course)
    rows = _mirrored_rows(client, ctx)
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


# ---- writer ---------------------------------------------------------------------------


def _asg(obj: CanvasObject) -> Assignment:
    if not isinstance(obj, Assignment):
        raise TypeError(f"expected an Assignment, got {type(obj).__name__}")
    return obj


def _canvas_id(a: Assignment) -> int:
    if a.canvas_id is None or isinstance(a.canvas_id, str):
        raise ValidationError(f"assignment {a.title!r} has no numeric canvas_id")
    return a.canvas_id


def _group_ids(client: CanvasClient, ctx: ApplyContext, name: str) -> list[int]:
    rows: list[dict[str, Any]] = client.get(
        f"/courses/{ctx.config.course_id}/assignment_groups", _PER_PAGE
    )
    return [int(r["id"]) for r in rows if str(r.get("name") or "") == name]


def _group_id(client: CanvasClient, ctx: ApplyContext, name: str) -> int:
    """Canvas id of the assignment group called ``name`` (created earlier in this push, else
    looked up). Duplicate names in Canvas are ambiguous, so they are refused, not guessed."""
    made = ctx.ids.get(group_key(name))
    if isinstance(made, int):
        return made
    ids = _group_ids(client, ctx, name)
    if len(ids) == 1:
        return ids[0]
    if not ids:
        raise ValidationError(f"assignment group {name!r} does not exist in Canvas")
    raise ValidationError(f"assignment group {name!r} exists more than once in Canvas; rename one")


def _description(a: Assignment, ctx: ApplyContext) -> str:
    if a.body_format == "html":
        return a.body
    # Every assignment file sits directly in assignments/, so relative links resolve the same
    # from any file name.
    link_ctx = LinkContext(
        course_id=ctx.config.course_id,
        from_path=f"{FOLDER}/_.md",
        ids=lambda key: ctx.ids.get(key),
        paths=lambda kind, cid: None,
    )
    return md_to_html(a.body, link_ctx)


def _fields(
    a: Assignment, names: set[str], client: CanvasClient, ctx: ApplyContext
) -> dict[str, Any]:
    """Canvas body for the model fields in ``names``. A cleared value is an explicit null."""
    out: dict[str, Any] = {}
    if "title" in names:
        out["name"] = a.title
    if "points" in names:
        out["points_possible"] = a.points
    if "group" in names:
        out["assignment_group_id"] = None if a.group is None else _group_id(client, ctx, a.group)
    if "submission_types" in names:
        out["submission_types"] = a.submission_types or ["none"]
    if "allowed_extensions" in names:
        out["allowed_extensions"] = a.allowed_extensions
    if "grading_type" in names:
        out["grading_type"] = a.grading_type
    if "omit_from_final_grade" in names:
        out["omit_from_final_grade"] = a.omit_from_final_grade
    if "published" in names and a.published is not None:
        out["published"] = a.published
    if "assign_to_overrides_only" in names:
        out["only_visible_to_overrides"] = a.assign_to_overrides_only
    for local, remote in (("unlock", "unlock_at"), ("due", "due_at"), ("lock", "lock_at")):
        if local in names:
            out[remote] = to_canvas(getattr(a, local))
    if "body" in names or "body_format" in names:
        out["description"] = _description(a, ctx)
    return out


_UNKNOWN = "unknown:"


@dataclass(frozen=True)
class _Target:
    """An override audience in Canvas ids: ("section"|"group", {id}) or ("students", ids)."""

    kind: str
    ids: frozenset[int]


def _unknown_id(ref: str) -> int | None:
    if ref.startswith(_UNKNOWN) and ref[len(_UNKNOWN) :].isdigit():
        return int(ref[len(_UNKNOWN) :])
    return None


def _resolve(o: Override, ctx: ApplyContext, path: str) -> _Target:
    """Canvas ids for an override's target; ValidationError (no student names) if any is unknown."""
    if o.students is not None:
        ids: set[int] = set()
        missing = 0
        for ref in o.students:
            uid = _unknown_id(ref)
            if uid is None:
                uid = ctx.roster.students.get(ref)
            if uid is None:
                missing += 1
            else:
                ids.add(uid)
        if missing:
            raise ValidationError(
                f"{path}: {missing} student(s) in an override are not in the roster cache; "
                "run canvas fetch"
            )
        return _Target("students", frozenset(ids))
    kind, name = ("section", o.section) if o.section is not None else ("group", o.group)
    assert name is not None
    table = ctx.roster.sections if kind == "section" else ctx.roster.groups
    oid = _unknown_id(name)
    if oid is None:
        oid = table.get(name)
    if oid is None:
        raise ValidationError(f"{path}: unknown {kind} {name!r} in an override; run canvas fetch")
    return _Target(kind, frozenset({oid}))


def _live_target(raw: dict[str, Any]) -> _Target | None:
    ids = {n for n in map(_int, _list(raw.get("student_ids"))) if n is not None}
    if ids:
        return _Target("students", frozenset(ids))
    for kind, key in (("group", "group_id"), ("section", "course_section_id")):
        n = _int(raw.get(key))
        if n is not None:
            return _Target(kind, frozenset({n}))
    return None


@dataclass(frozen=True)
class _Step:
    method: Literal["POST", "PUT", "DELETE"]
    override_id: int | None
    body: dict[str, Any] | None


def _override_body(o: Override, target: _Target, *, create: bool) -> dict[str, Any]:
    """Every date is stated (a cleared one is an explicit null); a section or group id only on
    create (an override cannot change its audience)."""
    body: dict[str, Any] = {
        "unlock_at": to_canvas(o.unlock),
        "due_at": to_canvas(o.due),
        "lock_at": to_canvas(o.lock),
    }
    if target.kind == "students":
        body["student_ids"] = sorted(target.ids)
        title = custom_title(o)
        if title is not None:
            body["title"] = title
        elif not create:
            body["title"] = None  # cleared: Canvas falls back to its own "N students"
    elif create:
        body["course_section_id" if target.kind == "section" else "group_id"] = next(
            iter(target.ids)
        )
    return body


def _plan_override_writes(
    client: CanvasClient,
    ctx: ApplyContext,
    aid: int | None,
    changes: list[OverrideChange],
    path: str,
) -> list[_Step]:
    """Read-only: resolve names and match the changes to live overrides. Raises before any
    write if a name is unknown. ``aid`` is None for an assignment that does not exist yet."""
    resolved = [
        (
            c,
            _resolve(c.before, ctx, path) if c.before is not None else None,
            _resolve(c.after, ctx, path) if c.after is not None else None,
        )
        for c in changes
    ]
    live: list[tuple[int, dict[str, Any], _Target]] = []
    if aid is not None and changes:
        rows: list[dict[str, Any]] = client.get(
            f"/courses/{ctx.config.course_id}/assignments/{aid}/overrides", _PER_PAGE
        )
        for raw in rows:
            lt = None if raw.get("noop_id") else _live_target(raw)  # mastery paths: untouched
            if lt is not None and _int(raw.get("id")) is not None:
                live.append((int(raw["id"]), raw, lt))

    def find(old: _Target, title: str | None) -> int | None:
        exact = [oid for oid, _, lt in live if lt == old]
        if len(exact) > 1:
            raise ValidationError(
                f"{path}: more than one Canvas override targets the same {old.kind}; "
                "fix it in Canvas first"
            )
        if exact:
            return exact[0]
        if old.kind == "students" and title:  # membership drifted on Canvas: match by title
            hits = [
                oid for oid, raw, lt in live if lt.kind == "students" and raw.get("title") == title
            ]
            if len(hits) > 1:
                raise ValidationError(
                    f"{path}: more than one Canvas student override has the title {title!r}; "
                    "fix it in Canvas first"
                )
            if hits:
                return hits[0]
        return None

    steps: list[_Step] = []
    for c, old, new in resolved:
        if c.kind == "remove" and old is not None and c.before is not None:
            oid = find(old, custom_title(c.before))
            if oid is not None:  # already gone on Canvas: nothing to do
                steps.append(_Step("DELETE", oid, None))
        elif c.after is not None and new is not None:
            oid = (
                find(old, custom_title(c.before) if c.before else None) if old else find(new, None)
            )
            if oid is None:
                steps.append(_Step("POST", None, _override_body(c.after, new, create=True)))
            else:
                steps.append(_Step("PUT", oid, _override_body(c.after, new, create=False)))
    return steps


def _run_override_steps(
    client: CanvasClient, ctx: ApplyContext, aid: int, steps: list[_Step]
) -> None:
    base = f"/courses/{ctx.config.course_id}/assignments/{aid}/overrides"
    for step in steps:
        if step.method == "DELETE":
            client.delete(f"{base}/{step.override_id}")
        elif step.method == "PUT":
            client.put(f"{base}/{step.override_id}", json={"assignment_override": step.body})
        else:
            client.post(base, json={"assignment_override": step.body})


def preflight(
    client: CanvasClient, before: CanvasObject | None, after: CanvasObject, ctx: ApplyContext
) -> None:
    """Read-only, called by apply before any write: refuse an override that names an unknown
    section, group or student (so a push never half-applies)."""
    if not isinstance(after, Assignment):
        return
    old = before if isinstance(before, Assignment) else None
    if old is not None and old.overrides == after.overrides:
        return
    where = f"assignment {after.title!r}"
    aid = old.canvas_id if old is not None and isinstance(old.canvas_id, int) else None
    _plan_override_writes(
        client, ctx, aid, diff_overrides(old.overrides if old else [], after.overrides), where
    )


def create(client: CanvasClient, obj: CanvasObject, ctx: ApplyContext) -> int | str:
    """Returns the new canvas_id. An :class:`AssignmentGroup` reuses a group of that name that
    already exists in Canvas instead of creating a duplicate."""
    course = ctx.config.course_id
    if isinstance(obj, AssignmentGroup):
        ids = _group_ids(client, ctx, obj.name)
        if len(ids) == 1:
            return ids[0]
        if ids:
            raise ValidationError(f"assignment group {obj.name!r} exists more than once in Canvas")
        made = client.post(f"/courses/{course}/assignment_groups", json={"name": obj.name})
        return int(made["id"])
    a = _asg(obj)
    # Resolve every override name first: nothing is written if one is unknown.
    steps = _plan_override_writes(
        client, ctx, None, diff_overrides([], a.overrides), f"assignment {a.title!r}"
    )
    names = {"title", "grading_type", "omit_from_final_grade"}
    if a.points is not None:
        names.add("points")
    if a.group is not None:
        names.add("group")
    if a.submission_types:
        names.add("submission_types")
    if a.allowed_extensions:
        names.add("allowed_extensions")
    if a.body:
        names.add("body")
    names |= {n for n in ("unlock", "due", "lock") if getattr(a, n) is not None}
    if a.assign_to_overrides_only:
        names.add("assign_to_overrides_only")
    body = _fields(a, names, client, ctx)
    body["published"] = a.published if a.published is not None else ctx.config.defaults.published
    made = client.post(f"/courses/{course}/assignments", json={"assignment": body})
    new_id = int(made["id"])
    _run_override_steps(client, ctx, new_id, steps)
    return new_id


def update(
    client: CanvasClient, before: CanvasObject, after: CanvasObject, ctx: ApplyContext
) -> None:
    """Only changed fields are sent. Overrides: names are resolved and live overrides read
    before the first write; then one call per changed override. Turning on
    ``assign_to_overrides_only`` happens after the overrides exist, so the audience is never
    empty. The guard has already run on this operation (CONTRACTS.md section 10)."""
    old, new = _asg(before), _asg(after)
    aid = _canvas_id(old)
    changed = set(assignment_changes(old, new))
    steps = (
        _plan_override_writes(
            client, ctx, aid, diff_overrides(old.overrides, new.overrides), f"assignment {aid}"
        )
        if "overrides" in changed
        else []
    )
    body = _fields(new, changed - {"overrides"}, client, ctx)
    turning_on = "assign_to_overrides_only" in changed and new.assign_to_overrides_only
    url = f"/courses/{ctx.config.course_id}/assignments/{aid}"
    if turning_on:
        _run_override_steps(client, ctx, aid, steps)
    if body:
        client.put(url, json={"assignment": body})
    if not turning_on:
        _run_override_steps(client, ctx, aid, steps)


def delete(client: CanvasClient, obj: CanvasObject, ctx: ApplyContext) -> None:
    """Deletes an assignment only if the guard allows it. The guard re-reads Canvas and refuses
    when any submission exists (no flag overrides that); it is consulted here as well, so that
    no caller can reach the DELETE without it. Assignment groups are never deleted."""
    if isinstance(obj, AssignmentGroup):
        raise GuardBlocked("assignment groups are never deleted (delete empty groups in Canvas)")
    a = _asg(obj)
    aid = _canvas_id(a)
    op = Operation(ObjectKey(Kind.ASSIGNMENT, f"{FOLDER}/{aid}"), Action.DELETE, a, None, {})
    ctx.guard.assert_allowed(op, set())
    client.delete(f"/courses/{ctx.config.course_id}/assignments/{aid}")
