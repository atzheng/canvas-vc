"""Explorer UI: changes view (pending plan, history). Offline."""

from __future__ import annotations

from pathlib import Path

import pytest

from canvas_sync import gitops
from canvas_sync.commands.diff import build_plan
from canvas_sync.remote.http import CanvasClient
from canvas_sync.ui.server import dispatch
from tests.helpers.ui_repo import COURSE_YAML, git, make_repo

REMOTE = {
    "course.yaml": COURSE_YAML,
    "pages/a.md": "---\ntitle: A\npublished: true\n---\n\nhi\n",
}
HEAD = {
    **REMOTE,
    "pages/a.md": "---\ntitle: <b>A2</b>\npublished: true\n---\n\nhi\n",
    "pages/new.md": "---\ntitle: New\npublished: false\n---\n\nbody\n",
}


def html(root: Path) -> str:
    r = dispatch(root, "/changes")
    assert r.status == 200
    return r.body.decode()


def test_never_fetched(tmp_path: Path) -> None:
    out = html(make_repo(tmp_path, HEAD))
    assert "never fetched" in out and "canvas fetch" in out


def test_pending_matches_build_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: object, **k: object) -> None:
        raise AssertionError("CanvasClient constructed")

    monkeypatch.setattr(CanvasClient, "__init__", boom)
    root = make_repo(tmp_path, HEAD, remote_files=REMOTE)
    ops = build_plan(root).plan.ops
    assert len(ops) == 2
    out = html(root)
    assert f"{len(ops)} operations" in out
    for op in ops:
        assert f'href="/o/{op.key.path}"' in out
        assert op.action.value in out
    assert "&lt;b&gt;A2&lt;/b&gt;" in out and "<b>" not in out
    assert "Uncommitted" not in out


def test_in_sync(tmp_path: Path) -> None:
    root = make_repo(tmp_path, REMOTE, remote_files=REMOTE)
    out = html(root)
    assert "nothing to push" in out and "0 operations" in out


def test_uncommitted_note(tmp_path: Path) -> None:
    root = make_repo(tmp_path, HEAD, remote_files=REMOTE)
    (root / "pages/a.md").write_text("---\ntitle: edited\n---\n\nx\n")
    assert "Uncommitted: 1 changed file" in html(root)


def test_history(tmp_path: Path) -> None:
    root = make_repo(tmp_path, HEAD, remote_files=REMOTE)
    out = html(root)
    remote = gitops.ref_commit(root)
    head = gitops.head_commit(root)
    assert remote and head
    assert remote[:7] in out and "canvas fetch: test" in out
    assert head[:7] in out and "initial" in out  # unpushed
    assert "pages/new.md" in out and "course.yaml" in out
    log = gitops.log_commits(root, "HEAD", exclude=remote)
    assert [c.subject for c in log] == ["initial"]
    assert "pages/new.md" in log[0].paths


def test_plan_error_is_one_line(tmp_path: Path) -> None:
    root = make_repo(tmp_path, {"course.yaml": "not: [valid"}, remote_files=REMOTE)
    r = dispatch(root, "/changes")
    assert r.status == 200 and "cannot compute the plan" in r.body.decode()


def test_log_leaves_repo_untouched(tmp_path: Path) -> None:
    root = make_repo(tmp_path, HEAD, remote_files=REMOTE)
    before = (git(root, "status", "--porcelain"), git(root, "for-each-ref"))
    html(root)
    assert (git(root, "status", "--porcelain"), git(root, "for-each-ref")) == before
