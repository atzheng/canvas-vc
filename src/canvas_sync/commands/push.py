"""canvas push (SPEC.md "Push rules", CONTRACTS.md sections 9-11).

Order of events in :func:`run_push`:

1. refusals that need no network: unmerged paths, conflict markers, dirty tree,
   canvas/main not an ancestor of HEAD, ``validate`` errors;
2. plan = diff(canvas/main, HEAD), with ``.canvasignore`` applied to both trees;
3. display, then refusals that need the live guard (hard blocks, missing confirm flags)
   and the live staleness check of every touched object, then the confirmation prompt;
4. backup of every touched assignment (abort on failure), then ``apply`` in write mode;
5. a second ``apply`` for bodies that link to objects created later in the same push;
6. refetch, snapshot with HEAD as an extra parent, fast-forward.

There is no separate write-back commit: the new ids arrive on the branch through the
fast-forward to the snapshot. The refetch uses HEAD plus the new ids as its "previous"
tree, so objects created by this push keep the file paths the user gave them.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated

import typer
from pydantic import BaseModel
from rich.console import Console

from canvas_sync import gitops
from canvas_sync.apply import ApplyResult, apply, needs_guard
from canvas_sync.commands.fetch import bodies_at, committed_inputs
from canvas_sync.commands.validate import find_conflicts, has_errors, validate_tree
from canvas_sync.config import CourseConfig, get_token, load_config
from canvas_sync.errors import (
    CanvasSyncError,
    DirtyTree,
    GuardBlocked,
    NotFastForward,
    StaleRemote,
    UnresolvedConflicts,
    ValidationError,
)
from canvas_sync.ignore import IgnoreRules
from canvas_sync.model.base import CanvasObject, Kind, ObjectKey
from canvas_sync.model.course import Course, dump_tree
from canvas_sync.model.modules import Module, ModuleItem
from canvas_sync.model.roster import RosterCache
from canvas_sync.plan.core import Action, Operation, Plan, diff, unconfirmed_risks
from canvas_sync.plan.modules import PlacedItem, module_keys
from canvas_sync.remote import roster
from canvas_sync.remote.backup import backup
from canvas_sync.remote.context import ApplyContext, FetchContext
from canvas_sync.remote.fetch import fetch_course
from canvas_sync.remote.guard import SubmissionGuard
from canvas_sync.remote.http import CanvasClient
from canvas_sync.remote.paths import apply_link_context
from canvas_sync.render.bodies import Bodies
from canvas_sync.render.links import LinkContext
from canvas_sync.render.md2html import md_to_html
from canvas_sync.snapshot import BlobStore, snapshot

ALL_FLAGS = frozenset({"--confirm-grading-change", "--confirm-access-removal", "--confirm-notify"})
_BODY_KINDS = (Kind.PAGE, Kind.ASSIGNMENT, Kind.ANNOUNCEMENT)


@dataclass
class PushResult:
    """What happened. ``ok`` is False after a partial failure or an unexpected snapshot."""

    ops_total: int = 0
    ops_done: int = 0
    commit: str | None = None  # new canvas/main commit
    fast_forwarded: bool = False
    failed: Operation | None = None
    error: Exception | None = None
    unexpected: list[str] = field(default_factory=list[str])  # tree paths that differ from HEAD
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        return self.failed is None and not self.unexpected


# ---- trees ------------------------------------------------------------------------------


def _without_ignored(course: Course, ignore: IgnoreRules) -> Course:
    objs = {k: v for k, v in course.objects.items() if not ignore.ignored(k.path)}
    files = {p: e for p, e in course.files_meta.files.items() if not ignore.ignored(p)}
    meta = course.files_meta.model_copy(update={"files": files})
    return dataclasses.replace(course, objects=objs, files_meta=meta)


def _with_ids(course: Course, ids: dict[ObjectKey, int | str]) -> Course:
    """``course`` with the ids in ``ids`` (object keys, module keys, module item keys)."""
    objs: dict[ObjectKey, CanvasObject] = {}
    for key, obj in course.objects.items():
        new = ids.get(key)
        objs[key] = obj.model_copy(update={"canvas_id": new}) if new is not None else obj
    files = {p: objs.get(ObjectKey(Kind.FILE, p), e) for p, e in course.files_meta.files.items()}
    meta = course.files_meta.model_copy(update={"files": files})
    mods: list[Module] = []
    for mkey, mod in zip(module_keys(course.modules), course.modules, strict=True):
        items: list[ModuleItem] = []
        for j, item in enumerate(mod.items):
            iid = ids.get(ObjectKey(Kind.MODULE, f"{mkey}/items[{j}]"))
            items.append(
                item.model_copy(update={"canvas_id": iid}) if isinstance(iid, int) else item
            )
        mid = ids.get(ObjectKey(Kind.MODULE, mkey))
        update: dict[str, object] = {"items": items}
        if isinstance(mid, int):
            update["canvas_id"] = mid
        mods.append(mod.model_copy(update=update))
    return dataclasses.replace(course, objects=objs, files_meta=meta, modules=mods)


def _existing_ids(target: Course) -> dict[ObjectKey, int | str]:
    """Ids of every object in the target tree that already has one (str slugs for pages)."""
    ids: dict[ObjectKey, int | str] = {}
    for key, obj in target.objects.items():
        if obj.canvas_id is not None:
            ids[key] = obj.canvas_id
    for mkey, mod in zip(module_keys(target.modules), target.modules, strict=True):
        if mod.canvas_id is not None:
            ids[ObjectKey(Kind.MODULE, mkey)] = mod.canvas_id
        for j, item in enumerate(mod.items):
            if item.canvas_id is not None:
                ids[ObjectKey(Kind.MODULE, f"{mkey}/items[{j}]")] = item.canvas_id
    return ids


# ---- pre-flight checks ------------------------------------------------------------------


def check_repo(root: Path, *, allow_dirty: bool) -> tuple[str, str]:
    """Refuse unless the repository is in a pushable state; returns (HEAD, canvas/main)."""
    head = gitops.head_commit(root)
    if head is None:
        raise NotFastForward("no commits yet; nothing to push")
    unmerged = gitops.unmerged_paths(root)
    if unmerged:
        raise UnresolvedConflicts(
            f"unmerged paths: {', '.join(unmerged)}; resolve them and commit, then push again"
        )
    marked = find_conflicts(root)
    if marked:
        raise UnresolvedConflicts(
            f"conflict markers in {', '.join(marked)}; resolve them and commit, then push again"
        )
    if not allow_dirty and not gitops.is_clean(root):
        raise DirtyTree("working tree has uncommitted changes; commit them or use --allow-dirty")
    main = gitops.ref_commit(root)
    if main is None:
        raise NotFastForward("no canvas/main yet; run canvas fetch (or canvas pull) first")
    if not gitops.is_ancestor(root, main, head):
        if gitops.is_ancestor(root, head, main):
            hint = (
                f"git merge --ff-only {PUSHED_REF}"
                if gitops.ref_commit(root, PUSHED_REF)
                and gitops.is_ancestor(root, head, PUSHED_REF)
                else "canvas pull"
            )
            raise NotFastForward(
                f"canvas/main is ahead of HEAD (a previous push did not fast-forward); run {hint}"
            )
        raise NotFastForward("canvas/main is not an ancestor of HEAD; run canvas pull and merge")
    return head, main


def check_validation(root: Path, console: Console) -> None:
    issues = validate_tree(root)
    errors = [i for i in issues if i.severity == "error"]
    if not has_errors(issues):
        return
    for issue in errors[:20]:
        console.print(f"  {issue}", markup=False, highlight=False)
    raise ValidationError(f"validation failed ({len(errors)} errors); run canvas validate")


def _context(
    root: Path, config: CourseConfig, previous: Course | None, bodies: Bodies
) -> FetchContext:
    config, ignore = committed_inputs(root, config)
    return FetchContext(
        config=config,
        tz=config.tz,
        bodies=bodies,
        previous=previous,
        roster=roster.load_cache(root) if (root / roster.CACHE_PATH).exists() else RosterCache(),
        ignore=ignore,
        blob_store=BlobStore(root),
    )


def _path_by_id(course: Course, kind: Kind, cid: int | str | None, path: str) -> str | None:
    """Tree path of the object of ``kind`` with Canvas id ``cid`` (or at ``path`` without one)."""
    if cid is None:
        return path if ObjectKey(kind, path) in course.objects else None
    for key, obj in course.objects.items():
        if key.kind is kind and obj.canvas_id == cid:
            return key.path
    return None


def check_stale(
    root: Path,
    client: CanvasClient,
    config: CourseConfig,
    plan: Plan,
    base: Course,
    main: str,
) -> None:
    """Live staleness check: re-read the course and compare every object the plan changes
    with canvas/main, in canonical serialized form (a fetched object and one loaded from a
    file differ in trivia such as a trailing newline). Raises ``StaleRemote`` naming the
    objects that differ."""
    ctx = _context(root, config, base, bodies_at(root, main))
    live = fetch_course(client, ctx, root=root)
    old, new = dump_tree(base), dump_tree(live)
    stale: list[str] = []
    for op in plan.ops:
        if op.key.kind is Kind.MODULE:
            if old.get("modules.yaml") != new.get("modules.yaml"):
                stale.append("modules.yaml")
            continue
        if op.before is None:
            continue
        cid = op.before.canvas_id
        bpath = _path_by_id(base, op.key.kind, cid, op.key.path)
        if bpath is None:
            continue
        lpath = _path_by_id(live, op.key.kind, cid, op.key.path)
        if lpath != bpath or old.get(bpath) != new.get(bpath):
            stale.append(op.key.path)
    if stale:
        names = ", ".join(dict.fromkeys(stale))
        raise StaleRemote(f"Canvas has changed since your last fetch ({names}); run canvas pull")


def _guard_preflight(
    plan: Plan, guard: SubmissionGuard, confirmed: set[str], *, dry_run: bool
) -> None:
    """Hard guard refusals and missing confirm flags, before anything is asked or written.
    Every problem is reported at once, one per line segment."""
    problems: list[str] = []
    for op in plan.ops:
        if not needs_guard(op):
            continue
        path = op.key.path
        try:
            guard.assert_allowed(op, set(ALL_FLAGS) if dry_run else confirmed)
        except GuardBlocked as e:
            msg = str(e)
            if op.action is Action.DELETE and op.key.kind is Kind.ASSIGNMENT:
                problems.append(
                    f"{path}: assignment has student submissions and cannot be deleted ({msg}); "
                    f"restore it (git checkout -- {path}) or add it to .canvasignore "
                    "and run canvas fetch"
                )
            else:
                problems.append(msg if msg.startswith(path) else f"{path}: {msg}")
    if not dry_run:
        flagged = {p.split(":", 1)[0] for p in problems}
        for o, r in unconfirmed_risks(plan, confirmed):
            if o.key.path not in flagged:
                problems.append(f"{o.key.path}: {r.message} (needs {r.needs_confirm})")
    if problems:
        raise GuardBlocked("; ".join(problems))


def _backup_ids(plan: Plan) -> list[int]:
    """Canvas ids of the existing assignments the plan touches (creates have none)."""
    ids: set[int] = set()
    for op in plan.ops:
        if op.key.kind is Kind.ASSIGNMENT and op.action is not Action.CREATE:
            cid = op.before.canvas_id if op.before is not None else None
            if isinstance(cid, int) and not isinstance(cid, bool):
                ids.add(cid)
        elif op.touches_assignment:
            for obj in (op.before, op.after):
                if isinstance(obj, PlacedItem) and obj.assignment is not None:
                    cid = obj.content_id
                    if isinstance(cid, int) and not isinstance(cid, bool):
                        ids.add(cid)
    return sorted(ids)


# ---- second pass: links to objects created later in the same push ----------------------


def _render(ctx: ApplyContext, key: ObjectKey, body: str) -> str:
    if key.kind is Kind.ASSIGNMENT:  # the assignments writer resolves from assignments/_.md
        link = LinkContext(
            course_id=ctx.config.course_id,
            from_path="assignments/_.md",
            ids=lambda k: ctx.ids.get(k),
            paths=lambda kind, cid: None,
        )
        return md_to_html(body, link)
    return md_to_html(body, apply_link_context(ctx, key.path))


def _rerender_ops(plan: Plan, ctx: ApplyContext, before_ids: dict[ObjectKey, int | str]) -> Plan:
    """UPDATE operations for bodies whose HTML changes now that every object created by
    this push has an id (a link to an object created later in the push was left as written)."""
    early = dataclasses.replace(ctx, ids=before_ids)
    ops: list[Operation] = []
    for op in plan.ops:
        after = op.after
        if op.key.kind not in _BODY_KINDS or after is None or op.action is Action.DELETE:
            continue
        if getattr(after, "body_format", "md") != "md":
            continue
        body = str(getattr(after, "body", ""))
        if not body or _render(early, op.key, body) == _render(ctx, op.key, body):
            continue
        cid = ctx.ids.get(op.key)
        fresh = after.model_copy(update={"canvas_id": cid})
        stale = fresh.model_copy(update={"body": ""})
        ops.append(Operation(op.key, Action.UPDATE, stale, fresh, {"body": ("", body)}))
    return Plan(ops)


def _record_assignment_bodies(client: CanvasClient, ctx: ApplyContext, plan: Plan) -> None:
    """The assignments writer does not record bodies, so do it here: the HTML Canvas holds
    for each pushed Markdown body, so the refetch reuses the Markdown instead of converting."""
    c = ctx.config.course_id
    for op in plan.ops:
        after = op.after
        if op.key.kind is not Kind.ASSIGNMENT or op.action is Action.DELETE or after is None:
            continue
        body, fmt = str(getattr(after, "body", "")), getattr(after, "body_format", "md")
        cid = ctx.ids.get(op.key)
        if not body or fmt != "md" or not isinstance(cid, int):
            continue
        row = client.get_one(f"/courses/{c}/assignments/{cid}")
        if isinstance(row, dict):
            html = str(row.get("description") or "")  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
            ctx.bodies.record(op.key.path, body, html)


PUSHED_REF = "refs/canvas/pushed"


def _branch_target(
    root: Path, head: str, new_main: str, managed: set[str], preserve: set[str]
) -> str:
    """Commit to fast-forward the branch to. Normally canvas/main itself. A plain fast-forward
    would delete files the snapshot has no place for (``.gitignore``, ``.canvasignore``,
    ignored objects, a README) and rewrite files whose content did not change but whose bytes
    differ from the canonical dump (comments, formatting) -- ``preserve`` lists those. If there
    are any, the branch gets a merge commit (parents HEAD and canvas/main) whose tree is the
    snapshot with those files put back from HEAD."""
    tree = gitops.tree_with_carried_files(root, new_main, head, managed, preserve)
    if tree is None:
        return new_main
    return gitops.commit_tree(root, tree, [head, new_main], "canvas push: keep local files")


def _move_bodies(bodies: Bodies, base: Course, target: Course) -> None:
    """A page, assignment or announcement whose file moved keeps its recorded body."""
    for key, obj in target.objects.items():
        if key.kind not in _BODY_KINDS or obj.canvas_id is None or key.path in bodies.entries:
            continue
        for bkey, bobj in base.objects.items():
            if bkey.kind is key.kind and bobj.canvas_id == obj.canvas_id and bkey != key:
                rec = bodies.entries.get(bkey.path)
                if rec is not None:
                    bodies.entries[key.path] = dict(rec)
                break


# ---- the push ---------------------------------------------------------------------------


def _build_plan(
    root: Path, head: str, main: str, ignore: IgnoreRules
) -> tuple[Plan, Course, Course]:
    """canvas/main tree vs HEAD tree, ``.canvasignore`` applied to both before diffing (an
    ignored file absent from HEAD must not plan a DELETE); a parse error in either tree
    aborts. Kept in one place so it can be swapped for ``commands.diff.build_plan``."""
    base = _without_ignored(gitops.load_course_at(root, main), ignore)
    target = _without_ignored(gitops.load_course_at(root, head), ignore)
    return diff(base, target), base, target


def _count(plan: Plan) -> str:
    n = len(plan.ops)
    return f"{n} change" + ("" if n == 1 else "s")


def _user_fields_differ(exp: BaseModel, got: BaseModel, skip: set[str]) -> bool:
    """Do the fields the user wrote in ``exp`` differ in ``got``? Fields the user left out
    may take any value (Canvas fills in defaults for a new object)."""
    for f in exp.model_fields_set - skip - {"canvas_id"}:
        ev, gv = getattr(exp, f), getattr(got, f, None)
        if f == "body":
            ev, gv = str(ev).strip(), str(gv or "").strip()
        if ev != gv:
            return True
    return False


def _modules_differ(expected: Course, fetched: Course) -> bool:
    if len(expected.modules) != len(fetched.modules):
        return True
    for em, gm in zip(expected.modules, fetched.modules, strict=True):
        if _user_fields_differ(em, gm, {"items"}) or len(em.items) != len(gm.items):
            return True
        if any(
            _user_fields_differ(ei, gi, set()) for ei, gi in zip(em.items, gm.items, strict=True)
        ):
            return True
    return False


def _unexpected(expected: Course, fetched: Course, plan: Plan) -> list[str]:
    """Tree paths the push changed where Canvas stored something other than what HEAD says
    (apart from ids, and values the user did not write). Deleted objects are not checked:
    Canvas may keep one (an unpublished announcement), and the fast-forward then brings its
    file back, like any remote change."""
    bad: list[str] = []
    modules_checked = False
    for op in plan.ops:
        if op.after is None or op.action is Action.DELETE:
            continue
        if op.key.kind is Kind.MODULE:
            if not modules_checked and _modules_differ(expected, fetched):
                bad.append("modules.yaml")
            modules_checked = True
            continue
        if op.key.kind is Kind.FILE:
            continue
        exp, got = expected.objects.get(op.key), fetched.objects.get(op.key)
        if exp is None:
            continue
        if got is None or _user_fields_differ(exp, got, set()):
            bad.append(op.key.path)
    return sorted(set(bad))


def run_push(
    root: Path,
    client: CanvasClient,
    config: CourseConfig,
    *,
    dry_run: bool = False,
    yes: bool = False,
    allow_dirty: bool = False,
    no_delete: bool = False,
    confirmed: set[str] | None = None,
    confirm: Callable[[str], bool] | None = None,
    console: Console | None = None,
) -> PushResult:
    """Push HEAD to Canvas. Raises ``CanvasSyncError`` subclasses for refusals (nothing
    written); returns a result (``ok`` False) when the push stopped part way."""
    out = console or Console()
    confirmed = set(confirmed or ())
    head, main = check_repo(root, allow_dirty=allow_dirty)
    check_validation(root, out)

    config, ignore = committed_inputs(root, config)
    plan, base, target = _build_plan(root, head, main, ignore)
    skipped: list[Operation] = []
    if no_delete:
        skipped = [o for o in plan.ops if o.action is Action.DELETE]
        plan = Plan([o for o in plan.ops if o.action is not Action.DELETE])
    result = PushResult(ops_total=len(plan.ops), dry_run=dry_run)
    for op in skipped:
        out.print(f"  kept on Canvas (--no-delete): {op.key.path}", markup=False, highlight=False)
    if plan.is_empty():
        out.print("Nothing to push.", highlight=False)
        return result
    out.print(plan.render())

    guard = SubmissionGuard(client, config)
    _guard_preflight(plan, guard, confirmed, dry_run=dry_run)
    check_stale(root, client, config, plan, base, main)
    if dry_run:
        out.print(f"Dry run: {_count(plan)} would be pushed; nothing written.", highlight=False)
        return result
    if not yes:
        ask: Callable[[str], bool] = confirm or (lambda msg: typer.confirm(msg, default=False))
        if not ask(f"Push {_count(plan)} to Canvas?"):
            raise CanvasSyncError("aborted; nothing written")
        check_stale(root, client, config, plan, base, main)  # the prompt may have waited

    ids = backup_and_ids(client, config, plan, target)
    bodies = bodies_at(root, main)
    ctx = ApplyContext(
        config=config,
        tz=config.tz,
        roster=roster.load_cache(root) if (root / roster.CACHE_PATH).exists() else RosterCache(),
        ids=dict(ids),
        guard=guard,
        blobs=BlobStore(root),
        bodies=bodies,
    )
    applied = apply(plan, client, ctx, confirmed=confirmed)
    result.ops_done = len(applied.done)
    if applied.error is None:
        applied = _second_pass(plan, ctx, client, ids, confirmed, applied)
    if applied.error is not None or applied.failed is not None:
        result.failed, result.error = applied.failed, applied.error
        _report_failure(out, result)
        return result

    _record_assignment_bodies(client, ctx, plan)
    expected = _with_ids(target, ctx.ids)
    _move_bodies(ctx.bodies, base, expected)
    fctx = _context(root, config, expected, ctx.bodies)
    fetched = fetch_course(client, fctx, root=root)
    live = {k.path for k in fetched.objects}
    final = Bodies({p: r for p, r in ctx.bodies.entries.items() if p in live})
    result.unexpected = _unexpected(expected, fetched, plan)
    if result.unexpected:
        out.print(
            "error: Canvas stored something different from HEAD in "
            f"{', '.join(result.unexpected)}; this is a bug; canvas/main was not updated "
            "(run canvas fetch and inspect git diff HEAD canvas/main)",
            markup=False,
            highlight=False,
        )
        return result
    result.commit = snapshot(
        root, fetched, final, fctx.blob_store, f"canvas push: {len(plan.ops)} operations", head
    )
    new_main = result.commit or main
    # Files whose parsed content equals what Canvas stored keep HEAD's exact bytes.
    mine, theirs = dump_tree(target), dump_tree(fetched)
    preserve = {p for p, data in mine.items() if theirs.get(p) == data}
    target_commit = _branch_target(root, head, new_main, set(mine), preserve)
    result.fast_forwarded = gitops.fast_forward(root, target_commit)
    if result.fast_forwarded:
        out.print(
            f"Pushed {_count(plan)}; canvas/main is {str(result.commit)[:7]}", highlight=False
        )
    else:
        old = gitops.ref_commit(root, PUSHED_REF)
        gitops.update_ref(root, PUSHED_REF, target_commit, old)
        out.print(
            f"Pushed {_count(plan)}, but could not fast-forward the branch "
            "(uncommitted changes in the way?); commit or stash them, then run: "
            f"git merge --ff-only {PUSHED_REF}",
            markup=False,
            highlight=False,
        )
    return result


def backup_and_ids(
    client: CanvasClient, config: CourseConfig, plan: Plan, target: Course
) -> dict[ObjectKey, int | str]:
    """Back up every touched existing assignment (raises ``BackupError``, so nothing is
    written), then return the ids to pre-fill ``ApplyContext.ids`` with."""
    ids = _backup_ids(plan)
    if ids:
        backup(client, config, ids)
    return _existing_ids(target)


def _second_pass(
    plan: Plan,
    ctx: ApplyContext,
    client: CanvasClient,
    before_ids: dict[ObjectKey, int | str],
    confirmed: set[str],
    first: ApplyResult,
) -> ApplyResult:
    extra = _rerender_ops(plan, ctx, before_ids)
    if extra.is_empty():
        return first
    second = apply(extra, client, ctx, confirmed=confirmed)
    second.done = first.done + second.done
    second.new_ids = {**first.new_ids, **second.new_ids}
    return second


def _report_failure(out: Console, result: PushResult) -> None:
    where = result.failed.key.path if result.failed is not None else "?"
    out.print(
        f"error: push stopped at {where}: {result.error}; "
        f"{result.ops_done} of {result.ops_total} operations were applied. "
        "Nothing is lost: run canvas pull, then canvas push to finish",
        markup=False,
        highlight=False,
    )


def push(
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show the plan only")] = False,
    yes: Annotated[bool, typer.Option("--yes", help="Skip the confirmation prompt")] = False,
    allow_dirty: Annotated[bool, typer.Option("--allow-dirty", help="Allow a dirty tree")] = False,
    no_delete: Annotated[
        bool, typer.Option("--no-delete", help="Keep Canvas objects whose files were deleted")
    ] = False,
    confirm_grading_change: Annotated[
        bool,
        typer.Option("--confirm-grading-change", help="Allow changes that rescore or re-penalise"),
    ] = False,
    confirm_access_removal: Annotated[
        bool, typer.Option("--confirm-access-removal", help="Allow removing students' access")
    ] = False,
    confirm_notify: Annotated[
        bool, typer.Option("--confirm-notify", help="Allow posting announcements now")
    ] = False,
) -> None:
    """Apply the local changes to Canvas."""
    confirmed: set[str] = set()
    for flag, given in (
        ("--confirm-grading-change", confirm_grading_change),
        ("--confirm-access-removal", confirm_access_removal),
        ("--confirm-notify", confirm_notify),
    ):
        if given:
            confirmed.add(flag)
    err = Console(stderr=True)
    try:
        root = gitops.repo_root(Path.cwd())
        config = load_config(root)
        with CanvasClient(
            str(config.canvas_url),
            get_token(config),
            course_id=config.course_id,
            mode="read" if dry_run else "write",
        ) as client:
            result = run_push(
                root,
                client,
                config,
                dry_run=dry_run,
                yes=yes,
                allow_dirty=allow_dirty,
                no_delete=no_delete,
                confirmed=confirmed,
            )
    except CanvasSyncError as e:
        err.print(f"error: {e}", markup=False, highlight=False)
        raise typer.Exit(1) from None
    if not result.ok:
        raise typer.Exit(1)
