"""remote/files.py reader against FakeCanvas."""

from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import pytest

from canvas_sync.config import CourseConfig
from canvas_sync.dates import to_canvas
from canvas_sync.errors import CanvasSyncError
from canvas_sync.ignore import IgnoreRules
from canvas_sync.model.course import Course
from canvas_sync.model.files import FileEntry, FileRule, FileSettings, FilesMeta
from canvas_sync.model.roster import Roster, RosterCache
from canvas_sync.remote import files as remote_files
from canvas_sync.remote.context import FetchContext
from canvas_sync.render.bodies import Bodies
from canvas_sync.snapshot import BlobStore
from tests.fake_canvas.core import FakeCanvas
from tests.fake_canvas.files import replace_file


def make_cfg(files_root: str = "/") -> CourseConfig:
    return CourseConfig(
        canvas_url="https://canvas.test",  # pyright: ignore[reportArgumentType]
        course_id=101,
        timezone="America/Vancouver",
        files_root=files_root,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    return tmp_path


def ctx_for(
    repo: Path,
    *,
    previous: FilesMeta | None = None,
    ignore: list[str] | None = None,
    files_root: str = "/",
) -> FetchContext:
    cfg = make_cfg(files_root)
    prev = (
        Course(config=cfg, objects={}, files_meta=previous, modules=[], roster=Roster())
        if previous is not None
        else None
    )
    return FetchContext(
        config=cfg,
        tz=cfg.tz,
        bodies=cast(Bodies, None),
        previous=prev,
        roster=RosterCache(),
        ignore=IgnoreRules(lines=ignore or []),
        blob_store=BlobStore(repo),
    )


def downloads(fake: FakeCanvas) -> list[str]:
    return [r.path for r in fake.log if r.path.endswith("/download")]


@pytest.fixture
def fake() -> FakeCanvas:
    f = FakeCanvas()
    lectures = f.add_folder("lectures", parent_folder_id=f.root_folder_id)
    f.add_file(f.root_folder_id, "syllabus.pdf", b"syl")
    f.add_file(lectures["id"], "01.pdf", b"one")
    return f


def pin_stable(meta: FilesMeta, path: str) -> FilesMeta:
    entry = meta.files[path].model_copy(update={"stable_link": True})
    return FilesMeta(files={**meta.files, path: entry})


def test_maps_folders_to_tree_paths_and_stores_blobs(fake: FakeCanvas, repo: Path) -> None:
    ctx = ctx_for(repo)
    out = remote_files.read(fake.client("read"), ctx)
    assert sorted(out) == ["files/lectures/01.pdf", "files/syllabus.pdf"]
    e = out["files/lectures/01.pdf"]
    assert isinstance(e, FileEntry)
    assert e.path == "lectures/01.pdf"
    assert ctx.blob_store.get(e.sha256) == b"one"
    assert not fake.writes()


def test_files_root_subfolder(fake: FakeCanvas, repo: Path) -> None:
    out = remote_files.read(fake.client("read"), ctx_for(repo, files_root="/lectures"))
    assert sorted(out) == ["files/01.pdf"]


def test_missing_files_root_is_empty(fake: FakeCanvas, repo: Path) -> None:
    assert remote_files.read(fake.client("read"), ctx_for(repo, files_root="/nope")) == {}


def test_second_fetch_downloads_nothing(fake: FakeCanvas, repo: Path) -> None:
    first = remote_files.read_files_meta(fake.client("read"), ctx_for(repo))
    assert len(downloads(fake)) == 2
    fake.log.clear()
    second = remote_files.read_files_meta(fake.client("read"), ctx_for(repo, previous=first))
    assert downloads(fake) == []
    assert second == first


def test_changed_file_is_downloaded_again(fake: FakeCanvas, repo: Path) -> None:
    first = remote_files.read_files_meta(fake.client("read"), ctx_for(repo))
    fid = int(first.files["files/syllabus.pdf"].canvas_id or 0)
    fake.files[fid]["updated_at"] = "2026-02-02T00:00:00Z"
    fake.file_content[fid] = b"syl2"
    fake.log.clear()
    second = remote_files.read_files_meta(fake.client("read"), ctx_for(repo, previous=first))
    assert len(downloads(fake)) == 1
    assert second.files["files/syllabus.pdf"].sha256 != first.files["files/syllabus.pdf"].sha256


def test_size_change_downloads(fake: FakeCanvas, repo: Path) -> None:
    first = remote_files.read_files_meta(fake.client("read"), ctx_for(repo))
    fid = int(first.files["files/syllabus.pdf"].canvas_id or 0)
    fake.files[fid]["size"] = 99
    fake.log.clear()
    remote_files.read_files_meta(fake.client("read"), ctx_for(repo, previous=first))
    assert len(downloads(fake)) == 1


def test_missing_cache_downloads(fake: FakeCanvas, repo: Path) -> None:
    first = remote_files.read_files_meta(fake.client("read"), ctx_for(repo))
    (repo / ".git" / "canvas-blobs" / remote_files.INDEX_NAME).unlink()
    fake.log.clear()
    remote_files.read_files_meta(fake.client("read"), ctx_for(repo, previous=first))
    assert len(downloads(fake)) == 2


def test_replacement_chain_matches_previous_and_keeps_stable_link(
    fake: FakeCanvas, repo: Path
) -> None:
    first = remote_files.read_files_meta(fake.client("read"), ctx_for(repo))
    old = first.files["files/syllabus.pdf"]
    pinned = pin_stable(first, "files/syllabus.pdf")
    new = replace_file(fake, int(old.canvas_id or 0), b"syl v2")
    fake.log.clear()
    out = remote_files.read_files_meta(fake.client("read"), ctx_for(repo, previous=pinned))
    e = out.files["files/syllabus.pdf"]
    assert e.canvas_id == new["id"]
    assert e.stable_link is True
    assert any(
        r.path == f"/files/{old.canvas_id}" and "replacement_chain_context_id" in r.params
        for r in fake.log
    )
    assert len(downloads(fake)) == 1  # only the replaced file


def test_chain_not_followed_still_matches_by_path(fake: FakeCanvas, repo: Path) -> None:
    fake.state("files")["follow_chain"] = False
    first = remote_files.read_files_meta(fake.client("read"), ctx_for(repo))
    old = first.files["files/syllabus.pdf"]
    pinned = pin_stable(first, "files/syllabus.pdf")
    replace_file(fake, int(old.canvas_id or 0), b"syl v2")
    out = remote_files.read_files_meta(fake.client("read"), ctx_for(repo, previous=pinned))
    assert out.files["files/syllabus.pdf"].stable_link is True


def test_renamed_and_replaced_file_matched_via_chain(fake: FakeCanvas, repo: Path) -> None:
    first = remote_files.read_files_meta(fake.client("read"), ctx_for(repo))
    old = first.files["files/syllabus.pdf"]
    pinned = pin_stable(first, "files/syllabus.pdf")
    new = replace_file(fake, int(old.canvas_id or 0), b"v2")
    fake.files[new["id"]]["display_name"] = "syllabus-final.pdf"
    out = remote_files.read_files_meta(fake.client("read"), ctx_for(repo, previous=pinned))
    assert out.files["files/syllabus-final.pdf"].stable_link is True
    assert "files/syllabus.pdf" not in out.files


def only_entry(fake: FakeCanvas, repo: Path, previous: FilesMeta | None = None) -> FileEntry:
    out = remote_files.read_files_meta(fake.client("read"), ctx_for(repo, previous=previous))
    return out.files["files/x.pdf"]


def fake_with_x(**fields: Any) -> FakeCanvas:
    fake = FakeCanvas()
    fake.add_file(fake.root_folder_id, "x.pdf", b"x", **fields)
    return fake


def test_settings_read(repo: Path) -> None:
    fake = fake_with_x(
        hidden=True,
        locked=True,
        unlock_at="2026-09-19T16:00:00Z",
        lock_at="2026-12-21T07:59:00Z",
    )
    e = only_entry(fake, repo)
    assert e.published is False
    assert e.hidden is True
    assert to_canvas(cast(datetime, e.unlock)) == "2026-09-19T16:00:00Z"
    assert to_canvas(cast(datetime, e.lock)) == "2026-12-21T07:59:00Z"
    assert cast(datetime, e.lock).tzinfo == make_cfg().tz


def test_defaults_give_no_entry_settings(repo: Path) -> None:
    e = only_entry(fake_with_x(), repo)
    assert (e.published, e.hidden, e.unlock, e.lock) == (None, None, None, None)


def test_settings_matching_rules_are_not_written(repo: Path) -> None:
    fake = fake_with_x(hidden=True, lock_at="2026-12-21T07:59:00Z")
    first = only_entry(fake, repo)
    rule = FileRule(match="*.pdf", settings=FileSettings(hidden=True, lock=first.lock))
    e = only_entry(fake, repo, FilesMeta(rules=[rule]))
    assert (e.published, e.hidden, e.unlock, e.lock) == (None, None, None, None)


def test_no_lock_over_rule_lock_is_false(repo: Path) -> None:
    lock = datetime(2026, 12, 20, 23, 59, tzinfo=make_cfg().tz)
    rule = FileRule(match="*.pdf", settings=FileSettings(hidden=True, lock=lock))
    e = only_entry(fake_with_x(), repo, FilesMeta(rules=[rule]))
    assert e.lock is False
    assert e.hidden is False  # rule says hidden, Canvas says visible


def test_rules_are_carried_into_meta(repo: Path) -> None:
    rule = FileRule(match="*.pdf", settings=FileSettings(hidden=True))
    meta = FilesMeta(defaults=FileSettings(published=True), rules=[rule])
    out = remote_files.read_files_meta(fake_with_x().client("read"), ctx_for(repo, previous=meta))
    assert out.rules == meta.rules
    assert out.defaults == meta.defaults


def test_canvasignore(fake: FakeCanvas, repo: Path) -> None:
    out = remote_files.read(fake.client("read"), ctx_for(repo, ignore=["lectures/"]))
    assert sorted(out) == ["files/syllabus.pdf"]
    assert len(downloads(fake)) == 1


def test_user_folder_ignored(repo: Path) -> None:
    fake = FakeCanvas()
    other = fake.add_folder("submissions", context_type="User", context_id=7)
    fake.add_file(other["id"], "secret.pdf", b"s")
    assert remote_files.read(fake.client("read"), ctx_for(repo)) == {}


def test_pagination(repo: Path) -> None:
    fake = FakeCanvas(max_per_page=2)
    for i in range(5):
        fake.add_file(fake.root_folder_id, f"f{i}.txt", b"%d" % i)
    assert len(remote_files.read(fake.client("read"), ctx_for(repo))) == 5


def test_case_collision_raises(repo: Path) -> None:
    fake = FakeCanvas()
    fake.add_file(fake.root_folder_id, "A.pdf", b"1")
    fake.add_file(fake.root_folder_id, "a.pdf", b"2")
    with pytest.raises(CanvasSyncError, match="collides"):
        remote_files.read(fake.client("read"), ctx_for(repo))
