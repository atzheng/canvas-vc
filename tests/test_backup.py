"""Pre-push backup: GET only, outside the repo, aborts the push on failure."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

import pytest

from canvas_sync.config import CourseConfig
from canvas_sync.errors import CanvasSyncError
from canvas_sync.remote.backup import BackupError, backup
from tests.fake_canvas.core import FakeCanvas
from tests.helpers import guard_fake as gf


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    return h


@pytest.fixture
def fake() -> FakeCanvas:
    f = FakeCanvas(course_id=101)
    gf.install(f)
    return f


def cfg() -> CourseConfig:
    return CourseConfig.model_validate(
        {"canvas_url": "https://canvas.test", "course_id": 101, "timezone": "UTC"}
    )


def seeded(fake: FakeCanvas) -> int:
    a = gf.add_assignment(fake, has_submitted_submissions=True)
    fake.seed_submissions(
        a["id"],
        [
            {
                "user_id": 7,
                "score": 8.5,
                "grade": "8.5",
                "workflow_state": "graded",
                "submission_comments": [{"id": 1, "comment": "Nice work", "author_id": 2}],
            }
        ],
    )
    gf.add_override(fake, a["id"], course_section_id=5, due_at="2026-03-10T23:59:00Z")
    gf.set_late_policy(fake, True)
    return a["id"]


def test_backup_contains_assignment_overrides_submissions_scores_comments(
    fake: FakeCanvas, home: Path
) -> None:
    aid = seeded(fake)
    path = backup(fake.client(), cfg(), [aid])
    assert path.parent == home / ".canvas-sync" / "backups" / "101"
    assert path.suffix == ".json"
    data: dict[str, Any] = json.loads(path.read_text())
    entry = data["assignments"][str(aid)]
    assert entry["assignment"]["id"] == aid
    assert entry["overrides"][0]["course_section_id"] == 5
    sub = entry["submissions"][0]
    assert (sub["user_id"], sub["score"], sub["grade"]) == (7, 8.5, "8.5")
    assert sub["submission_comments"][0]["comment"] == "Nice work"
    assert data["late_policy"]["late_policy"]["late_submission_deduction_enabled"] is True


def test_backup_uses_only_get(fake: FakeCanvas, home: Path) -> None:
    aid = seeded(fake)
    backup(fake.client(mode="read"), cfg(), [aid])  # a read-only client is enough
    assert fake.log and all(e.method == "GET" for e in fake.log)
    assert fake.writes() == []
    assert any(e.path.endswith("/submissions") for e in fake.log)


def test_backup_file_is_private(fake: FakeCanvas, home: Path) -> None:
    path = backup(fake.client(), cfg(), [seeded(fake)])
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_two_backups_do_not_overwrite_each_other(fake: FakeCanvas, home: Path) -> None:
    aid = seeded(fake)
    a = backup(fake.client(), cfg(), [aid])
    b = backup(fake.client(), cfg(), [aid])
    assert a != b and a.exists() and b.exists()


def test_backup_without_late_policy_or_assignments(fake: FakeCanvas, home: Path) -> None:
    path = backup(fake.client(), cfg(), [])
    data = json.loads(path.read_text())
    assert data["late_policy"] is None and data["assignments"] == {}


@pytest.mark.parametrize(
    "failing", [r"/courses/101/assignments/\d+/submissions", r"/courses/101/assignments/\d+"]
)
def test_backup_failure_raises_and_leaves_no_file(
    fake: FakeCanvas, home: Path, failing: str
) -> None:
    aid = seeded(fake)
    fake.fail(500, method="GET", path=failing, times=None)
    with pytest.raises(BackupError):
        backup(fake.client(), cfg(), [aid])
    assert not list(home.rglob("*.json"))


def test_backup_of_missing_assignment_aborts(fake: FakeCanvas, home: Path) -> None:
    with pytest.raises(BackupError):
        backup(fake.client(), cfg(), [999999])


def test_backup_error_is_a_canvas_sync_error_for_the_cli(fake: FakeCanvas, home: Path) -> None:
    assert issubclass(BackupError, CanvasSyncError)


def test_backup_unwritable_location_aborts(fake: FakeCanvas, home: Path) -> None:
    aid = seeded(fake)
    (home / ".canvas-sync").write_text("a file where a directory should be")
    with pytest.raises(BackupError):
        backup(fake.client(), cfg(), [aid])


def test_backup_refuses_to_land_inside_a_course_repo(
    fake: FakeCanvas, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "course"
    repo.mkdir()
    (repo / "course.yaml").write_text("course_id: 101\n")
    monkeypatch.setenv("HOME", str(repo))
    with pytest.raises(BackupError, match="course repo"):
        backup(fake.client(), cfg(), [seeded(fake)])
    assert not list(repo.rglob("*.json"))


def test_errors_do_not_carry_student_data(fake: FakeCanvas, home: Path) -> None:
    aid = seeded(fake)
    fake.fail(500, method="GET", path=r".*/submissions", times=None, body="Ada Example 12345678")
    with pytest.raises(BackupError) as exc:
        backup(fake.client(), cfg(), [aid])
    assert "Ada" not in str(exc.value) and "12345678" not in str(exc.value)
