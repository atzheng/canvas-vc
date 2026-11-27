"""canvas diff."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from canvas_sync import gitops
from canvas_sync.errors import CanvasSyncError
from canvas_sync.plan import core


def build_plan(root: Path) -> core.Plan:
    """The plan for ``canvas/main..HEAD``; the same one ``push`` computes.

    ``core.diff(base=canvas/main tree, target=HEAD tree)``; ``.canvas/`` is ignored by
    ``core.diff`` (CONTRACTS section 11). Raises ``CanvasSyncError`` without either commit.
    """
    base = gitops.ref_commit(root)
    if base is None:
        raise CanvasSyncError("no canvas/main yet; run `canvas fetch` first")
    head = gitops.head_commit(root)
    if head is None:
        raise CanvasSyncError("no commits yet; nothing to diff")
    return core.diff(gitops.load_course_at(root, base), gitops.load_course_at(root, head))


def diff() -> None:
    """Show the API-level plan that push would carry out."""
    try:
        root = gitops.repo_root(Path.cwd())
        console = Console()
        plan = build_plan(root)
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
