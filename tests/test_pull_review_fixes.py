"""Review fixes for canvas pull/status/diff (bead vtr.1)."""
# ruff: noqa: F811  (fixtures are imported from test_pull_status_diff)

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from canvas_sync import gitops
from canvas_sync.cli import app
from canvas_sync.commands import status as status_cmd
from tests.fake_canvas.core import FakeCanvas
from tests.test_pull_status_diff import (  # noqa: F401  (fixtures)
    SYLLABUS,
    _files_differ_stub,
    commit,
    fake,
    fetch,
    git,
    pull,
    repo,
    set_body,
)


def make_conflict(fake: FakeCanvas, repo: Path, rebase: bool = False) -> None:
    text = (repo / SYLLABUS).read_text()
    (repo / SYLLABUS).write_text(text.replace("line two", "LOCAL"))
    commit(repo)
    set_body(fake, "Syllabus", "<p>line one</p>\n<p>REMOTE</p>\n<p>line three</p>")
    fetch(fake, repo)
    assert not pull(repo, rebase)[0]


def test_pull_refuses_while_merge_unfinished(fake: FakeCanvas, repo: Path) -> None:
    make_conflict(fake, repo)
    ok, out = pull(repo)
    assert not ok and "in progress" in out and len(out.strip().splitlines()) == 1


def test_pull_refuses_while_rebase_unfinished(fake: FakeCanvas, repo: Path) -> None:
    make_conflict(fake, repo, rebase=True)
    assert gitops.operation_in_progress(repo) == "rebase"
    ok, out = pull(repo)
    assert not ok and "rebase in progress" in out


def test_dirty_refusal_lists_paths(fake: FakeCanvas, repo: Path) -> None:
    (repo / SYLLABUS).write_text("dirty\n")
    set_body(fake, "Syllabus", "<p>remote change</p>")
    fetch(fake, repo)
    _, out = pull(repo)
    assert SYLLABUS in out and len(out.strip().splitlines()) == 1


def test_status_conflict_once_and_plan_error_is_one_line(
    fake: FakeCanvas, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_conflict(fake, repo)
    lines = status_cmd.local_lines(repo)
    assert [ln for ln in lines if SYLLABUS in ln] == [f"unmerged: {SYLLABUS}"]

    def boom(base: object, target: object) -> list[object]:
        raise NotImplementedError

    from canvas_sync.plan import files

    monkeypatch.setattr(files, "diff_type", boom)
    monkeypatch.chdir(repo)
    monkeypatch.setattr(status_cmd, "_open_client", lambda cfg: fake.client("read"))
    res = CliRunner().invoke(app, ["status"])
    assert res.exit_code == 0 and f"unmerged: {SYLLABUS}" in res.output
    assert "to push: cannot compute plan:" in res.output
    assert "remote:" in res.output
    res = CliRunner().invoke(app, ["diff"])
    assert res.exit_code == 1 and res.output.startswith("error:")


def test_dirty_note_in_diff_and_status(
    fake: FakeCanvas, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo)
    monkeypatch.setattr(status_cmd, "_open_client", lambda cfg: fake.client("read"))
    (repo / SYLLABUS).write_text("dirty\n")
    for cmd in ("diff", "status"):
        assert "committed HEAD" in CliRunner().invoke(app, [cmd]).output
    git(repo, "checkout", "--", SYLLABUS)
    assert "committed HEAD" not in CliRunner().invoke(app, ["diff"]).output
