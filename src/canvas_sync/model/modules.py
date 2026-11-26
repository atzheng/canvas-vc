"""Module model and ``modules.yaml`` (CONTRACTS.md sections 4 and 5).

``modules.yaml`` is a YAML list of modules; the order of modules and of the
items inside each module is the order on Canvas and is preserved exactly.
Every module and item carries its ``canvas_id`` once known, so the plan can
match by ID first (dump_tree keys duplicate module names positionally).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    StrictInt,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic import ValidationError as PydanticValidationError

from canvas_sync.config import CourseConfig
from canvas_sync.dates import LocalDT, parse_local
from canvas_sync.errors import ValidationError
from canvas_sync.model.base import CanvasObject, Kind
from canvas_sync.model.overrides import Override, validate_overrides
from canvas_sync.model.serialize import dump_yaml, load_yaml

FILENAME = "modules.yaml"
KEY_ORDER = ["canvas_id", "name", "published", "unlock", "overrides", "items"]
OVERRIDE_KEY_ORDER = ["section", "group", "students", "unlock"]
ITEM_KEY_ORDER = [
    "canvas_id",
    "page",
    "assignment",
    "file",
    "header",
    "url",
    "quiz",
    "discussion",
    "title",
    "indent",
    "new_tab",
    "published",
]
NESTED_ORDERS = {"items": ITEM_KEY_ORDER, "overrides": OVERRIDE_KEY_ORDER}

_ITEM_TYPES = ("page", "assignment", "file", "header", "url", "quiz", "discussion")
_PATH_TYPES = ("page", "assignment", "file")


class UnmanagedRef(BaseModel):
    """Quiz or discussion item: an object canvas-sync does not manage."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    canvas_id: StrictInt


class ModuleOverride(Override):
    """Module "Assign To" entry: one target (section, group or students) plus ``unlock``.

    Reuses ``Override`` for the target rule; modules only support ``unlock``,
    so ``title``, ``due`` and ``lock`` are rejected and ``unlock`` is required.
    """

    @model_validator(mode="after")
    def _unlock_only(self) -> ModuleOverride:
        if self.unlock is None:
            raise ValueError("a module override needs an unlock date")
        extra = [k for k in ("title", "due", "lock") if getattr(self, k) is not None]
        if extra:
            raise ValueError(f"module overrides only support unlock (got {', '.join(extra)})")
        return self


class ModuleItem(BaseModel):
    """Exactly one of page, assignment, file, header, url, quiz, discussion is set.

    page / assignment / file refer to local objects by tree path (page and
    assignment without folder or extension, file relative to ``files/``);
    quiz / discussion refer to unmanaged Canvas objects by ``canvas_id``.
    Only the shape is checked here; whether the target exists is ``validate``'s job.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    page: str | None = None
    assignment: str | None = None
    file: str | None = None
    header: str | None = None
    url: str | None = None
    quiz: UnmanagedRef | None = None
    discussion: UnmanagedRef | None = None
    title: str | None = None
    indent: StrictInt = 0
    new_tab: bool = False
    published: bool | None = None
    canvas_id: StrictInt | None = None

    @field_validator("indent", "new_tab", mode="before")
    @classmethod
    def _null_is_default(cls, v: object, info: ValidationInfo) -> object:
        """Absent and null both mean "no value": 0 and False."""
        if v is None:
            return 0 if info.field_name == "indent" else False
        return v

    @model_validator(mode="after")
    def _one_type(self) -> ModuleItem:
        kinds = [t for t in _ITEM_TYPES if getattr(self, t) is not None]
        if len(kinds) != 1:
            got = ", ".join(kinds) if kinds else "none"
            raise ValueError(
                f"a module item needs exactly one of {', '.join(_ITEM_TYPES)} (got {got})"
            )
        kind = kinds[0]
        value = getattr(self, kind)
        if kind in _PATH_TYPES:
            _check_ref(kind, cast(str, value))
        elif kind == "header" and not cast(str, value).strip():
            raise ValueError("header text must not be empty")
        elif kind == "url":
            _check_url(cast(str, value))
        if self.indent < 0:
            raise ValueError("indent must be >= 0")
        return self

    @property
    def item_type(self) -> str:
        """Which of the seven reference types this item has."""
        return next(t for t in _ITEM_TYPES if getattr(self, t) is not None)


def _check_ref(kind: str, value: str) -> None:
    if not value.strip() or value != value.strip():
        raise ValueError(f"{kind} must be a non-empty path")
    parts = value.split("/")
    if "\\" in value or value.startswith("/") or any(p in ("", ".", "..") for p in parts):
        raise ValueError(f"{kind} {value!r} must be a relative path without '..' or empty parts")


def _check_url(value: str) -> None:
    if not value.startswith(("http://", "https://")) or " " in value:
        raise ValueError(f"url {value!r} must be an http(s) URL without spaces")


class Module(CanvasObject):
    canvas_id: StrictInt | None = None  # pyright: ignore[reportIncompatibleVariableOverride]
    name: str
    published: bool | None = None
    unlock: LocalDT | None = None
    overrides: list[ModuleOverride] = []  # unlock only
    items: list[ModuleItem] = []

    @field_validator("overrides")
    @classmethod
    def _canonical_overrides(cls, v: list[ModuleOverride]) -> list[ModuleOverride]:
        return cast("list[ModuleOverride]", validate_overrides(v, "modules.yaml overrides"))


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _module_key(name: str, taken: dict[str, CanvasObject]) -> str:
    """Same scheme as ``dump_tree``: ``modules.yaml#<name>``, ``#<name>#2`` for repeats."""
    key = f"{FILENAME}#{name}"
    n = 2
    while key in taken:
        key = f"{FILENAME}#{name}#{n}"
        n += 1
    return key


def _date(value: object, cfg: CourseConfig, where: str) -> LocalDT | None:
    if value is None:
        return None
    try:
        return parse_local(cast("str", value), cfg.tz, cfg.defaults.unlock_time)
    except ValidationError as e:
        raise ValidationError(f"{FILENAME}: {where}: {e}") from None


def _map(raw: object, where: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValidationError(f"{FILENAME}: {where} must be a mapping")
    return dict(cast("dict[str, Any]", raw))


def _parse_module(raw: object, idx: int, cfg: CourseConfig) -> Module:
    where = f"[{idx}]"
    data = _map(raw, where)
    if "unlock" in data:
        data["unlock"] = _date(data["unlock"], cfg, f"{where}.unlock")
    ovs = data.get("overrides")
    if ovs is not None:
        if not isinstance(ovs, list):
            raise ValidationError(f"{FILENAME}: {where}.overrides must be a list")
        fixed: list[dict[str, Any]] = []
        for j, o in enumerate(cast("list[Any]", ovs)):
            od = _map(o, f"{where}.overrides[{j}]")
            if "unlock" in od:
                od["unlock"] = _date(od["unlock"], cfg, f"{where}.overrides[{j}].unlock")
            fixed.append(od)
        data["overrides"] = fixed
    # absent and null both mean "no value"
    for key in ("overrides", "items"):
        if key in data and data[key] is None:
            del data[key]
    try:
        return Module.model_validate(data)
    except PydanticValidationError as e:
        err = e.errors()[0]
        loc = "".join(f".{x}" if isinstance(x, str) else f"[{x}]" for x in err["loc"])
        msg = str(err["msg"]).removeprefix("Value error, ")
        raise ValidationError(f"{FILENAME}: {where}{loc}: {msg}") from None


def _check_unique_ids(mods: list[Module]) -> None:
    seen: set[int] = set()
    for i, m in enumerate(mods):
        ids = [(f"[{i}]", m.canvas_id)]
        ids += [(f"[{i}].items[{j}]", it.canvas_id) for j, it in enumerate(m.items)]
        for where, cid in ids:
            if cid is None:
                continue
            if cid in seen:
                raise ValidationError(f"{FILENAME}: {where}: duplicate canvas_id {cid}")
            seen.add(cid)


def parse_modules(text: str, cfg: CourseConfig) -> list[Module]:
    """Parse the text of ``modules.yaml``; tolerant (date-only, key order, CRLF, BOM)."""
    raw = load_yaml(text, FILENAME)
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValidationError(f"{FILENAME}: must be a list of modules")
    mods = [_parse_module(r, i, cfg) for i, r in enumerate(cast("list[Any]", raw))]
    _check_unique_ids(mods)
    return mods


def _dump_data(m: Module) -> dict[str, Any]:
    data = m.model_dump(exclude_defaults=True, exclude_none=True)
    data["name"] = m.name  # required, even when empty
    return data


class _Spec:
    kind = Kind.MODULE
    folder = "modules"

    def load(self, root: Path, cfg: CourseConfig) -> dict[str, CanvasObject]:
        path = root / FILENAME
        if not path.exists():
            return {}
        out: dict[str, CanvasObject] = {}
        for m in parse_modules(path.read_text(encoding="utf-8"), cfg):
            out[_module_key(m.name, out)] = m
        return out

    def dump(self, objs: dict[str, CanvasObject], cfg: CourseConfig) -> dict[str, bytes]:
        """Modules are written in dict order (the order on Canvas); no modules, no file."""
        mods: list[Module] = []
        for o in objs.values():
            if not isinstance(o, Module):
                raise TypeError(f"MODULE spec got {type(o).__name__}, expected Module")
            mods.append(o)
        if not mods:
            return {}
        text = dump_yaml([_dump_data(m) for m in mods], KEY_ORDER, nested_orders=NESTED_ORDERS)
        return {FILENAME: text.encode("utf-8")}


MODULE_SPEC = _Spec()
