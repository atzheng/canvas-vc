"""canvas pull."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from canvas_sync import gitops
from canvas_sync.commands.fetch import report, run_fetch
from canvas_sync.config import get_token, load_config
from canvas_sync.errors import CanvasSyncError
from canvas_sync.remote.http import CanvasClient


def merge_canvas(root: Path, rebase: bool, console: Console) -> bool:
    """Merge (or rebase onto) canvas/main; git decides what is allowed. True if clean.

    Prints one line per problem: ``CONFLICT: <path>`` per unmerged path, or git's own
    message when it refused (e.g. a dirty tree). Never touches canvas/main.
    """
    ref = gitops.ref_commit(root)
    if ref is None:
        console.print("no canvas/main yet; nothing to merge", highlight=False)
        return True
    head = gitops.head_commit(root)
    if head is not None and gitops.is_ancestor(root, ref, head):
        console.print("already up to date", highlight=False)
        return True
    code, message = gitops.merge_with_output(root, rebase=rebase)
    if code == 0:
        console.print(f"{'rebased onto' if rebase else 'merged'} canvas/main", highlight=False)
        return True
    conflicts = gitops.unmerged_paths(root)
    for path in conflicts:
        console.print(f"CONFLICT: {path}", markup=False, highlight=False)
    if conflicts:
        how = "git rebase --continue" if rebase else "git commit"
        console.print(
            f"resolve the conflicts, `git add` them, then `{how}`", markup=False, highlight=False
        )
    else:
        console.print(f"git refused: {message or f'exit {code}'}", markup=False, highlight=False)
    return False


def pull(
    rebase: Annotated[bool, typer.Option("--rebase", help="Rebase instead of merge")] = False,
) -> None:
    """Fetch, then merge (or rebase onto) canvas/main."""
    try:
        root = gitops.repo_root(Path.cwd())
        config = load_config(root)
        with CanvasClient(
            str(config.canvas_url), get_token(config), course_id=config.course_id, mode="read"
        ) as client:
            report(run_fetch(root, client, config))
        if not merge_canvas(root, rebase, Console()):
            raise typer.Exit(1)
    except CanvasSyncError as e:
        Console(stderr=True).print(f"error: {e}", markup=False, highlight=False)
        raise typer.Exit(1) from None
