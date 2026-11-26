"""Assignment model (CONTRACTS.md section 4)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, cast

from pydantic import ValidationError as PydanticValidationError
from pydantic import field_validator, model_validator

from canvas_sync.config import CourseConfig
from canvas_sync.dates import LocalDT, parse_local
from canvas_sync.errors import ValidationError
from canvas_sync.model import overrides as ov
from canvas_sync.model.base import CanvasObject, Kind
from canvas_sync.model.overrides import Override
from canvas_sync.model.serialize import dump_frontmatter, load_frontmatter

KEY_ORDER = [
    "canvas_id",
    "title",
    "points",
    "group",
    "submission_types",
    "allowed_extensions",
    "grading_type",
    "omit_from_final_grade",
    "published",
    "unlock",
    "due",
    "lock",
    "assign_to_overrides_only",
    "overrides",
    "body_format",
]


class Assignment(CanvasObject):
    title: str = ""
    points: float | None = None
    group: str | None = None
    submission_types: list[str] = []
    allowed_extensions: list[str] = []
    grading_type: str = "points"
    omit_from_final_grade: bool = False
    published: bool | None = None
    unlock: LocalDT | None = None
    due: LocalDT | None = None
    lock: LocalDT | None = None
    assign_to_overrides_only: bool = False
    overrides: list[Override] = []
    body_format: Literal["md", "html"] = "md"
    body: str = ""

    @field_validator("overrides")
    @classmethod
    def _canonical_overrides(cls, v: list[Override]) -> list[Override]:
        return ov.validate_overrides(v)

    @model_validator(mode="after")
    def _overrides_only_has_no_base_dates(self) -> Assignment:
        if self.assign_to_overrides_only:
            base = [k for k in ov.DATE_KEYS if getattr(self, k) is not None]
            if base:
                raise ValidationError(
                    "assign_to_overrides_only removes 'Everyone else', so base "
                    f"{'/'.join(base)} must be absent"
                )
        return self


class _Spec:
    kind = Kind.ASSIGNMENT
    folder = "assignments"

    def load(self, root: Path, cfg: CourseConfig) -> dict[str, CanvasObject]:
        out: dict[str, CanvasObject] = {}
        for p in sorted((root / self.folder).glob("*.md")):
            path = f"{self.folder}/{p.name}"
            meta, body = load_frontmatter(p.read_text(encoding="utf-8"), path)
            out[path] = _build(meta, body, cfg, path)
        return out

    def dump(self, objs: dict[str, CanvasObject], cfg: CourseConfig) -> dict[str, bytes]:
        files: dict[str, bytes] = {}
        for path, obj in sorted(objs.items()):
            a = cast(Assignment, obj)
            meta = a.model_dump(exclude={"body"}, exclude_defaults=True)
            text = dump_frontmatter(
                meta, a.body, KEY_ORDER, nested_orders={"overrides": ov.KEY_ORDER}
            )
            files[path] = text.encode()
        return files


def _build(meta: dict[str, Any], body: str, cfg: CourseConfig, path: str) -> Assignment:
    d = cfg.defaults
    unlock_t, due_t = d.unlock_time, d.assignment.due_time
    for key in ov.DATE_KEYS:
        if meta.get(key) is not None:
            try:
                meta[key] = parse_local(meta[key], cfg.tz, unlock_t if key == "unlock" else due_t)
            except ValidationError as e:
                raise ValidationError(f"{path}: {key}: {e}") from None
    raw_overrides = meta.get("overrides")
    if raw_overrides is not None:
        if not isinstance(raw_overrides, list):
            raise ValidationError(f"{path}: overrides must be a list")
        parsed: list[Any] = []
        for i, item in enumerate(cast("list[Any]", raw_overrides), 1):
            if not isinstance(item, dict):
                raise ValidationError(f"{path}: override {i} must be a mapping")
            try:
                parsed.append(
                    ov.parse_override_dates(
                        cast("dict[str, Any]", item), cfg.tz, unlock_t, due_t, i
                    )
                )
            except ValidationError as e:
                raise ValidationError(f"{path}: {e}") from None
        meta["overrides"] = parsed
    try:
        return Assignment(body=body, **meta)
    except PydanticValidationError as e:
        err = e.errors()[0]
        loc = ".".join(str(x + 1 if isinstance(x, int) else x) for x in err["loc"])  # 1-based
        raise ValidationError(f"{path}: {loc}: {err['msg']}") from None
    except ValidationError as e:
        raise ValidationError(f"{path}: {e}") from None


ASSIGNMENT_SPEC = _Spec()
