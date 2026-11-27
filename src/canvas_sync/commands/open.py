"""canvas open: open the Canvas page of a tree path (SPEC.md "Commands").

The URL is built offline from ``course.yaml`` (``canvas_url``, ``course_id``) and the
``canvas_id`` stored in the object's own file. Nothing touches the network.
"""

from __future__ import annotations

import webbrowser
from pathlib import Path, PurePosixPath
from typing import Annotated

import typer
from rich.console import Console

from canvas_sync import model
from canvas_sync.config import load_config
from canvas_sync.errors import CanvasSyncError, ValidationError
from canvas_sync.model.base import Kind, kind_for_path
from canvas_sync.model.files import FILE_SPEC

_COURSE_PATHS = {"", ".", "course.yaml"}
_SEGMENTS = {
    Kind.PAGE: "pages",
    Kind.ASSIGNMENT: "assignments",
    Kind.ANNOUNCEMENT: "discussion_topics",
}


def _normalize(root: Path, raw: str) -> str:
    """Tree path (POSIX, relative to ``root``) for what the user typed."""
    head, sep, frag = raw.partition("#")
    p = Path(head) if head else Path()
    if p.is_absolute():
        try:
            p = p.resolve().relative_to(root.resolve())
        except ValueError:
            raise ValidationError(f"{raw}: not inside the course tree") from None
    rel = PurePosixPath(*p.parts).as_posix()
    rel = "" if rel == "." else rel
    return rel + sep + frag


def _need_id(path: str, canvas_id: object) -> str:
    if canvas_id is None or canvas_id == "":
        raise ValidationError(f"{path}: not on Canvas yet (no canvas_id); push it first")
    return str(canvas_id)


def canvas_url_for(root: Path, raw_path: str) -> str:
    """URL on Canvas of the object at tree path ``raw_path`` (relative to ``root`` or absolute).

    Raises ``ValidationError`` for an unknown path or an object without ``canvas_id``.
    """
    cfg = load_config(root)
    course = f"{str(cfg.canvas_url).rstrip('/')}/courses/{cfg.course_id}"
    path = _normalize(root, raw_path)
    if path in _COURSE_PATHS:
        return course
    if path == "modules.yaml":
        return f"{course}/modules"
    if path.startswith("modules.yaml#"):
        mods = model.REGISTRY[Kind.MODULE].load(root, cfg)
        if path not in mods:
            raise ValidationError(f"{path}: no such module in modules.yaml")
        return f"{course}/modules#module_{_need_id(path, mods[path].canvas_id)}"
    try:
        kind = kind_for_path(path)
    except ValueError:
        raise ValidationError(f"{path}: not an object of the course tree") from None
    if kind is Kind.FILE:
        entry = FILE_SPEC.load_files_meta(root, cfg).files.get(path)
        if entry is None:
            raise ValidationError(f"{path}: not tracked in files/_meta.yaml (no canvas_id)")
        return f"{course}/files/{_need_id(path, entry.canvas_id)}"
    objs = model.REGISTRY[kind].load(root, cfg)
    if path not in objs:
        raise ValidationError(f"{path}: no such file in the course tree")
    return f"{course}/{_SEGMENTS[kind]}/{_need_id(path, objs[path].canvas_id)}"


def open_(
    path: Annotated[
        str,
        typer.Argument(
            help="Tree path of the object (assignments/hw1.md, modules.yaml#Week 1, "
            "course.yaml for the course home)"
        ),
    ],
    print_only: Annotated[
        bool, typer.Option("--print", help="Only print the URL, do not open a browser")
    ] = False,
) -> None:
    """Open the object on Canvas."""
    console = Console()
    try:
        url = canvas_url_for(Path.cwd(), path)
    except CanvasSyncError as e:
        console.print(str(e), style="red", markup=False, highlight=False, soft_wrap=True)
        raise typer.Exit(1) from None
    if print_only:
        console.print(url, markup=False, highlight=False, soft_wrap=True)
    elif not webbrowser.open(url):
        console.print(
            f"could not open a browser; URL: {url}", markup=False, highlight=False, soft_wrap=True
        )
        raise typer.Exit(1)
