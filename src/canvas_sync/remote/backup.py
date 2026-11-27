"""Pre-push backup (CONTRACTS.md section 10), safety layer 4.

GET-only export of every assignment a push will touch: assignment JSON, overrides
and submissions (metadata, score, grade, comments). The file holds student data,
so it lives under ``~/.canvas-sync/backups`` (never inside a course repo, file mode
0600) and nothing in it is ever logged or put in an error message.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from canvas_sync.config import CourseConfig
from canvas_sync.errors import CanvasAPIError, CanvasSyncError
from canvas_sync.remote.http import CanvasClient

FORMAT_VERSION = 1
_SUBMISSION_INCLUDES = ["submission_comments", "rubric_assessment"]


class BackupError(CanvasSyncError):
    """The backup could not be written; push must stop before its first write."""


def backup_dir(config: CourseConfig) -> Path:
    return Path.home() / ".canvas-sync" / "backups" / str(config.course_id)


def _refuse_inside_course_repo(directory: Path) -> None:
    for parent in (directory, *directory.parents):
        if (parent / "course.yaml").exists() or (parent / ".canvas").is_dir():
            raise BackupError("refusing to write a backup inside a course repo")


def backup(client: CanvasClient, config: CourseConfig, assignment_ids: Iterable[int]) -> Path:
    """Write ~/.canvas-sync/backups/<course_id>/<UTC timestamp>.json (GET only).

    Raises ``BackupError`` on any failure (a missing assignment, an API error, a
    write error, a file that does not read back); the caller must abort the push.
    """
    c = config.course_id
    ids = sorted({int(i) for i in assignment_ids})
    try:
        data: dict[str, Any] = {
            "format": FORMAT_VERSION,
            "course_id": c,
            "created_at": datetime.now(UTC).isoformat(),
            "late_policy": _late_policy(client, c),
            "assignments": {},
        }
        for aid in ids:
            base = f"/courses/{c}/assignments/{aid}"
            data["assignments"][str(aid)] = {
                "assignment": client.get_one(base),
                "overrides": client.get(f"{base}/overrides"),
                "submissions": client.get(
                    f"{base}/submissions", {"include[]": _SUBMISSION_INCLUDES}
                ),
            }
    except CanvasAPIError as e:
        raise BackupError(f"backup failed: Canvas API error {e.status}") from None
    except Exception as e:
        raise BackupError(f"backup failed: {type(e).__name__}") from None
    return _write(backup_dir(config), data, ids)


def _late_policy(client: CanvasClient, course_id: int) -> Any:
    try:
        return client.get_one(f"/courses/{course_id}/late_policy")
    except CanvasAPIError as e:
        if e.status == 404:  # no late policy configured
            return None
        raise


def _private_dirs(directory: Path) -> None:
    """Create ``directory`` and any missing parents, each chmod 0700 (mkdir's mode is
    masked by umask and ignored for parents). Only directories created here are changed;
    pre-existing ones (``~``, a ``~/.canvas-sync`` the user made) are left as they are."""
    missing = [d for d in (directory, *directory.parents) if not d.exists()]
    for d in reversed(missing):
        d.mkdir(mode=0o700)
        d.chmod(0o700)


def _write(directory: Path, data: dict[str, Any], ids: list[int]) -> Path:
    try:
        _refuse_inside_course_repo(directory.resolve())
        _private_dirs(directory)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        text = json.dumps(data, indent=2, sort_keys=True)
        for n in range(1000):
            target = directory / f"{stamp}{'' if n == 0 else f'-{n}'}.json"
            try:
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            break
        else:
            raise BackupError("backup failed: no free file name")
        back = json.loads(target.read_text(encoding="utf-8"))
        if sorted(int(k) for k in back["assignments"]) != ids:
            raise BackupError("backup failed: file did not read back")
        return target
    except BackupError:
        raise
    except Exception as e:
        raise BackupError(f"backup failed: {type(e).__name__}") from None
