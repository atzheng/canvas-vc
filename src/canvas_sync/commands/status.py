"""canvas status."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer
from rich.console import Console

from canvas_sync import gitops
from canvas_sync.commands.diff import build_plan
from canvas_sync.config import CourseConfig, get_token, load_config
from canvas_sync.errors import CanvasSyncError
from canvas_sync.remote.http import CanvasClient

# (label, list endpoint under the course, params). Only GET list calls; modules carry no
# updated_at and files need a folder walk, so neither is part of the cheap check.
_CHECKS: tuple[tuple[str, str, dict[str, str]], ...] = (
    ("pages", "pages", {}),
    ("assignments", "assignments", {}),
    ("announcements", "discussion_topics", {"only_announcements": "true"}),
)


DIRTY_NOTE = "note: the plan is from committed HEAD; uncommitted edits are not included"


def _parse(stamp: str) -> datetime | None:
    try:
        when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def moved_kinds(listings: dict[str, list[dict[str, Any]]], since: datetime) -> list[str]:
    """Labels whose listing has an ``updated_at`` later than ``since``. Pure."""
    out: list[str] = []
    for label, rows in listings.items():
        for row in rows:
            stamp = row.get("updated_at")
            when = _parse(stamp) if isinstance(stamp, str) else None
            if when is not None and when > since:
                out.append(label)
                break
    return out


def remote_moved(client: CanvasClient, config: CourseConfig, since: datetime) -> list[str]:
    """Cheap check: GET the list endpoints and compare ``updated_at`` with the last fetch."""
    listings: dict[str, list[dict[str, Any]]] = {}
    for label, endpoint, params in _CHECKS:
        rows: Any = client.get(f"/courses/{config.course_id}/{endpoint}", params)
        listings[label] = rows if isinstance(rows, list) else []
    return moved_kinds(listings, since)


def local_lines(root: Path) -> list[str]:
    """Offline report of the local state, one line each."""
    lines: list[str] = []
    unmerged = gitops.unmerged_paths(root)
    lines.extend(f"unmerged: {p}" for p in unmerged)
    lines.extend(
        f"conflict markers: {p}" for p in gitops.conflict_marker_paths(root) if p not in unmerged
    )
    lines.extend(f"uncommitted: {p}" for p in gitops.changed_paths(root) if p not in unmerged)
    if gitops.ref_commit(root) is None:
        lines.append("canvas/main: not fetched yet; run `canvas fetch`")
        return lines
    _, behind = gitops.ahead_behind(root)
    if behind:
        lines.append(f"canvas/main has {behind} commit(s) not in HEAD; run `canvas pull`")
    return lines


def unpushed_lines(root: Path) -> list[str]:
    """One line per operation push would perform; a single line if the plan cannot be built."""
    try:
        plan = build_plan(root)
    except (CanvasSyncError, NotImplementedError) as e:
        return [f"cannot compute plan: {e or type(e).__name__}"]
    return [f"{op.action.value} {op.key.path}" for op in plan.ops]


def _open_client(config: CourseConfig) -> CanvasClient:
    """A GET-only client (``mode="read"``); tests replace this with a FakeCanvas client."""
    return CanvasClient(
        str(config.canvas_url), get_token(config), course_id=config.course_id, mode="read"
    )


def _remote_line(root: Path) -> str:
    """The remote-moved line; any failure is reported, never raised (status works offline)."""
    stamp = gitops.last_fetch_time(root)
    since = _parse(stamp) if stamp else None
    if since is None:
        return "remote: not checked (never fetched)"
    try:
        config = load_config(root)
        with _open_client(config) as client:
            moved = remote_moved(client, config, since)
    except CanvasSyncError as e:
        return f"remote: not checked ({e})"
    if moved:
        return f"remote: Canvas changed since last fetch ({', '.join(moved)}); run `canvas pull`"
    return "remote: unchanged since last fetch"


def status() -> None:
    """Show unpushed local changes and whether Canvas has moved."""
    console = Console()
    try:
        root = gitops.repo_root(Path.cwd())
        for line in local_lines(root):
            console.print(line, markup=False, highlight=False)
        if gitops.ref_commit(root) is not None and gitops.head_commit(root) is not None:
            if gitops.changed_paths(root):
                console.print(DIRTY_NOTE, markup=False, highlight=False)
            for line in unpushed_lines(root):
                console.print(f"to push: {line}", markup=False, highlight=False)
        console.print(_remote_line(root), markup=False, highlight=False)
    except CanvasSyncError as e:
        Console(stderr=True).print(f"error: {e}", markup=False, highlight=False)
        raise typer.Exit(1) from None
