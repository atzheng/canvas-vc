"""pull after push keeps the local-only files push carried (bead 9te)."""
# ruff: noqa: F811  (fixtures are imported)

from __future__ import annotations

from pathlib import Path

import pytest

from canvas_sync import gitops
from tests.fake_canvas.core import FakeCanvas
from tests.test_pull_status_diff import fetch, pull, set_body
from tests.test_push import Seed, commit, edit, fake, git, push, repo, s  # noqa: F401


def pushed_repo(fake: FakeCanvas, repo: Path) -> None:
    (repo / "README.md").write_text("my notes\n")
    edit(repo, "pages/notes.md", "Plain notes.", "Edited notes.")
    commit(repo)
    result, out = push(fake, repo)
    assert result.ok and result.fast_forwarded, out
    # the tip is a merge commit carrying the local-only files
    assert len(git(repo, "rev-list", "--parents", "-1", "HEAD").split()) == 3


def tracked(repo: Path) -> set[str]:
    return set(git(repo, "ls-files").split())


@pytest.mark.parametrize("rebase", [False, True])
def test_pull_keeps_carried_files(fake: FakeCanvas, s: Seed, repo: Path, rebase: bool) -> None:
    pushed_repo(fake, repo)
    set_body(fake, "Syllabus", "<p>Remote edit.</p>")
    fetch(fake, repo)
    ok, out = pull(repo, rebase)
    assert ok, out
    assert {".gitignore", "README.md"} <= tracked(repo)
    assert "Remote edit." in (repo / "pages/syllabus.md").read_text()
    assert gitops.is_clean(repo)


def test_rebase_remote_deletion_stays_deleted(fake: FakeCanvas, s: Seed, repo: Path) -> None:
    pushed_repo(fake, repo)
    pages = fake.state("pages")["pages"]
    for pid in [k for k, v in pages.items() if v["title"] == "Notes"]:
        del pages[pid]
    fetch(fake, repo)
    ok, out = pull(repo, rebase=True)
    assert ok, out
    assert "pages/notes.md" not in tracked(repo)
    assert {".gitignore", "README.md"} <= tracked(repo)


@pytest.mark.parametrize("rebase", [False, True])
def test_dirty_tree_refuses_unchanged(fake: FakeCanvas, s: Seed, repo: Path, rebase: bool) -> None:
    pushed_repo(fake, repo)
    set_body(fake, "Syllabus", "<p>Remote edit.</p>")
    fetch(fake, repo)
    (repo / "pages/syllabus.md").write_text("dirty\n")
    head = git(repo, "rev-parse", "HEAD")
    ok, _ = pull(repo, rebase)
    assert not ok
    assert git(repo, "rev-parse", "HEAD") == head
    assert (repo / "pages/syllabus.md").read_text() == "dirty\n"


def test_pull_nothing_changed(fake: FakeCanvas, s: Seed, repo: Path) -> None:
    pushed_repo(fake, repo)
    fetch(fake, repo)
    ok, out = pull(repo)
    assert ok and "already up to date" in out
