"""Kind, ObjectKey, CanvasObject (CONTRACTS.md section 4)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, ConfigDict

from canvas_sync.config import CourseConfig

if TYPE_CHECKING:
    from canvas_sync.model.files import FilesMeta


class Kind(StrEnum):
    PAGE = "page"
    ASSIGNMENT = "assignment"
    ANNOUNCEMENT = "announcement"
    FILE = "file"
    MODULE = "module"


@dataclass(frozen=True, order=True)
class ObjectKey:
    kind: Kind
    path: str  # tree path, e.g. "assignments/hw1.md"; modules: "modules.yaml#<name>"


class CanvasObject(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    canvas_id: int | str | None = None  # pages use their URL slug (str)

    def key(self, path: str) -> ObjectKey:
        """ObjectKey for this object stored at tree path ``path``.

        The kind is derived from the path (``modules.yaml#...`` or the first
        folder, matched against the registered TypeSpecs), so type modules
        do not need to declare it.
        """
        return ObjectKey(kind_for_path(path), path)


def kind_for_path(path: str) -> Kind:
    """Kind of the object at tree path ``path``; ValueError if no type owns it."""
    from canvas_sync import model  # late: model/__init__ imports the type modules

    if path.startswith("modules.yaml#"):
        return Kind.MODULE
    folder = path.split("/", 1)[0]
    for kind, spec in model.REGISTRY.items():
        if spec.folder == folder and kind is not Kind.MODULE:
            return kind
    raise ValueError(f"no object type owns tree path {path!r}")


class TypeSpec(Protocol):
    """One per type, registered in model/__init__.py (never edited by type beads).

    ``load`` reads this type's files under ``root`` and returns objects keyed by
    tree path (``pages/syllabus.md``; modules: ``modules.yaml#<name>``, in
    on-Canvas order; files: ``files/<path>`` -> FileEntry). ``dump`` is the
    inverse: it returns ``{tree path: exact bytes}`` for the files this type
    owns, in canonical form (use ``model.serialize``). Together they must make
    ``dump(load(dump(objs)))`` equal ``dump(objs)``; ``tests/helpers/fixed_point``
    checks that. ``dump`` must not depend on dict order of ``objs`` (sort, or
    keep list order for modules, explicitly). Types that own a single file
    (modules.yaml) or a meta file plus blobs (files/_meta.yaml) still
    implement this interface; blobs are never returned by ``dump``.
    """

    kind: Kind
    folder: str  # "pages", "assignments", ...

    def load(self, root: Path, cfg: CourseConfig) -> dict[str, CanvasObject]: ...

    def dump(self, objs: dict[str, CanvasObject], cfg: CourseConfig) -> dict[str, bytes]: ...


class FilesMetaSpec(TypeSpec, Protocol):
    """Optional extension, implemented by the FILE spec only.

    ``files/_meta.yaml`` carries more than per-file entries (defaults and
    rules), which a ``dict[str, CanvasObject]`` cannot hold. When the FILE spec
    has these two methods, ``load_tree``/``dump_tree`` use them for
    ``Course.files_meta`` instead of ``load``/``dump``. Keys of
    ``FilesMeta.files`` are tree paths (``files/lectures/01.pdf``).
    """

    def load_files_meta(
        self, root: Path, cfg: CourseConfig, file_hashes: Mapping[str, str] | None = None
    ) -> FilesMeta: ...

    def dump_files_meta(self, meta: FilesMeta, cfg: CourseConfig) -> dict[str, bytes]: ...
