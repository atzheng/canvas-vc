"""Course aggregate, load_tree and dump_tree (CONTRACTS.md section 4).

Everything per-type goes through ``model.REGISTRY``; this module only knows
about the course-level files (course.yaml, roster.yaml) and how to route
loaded objects into ``Course``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from pathlib import Path
from typing import Any, cast

from pydantic import ValidationError as PydanticValidationError

from canvas_sync import model
from canvas_sync.config import CourseConfig, load_config
from canvas_sync.errors import ValidationError
from canvas_sync.model.base import CanvasObject, FilesMetaSpec, Kind, ObjectKey
from canvas_sync.model.files import FileEntry, FilesMeta
from canvas_sync.model.modules import Module
from canvas_sync.model.roster import Roster
from canvas_sync.model.serialize import dump_yaml, load_yaml

CONFIG_KEY_ORDER = [
    "canvas_url",
    "course_id",
    "timezone",
    "token_env",
    "student_ref",
    "files_root",
    "defaults",
]
CONFIG_NESTED_ORDERS = {
    "defaults": ["published", "unlock_time", "assignment"],
    "assignment": ["submission_types", "due_time"],
}
ROSTER_KEY_ORDER = ["sections", "groups_category"]


@dataclass(frozen=True)
class Course:
    config: CourseConfig
    objects: dict[ObjectKey, CanvasObject]  # pages, assignments, announcements, files (FileEntry)
    files_meta: FilesMeta
    modules: list[Module]  # order matters
    roster: Roster


def _fmt_time(t: time) -> str:
    return t.strftime("%H:%M" if t.second == 0 else "%H:%M:%S")


def _fix(v: Any) -> Any:
    if isinstance(v, time):
        return _fmt_time(v)
    if isinstance(v, dict):
        return {k: _fix(x) for k, x in cast("dict[str, Any]", v).items()}
    return v


def _config_data(cfg: CourseConfig) -> dict[str, Any]:
    """course.yaml content: everything that differs from the defaults."""
    data = cast("dict[str, Any]", _fix(cfg.model_dump(exclude_defaults=True)))
    url = str(cfg.canvas_url)
    # pydantic adds "/" to a bare host; the SPEC example has none.
    data["canvas_url"] = url[:-1] if url.endswith("/") and url.count("/") == 3 else url
    return data


def _load_roster(root: Path) -> Roster:
    path = root / "roster.yaml"
    if not path.exists():
        return Roster()
    raw = load_yaml(path.read_text(encoding="utf-8"), "roster.yaml")
    try:
        return Roster.model_validate({} if raw is None else raw)
    except PydanticValidationError as e:
        err = e.errors()[0]
        loc = ".".join(str(x) for x in err["loc"])
        raise ValidationError(f"roster.yaml: {loc}: {err['msg']}") from None


def _as_file(o: CanvasObject) -> FileEntry:
    if not isinstance(o, FileEntry):
        raise TypeError(f"FILE spec returned {type(o).__name__}, expected FileEntry")
    return o


def _as_module(o: CanvasObject) -> Module:
    if not isinstance(o, Module):
        raise TypeError(f"MODULE spec returned {type(o).__name__}, expected Module")
    return o


def load_tree(root: Path) -> Course:
    """Read a course tree. Tolerant: date-only values, any key order, CRLF, BOM."""
    cfg = load_config(root)
    objects: dict[ObjectKey, CanvasObject] = {}
    files_meta = FilesMeta()
    modules: list[Module] = []
    for kind, spec in model.REGISTRY.items():
        if kind is Kind.FILE:
            if hasattr(spec, "load_files_meta"):
                files_meta = cast(FilesMetaSpec, spec).load_files_meta(root, cfg)
            else:
                loaded = spec.load(root, cfg)
                files_meta = FilesMeta(files={p: _as_file(o) for p, o in loaded.items()})
            objects.update({ObjectKey(kind, p): e for p, e in files_meta.files.items()})
        elif kind is Kind.MODULE:
            modules = [_as_module(o) for o in spec.load(root, cfg).values()]
        else:
            objects.update({ObjectKey(kind, p): o for p, o in spec.load(root, cfg).items()})
    return Course(cfg, objects, files_meta, modules, _load_roster(root))


def _module_key(name: str, taken: dict[str, CanvasObject]) -> str:
    key = f"modules.yaml#{name}"
    n = 2
    while key in taken:  # Canvas allows duplicate module names; keep both, in order
        key = f"modules.yaml#{name}#{n}"
        n += 1
    return key


def dump_tree(course: Course) -> dict[str, bytes]:
    """Path -> exact file contents; canonical (LF, UTF-8, no BOM), sorted by path."""
    cfg = course.config
    out: dict[str, bytes] = {
        "course.yaml": dump_yaml(
            _config_data(cfg), CONFIG_KEY_ORDER, nested_orders=CONFIG_NESTED_ORDERS
        ).encode()
    }
    roster = course.roster.model_dump(exclude_defaults=True)
    if roster:
        out["roster.yaml"] = dump_yaml(roster, ROSTER_KEY_ORDER).encode()

    for kind, spec in model.REGISTRY.items():
        objs: dict[str, CanvasObject] = {}
        if kind is Kind.MODULE:
            for m in course.modules:
                objs[_module_key(m.name, objs)] = m
        elif kind is Kind.FILE and hasattr(spec, "dump_files_meta"):
            files = cast(FilesMetaSpec, spec).dump_files_meta(course.files_meta, cfg)
            _merge(out, files)
            continue
        else:
            objs = {k.path: o for k, o in sorted(course.objects.items()) if k.kind is kind}
        _merge(out, spec.dump(objs, cfg))
    return dict(sorted(out.items()))


def _merge(out: dict[str, bytes], files: dict[str, bytes]) -> None:
    for path, data in files.items():
        if path in out:
            raise ValueError(f"two sources write {path!r}")
        out[path] = data
