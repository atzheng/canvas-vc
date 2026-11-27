"""FetchContext and ApplyContext (CONTRACTS.md section 8)."""

from __future__ import annotations

from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from canvas_sync.config import CourseConfig
from canvas_sync.ignore import IgnoreRules
from canvas_sync.model.base import ObjectKey
from canvas_sync.model.course import Course
from canvas_sync.model.roster import RosterCache
from canvas_sync.remote.guard import SubmissionGuard
from canvas_sync.render.bodies import Bodies
from canvas_sync.snapshot import BlobStore


@dataclass
class FetchContext:
    config: CourseConfig
    tz: ZoneInfo
    bodies: Bodies
    previous: Course | None  # last canvas/main tree, for body reuse and file hashes
    roster: RosterCache
    ignore: IgnoreRules
    blob_store: BlobStore  # where downloaded file contents go


@dataclass
class ApplyContext:
    config: CourseConfig
    tz: ZoneInfo
    roster: RosterCache
    ids: dict[ObjectKey, int | str]  # filled in as creates happen; used to render links
    guard: SubmissionGuard
    blobs: BlobStore
    # Writers record (tree path, Markdown, HTML Canvas returned) here after each body write;
    # push dumps it to .canvas/bodies.json so the next fetch reuses the Markdown.
    bodies: Bodies = field(default_factory=Bodies)
