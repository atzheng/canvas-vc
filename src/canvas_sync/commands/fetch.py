"""canvas fetch."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import typer
from pydantic import ValidationError as PydanticValidationError
from rich.console import Console

from canvas_sync import gitops
from canvas_sync.config import CourseConfig, get_token, load_config
from canvas_sync.errors import CanvasSyncError, ValidationError
from canvas_sync.ignore import IGNORE_FILE, IgnoreRules
from canvas_sync.model.course import Course, dump_tree
from canvas_sync.model.roster import RosterCache
from canvas_sync.model.serialize import load_yaml
from canvas_sync.remote import roster
from canvas_sync.remote.context import FetchContext
from canvas_sync.remote.fetch import fetch_course
from canvas_sync.remote.http import CanvasClient
from canvas_sync.render.bodies import BODIES_FILE, Bodies
from canvas_sync.snapshot import BlobStore, snapshot


@dataclass(frozen=True)
class FetchResult:
    commit: str | None  # new canvas/main commit; None when nothing changed
    changed: int  # number of tree paths that differ from the previous canvas/main


def bodies_at(root: Path, commit: str | None) -> Bodies:
    """``.canvas/bodies.json`` of a canvas/main commit (it exists only in snapshots)."""
    data = gitops.read_file_at(root, commit, BODIES_FILE) if commit else None
    if data is None:
        return Bodies()
    with tempfile.TemporaryDirectory(prefix="canvas-bodies-") as tmp:
        dest = Path(tmp) / BODIES_FILE
        dest.parent.mkdir(parents=True)
        dest.write_bytes(data)
        return Bodies.load(Path(tmp))


def _changed(previous: Course | None, course: Course) -> int:
    old = dump_tree(previous) if previous is not None else {}
    new = dump_tree(course)
    return sum(1 for p in new.keys() | old.keys() if old.get(p) != new.get(p))


def committed_inputs(root: Path, config: CourseConfig) -> tuple[CourseConfig, IgnoreRules]:
    """course.yaml and .canvasignore as committed at HEAD, so uncommitted edits cannot leak
    into canvas/main. Without HEAD (clone) or when HEAD lacks or cannot parse them, the
    given config and the work tree's .canvasignore are used."""
    head = gitops.head_commit(root)
    if head is None:
        return config, IgnoreRules.load(root)
    raw = gitops.read_file_at(root, head, "course.yaml")
    if raw is not None:
        try:
            parsed = load_yaml(raw.decode("utf-8"), "course.yaml")
            config = CourseConfig.model_validate(parsed)
        except (UnicodeDecodeError, ValidationError, PydanticValidationError):
            pass
    ignore_raw = gitops.read_file_at(root, head, IGNORE_FILE)
    lines = ignore_raw.decode("utf-8-sig", errors="replace").splitlines() if ignore_raw else []
    return config, IgnoreRules(root, lines)


def run_fetch(root: Path, client: CanvasClient, config: CourseConfig) -> FetchResult:
    """Download Canvas, commit the result to canvas/main (no commit if unchanged).

    Never touches the work tree or the user's index; the only files written outside git
    are the untracked roster cache and the blob store. ``config`` supplies the first fetch
    (clone) and the fallback; afterwards course.yaml and .canvasignore come from HEAD.
    """
    started = datetime.now(UTC).isoformat(timespec="seconds")
    config, ignore = committed_inputs(root, config)
    prev = gitops.ref_commit(root)
    previous = gitops.load_course_at(root, prev) if prev else None
    bodies = bodies_at(root, prev)
    ctx = FetchContext(
        config=config,
        tz=config.tz,
        bodies=bodies,
        previous=previous,
        roster=roster.load_cache(root) if (root / roster.CACHE_PATH).exists() else RosterCache(),
        ignore=ignore,
        blob_store=BlobStore(root),
    )
    course = fetch_course(client, ctx, root=root)
    live = {k.path for k in course.objects}
    out = Bodies({p: r for p, r in bodies.entries.items() if p in live})
    changed = _changed(previous, course)
    commit = snapshot(root, course, out, ctx.blob_store, f"canvas fetch: {changed} changed")
    gitops.record_fetch_time(root, started)  # lets `canvas status` do a cheap moved check
    return FetchResult(commit, changed)


def report(result: FetchResult, console: Console | None = None) -> None:
    out = console or Console()
    if result.commit is None:
        out.print("canvas/main: already up to date", highlight=False)
    else:
        out.print(f"canvas/main: {result.changed} changed ({result.commit[:7]})", highlight=False)


def fetch() -> None:
    """Download the Canvas state and commit it to canvas/main."""
    try:
        root = gitops.repo_root(Path.cwd())
        config = load_config(root)
        with CanvasClient(
            str(config.canvas_url), get_token(config), course_id=config.course_id, mode="read"
        ) as client:
            report(run_fetch(root, client, config))
    except CanvasSyncError as e:
        Console(stderr=True).print(f"error: {e}", markup=False, highlight=False)
        raise typer.Exit(1) from None
