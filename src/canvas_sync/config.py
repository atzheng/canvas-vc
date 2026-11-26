"""Course configuration (CONTRACTS.md section 3)."""

from __future__ import annotations

from datetime import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, HttpUrl


class AssignmentDefaults(BaseModel):
    submission_types: list[str] = ["online_upload"]
    due_time: time = time(23, 59)


class Defaults(BaseModel):
    published: bool = False
    unlock_time: time = time(9, 0)
    assignment: AssignmentDefaults = AssignmentDefaults()


class CourseConfig(BaseModel):
    canvas_url: HttpUrl
    course_id: int
    timezone: str  # IANA name; validated with zoneinfo
    token_env: str = "CANVAS_TOKEN"
    student_ref: Literal["login_id", "sis", "alias"] = "login_id"
    files_root: str = "/"  # Canvas folder that files/ maps to
    defaults: Defaults = Defaults()


def load_config(root: Path) -> CourseConfig:
    raise NotImplementedError


def get_token(cfg: CourseConfig) -> str:
    """Raises ConfigError if the env var is unset."""
    raise NotImplementedError
