"""fetch_course: calls each type's reader (CONTRACTS.md section 8).

Order: roster -> files -> id/path pre-pass -> pages -> assignments -> announcements ->
modules. The roster cache is built first (assignment overrides resolve through it) and
files first among the objects (their ids are needed to turn file URLs into paths). The
pre-pass uses list calls only, so every body can link to any object, including one that is
new in this same fetch; the same complete map is handed to all readers.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from canvas_sync.model.base import CanvasObject, Kind, ObjectKey
from canvas_sync.model.course import Course
from canvas_sync.model.files import FilesMeta
from canvas_sync.model.roster import Roster, RosterCache
from canvas_sync.progress import Progress, safe
from canvas_sync.remote import announcements, assignments, files, modules, pages, roster
from canvas_sync.remote.context import FetchContext
from canvas_sync.remote.http import CanvasClient

PathMap = dict[tuple[Kind, int | str], str]


def _initial_roster(client: CanvasClient, cache: RosterCache) -> Roster:
    """roster.yaml for a first fetch: the section names, and the group category when the
    course has exactly one. Later fetches keep the tracked roster as it is (people edit it)."""
    cats = client.get(f"/courses/{client.course_id}/group_categories")
    name: str | None = None
    if len(cats) == 1 and cache.groups:
        name = str(cats[0]["name"])
    return Roster(sections=sorted(cache.sections), groups_category=name)


def _tree_paths(
    client: CanvasClient, ctx: FetchContext, meta: FilesMeta
) -> tuple[PathMap, PathMap]:
    """(tree paths for link rewriting, module refs), both ``(kind, id) -> path`` and without
    ``.canvasignore``d objects (a link to one stays a Canvas URL; a module item becomes
    unmanaged)."""
    tree: PathMap = {}
    for slug, path in pages.assign_paths(client, ctx).items():
        tree[(Kind.PAGE, slug)] = path
    for aid, path in assignments.assign_paths(client, ctx).items():
        tree[(Kind.ASSIGNMENT, aid)] = path
    for tid, path in announcements.assign_paths(client, ctx).items():
        tree[(Kind.ANNOUNCEMENT, tid)] = path
    for path, entry in meta.files.items():
        if entry.canvas_id is not None:
            tree[(Kind.FILE, entry.canvas_id)] = path
    tree = {k: p for k, p in tree.items() if not ctx.ignore.ignored(p)}
    refs: PathMap = {}
    for (kind, ident), path in tree.items():
        if kind in (Kind.PAGE, Kind.ASSIGNMENT):
            refs[(kind, ident)] = path.partition("/")[2].removesuffix(".md")
        elif kind is Kind.FILE:
            refs[(kind, ident)] = path.removeprefix("files/")
    return tree, refs


def fetch_course(
    client: CanvasClient,
    ctx: FetchContext,
    *,
    root: Path | None = None,
    progress: Progress | None = None,
) -> Course:
    """Read the whole course from Canvas (GET only) into a ``Course``.

    ``root`` (default: the blob store's repository) is where the untracked
    ``roster-aliases.yaml`` is read and ``.canvas/roster-cache.json`` written.
    """
    base = root if root is not None else ctx.blob_store.root
    progress = safe(progress)
    progress.phase("roster")
    cache = roster.read(client, ctx, aliases=roster.load_aliases(base))
    roster.write_cache(base, cache)
    ctx = replace(ctx, roster=cache)

    meta = files.read_files_meta(client, ctx, progress=progress)
    tree, refs = _tree_paths(client, ctx, meta)

    objects: dict[ObjectKey, CanvasObject] = {}
    for path, entry in meta.files.items():
        objects[ObjectKey(Kind.FILE, path)] = entry
    for kind, read in (
        (Kind.PAGE, pages.read),
        (Kind.ASSIGNMENT, assignments.read),
        (Kind.ANNOUNCEMENT, announcements.read),
    ):
        progress.phase(kind.value + "s", sum(1 for k in tree if k[0] is kind))
        for path, obj in read(client, ctx, paths=tree).items():
            objects[ObjectKey(kind, path)] = obj
            progress.advance(label=path)
    progress.phase("modules")
    module_list = modules.read(client, ctx, paths=refs)

    tracked = ctx.previous.roster if ctx.previous is not None else _initial_roster(client, cache)
    return Course(
        config=ctx.config,
        objects=dict(sorted(objects.items())),
        files_meta=meta,
        modules=module_list,
        roster=tracked,
    )
