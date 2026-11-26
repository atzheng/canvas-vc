"""Course configuration (CONTRACTS.md section 3)."""

from __future__ import annotations

import os
from datetime import time
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, HttpUrl, field_validator
from pydantic import ValidationError as PydanticValidationError
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from canvas_sync.errors import ConfigError


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

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            raise ValueError(f"unknown IANA timezone {v!r}") from None
        return v

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def load_config(root: Path) -> CourseConfig:
    """Read ``<root>/course.yaml``; raises ConfigError with a one-line message."""
    path = root / "course.yaml"
    try:
        raw: object = YAML(typ="safe").load(  # pyright: ignore[reportUnknownMemberType]
            path.read_text(encoding="utf-8")
        )
    except FileNotFoundError:
        raise ConfigError(f"{path}: not found") from None
    except (YAMLError, OSError, UnicodeDecodeError) as e:
        raise ConfigError(f"{path}: cannot read: {str(e).splitlines()[0]}") from None
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: expected a mapping at top level")
    try:
        return CourseConfig.model_validate(raw)
    except PydanticValidationError as e:
        err = e.errors()[0]
        loc = ".".join(str(x) for x in err["loc"])
        raise ConfigError(f"{path}: {loc}: {err['msg']}") from None


def get_token(cfg: CourseConfig) -> str:
    """Raises ConfigError if the env var is unset or empty."""
    token = os.environ.get(cfg.token_env, "").strip()
    if not token:
        raise ConfigError(f"environment variable {cfg.token_env} is not set")
    return token
