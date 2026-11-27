"""Roster reader (CONTRACTS.md section 8). Read-only: no create/update/delete.

Builds the untracked ``RosterCache`` (section, group and student ids keyed by the names or
refs used in tracked files). Student refs follow ``course.yaml: student_ref``: the Canvas
login ID, ``sis:<id>``, or an alias from the untracked ``roster-aliases.yaml``. Names and
Canvas user ids never leave this module except inside the cache.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from rich.console import Console

from canvas_sync.config import CourseConfig
from canvas_sync.errors import ValidationError
from canvas_sync.model.roster import RosterCache
from canvas_sync.model.serialize import load_yaml
from canvas_sync.remote.context import FetchContext
from canvas_sync.remote.http import CanvasClient

CACHE_PATH = ".canvas/roster-cache.json"  # untracked
ALIASES_PATH = "roster-aliases.yaml"  # untracked


def _warn(message: str) -> None:
    """One-line warning on stderr. Never put a student identifier in ``message``."""
    Console(stderr=True).print(f"warning: {message}", markup=False, highlight=False)


def student_ref(
    user: Mapping[str, Any], cfg: CourseConfig, login_to_alias: Mapping[str, str]
) -> str | None:
    """The ref for one Canvas user JSON under ``cfg.student_ref``; None if it has none."""
    login = user.get("login_id")
    if cfg.student_ref == "sis":
        sis = user.get("sis_user_id")
        return f"sis:{sis}" if sis else None
    if cfg.student_ref == "alias":
        return login_to_alias.get(str(login).casefold()) if login else None
    return str(login) if login else None


def read(
    client: CanvasClient,
    ctx: FetchContext,
    *,
    aliases: Mapping[str, str] | None = None,
) -> RosterCache:
    """Fetch sections, active students and groups into a ``RosterCache``.

    ``aliases`` maps alias -> login ID (``load_aliases``); required when
    ``student_ref: alias``. Students with no usable ref (no login/SIS id, or no alias) are
    left out, so nothing can name them. Groups are limited to ``roster.groups_category``
    from ``ctx.previous`` when known, else every group in the course.
    """
    cfg = ctx.config
    login_to_alias: dict[str, str] = {}
    if cfg.student_ref == "alias":
        if aliases is None:
            raise ValidationError(f"{ALIASES_PATH}: required when student_ref is alias")
        for alias, login in aliases.items():  # logins match case-insensitively
            if login.casefold() in login_to_alias:
                raise ValidationError(f"{ALIASES_PATH}: one login is mapped by two aliases")
            login_to_alias[login.casefold()] = alias

    students = _students(client, cfg, login_to_alias)
    if cfg.student_ref == "alias":
        for alias in sorted(set(login_to_alias.values()) - set(students)):
            _warn(f"{ALIASES_PATH}: alias {alias!r} matches no student in Canvas")
    return RosterCache(
        students=students,
        sections=_by_name(client.get(f"/courses/{cfg.course_id}/sections"), "section"),
        groups=_groups(client, ctx),
    )


def _by_name(rows: list[dict[str, Any]], what: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in rows:
        name = str(row["name"])
        if name in out:
            raise ValidationError(f"Canvas: two {what}s are named {name!r}")
        out[name] = int(row["id"])
    return out


def _students(
    client: CanvasClient, cfg: CourseConfig, login_to_alias: Mapping[str, str]
) -> dict[str, int]:
    users: list[dict[str, Any]] = client.get(
        f"/courses/{cfg.course_id}/users",
        {"enrollment_type[]": "student", "enrollment_state[]": "active"},
    )
    out: dict[str, int] = {}
    for user in users:
        ref = student_ref(user, cfg, login_to_alias)
        if ref is None:
            continue
        if ref in out:
            raise ValidationError("Canvas: two students share the same student ref")
        out[ref] = int(user["id"])
    return out


def _groups(client: CanvasClient, ctx: FetchContext) -> dict[str, int]:
    course = ctx.config.course_id
    category = ctx.previous.roster.groups_category if ctx.previous else None
    if category is None:
        return _unambiguous_groups(client.get(f"/courses/{course}/groups"))
    cats: list[dict[str, Any]] = client.get(f"/courses/{course}/group_categories")
    match = [c for c in cats if c["name"] == category]
    if not match:
        _warn(f"roster.yaml: group category {category!r} not found in Canvas")
        return {}
    cid = int(match[0]["id"])
    return _by_name(client.get(f"/courses/{course}/group_categories/{cid}/groups"), "group")


def _unambiguous_groups(rows: list[dict[str, Any]]) -> dict[str, int]:
    """All groups by name; names used in several categories are left out with a warning."""
    ids: dict[str, list[int]] = {}
    for row in rows:
        ids.setdefault(str(row["name"]), []).append(int(row["id"]))
    for name, found in ids.items():
        if len(found) > 1:
            _warn(
                f"group {name!r} exists in several categories; set groups_category in roster.yaml"
            )
    return {name: found[0] for name, found in ids.items() if len(found) == 1}


def load_aliases(root: Path) -> dict[str, str] | None:
    """``roster-aliases.yaml`` as alias -> login ID; None when the file is absent."""
    path = root / ALIASES_PATH
    if not path.exists():
        return None
    raw = load_yaml(path.read_text(encoding="utf-8"), ALIASES_PATH)
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValidationError(f"{ALIASES_PATH}: expected a mapping of alias: login")
    return {str(k): str(v) for k, v in cast("dict[object, object]", raw).items()}


def write_cache(root: Path, cache: RosterCache) -> Path:
    path = root / CACHE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(cache.to_json(), encoding="utf-8")
    return path


def load_cache(root: Path) -> RosterCache:
    path = root / CACHE_PATH
    if not path.exists():
        return RosterCache()
    return RosterCache.from_json(path.read_text(encoding="utf-8"))
