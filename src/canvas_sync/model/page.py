"""Page model and TypeSpec (CONTRACTS.md sections 4 and 5; SPEC.md "Pages")."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, cast

from pydantic import ValidationError as PydanticValidationError

from canvas_sync.config import CourseConfig
from canvas_sync.dates import LocalDT, parse_local
from canvas_sync.errors import ValidationError
from canvas_sync.model.base import CanvasObject, Kind
from canvas_sync.model.serialize import dump_frontmatter, load_frontmatter

KEY_ORDER = ["canvas_id", "title", "published", "publish_at", "front_page", "body_format"]


class Page(CanvasObject):
    # canvas_id is the page URL slug (str); None for a page not yet on Canvas.
    title: str = ""
    published: bool | None = None  # None: no value, the consumer applies course defaults
    publish_at: LocalDT | None = None
    front_page: bool = False
    body_format: Literal["md", "html"] = "md"
    body: str = ""


def _parse(text: str, source: str, cfg: CourseConfig) -> Page:
    meta, body = load_frontmatter(text, source)
    for key in meta:
        if key not in KEY_ORDER:
            raise ValidationError(f"{source}: unknown frontmatter key {key!r}")
    meta = {k: v for k, v in meta.items() if v is not None}
    if "publish_at" in meta:
        meta["publish_at"] = parse_local(meta["publish_at"], cfg.tz, cfg.defaults.unlock_time)
    if "canvas_id" in meta:
        meta["canvas_id"] = str(meta["canvas_id"])  # an all-digit slug parses as int
    try:
        return Page(body=body, **meta)
    except PydanticValidationError as e:
        bad = ", ".join(str(err["loc"][0]) for err in e.errors() if err["loc"])
        raise ValidationError(f"{source}: invalid frontmatter value for {bad}: {e}") from None


class _Spec:
    kind = Kind.PAGE
    folder = "pages"

    def load(self, root: Path, cfg: CourseConfig) -> dict[str, CanvasObject]:
        out: dict[str, CanvasObject] = {}
        seen: dict[object, str] = {}
        folder = root / self.folder
        entries = sorted(folder.iterdir()) if folder.is_dir() else []
        for p in entries:
            if p.name.startswith("."):
                continue
            rel = f"{self.folder}/{p.name}"
            if not (p.is_file() and p.suffix == ".md"):
                raise ValidationError(
                    f"{rel}: only top-level .md files are allowed in {self.folder}/"
                )
            obj = _parse(p.read_text(encoding="utf-8"), rel, cfg)
            if obj.canvas_id is not None:
                if obj.canvas_id in seen:
                    raise ValidationError(
                        f"{rel}: duplicate canvas_id {obj.canvas_id!r} (also in {seen[obj.canvas_id]})"  # noqa: E501
                    )
                seen[obj.canvas_id] = rel
            out[rel] = obj
        return out

    def dump(self, objs: dict[str, CanvasObject], cfg: CourseConfig) -> dict[str, bytes]:
        files: dict[str, bytes] = {}
        for path in sorted(objs):
            page = cast("Page", objs[path])
            meta: dict[str, Any] = page.model_dump(exclude={"body"}, exclude_defaults=True)
            files[path] = dump_frontmatter(meta, page.body, KEY_ORDER).encode("utf-8")
        return files


PAGE_SPEC = _Spec()
