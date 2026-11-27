"""canvas validate: offline checks and ``--fix`` (SPEC.md "validate", CONTRACTS.md sections 4-5).

The checks are a pure function, :func:`validate_tree`, so ``push`` can run them
before pushing. Each problem is an :class:`Issue` printed as ``path: message``.
Nothing here touches the network; ``.canvas/`` is read only for the roster cache.
"""

from __future__ import annotations

import re
import shutil
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal, cast

import typer
from rich.console import Console

from canvas_sync import model
from canvas_sync.config import CourseConfig, load_config
from canvas_sync.dates import is_ambiguous_local
from canvas_sync.errors import CanvasSyncError, ValidationError
from canvas_sync.model.announcement import Announcement
from canvas_sync.model.assignment import Assignment
from canvas_sync.model.base import CanvasObject, Kind, ObjectKey
from canvas_sync.model.course import dump_tree, load_roster, load_tree
from canvas_sync.model.files import FILE_SPEC, FilesMeta
from canvas_sync.model.modules import Module
from canvas_sync.model.overrides import Override
from canvas_sync.model.page import Page
from canvas_sync.model.roster import Roster, RosterCache, check_override_targets
from canvas_sync.render.links import LinkContext
from canvas_sync.render.md2html import md_to_html_report

CACHE_PATH = ".canvas/roster-cache.json"
_BODY_FOLDERS = (Kind.PAGE, Kind.ASSIGNMENT, Kind.ANNOUNCEMENT)
# A full marker sequence, in order; a lone `<<<<<<<` line (e.g. in a code block) is not one.
_CONFLICT = re.compile(r"^<{7}( |$).*?^={7}$.*?^>{7}( |$)", re.MULTILINE | re.DOTALL)
_YAML_COMMENT = re.compile(r"^\s*#", re.MULTILINE)
_YAML_FILES = ("course.yaml", "roster.yaml", "modules.yaml", "files/_meta.yaml")
_LOAD_ERRORS = (CanvasSyncError, OSError, UnicodeError)
_PATHLIKE = re.compile(r"^[^\s:]+: ")


@dataclass(frozen=True)
class Issue:
    """One problem, shown as ``path: message``. Warnings never fail validation."""

    path: str
    message: str
    severity: Literal["error", "warning"] = "error"

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


def has_errors(issues: Iterable[Issue]) -> bool:
    return any(i.severity == "error" for i in issues)


def _from_error(path: str, exc: Exception, root: Path) -> Issue:
    """Issue from a loader error; loader messages normally start with ``path: ``."""
    msg = str(exc).replace(f"{root}/", "")
    if _PATHLIKE.match(msg):
        head, _, rest = msg.partition(": ")
        return Issue(head, rest)
    return Issue(path, msg)


def _text_files(root: Path) -> list[str]:
    """Tree paths of every text file that can carry a git conflict."""
    out = [
        f
        for f in ("course.yaml", "roster.yaml", "modules.yaml", "files/_meta.yaml")
        if (root / f).is_file()
    ]
    for kind in _BODY_FOLDERS:
        folder = model.REGISTRY[kind].folder
        d = root / folder
        if d.is_dir():
            out += [
                f"{folder}/{p.name}"
                for p in sorted(d.iterdir())
                if p.is_file() and p.suffix == ".md" and not p.name.startswith(".")
            ]
    return out


def find_conflicts(root: Path) -> list[str]:
    """Tree paths of text files that still contain git conflict markers."""
    bad: list[str] = []
    for rel in _text_files(root):
        try:
            text = (root / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if _CONFLICT.search(text):
            bad.append(rel)
    return bad


def _load_folder(
    kind: Kind, root: Path, cfg: CourseConfig, conflicted: set[str], issues: list[Issue]
) -> dict[str, CanvasObject]:
    """Load a folder-per-type; on failure go file by file so every error is listed."""
    spec = model.REGISTRY[kind]
    prefix = f"{spec.folder}/"
    mine = {c for c in conflicted if c.startswith(prefix)}
    if not mine:
        try:
            return spec.load(root, cfg)
        except _LOAD_ERRORS as whole:
            failure: Exception | None = whole
    else:
        failure = None
    out: dict[str, CanvasObject] = {}
    found = False
    folder = root / spec.folder
    names = sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []
    for name in names:
        rel = f"{prefix}{name}"
        if name.startswith(".") or rel in mine:
            continue
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / spec.folder).mkdir()
            src = folder / name
            if src.is_file():
                shutil.copyfile(src, Path(tmp) / rel)
            else:
                (Path(tmp) / rel).mkdir()
            try:
                out.update(spec.load(Path(tmp), cfg))
            except _LOAD_ERRORS as e:
                found = True
                issues.append(_from_error(rel, e, root))
    if failure is not None and not found:
        issues.append(_from_error(spec.folder, failure, root))  # e.g. duplicate canvas_id
    return out


@dataclass(frozen=True)
class DateProblem:
    """One date problem of an object; ``fields`` are the date fields it concerns."""

    fields: tuple[str, ...]
    message: str
    severity: Literal["error", "warning"] = "error"


def date_problems(obj: object) -> list[DateProblem]:
    """Pure date checks for one object (or override): ambiguous times, unlock/due/lock order."""
    d: dict[str, datetime] = {}
    for name in ("publish_at", "post_at", "unlock", "due", "lock"):
        v = getattr(obj, name, None)
        if isinstance(v, datetime):
            d[name] = v
    out: list[DateProblem] = []
    for name, v in d.items():
        if is_ambiguous_local(v):
            msg = f"{name} {v:%Y-%m-%d %H:%M} is ambiguous (fall-back hour)"
            out.append(DateProblem((name,), msg, "warning"))
    if "unlock" in d and "lock" in d and d["unlock"] > d["lock"]:
        out.append(DateProblem(("unlock", "lock"), "unlock is after lock"))
    if "unlock" in d and "due" in d and d["unlock"] > d["due"]:
        out.append(DateProblem(("unlock", "due"), "unlock is after due", "warning"))
    if "due" in d and "lock" in d and d["due"] > d["lock"]:
        out.append(DateProblem(("due", "lock"), "due is after lock", "warning"))
    return out


def _check_dates(where: str, obj: object, issues: list[Issue]) -> None:
    issues.extend(Issue(where, p.message, p.severity) for p in date_problems(obj))


def _check_roster(
    where: str,
    overrides: Iterable[Override],
    roster: Roster,
    cache: RosterCache | None,
    issues: list[Issue],
) -> None:
    ovs = list(overrides)
    if cache is None:
        if not roster.sections:
            return
        # Without the cache only sections listed in roster.yaml can be checked.
        ovs = [o for o in ovs if o.section is not None]
        cache = RosterCache()
    for problem in check_override_targets(ovs, roster, cache, where):
        issues.append(Issue(where, problem.partition(": ")[2]))


def _load_cache(root: Path, issues: list[Issue]) -> RosterCache | None:
    p = root / CACHE_PATH
    if not p.is_file():
        msg = "missing; student, group and section names are not checked (run canvas fetch)"
        issues.append(Issue(CACHE_PATH, msg, "warning"))
        return None
    try:
        return RosterCache.from_json(p.read_text(encoding="utf-8"))
    except _LOAD_ERRORS as e:
        issues.append(Issue(CACHE_PATH, str(e), "warning"))
        return None


def validate_tree(root: Path, *, cache: RosterCache | None = None) -> list[Issue]:
    """All offline problems in the course tree at ``root``, in a stable order.

    ``cache`` overrides ``.canvas/roster-cache.json`` (a missing cache is a
    warning and skips student/group checks). Never raises for bad content and
    never writes. Callers fail on ``has_errors(issues)``.
    """
    issues: list[Issue] = []
    conflicted = set(find_conflicts(root))
    issues += [Issue(p, "unresolved git conflict markers") for p in sorted(conflicted)]
    if "course.yaml" in conflicted:
        return issues  # nothing else can be loaded without the config
    try:
        cfg = load_config(root)
    except _LOAD_ERRORS as e:
        return [*issues, _from_error("course.yaml", e, root)]

    objs: dict[ObjectKey, CanvasObject] = {}
    for kind in _BODY_FOLDERS:
        loaded = _load_folder(kind, root, cfg, conflicted, issues)
        objs.update({ObjectKey(kind, p): o for p, o in loaded.items()})

    roster = Roster()
    if "roster.yaml" not in conflicted:
        try:
            roster = load_roster(root)
        except _LOAD_ERRORS as e:
            issues.append(_from_error("roster.yaml", e, root))

    meta = FilesMeta()
    if "files/_meta.yaml" not in conflicted:
        try:
            meta = FILE_SPEC.load_files_meta(root, cfg)
        except _LOAD_ERRORS as e:
            issues.append(_from_error("files/_meta.yaml", e, root))

    modules: list[Module] = []
    if "modules.yaml" not in conflicted:
        try:
            loaded_mods = model.REGISTRY[Kind.MODULE].load(root, cfg)
            modules = [cast(Module, m) for m in loaded_mods.values()]
        except _LOAD_ERRORS as e:
            issues.append(_from_error("modules.yaml", e, root))

    if cache is None:
        cache = _load_cache(root, issues)

    paths = {k.path for k in objs}
    ids = {k: o.canvas_id for k, o in objs.items()}
    ids.update({ObjectKey(Kind.FILE, p): e.canvas_id for p, e in meta.files.items()})

    def file_exists(rel: str) -> bool:
        return rel in meta.files or (root / rel).is_file()

    def exists(path: str) -> bool:
        return path in paths or (path.startswith("files/") and file_exists(path))

    fronts = [k.path for k, o in sorted(objs.items()) if isinstance(o, Page) and o.front_page]
    for extra in fronts[1:]:
        issues.append(Issue(extra, f"front_page: true is also set on {fronts[0]}"))

    for key, obj in sorted(objs.items()):
        _check_dates(key.path, obj, issues)
        if isinstance(obj, Assignment):
            _check_roster(key.path, obj.overrides, roster, cache, issues)
        if isinstance(obj, (Page, Assignment, Announcement)) and obj.body:
            ctx = LinkContext(
                course_id=cfg.course_id,
                from_path=key.path,
                ids=lambda k: ids.get(k),
                paths=lambda _kind, _id: None,
                exists=exists,
            )
            for link in md_to_html_report(obj.body, ctx).missing_links:
                issues.append(Issue(key.path, f"broken link {link!r}: no such local object"))

    for mod in modules:
        _check_dates("modules.yaml", mod, issues)
        _check_roster("modules.yaml", mod.overrides, roster, cache, issues)
        for item in mod.items:
            if item.page is not None and f"pages/{item.page}.md" not in paths:
                missing = f"page {item.page!r} (pages/{item.page}.md)"
            elif item.assignment is not None and f"assignments/{item.assignment}.md" not in paths:
                missing = f"assignment {item.assignment!r} (assignments/{item.assignment}.md)"
            elif item.file is not None and not file_exists(f"files/{item.file}"):
                missing = f"file {item.file!r} (files/{item.file})"
            else:
                continue
            issues.append(Issue("modules.yaml", f"module {mod.name!r}: no such {missing}"))
    return issues


def fix_tree(root: Path, *, warn: Callable[[str], None] | None = None) -> list[str]:
    """Rewrite files into canonical form; returns the tree paths written.

    Raises ``ValidationError`` (writing nothing) when a file has conflict markers
    or does not load. Only writes files whose bytes differ; never deletes, and
    never touches blobs or ``.canvas/``. Idempotent. ``warn`` gets a one-line message
    for each YAML file about to lose ``#`` comments.
    """
    conflicts = find_conflicts(root)
    if conflicts:
        raise ValidationError(f"{conflicts[0]}: unresolved git conflict markers")
    try:
        canonical = dump_tree(load_tree(root))
    except (OSError, UnicodeError) as e:
        raise ValidationError(f"cannot read tree: {e}") from None
    written: list[str] = []
    for rel, data in canonical.items():
        path = root / rel
        if path.is_file() and path.read_bytes() == data:
            continue
        if (
            warn is not None
            and rel in _YAML_FILES
            and path.is_file()
            and _YAML_COMMENT.search(path.read_text(encoding="utf-8"))
        ):
            warn(f"{rel}: YAML comments are dropped when rewriting to canonical form")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        written.append(rel)
    return written


def validate(
    fix: Annotated[bool, typer.Option("--fix", help="Normalize files to canonical form")] = False,
) -> None:
    """Offline checks: frontmatter, references, dates, roster names."""
    console = Console()
    root = Path.cwd()

    def show(text: str, style: str) -> None:
        console.print(text, style=style, markup=False, highlight=False, soft_wrap=True)

    if fix:
        try:
            for rel in fix_tree(root, warn=lambda m: show(f"warning: {m}", "yellow")):
                show(f"{rel}: rewritten in canonical form", "green")
        except _LOAD_ERRORS as e:
            show(f"fix skipped: {_from_error('course.yaml', e, root)}", "red")
    issues = validate_tree(root)
    for i in issues:
        if i.severity == "warning":
            show(f"warning: {i}", "yellow")
        else:
            show(str(i), "red")
    if has_errors(issues):
        raise typer.Exit(1)
