"""canvas diff."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import NamedTuple

import typer
from rich.console import Console

from canvas_sync import gitops
from canvas_sync.commands.fetch import committed_ignore
from canvas_sync.errors import CanvasSyncError
from canvas_sync.ignore import IgnoreRules
from canvas_sync.model.course import Course
from canvas_sync.plan import core


class PlanTrees(NamedTuple):
    """A plan with the two (``.canvasignore``-filtered) trees it was computed from."""

    plan: core.Plan
    base: Course  # canvas/main
    target: Course  # HEAD


def without_ignored(course: Course, ignore: IgnoreRules) -> Course:
    objs = {k: v for k, v in course.objects.items() if not ignore.ignored(k.path)}
    files = {p: e for p, e in course.files_meta.files.items() if not ignore.ignored(p)}
    meta = course.files_meta.model_copy(update={"files": files})
    return dataclasses.replace(course, objects=objs, files_meta=meta)


def build_plan(
    root: Path,
    ignore: IgnoreRules | None = None,
    *,
    head: str | None = None,
    main: str | None = None,
) -> PlanTrees:
    """The plan for ``canvas/main..HEAD``; the one ``push`` runs. Offline (git only).

    ``core.diff(base=canvas/main tree, target=HEAD tree)`` with ``.canvasignore`` applied to
    both trees first (an ignored file absent from HEAD must not plan a DELETE). ``ignore``
    defaults to the ``.canvasignore`` committed at HEAD; ``head``/``main`` default to the
    current commits. ``.canvas/`` is ignored by ``core.diff`` (CONTRACTS section 11).
    Raises ``CanvasSyncError`` without either commit or when a tree does not parse.
    """
    if main is None:
        main = gitops.ref_commit(root)
        if main is None:
            raise CanvasSyncError("no canvas/main yet; run `canvas fetch` first")
    if head is None:
        head = gitops.head_commit(root)
        if head is None:
            raise CanvasSyncError("no commits yet; nothing to diff")
    if ignore is None:
        ignore = committed_ignore(root, head)
    base = without_ignored(gitops.load_course_at(root, main), ignore)
    target = without_ignored(gitops.load_course_at(root, head), ignore)
    return PlanTrees(core.diff(base, target), base, target)


def diff() -> None:
    """Show the API-level plan that push would carry out."""
    try:
        root = gitops.repo_root(Path.cwd())
        console = Console()
        plan = build_plan(root).plan
        if gitops.changed_paths(root):
            console.print(
                "note: the plan is from committed HEAD; uncommitted edits are not included",
                markup=False,
                highlight=False,
            )
        console.print(plan.render(), highlight=False)
    except (CanvasSyncError, NotImplementedError) as e:
        Console(stderr=True).print(f"error: {e or type(e).__name__}", markup=False, highlight=False)
        raise typer.Exit(1) from None
