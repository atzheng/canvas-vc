"""canvas clone."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Annotated, cast

import typer
from rich.console import Console

from canvas_sync import gitops
from canvas_sync.commands.fetch import FetchResult, report, run_fetch
from canvas_sync.config import CourseConfig, get_token
from canvas_sync.errors import CanvasSyncError, ConfigError
from canvas_sync.remote import roster
from canvas_sync.remote.http import CanvasClient

GITIGNORE = f"{roster.CACHE_PATH}\n{roster.ALIASES_PATH}\n"


def _git(root: Path, *args: str) -> None:
    proc = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, check=False, text=True
    )
    if proc.returncode != 0:
        lines = proc.stderr.strip().splitlines()
        raise CanvasSyncError(f"git {args[0]} failed: {lines[-1] if lines else proc.returncode}")


def _timezone(client: CanvasClient, override: str | None) -> str:
    if override:
        return override
    course = client.get_one(f"/courses/{client.course_id}")
    name: object = (
        cast("dict[str, object]", course).get("time_zone") if isinstance(course, dict) else None
    )
    if not isinstance(name, str) or not name:
        raise ConfigError("Canvas gave no time zone for the course; pass --timezone")
    return name


def clone_course(
    client: CanvasClient, url: str, course_id: int, root: Path, timezone: str | None = None
) -> FetchResult:
    """Create the repo at ``root``, fetch, and check out ``main`` at canvas/main.

    The snapshot carries course.yaml, roster.yaml and everything else; the checkout then
    gets one extra commit for ``.gitignore`` (the roster cache and aliases stay untracked;
    ``.canvas/bodies.json`` is tracked, written only by fetch).
    """
    if root.exists() and any(root.iterdir()):
        raise CanvasSyncError(f"{root}: directory exists and is not empty")
    try:
        config = CourseConfig.model_validate(
            {"canvas_url": url, "course_id": course_id, "timezone": _timezone(client, timezone)}
        )
    except ValueError as e:
        raise ConfigError(f"course.yaml: {str(e).splitlines()[0]}") from None
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "--quiet", "--initial-branch=main")
    result = run_fetch(root, client, config)
    if not gitops.fast_forward(root):
        raise CanvasSyncError(f"{root}: could not check out canvas/main")
    (root / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
    _git(root, "add", ".gitignore")
    _git(
        root,
        "-c",
        "user.name=canvas-sync",
        "-c",
        "user.email=canvas-sync@localhost",
        "commit",
        "--quiet",
        "-m",
        "canvas clone: .gitignore",
    )
    return result


def clone(
    url: Annotated[str, typer.Argument(help="Canvas base URL")],
    course_id: Annotated[int, typer.Argument(help="Canvas course id")],
    directory: Annotated[Path | None, typer.Argument(help="Target directory")] = None,
    timezone: Annotated[
        str | None, typer.Option(help="IANA time zone (default: the course's own)")
    ] = None,
) -> None:
    """Create the repo, fetch, and check out main."""
    root = directory if directory is not None else Path(f"course-{course_id}")
    try:
        probe = CourseConfig.model_validate(
            {"canvas_url": url, "course_id": course_id, "timezone": "UTC"}
        )
        with CanvasClient(
            str(probe.canvas_url), get_token(probe), course_id=course_id, mode="read"
        ) as client:
            report(clone_course(client, url, course_id, root, timezone))
    except ValueError as e:
        Console(stderr=True).print(f"error: {str(e).splitlines()[0]}", markup=False)
        raise typer.Exit(1) from None
    except CanvasSyncError as e:
        Console(stderr=True).print(f"error: {e}", markup=False, highlight=False)
        raise typer.Exit(1) from None
