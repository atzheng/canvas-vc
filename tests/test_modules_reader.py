"""remote/modules.py reader against FakeCanvas."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from canvas_sync.config import CourseConfig
from canvas_sync.ignore import IgnoreRules
from canvas_sync.model.base import CanvasObject, Kind, ObjectKey
from canvas_sync.model.course import Course
from canvas_sync.model.files import FileEntry, FilesMeta
from canvas_sync.model.modules import MODULE_SPEC
from canvas_sync.model.page import Page
from canvas_sync.model.roster import Roster, RosterCache
from canvas_sync.remote import modules as remote_modules
from canvas_sync.remote.context import FetchContext
from canvas_sync.render.bodies import Bodies
from canvas_sync.snapshot import BlobStore
from tests.fake_canvas.core import FakeCanvas
from tests.fake_canvas.modules import add_item, add_module


def make_cfg() -> CourseConfig:
    return CourseConfig(
        canvas_url="https://canvas.test",  # pyright: ignore[reportArgumentType]
        course_id=101,
        timezone="America/Vancouver",
    )


def ctx_for(tmp_path: Path, previous: Course | None = None) -> FetchContext:
    cfg = make_cfg()
    return FetchContext(
        config=cfg,
        tz=cfg.tz,
        bodies=cast(Bodies, None),
        previous=previous,
        roster=RosterCache(),
        ignore=IgnoreRules(lines=[]),
        blob_store=BlobStore(tmp_path),
    )


PATHS: dict[tuple[Kind, int | str], str] = {
    (Kind.PAGE, "syllabus"): "syllabus",
    (Kind.ASSIGNMENT, 77): "hw1",
    (Kind.FILE, 55): "lectures/01.pdf",
}


def seed(fake: FakeCanvas) -> tuple[dict[str, object], dict[str, object]]:
    m1 = add_module(fake, "Week 1", unlock_at="2026-09-08T16:00:00Z")
    m2 = add_module(
        fake,
        "Week 2",
        published=False,
        prerequisite_module_ids=[m1["id"]],
        require_sequential_progress=True,
    )
    add_item(fake, m1["id"], "Page", title="Syllabus", page_url="syllabus")
    add_item(fake, m1["id"], "SubHeader", title="Lecture")
    add_item(fake, m1["id"], "File", title="Slides", content_id=55, indent=1)
    add_item(fake, m1["id"], "Assignment", title="HW 1", content_id=77)
    add_item(
        fake,
        m1["id"],
        "ExternalUrl",
        title="sklearn",
        external_url="https://scikit-learn.org/",
        new_tab=True,
        indent=1,
    )
    tool = add_item(
        fake,
        m1["id"],
        "ExternalTool",
        title="Clicker",
        content_id=9,
        external_url="https://tool.example/launch",
        indent=2,
    )
    add_item(fake, m1["id"], "Quiz", title="Quiz 1", content_id=4321)
    add_item(fake, m1["id"], "Discussion", title="Talk", content_id=4322, published=False)
    quiz2 = add_item(fake, m2["id"], "Quiz", title="Quiz 2", content_id=4323)
    m2["completion_requirements"] = [
        {"id": quiz2["id"], "type": "min_score", "min_score": 80, "completed": True},
        {"id": tool["id"], "type": "must_view", "completed": False},
    ]
    return m1, m2


def test_reads_order_items_and_settings(tmp_path: Path) -> None:
    fake = FakeCanvas()
    m1, m2 = seed(fake)
    mods = remote_modules.read(fake.client("read"), ctx_for(tmp_path), paths=PATHS)
    assert [m.name for m in mods] == ["Week 1", "Week 2"]
    first, second = mods
    assert first.canvas_id == m1["id"]
    assert first.unlock is not None and first.unlock.hour == 9
    kinds = [it.item_type for it in first.items]
    assert kinds == [
        "page",
        "header",
        "file",
        "assignment",
        "url",
        "unmanaged",
        "quiz",
        "discussion",
    ]
    assert [it.indent for it in first.items] == [0, 0, 1, 0, 1, 2, 0, 0]
    assert first.items[0].page == "syllabus" and first.items[0].title is None
    assert first.items[1].header == "Lecture"
    assert first.items[2].file == "lectures/01.pdf" and first.items[2].title == "Slides"
    assert first.items[3].assignment == "hw1"
    assert first.items[4].url == "https://scikit-learn.org/" and first.items[4].new_tab
    tool = first.items[5]
    assert tool.unmanaged is not None
    assert (tool.unmanaged.type, tool.unmanaged.content_id) == ("ExternalTool", 9)
    assert tool.unmanaged.external_url == "https://tool.example/launch"
    assert tool.canvas_id is not None and tool.title == "Clicker"
    assert first.items[6].quiz is not None and first.items[6].quiz.canvas_id == 4321
    assert first.items[7].discussion is not None and first.items[7].published is False
    assert second.published is False
    assert second.prerequisites == [m1["id"]]
    assert second.require_sequential_progress is True
    reqs = second.completion_requirements
    assert [(r.item_id, r.type, r.min_score) for r in reqs] == [
        (second.items[0].canvas_id, "min_score", 80.0),
        (tool.canvas_id, "must_view", None),
    ]


def test_unknown_item_type_never_raises_or_reorders(tmp_path: Path) -> None:
    fake = FakeCanvas()
    m = add_module(fake, "M")
    add_item(fake, m["id"], "Page", page_url="syllabus")
    add_item(fake, m["id"], "SomethingNew", title="x")
    add_item(fake, m["id"], "SubHeader", title="Z")
    (mod,) = remote_modules.read(fake.client("read"), ctx_for(tmp_path), paths=PATHS)
    assert [it.item_type for it in mod.items] == ["page", "unmanaged", "header"]
    assert mod.items[1].unmanaged is not None and mod.items[1].unmanaged.type == "SomethingNew"


def test_positions_decide_order(tmp_path: Path) -> None:
    fake = FakeCanvas()
    b = add_module(fake, "B")
    a = add_module(fake, "A")
    b["position"], a["position"] = 2, 1
    add_item(fake, a["id"], "SubHeader", title="one", position=2)
    add_item(fake, a["id"], "SubHeader", title="two", position=1)
    mods = remote_modules.read(fake.client("read"), ctx_for(tmp_path), paths={})
    assert [m.name for m in mods] == ["A", "B"]
    assert [it.header for it in mods[0].items] == ["two", "one"]


def test_unknown_targets_become_unmanaged_with_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeCanvas()
    m = add_module(fake, "Week 1")
    add_item(fake, m["id"], "Page", title="Hidden", page_url="ignored-page", indent=1)
    add_item(fake, m["id"], "Assignment", title="HW", content_id=999)
    add_item(fake, m["id"], "File", title="f", content_id=998)
    (mod,) = remote_modules.read(fake.client("read"), ctx_for(tmp_path), paths={})
    assert [it.item_type for it in mod.items] == ["unmanaged"] * 3
    assert [it.unmanaged.type for it in mod.items if it.unmanaged] == ["Page", "Assignment", "File"]
    assert mod.items[0].indent == 1
    assert mod.items[1].unmanaged is not None and mod.items[1].unmanaged.content_id == 999
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 3
    assert err[0].startswith("warning: modules.yaml: Week 1: items[0]: page ignored-page")


def test_paths_default_to_previous_tree(tmp_path: Path) -> None:
    fake = FakeCanvas()
    m = add_module(fake, "M")
    add_item(fake, m["id"], "Page", page_url="syllabus")
    add_item(fake, m["id"], "File", content_id=55)
    cfg = make_cfg()
    prev = Course(
        config=cfg,
        objects={
            ObjectKey(Kind.PAGE, "pages/syllabus.md"): Page(
                canvas_id="syllabus", title="S", published=True
            ),
            ObjectKey(Kind.FILE, "files/lectures/01.pdf"): FileEntry(
                path="lectures/01.pdf", canvas_id=55, sha256="0" * 64
            ),
        },
        files_meta=FilesMeta(),
        modules=[],
        roster=Roster(),
    )
    (mod,) = remote_modules.read(fake.client("read"), ctx_for(tmp_path, prev))
    assert [it.page or it.file for it in mod.items] == ["syllabus", "lectures/01.pdf"]


def test_dump_is_stable_across_two_fetches_and_round_trips(tmp_path: Path) -> None:
    fake = FakeCanvas()
    seed(fake)
    cfg = make_cfg()

    def fetch() -> dict[str, CanvasObject]:
        mods = remote_modules.read(fake.client("read"), ctx_for(tmp_path), paths=PATHS)
        return {f"modules.yaml#{m.name}": m for m in mods}

    first, second = fetch(), fetch()
    assert first == second
    dumped = MODULE_SPEC.dump(first, cfg)
    assert dumped == MODULE_SPEC.dump(second, cfg)
    ((path, data),) = dumped.items()
    (tmp_path / path).write_bytes(data)
    loaded = MODULE_SPEC.load(tmp_path, cfg)
    assert MODULE_SPEC.dump(loaded, cfg) == dumped
    assert list(loaded.values()) == list(first.values())
    text = data.decode()
    assert (
        "ExternalTool" in text and "prerequisites:" in text and "completion_requirements:" in text
    )


def test_reader_only_gets(tmp_path: Path) -> None:
    fake = FakeCanvas()
    seed(fake)
    remote_modules.read(fake.client("read"), ctx_for(tmp_path), paths=PATHS)
    assert fake.writes() == []
