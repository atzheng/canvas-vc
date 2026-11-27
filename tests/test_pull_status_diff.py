"""canvas pull / status / diff over FakeCanvas and throwaway repos (bead vtr.1)."""

from __future__ import annotations

import io
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console
from typer.testing import CliRunner

from canvas_sync import gitops
from canvas_sync.cli import app
from canvas_sync.commands import diff as diff_cmd
from canvas_sync.commands import status as status_cmd
from canvas_sync.commands.clone import clone_course
from canvas_sync.commands.fetch import run_fetch
from canvas_sync.commands.pull import merge_canvas
from canvas_sync.config import load_config
from canvas_sync.errors import CanvasSyncError
from tests.fake_canvas.assignments import add_assignment
from tests.fake_canvas.core import FakeCanvas
from tests.fake_canvas.pages import add_page

URL = "https://canvas.test"
IDENT = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}
SYLLABUS = "pages/syllabus.md"


def git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, **IDENT},
    )
    return proc.stdout


@pytest.fixture
def fake() -> FakeCanvas:
    f = FakeCanvas()
    add_page(f, "Syllabus", "<p>line one</p>\n<p>line two</p>\n<p>line three</p>")
    add_page(f, "Other", "<p>other</p>")
    add_assignment(f, "Homework 1", points_possible=10)
    return f


@pytest.fixture
def repo(fake: FakeCanvas, tmp_path: Path) -> Path:
    root = tmp_path / "course"
    clone_course(fake.client("read"), URL, 101, root, "America/Vancouver")
    git(root, "config", "commit.gpgsign", "false")
    git(root, "config", "core.hooksPath", "/dev/null")
    return root


def set_body(fake: FakeCanvas, title: str, html: str) -> None:
    store: dict[int, dict[str, Any]] = fake.state("pages")["pages"]
    for page in store.values():
        if page["title"] == title:
            page["body"] = html
            page["updated_at"] = "2099-01-01T00:00:00Z"


def fetch(fake: FakeCanvas, root: Path) -> None:
    run_fetch(root, fake.client("read"), load_config(root))


def pull(root: Path, rebase: bool = False) -> tuple[bool, str]:
    buf = io.StringIO()
    ok = merge_canvas(root, rebase, Console(file=buf, width=200))
    return ok, buf.getvalue()


def commit(root: Path, msg: str = "edit") -> None:
    git(root, "add", "-A")
    git(root, "commit", "-qm", msg)


def test_pull_clean_merge_of_non_conflicting_edits(fake: FakeCanvas, repo: Path) -> None:
    (repo / "pages/other.md").write_text((repo / "pages/other.md").read_text() + "\nlocal\n")
    commit(repo)
    set_body(fake, "Syllabus", "<p>line one</p>\n<p>line two</p>\n<p>REMOTE</p>")
    fetch(fake, repo)
    ok, out = pull(repo)
    assert ok and "merged canvas/main" in out
    assert "REMOTE" in (repo / SYLLABUS).read_text()
    assert "local" in (repo / "pages/other.md").read_text()
    assert gitops.is_ancestor(repo, gitops.REMOTE_REF, "HEAD")
    assert gitops.unmerged_paths(repo) == []


def test_pull_conflict_is_a_normal_git_conflict(fake: FakeCanvas, repo: Path) -> None:
    text = (repo / SYLLABUS).read_text()
    (repo / SYLLABUS).write_text(text.replace("line two", "LOCAL"))
    commit(repo)
    set_body(fake, "Syllabus", "<p>line one</p>\n<p>REMOTE</p>\n<p>line three</p>")
    fetch(fake, repo)
    ok, out = pull(repo)
    assert not ok
    assert f"CONFLICT: {SYLLABUS}" in out
    assert gitops.unmerged_paths(repo) == [SYLLABUS]
    body = (repo / SYLLABUS).read_text()
    assert "<<<<<<<" in body and "LOCAL" in body and "REMOTE" in body and ">>>>>>>" in body


def test_pull_when_up_to_date(repo: Path) -> None:
    ok, out = pull(repo)
    assert ok and "already up to date" in out


def test_pull_rebase(fake: FakeCanvas, repo: Path) -> None:
    (repo / "pages/other.md").write_text((repo / "pages/other.md").read_text() + "\nlocal\n")
    commit(repo)
    set_body(fake, "Syllabus", "<p>line one</p>\n<p>line two</p>\n<p>REMOTE</p>")
    fetch(fake, repo)
    ok, out = pull(repo, rebase=True)
    assert ok and "rebased onto canvas/main" in out
    assert git(repo, "rev-list", "--merges", "--count", "HEAD").strip() == "0"
    assert gitops.is_ancestor(repo, gitops.REMOTE_REF, "HEAD")


def test_pull_refuses_dirty_tree_in_one_line(fake: FakeCanvas, repo: Path) -> None:
    (repo / SYLLABUS).write_text("dirty\n")
    set_body(fake, "Syllabus", "<p>remote change</p>")
    fetch(fake, repo)
    before = gitops.ref_commit(repo)
    ok, out = pull(repo)
    assert not ok
    lines = out.strip().splitlines()
    assert len(lines) == 1 and lines[0].startswith("git refused:")
    assert (repo / SYLLABUS).read_text() == "dirty\n"
    assert gitops.ref_commit(repo) == before


def test_pull_cli_uses_read_client_only(
    fake: FakeCanvas, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from canvas_sync.commands import pull as pull_mod

    monkeypatch.setattr(pull_mod, "get_token", lambda cfg: "t")
    monkeypatch.setattr(
        pull_mod,
        "CanvasClient",
        lambda url, token, course_id, mode: fake.client(mode),
    )
    set_body(fake, "Syllabus", "<p>line one</p>\n<p>line two</p>\n<p>NEW</p>")
    monkeypatch.chdir(repo)
    res = CliRunner().invoke(app, ["pull"])
    assert res.exit_code == 0, res.output
    assert "NEW" in (repo / SYLLABUS).read_text()
    assert fake.writes() == []


# ---- diff ---------------------------------------------------------------------------


def test_diff_matches_core_plan_and_ignores_canvas_dir(repo: Path) -> None:
    assert diff_cmd.build_plan(repo).is_empty()
    (repo / SYLLABUS).write_text((repo / SYLLABUS).read_text() + "\nmore\n")
    (repo / ".canvas").mkdir(exist_ok=True)
    (repo / ".canvas/note.txt").write_text("x")
    commit(repo)
    plan = diff_cmd.build_plan(repo)
    assert [(o.action.value, o.key.path) for o in plan.ops] == [("update", SYLLABUS)]


def test_diff_ignores_uncommitted_edits(repo: Path) -> None:
    (repo / SYLLABUS).write_text("uncommitted\n")
    assert diff_cmd.build_plan(repo).is_empty()


def test_diff_cli_output_and_errors(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (repo / SYLLABUS).write_text((repo / SYLLABUS).read_text() + "\nmore\n")
    commit(repo)
    monkeypatch.chdir(repo)
    res = CliRunner().invoke(app, ["diff"])
    assert res.exit_code == 0 and SYLLABUS in res.output
    git(repo, "update-ref", "-d", gitops.REMOTE_REF)
    res = CliRunner().invoke(app, ["diff"])
    assert res.exit_code == 1 and "canvas fetch" in res.output
    with pytest.raises(CanvasSyncError):
        diff_cmd.build_plan(repo)


# ---- status -------------------------------------------------------------------------


def test_status_local_lines_offline(repo: Path) -> None:
    assert status_cmd.local_lines(repo) == []
    (repo / SYLLABUS).write_text((repo / SYLLABUS).read_text() + "\nmore\n")
    assert status_cmd.local_lines(repo) == [f"uncommitted: {SYLLABUS}"]
    commit(repo)
    assert status_cmd.local_lines(repo) == []
    assert status_cmd.unpushed_lines(repo) == [f"update {SYLLABUS}"]


def test_status_reports_canvas_ahead(fake: FakeCanvas, repo: Path) -> None:
    set_body(fake, "Other", "<p>changed</p>")
    fetch(fake, repo)
    assert any("not in HEAD; run `canvas pull`" in line for line in status_cmd.local_lines(repo))


def test_moved_kinds_pure() -> None:
    since = datetime(2026, 1, 1, tzinfo=UTC)
    listings: dict[str, list[dict[str, Any]]] = {
        "pages": [{"updated_at": "2025-12-31T00:00:00Z"}, {"updated_at": None}],
        "assignments": [{"updated_at": "2026-01-02T00:00:00Z"}],
        "announcements": [{"updated_at": "garbage"}, {}],
    }
    assert status_cmd.moved_kinds(listings, since) == ["assignments"]


def test_status_cli_remote_check_is_get_only(
    fake: FakeCanvas, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo)
    monkeypatch.setattr(status_cmd, "_open_client", lambda cfg: fake.client("read"))
    mark = len(fake.requests())
    set_body(fake, "Syllabus", "<p>changed</p>")  # edited after the last fetch
    res = CliRunner().invoke(app, ["status"])
    assert res.exit_code == 0, res.output
    assert "remote: Canvas changed since last fetch (pages)" in res.output
    assert fake.writes() == []
    sent = fake.requests()[mark:]
    assert len(sent) == 3 and all(r.method == "GET" for r in sent)


def test_status_cli_unchanged_then_offline(
    fake: FakeCanvas, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo)
    real_open_client = status_cmd._open_client
    monkeypatch.setattr(status_cmd, "_open_client", lambda cfg: fake.client("read"))
    res = CliRunner().invoke(app, ["status"])
    assert "remote: unchanged since last fetch" in res.output
    monkeypatch.setattr(status_cmd, "_open_client", real_open_client)
    monkeypatch.delenv(load_config(repo).token_env, raising=False)
    res = CliRunner().invoke(app, ["status"])
    assert res.exit_code == 0
    assert "remote: not checked" in res.output
