"""canvas push end to end over FakeCanvas in tmp git repos (bead orx.8).

Every test that has seeded submissions ends with ``assert_submissions_intact``.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest
from rich.console import Console

from canvas_sync import gitops
from canvas_sync.commands.clone import clone_course
from canvas_sync.commands.fetch import run_fetch
from canvas_sync.commands.push import PushResult, run_push
from canvas_sync.config import load_config
from canvas_sync.errors import (
    CanvasSyncError,
    DirtyTree,
    GuardBlocked,
    NotFastForward,
    StaleRemote,
    UnresolvedConflicts,
    ValidationError,
)
from canvas_sync.plan import core as plan_core
from canvas_sync.remote.backup import BackupError
from tests.fake_canvas.assignments import add_assignment, add_assignment_group
from tests.fake_canvas.core import FakeCanvas
from tests.fake_canvas.discussion_topics import add_announcement
from tests.fake_canvas.modules import add_item, add_module
from tests.fake_canvas.pages import add_page

URL = "https://canvas.test"
TZ = "America/Vancouver"
_IDENT = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def git(root: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=check,
        env={**os.environ, **_IDENT},
    )
    return proc.stdout


def commit(root: Path, msg: str = "edit") -> None:
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", msg)


def edit(root: Path, path: str, old: str, new: str) -> None:
    p = root / path
    text = p.read_text(encoding="utf-8")
    assert old in text, (path, old)
    p.write_text(text.replace(old, new, 1), encoding="utf-8")


def write(root: Path, path: str, text: str) -> None:
    p = root / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


class Seed:
    hw1: dict[str, Any]
    hw2: dict[str, Any]
    page: dict[str, Any]
    ann: dict[str, Any]
    mod: dict[str, Any]


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    return h


@pytest.fixture(autouse=True)
def no_files_differ(monkeypatch: pytest.MonkeyPatch) -> None:
    """plan/files.py is still a stub (raises NotImplementedError); push is tested without it."""
    monkeypatch.setattr(
        plan_core,
        "DIFFER_MODULES",
        tuple(m for m in plan_core.DIFFER_MODULES if not m.endswith(".files")),
    )


@pytest.fixture
def fake() -> FakeCanvas:
    return FakeCanvas()


@pytest.fixture
def s(fake: FakeCanvas) -> Seed:
    s = Seed()
    ag = add_assignment_group(fake, "Homework")
    s.hw1 = add_assignment(
        fake,
        "Homework 1",
        description="<p>One</p>",
        points_possible=10,
        assignment_group_id=ag["id"],
        submission_types=["online_upload"],
        due_at="2026-09-22T06:59:00Z",
        has_submitted_submissions=True,
    )
    s.hw2 = add_assignment(
        fake,
        "Homework 2",
        description="<p>Two</p>",
        points_possible=5,
        assignment_group_id=ag["id"],
    )
    fake.seed_submissions(
        s.hw1["id"],
        [
            {
                "user_id": 7001,
                "workflow_state": "submitted",
                "submitted_at": "2026-09-21T10:00:00Z",
                "score": 8,
                "grade": "8",
                "attempt": 1,
                "submission_type": "online_upload",
            }
        ],
    )
    s.page = add_page(fake, "Syllabus", "<p>Welcome to the course.</p>", front_page=True)
    add_page(fake, "Notes", "<p>Plain notes.</p>")
    s.ann = add_announcement(fake, "Hello", "<p>First day.</p>", published=False)
    s.mod = add_module(fake, "Week 1")
    add_item(fake, s.mod["id"], "Page", title="Syllabus", page_url="syllabus")
    return s


@pytest.fixture
def repo(fake: FakeCanvas, s: Seed, tmp_path: Path) -> Path:
    root = tmp_path / "course"
    clone_course(fake.client("read"), URL, 101, root, TZ)
    return root


def push(fake: FakeCanvas, root: Path, **kw: Any) -> tuple[PushResult, str]:
    buf = io.StringIO()
    con = Console(file=buf, width=200, force_terminal=False)
    kw.setdefault("yes", True)
    mode = "read" if kw.get("dry_run") else "write"
    result = run_push(root, fake.client(mode), load_config(root), console=con, **kw)
    return result, buf.getvalue()


def assert_synced(root: Path) -> None:
    """canvas/main is merged into HEAD; they differ only in files Canvas has no place for
    (the .gitignore that clone adds); the tree is clean."""
    assert gitops.is_clean(root)
    assert gitops.is_ancestor(root, "refs/remotes/canvas/main", "HEAD")
    assert git(root, "diff", "--name-only", "canvas/main", "HEAD").split() == [".gitignore"]


def writes(fake: FakeCanvas) -> list[str]:
    return [f"{r.method} {r.path}" for r in fake.log if r.method != "GET"]


def test_nothing_to_push(fake: FakeCanvas, repo: Path) -> None:
    fake.log.clear()
    result, out = push(fake, repo)
    assert result.ops_total == 0 and "Nothing to push" in out
    assert writes(fake) == []


def test_update_push_roundtrip(fake: FakeCanvas, s: Seed, repo: Path) -> None:
    snap = fake.snapshot_submissions()
    edit(repo, "pages/notes.md", "Plain notes.", "Edited notes.")
    commit(repo)
    result, out = push(fake, repo)
    assert result.ok and result.fast_forwarded, out
    assert "pages/notes.md" in out
    assert_synced(repo)
    assert gitops.is_clean(repo)
    # second push is a no-op
    fake.log.clear()
    again, _ = push(fake, repo)
    assert again.ops_total == 0 and writes(fake) == []
    fake.assert_submissions_intact(snap)


def refuses(
    fake: FakeCanvas, root: Path, exc: type[CanvasSyncError], match: str, **kw: Any
) -> None:
    fake.log.clear()
    with pytest.raises(exc, match=match):
        push(fake, root, **kw)
    assert writes(fake) == []


# ---- refusals (nothing is written) ------------------------------------------------------


def conflict(root: Path) -> None:
    git(root, "checkout", "-q", "-b", "other")
    edit(root, "pages/notes.md", "Plain notes.", "Other notes.")
    commit(root, "other")
    git(root, "checkout", "-q", "main")
    edit(root, "pages/notes.md", "Plain notes.", "Main notes.")
    commit(root, "main")
    git(root, "merge", "other", check=False)


def test_refuses_unmerged_paths(fake: FakeCanvas, repo: Path) -> None:
    conflict(repo)
    refuses(fake, repo, UnresolvedConflicts, r"unmerged paths: pages/notes\.md")


def test_refuses_conflict_markers(fake: FakeCanvas, repo: Path) -> None:
    conflict(repo)
    git(repo, "add", "pages/notes.md")  # index resolved, markers still in the file
    refuses(fake, repo, UnresolvedConflicts, r"conflict markers in pages/notes\.md")


def test_refuses_dirty_tree(fake: FakeCanvas, repo: Path) -> None:
    edit(repo, "pages/notes.md", "Plain notes.", "Dirty notes.")
    refuses(fake, repo, DirtyTree, "uncommitted changes; commit them or use --allow-dirty")


def test_allow_dirty_pushes_head_not_the_edit(fake: FakeCanvas, repo: Path) -> None:
    edit(repo, "pages/notes.md", "Plain notes.", "Committed notes.")
    commit(repo)
    edit(repo, "pages/syllabus.md", "Welcome", "Dirty welcome")
    result, _ = push(fake, repo, allow_dirty=True)
    assert result.ok
    pages = {p["url"]: p for p in fake.state("pages")["pages"].values()}
    assert "Committed notes." in pages["notes"]["body"]
    assert "Dirty" not in pages["syllabus"]["body"]
    assert "Dirty welcome" in (repo / "pages/syllabus.md").read_text()  # edit survives


def test_refuses_when_canvas_main_not_merged(fake: FakeCanvas, s: Seed, repo: Path) -> None:
    pg = next(p for p in fake.state("pages")["pages"].values() if p["url"] == "syllabus")
    pg["body"] = "<p>Changed on Canvas.</p>"
    assert run_fetch(repo, fake.client("read"), load_config(repo)).commit
    edit(repo, "pages/notes.md", "Plain notes.", "Mine.")
    commit(repo)
    refuses(fake, repo, NotFastForward, "canvas/main is not an ancestor of HEAD; run canvas pull")


def test_refuses_validation_errors(fake: FakeCanvas, repo: Path) -> None:
    edit(repo, "assignments/homework-1.md", "points: 10", "points: not-a-number")
    commit(repo)
    refuses(fake, repo, ValidationError, "validation failed")


def test_refuses_stale_remote(fake: FakeCanvas, s: Seed, repo: Path) -> None:
    notes = next(p for p in fake.state("pages")["pages"].values() if p["url"] == "notes")
    notes["body"] = "<p>Edited on Canvas meanwhile.</p>"  # between fetch and push
    edit(repo, "pages/notes.md", "Plain notes.", "Local edit.")
    commit(repo)
    refuses(
        fake, repo, StaleRemote, r"Canvas has changed since your last fetch \(pages/notes\.md\)"
    )
    assert "Edited on Canvas" in notes["body"]


def test_stale_ignores_untouched_objects(fake: FakeCanvas, s: Seed, repo: Path) -> None:
    syl = next(p for p in fake.state("pages")["pages"].values() if p["url"] == "syllabus")
    syl["body"] = "<p>Edited on Canvas meanwhile.</p>"
    edit(repo, "pages/notes.md", "Plain notes.", "Local edit.")
    commit(repo)
    result, _ = push(fake, repo)
    assert result.ok


def test_refuses_missing_confirm_flags(fake: FakeCanvas, s: Seed, repo: Path) -> None:
    edit(repo, "assignments/homework-2.md", "points: 5", "points: 50")
    write(repo, "announcements/news.md", "---\ntitle: News\n---\nNow.\n")
    commit(repo)
    fake.log.clear()
    with pytest.raises(GuardBlocked) as e:
        push(fake, repo)
    msg = str(e.value)
    assert "--confirm-grading-change" in msg and "--confirm-notify" in msg
    assert "assignments/homework-2.md" in msg and "announcements/news.md" in msg
    assert writes(fake) == []
    result, _ = push(fake, repo, confirmed={"--confirm-grading-change", "--confirm-notify"})
    assert result.ok
    assert fake.state("assignments")["assignments"][s.hw2["id"]]["points_possible"] == 50


def test_scoped_confirm_flag(fake: FakeCanvas, s: Seed, repo: Path) -> None:
    edit(repo, "assignments/homework-2.md", "points: 5", "points: 6")
    commit(repo)
    refuses(fake, repo, GuardBlocked, "--confirm-grading-change")
    result, _ = push(fake, repo, confirmed={"--confirm-grading-change assignments/homework-2.md"})
    assert result.ok


def test_backup_failure_aborts_before_any_write(fake: FakeCanvas, s: Seed, repo: Path) -> None:
    snap = fake.snapshot_submissions()
    edit(repo, "assignments/homework-1.md", "One", "One, revised")
    edit(repo, "pages/notes.md", "Plain notes.", "Notes")
    commit(repo)
    fake.log.clear()
    fake.fail(500, method="GET", path=r"/courses/101/assignments/\d+/submissions", times=None)
    with pytest.raises(BackupError):
        push(fake, repo)
    assert writes(fake) == []
    fake.assert_submissions_intact(snap)


def test_backup_written_for_touched_assignments_only(
    fake: FakeCanvas, s: Seed, repo: Path, home: Path
) -> None:
    edit(repo, "assignments/homework-2.md", "Two", "Two, revised")
    commit(repo)
    result, _ = push(fake, repo)
    assert result.ok
    files = list((home / ".canvas-sync" / "backups" / "101").glob("*.json"))
    assert len(files) == 1
    data = json.loads(files[0].read_text())
    assert list(data["assignments"]) == [str(s.hw2["id"])]


def test_delete_of_assignment_with_submissions_refuses_whole_push(
    fake: FakeCanvas, s: Seed, repo: Path
) -> None:
    snap = fake.snapshot_submissions()
    (repo / "assignments/homework-1.md").unlink()
    edit(repo, "pages/notes.md", "Plain notes.", "Other edit that must not be applied.")
    commit(repo)
    fake.log.clear()
    with pytest.raises(GuardBlocked) as e:
        push(fake, repo)
    msg = str(e.value)
    assert "assignments/homework-1.md" in msg and "\n" not in msg
    assert "git checkout -- assignments/homework-1.md" in msg and ".canvasignore" in msg
    assert writes(fake) == []
    assert s.hw1["id"] in fake.state("assignments")["assignments"]
    fake.assert_submissions_intact(snap)


def test_delete_of_assignment_without_submissions(fake: FakeCanvas, s: Seed, repo: Path) -> None:
    (repo / "assignments/homework-2.md").unlink()
    commit(repo)
    result, _ = push(fake, repo)
    assert result.ok
    assert s.hw2["id"] not in fake.state("assignments")["assignments"]
    assert_synced(repo)


def test_no_delete_keeps_canvas_objects(fake: FakeCanvas, s: Seed, repo: Path) -> None:
    snap = fake.snapshot_submissions()
    (repo / "assignments/homework-1.md").unlink()  # would be refused without --no-delete
    (repo / "pages/notes.md").unlink()
    edit(repo, "pages/syllabus.md", "Welcome", "Hello")
    commit(repo)
    result, out = push(fake, repo, no_delete=True)
    assert result.ok, out
    assert "kept on Canvas (--no-delete): pages/notes.md" in out
    assert not any(r.method == "DELETE" for r in fake.log)
    assert any(p["url"] == "notes" for p in fake.state("pages")["pages"].values())
    fake.assert_submissions_intact(snap)


def test_ignored_file_is_not_a_delete(fake: FakeCanvas, s: Seed, repo: Path) -> None:
    snap = fake.snapshot_submissions()
    write(repo, ".canvasignore", "assignments/homework-1.md\n")
    (repo / "assignments/homework-1.md").unlink()
    commit(repo)
    fake.log.clear()
    result, _ = push(fake, repo)
    assert result.ok
    assert not any(r.method == "DELETE" for r in fake.log)
    assert s.hw1["id"] in fake.state("assignments")["assignments"]
    fake.assert_submissions_intact(snap)


def test_dry_run_writes_nothing(fake: FakeCanvas, s: Seed, repo: Path, home: Path) -> None:
    edit(repo, "assignments/homework-2.md", "Two", "Two, revised")
    write(repo, "pages/new-page.md", "---\ntitle: New Page\n---\nHi\n")
    commit(repo)
    head = git(repo, "rev-parse", "HEAD")
    main = git(repo, "rev-parse", "canvas/main")
    fake.log.clear()
    result, out = push(fake, repo, dry_run=True)
    assert result.dry_run and result.ok
    assert "assignments/homework-2.md" in out and "pages/new-page.md" in out
    assert writes(fake) == []
    assert not (home / ".canvas-sync").exists()
    assert git(repo, "rev-parse", "HEAD") == head and git(repo, "rev-parse", "canvas/main") == main


def test_dry_run_reports_guard_refusal(fake: FakeCanvas, s: Seed, repo: Path) -> None:
    (repo / "assignments/homework-1.md").unlink()
    commit(repo)
    fake.log.clear()
    with pytest.raises(GuardBlocked, match="student submissions"):
        push(fake, repo, dry_run=True)
    assert writes(fake) == []


def test_declined_confirmation_writes_nothing(fake: FakeCanvas, s: Seed, repo: Path) -> None:
    edit(repo, "pages/notes.md", "Plain notes.", "Edited")
    commit(repo)
    fake.log.clear()
    with pytest.raises(CanvasSyncError, match="aborted"):
        push(fake, repo, yes=False, confirm=lambda _m: False)
    assert writes(fake) == []
    asked: list[str] = []
    result, _ = push(fake, repo, yes=False, confirm=lambda m: asked.append(m) or True)
    assert result.ok and asked == ["Push 1 change to Canvas?"]


# ---- creates, id write-back, fast-forward -------------------------------------------------


def test_creates_get_ids_and_branch_fast_forwards(fake: FakeCanvas, s: Seed, repo: Path) -> None:
    snap = fake.snapshot_submissions()
    write(repo, "pages/zeta.md", "---\ntitle: Zeta Page\n---\nSee [alpha](alpha.md).\n")
    write(repo, "pages/alpha.md", "---\ntitle: Alpha Page\n---\nSee [zeta](zeta.md).\n")
    write(
        repo,
        "assignments/hw3.md",
        "---\ntitle: Homework 3\npoints: 3\ngroup: Homework\n---\nDo [alpha](../pages/alpha.md).\n",
    )
    mods = (repo / "modules.yaml").read_text()
    write(
        repo,
        "modules.yaml",
        mods + "- name: Week 2\n  items:\n  - page: alpha\n  - assignment: hw3\n",
    )
    commit(repo)
    fake.log.clear()
    result, out = push(fake, repo)
    assert result.ok and result.fast_forwarded, out
    assert gitops.is_clean(repo)
    assert_synced(repo)
    # ids arrived on the branch; files stayed where the user put them
    assert "canvas_id: alpha-page" in (repo / "pages/alpha.md").read_text()
    assert "canvas_id: zeta-page" in (repo / "pages/zeta.md").read_text()
    assert "canvas_id:" in (repo / "assignments/hw3.md").read_text()
    week2 = (repo / "modules.yaml").read_text().split("name: Week 2")[1]
    assert week2.count("canvas_id") == 2  # both items
    # the forward link (alpha -> zeta, created later) was resolved by the second pass
    pages = {p["url"]: p for p in fake.state("pages")["pages"].values()}
    assert "/courses/101/pages/zeta-page" in pages["alpha-page"]["body"]
    # a fetch now sees nothing new; a second push is a no-op
    assert run_fetch(repo, fake.client("read"), load_config(repo)).commit is None
    fake.log.clear()
    again, _ = push(fake, repo)
    assert again.ops_total == 0 and writes(fake) == []
    fake.assert_submissions_intact(snap)


def test_page_rename_writes_new_slug_back(fake: FakeCanvas, s: Seed, repo: Path) -> None:
    edit(repo, "pages/notes.md", "title: Notes", "title: Class Notes")
    commit(repo)
    result, out = push(fake, repo)
    assert result.ok, out
    assert "canvas_id: class-notes" in (repo / "pages/notes.md").read_text()
    assert gitops.is_clean(repo)


def test_moved_page_keeps_working(fake: FakeCanvas, s: Seed, repo: Path) -> None:
    git(repo, "mv", "pages/notes.md", "pages/class-notes.md")
    commit(repo)
    fake.log.clear()
    result, _ = push(fake, repo)
    assert result.ops_total == 0 and writes(fake) == []
    edit(repo, "pages/class-notes.md", "Plain notes.", "New text.")
    commit(repo)
    result, out = push(fake, repo)
    assert result.ok and result.fast_forwarded, out
    assert run_fetch(repo, fake.client("read"), load_config(repo)).commit is None


# ---- partial failure ----------------------------------------------------------------------


def test_partial_failure_then_pull_and_push_completes(
    fake: FakeCanvas, s: Seed, repo: Path
) -> None:
    snap = fake.snapshot_submissions()
    write(repo, "pages/a-first.md", "---\ntitle: A First\n---\nOne\n")
    write(repo, "pages/b-second.md", "---\ntitle: B Second\n---\nTwo\n")
    commit(repo)
    head = git(repo, "rev-parse", "HEAD")
    state = {"n": 0}
    real = fake.handle_request

    def flaky(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/pages"):
            state["n"] += 1
            if state["n"] == 2:
                return httpx.Response(500, json={"errors": "boom"})
        return real(request)

    fake.handle_request = flaky  # type: ignore[method-assign]
    result, out = push(fake, repo)
    fake.handle_request = real  # type: ignore[method-assign]
    assert not result.ok and result.ops_done == 1 and result.ops_total == 2
    assert "push stopped at pages/b-second.md" in out
    assert "1 of 2 operations were applied" in out and "canvas pull" in out
    assert git(repo, "rev-parse", "HEAD") == head  # branch untouched
    # pull = fetch + merge; the created page conflicts add/add (it differs only by canvas_id)
    assert run_fetch(repo, fake.client("read"), load_config(repo)).commit
    git(repo, "merge", "-X", "theirs", "-q", "--no-edit", "canvas/main", check=False)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "merge", check=False)
    again, out2 = push(fake, repo)
    assert again.ok, out2
    titles = sorted(p["title"] for p in fake.state("pages")["pages"].values())
    assert titles.count("A First") == 1 and titles.count("B Second") == 1
    assert_synced(repo)
    fake.assert_submissions_intact(snap)
