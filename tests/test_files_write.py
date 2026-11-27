"""remote/files.py writer, plan + apply and push end to end over FakeCanvas (bead orx.5)."""

from __future__ import annotations

import io
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from canvas_sync.apply import apply
from canvas_sync.commands.clone import clone_course
from canvas_sync.commands.fetch import run_fetch
from canvas_sync.commands.push import PushResult, run_push
from canvas_sync.config import CourseConfig, load_config
from canvas_sync.errors import CanvasSyncError, StaleRemote
from canvas_sync.model.base import Kind, ObjectKey
from canvas_sync.model.files import FileEntry
from canvas_sync.plan.core import Action, Operation, Plan
from canvas_sync.remote import files as remote_files
from canvas_sync.remote.context import ApplyContext
from canvas_sync.remote.guard import SubmissionGuard
from canvas_sync.render.bodies import Bodies
from canvas_sync.snapshot import BlobStore
from tests.fake_canvas.core import FakeCanvas
from tests.fake_canvas.files import replace_file
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


def git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, **_IDENT},
    )
    return proc.stdout


def commit(root: Path, msg: str = "edit") -> None:
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", msg)


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))


class Seed:
    slides: dict[str, Any]
    notes: dict[str, Any]
    lectures: dict[str, Any]
    page: dict[str, Any]
    item: dict[str, Any]


@pytest.fixture
def fake() -> FakeCanvas:
    return FakeCanvas()


@pytest.fixture
def s(fake: FakeCanvas) -> Seed:
    s = Seed()
    s.lectures = fake.add_folder("lectures", parent_folder_id=fake.root_folder_id)
    s.slides = fake.add_file(s.lectures["id"], "01.pdf", b"slides v1")
    s.notes = fake.add_file(fake.root_folder_id, "notes.txt", b"notes")
    s.page = add_page(
        fake,
        "Reading",
        f'<p>See <a href="/courses/101/files/{s.slides["id"]}?wrap=1">the slides</a>.</p>',
    )
    mod = add_module(fake, "Week 1")
    s.item = add_item(fake, mod["id"], "File", title="Slides", content_id=s.slides["id"], indent=0)
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
    result = run_push(root, fake.client("write"), load_config(root), console=con, **kw)
    return result, buf.getvalue()


def writes(fake: FakeCanvas) -> list[str]:
    return [f"{r.method} {r.path}" for r in fake.log if r.method != "GET"]


def uploads(fake: FakeCanvas) -> list[str]:
    return [w for w in writes(fake) if w == "POST /courses/101/files"]


def by_name(fake: FakeCanvas, name: str) -> dict[str, Any]:
    (f,) = [f for f in fake.files.values() if f["display_name"] == name]
    return f


def tree_diff_empty(root: Path) -> bool:
    """canvas/main and HEAD differ only in the .gitignore that clone adds."""
    names = git(root, "diff", "--name-only", "canvas/main", "HEAD").split()
    assert names == [".gitignore"], git(root, "diff", "canvas/main", "HEAD")
    return True


# ---- end to end through push ------------------------------------------------------------


def test_settings_only_change_keeps_id(fake: FakeCanvas, s: Seed, repo: Path) -> None:
    meta = repo / "files/_meta.yaml"
    meta.write_text("rules:\n  - match: notes.txt\n    hidden: true\n" + meta.read_text())
    commit(repo)
    result, out = push(fake, repo)
    assert result.ok and result.fast_forwarded, out
    assert by_name(fake, "notes.txt")["id"] == s.notes["id"]  # same id
    assert by_name(fake, "notes.txt")["hidden"] is True
    assert writes(fake) == [f"PUT /files/{s.notes['id']}"]
    assert tree_diff_empty(repo)
    fake.log.clear()
    again, _ = push(fake, repo)
    assert again.ops_total == 0 and writes(fake) == []


def test_content_change_end_to_end(fake: FakeCanvas, s: Seed, repo: Path) -> None:
    old_id = s.slides["id"]
    (repo / "files/lectures/01.pdf").write_bytes(b"slides v2")
    commit(repo)
    fake.log.clear()
    result, out = push(fake, repo)
    assert result.ok and result.fast_forwarded, out
    assert "new Canvas file id" in out  # FILE_ID_CHANGE shown in the plan
    # one upload, no blind settings PUT (hidden/locked carried over)
    assert writes(fake) == ["POST /courses/101/files", "POST /fake-upload/up1"]
    new = by_name(fake, "01.pdf")
    assert new["id"] != old_id and fake.file_content[new["id"]] == b"slides v2"
    # Canvas repointed the module item and the page link; we sent no write for them
    assert s.item["content_id"] == new["id"]
    body = fake.state("pages")["pages"][s.page["page_id"]]["body"]
    assert f"/files/{new['id']}?wrap=1&verifier=" in body and f"/files/{old_id}" not in body
    # push then fetch is a no-op: nothing differs, the page Markdown has no id or verifier
    assert tree_diff_empty(repo)
    assert (repo / "pages/reading.md").read_text().count("verifier") == 0
    assert f"canvas_id: {new['id']}" in (repo / "files/_meta.yaml").read_text()
    fake.log.clear()
    again, _ = push(fake, repo)
    assert again.ops_total == 0 and writes(fake) == []
    fetched = run_fetch(repo, fake.client("read"), load_config(repo))
    assert fetched.commit is None


def test_second_replacement_is_still_a_noop_after_fetch(
    fake: FakeCanvas, s: Seed, repo: Path
) -> None:
    """Replacing on Canvas (web UI) rewrites page links; the next fetch keeps the Markdown."""
    replace_file(fake, s.slides["id"], b"slides v3")
    fetched = run_fetch(repo, fake.client("read"), load_config(repo))
    assert fetched.commit  # the new file content and id are a real change
    changed = git(repo, "diff", "--name-only", "HEAD", "canvas/main").split()
    assert "pages/reading.md" not in changed
    assert "modules.yaml" not in changed


def test_content_change_with_settings_change_reapplies_settings_once(
    fake: FakeCanvas, s: Seed, repo: Path
) -> None:
    (repo / "files/lectures/01.pdf").write_bytes(b"slides v2")
    meta = repo / "files/_meta.yaml"
    meta.write_text("rules:\n  - match: 01.pdf\n    hidden: true\n" + meta.read_text())
    commit(repo)
    fake.log.clear()
    result, out = push(fake, repo)
    assert result.ok, out
    new = by_name(fake, "01.pdf")
    assert new["hidden"] is True
    puts = [w for w in writes(fake) if w.startswith("PUT")]
    assert puts == [f"PUT /files/{new['id']}"]  # on the new file, not the old one


def test_settings_reapplied_when_canvas_drops_them(fake: FakeCanvas, s: Seed, repo: Path) -> None:
    s.slides["hidden"] = True
    run_fetch(repo, fake.client("read"), load_config(repo))
    git(repo, "merge", "-q", "canvas/main")
    fake.state("files")["carry_settings"] = False
    (repo / "files/lectures/01.pdf").write_bytes(b"slides v2")
    commit(repo)
    fake.log.clear()
    result, out = push(fake, repo)
    assert result.ok, out
    new = by_name(fake, "01.pdf")
    assert new["hidden"] is True
    assert [w for w in writes(fake) if w.startswith("PUT")] == [f"PUT /files/{new['id']}"]


def test_new_file_in_new_folder(fake: FakeCanvas, s: Seed, repo: Path) -> None:
    (repo / "files/labs").mkdir()
    (repo / "files/labs/a.txt").write_bytes(b"lab a")
    commit(repo)
    fake.log.clear()
    result, out = push(fake, repo)
    assert result.ok, out
    f = by_name(fake, "a.txt")
    assert fake.folders[f["folder_id"]]["full_name"] == "course files/labs"
    assert fake.file_content[f["id"]] == b"lab a"
    assert f"canvas_id: {f['id']}" in (repo / "files/_meta.yaml").read_text()
    assert tree_diff_empty(repo)


def test_delete_removes_file_and_module_item(fake: FakeCanvas, s: Seed, repo: Path) -> None:
    git(repo, "rm", "-q", "files/notes.txt")
    meta = repo / "files/_meta.yaml"
    meta.write_text(re.sub(r"  notes\.txt:\n(    .*\n)+", "", meta.read_text()))
    commit(repo)
    fake.log.clear()
    result, out = push(fake, repo)
    assert result.ok, out
    assert writes(fake) == [f"DELETE /files/{s.notes['id']}"]
    assert s.notes["id"] not in fake.files and s.slides["id"] in fake.files


# ---- writer safety, direct --------------------------------------------------------------


def ctx_for(fake: FakeCanvas, root: Path) -> ApplyContext:
    cfg = CourseConfig(
        canvas_url=URL,  # pyright: ignore[reportArgumentType]
        course_id=101,
        timezone=TZ,
    )
    return ApplyContext(
        config=cfg,
        tz=cfg.tz,
        roster=load_roster_cache(),
        ids={},
        guard=SubmissionGuard(fake.client("write"), cfg),
        blobs=BlobStore(root),
        bodies=Bodies(),
    )


def load_roster_cache() -> Any:
    from canvas_sync.model.roster import RosterCache

    return RosterCache()


def entry(path: str, cid: int | None, sha: str = "") -> FileEntry:
    return FileEntry(canvas_id=cid, path=path, sha256=sha)


def test_stale_id_is_refused_for_put_and_delete(fake: FakeCanvas, s: Seed, repo: Path) -> None:
    old = s.slides["id"]
    replace_file(fake, old, b"someone else's newer upload")  # Canvas now answers GET old -> new
    fake.log.clear()
    ctx = ctx_for(fake, repo)
    stale = entry("lectures/01.pdf", old)
    client = fake.client("write")
    with pytest.raises(StaleRemote, match="was replaced on Canvas"):
        remote_files.update(client, stale, stale.model_copy(update={"hidden": True}), ctx)
    with pytest.raises(StaleRemote, match="was replaced on Canvas"):
        remote_files.delete(client, stale, ctx)
    assert writes(fake) == []
    assert by_name(fake, "01.pdf")["hidden"] is False


def test_file_outside_the_course_is_not_touched(fake: FakeCanvas, s: Seed, repo: Path) -> None:
    """A student's submission attachment lives in a user folder: no write, whatever the id."""
    folder = fake.add_folder("submissions", context_type="User", context_id=7001)
    att = fake.add_file(folder["id"], "hw1.pdf", b"student work")
    before = dict(att)
    ctx = ctx_for(fake, repo)
    victim = entry("hw1.pdf", att["id"])
    client = fake.client("write")
    with pytest.raises(CanvasSyncError, match="not in a folder of this course"):
        remote_files.delete(client, victim, ctx)
    with pytest.raises(CanvasSyncError, match="not in a folder of this course"):
        remote_files.update(client, victim, victim.model_copy(update={"hidden": True}), ctx)
    with pytest.raises(CanvasSyncError):
        remote_files.preflight(client, victim, victim, ctx)
    assert writes(fake) == []
    assert fake.files[att["id"]] == before and fake.file_content[att["id"]] == b"student work"


def test_renamed_or_moved_file_is_not_touched(fake: FakeCanvas, s: Seed, repo: Path) -> None:
    ctx = ctx_for(fake, repo)
    wrong = entry("other/01.pdf", s.slides["id"])  # tree says another folder
    with pytest.raises(StaleRemote, match="moved or renamed"):
        remote_files.delete(fake.client("write"), wrong, ctx)
    assert writes(fake) == []


def test_create_refuses_to_overwrite_unknown_file(fake: FakeCanvas, s: Seed, repo: Path) -> None:
    ctx = ctx_for(fake, repo)
    (repo / "files").mkdir(exist_ok=True)
    (repo / "files/notes.txt").write_bytes(b"mine")
    sha = ctx.blobs.put(b"mine")
    new = entry("notes.txt", None, sha)  # a same-name file already exists on Canvas
    with pytest.raises(StaleRemote, match="already exists on Canvas"):
        remote_files.create(fake.client("write"), new, ctx)
    assert writes(fake) == []
    assert fake.file_content[s.notes["id"]] == b"notes"


def test_missing_content_is_refused_before_any_write(fake: FakeCanvas, s: Seed, repo: Path) -> None:
    ctx = ctx_for(fake, repo)
    old = entry("lectures/01.pdf", s.slides["id"], "a" * 64)
    new = old.model_copy(update={"sha256": "b" * 64})
    with pytest.raises(CanvasSyncError, match="content for bbbbbbbb is not available"):
        remote_files.preflight(fake.client("write"), old, new, ctx)
    assert writes(fake) == []


def test_apply_records_new_id_for_replacement(fake: FakeCanvas, s: Seed, repo: Path) -> None:
    ctx = ctx_for(fake, repo)
    sha = ctx.blobs.put(b"slides v2")
    key = ObjectKey(Kind.FILE, "files/lectures/01.pdf")
    ctx.ids[key] = s.slides["id"]
    before = entry("lectures/01.pdf", s.slides["id"], "a" * 64)
    after = entry("lectures/01.pdf", s.slides["id"], sha)
    plan = Plan([Operation(key, Action.UPLOAD, before, after, {})])
    result = apply(plan, fake.client("write"), ctx, confirmed=set())
    assert result.error is None
    new = by_name(fake, "01.pdf")
    assert result.new_ids == {key: new["id"]} and new["id"] != s.slides["id"]


def test_verifier_query_is_not_kept_in_markdown_links() -> None:
    from canvas_sync.render.links import LinkContext, from_canvas_url

    ctx = LinkContext(
        course_id=101,
        from_path="pages/a.md",
        ids=lambda k: None,
        paths=lambda kind, cid: "files/x.pdf" if (kind, cid) == (Kind.FILE, 7) else None,
    )
    assert from_canvas_url("/courses/101/files/7?wrap=1&verifier=abc", ctx) == "../files/x.pdf"
    assert from_canvas_url("/courses/101/files/7/download?verifier=abc&x=1", ctx) == (
        "../files/x.pdf?x=1"
    )
