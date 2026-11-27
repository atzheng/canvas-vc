"""plan/modules.py differ and remote/modules.py writers, against FakeCanvas."""

from __future__ import annotations

import io
from datetime import datetime
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

import pytest

from canvas_sync.apply import ApplyResult, apply
from canvas_sync.config import CourseConfig
from canvas_sync.errors import ValidationError
from canvas_sync.ignore import IgnoreRules
from canvas_sync.model.assignment import Assignment
from canvas_sync.model.base import CanvasObject, Kind, ObjectKey
from canvas_sync.model.course import Course
from canvas_sync.model.files import FilesMeta
from canvas_sync.model.modules import Module, ModuleItem, UnmanagedRef
from canvas_sync.model.page import Page
from canvas_sync.model.roster import Roster, RosterCache
from canvas_sync.plan import modules as plan_modules
from canvas_sync.plan.core import Action, Operation, Plan, diff
from canvas_sync.remote import modules as remote_modules
from canvas_sync.remote.context import ApplyContext, FetchContext
from canvas_sync.render.bodies import Bodies
from canvas_sync.snapshot import BlobStore
from tests.fake_canvas.core import FakeCanvas
from tests.fake_canvas.modules import add_item, add_module

CFG = CourseConfig.model_validate(
    {"canvas_url": "https://canvas.test", "course_id": 101, "timezone": "America/Vancouver"}
)
TZ = ZoneInfo("America/Vancouver")


def course(mods: list[Module], objs: dict[ObjectKey, CanvasObject] | None = None) -> Course:
    return Course(CFG, objs or {}, FilesMeta(), mods, Roster())


def pg(ref: str, cid: str | None = None) -> tuple[ObjectKey, Page]:
    return ObjectKey(Kind.PAGE, f"pages/{ref}.md"), Page(title=ref, canvas_id=cid)


def asg(ref: str, cid: int | None = None) -> tuple[ObjectKey, Assignment]:
    return ObjectKey(Kind.ASSIGNMENT, f"assignments/{ref}.md"), Assignment(title=ref, canvas_id=cid)


def objects(*pairs: tuple[ObjectKey, CanvasObject]) -> dict[ObjectKey, CanvasObject]:
    return dict(pairs)


def hdr(text: str, cid: int | None = None, **kw: Any) -> ModuleItem:
    return ModuleItem(header=text, canvas_id=cid, **kw)


def run_diff(base: Course, target: Course) -> list[Operation]:
    return plan_modules.diff_type(base, target)


def describe(ops: list[Operation]) -> list[tuple[str, str]]:
    return [(o.action.value, o.key.path) for o in ops]


# ---- differ ----


def test_identical_modules_give_no_ops() -> None:
    m = Module(canvas_id=1, name="W1", items=[hdr("A", 10), hdr("B", 11)])
    assert run_diff(course([m]), course([m])) == []


def test_module_create_update_delete() -> None:
    old = [Module(canvas_id=1, name="W1"), Module(canvas_id=2, name="W2")]
    new = [
        Module(canvas_id=1, name="Week one", published=True),
        Module(name="W3", unlock=datetime(2026, 9, 8, 9, 0, tzinfo=TZ)),
    ]
    ops = run_diff(course(old), course(new))
    assert describe(ops) == [
        ("update", "modules.yaml#Week one"),
        ("create", "modules.yaml#W3"),
        ("delete", "modules.yaml#W2"),
    ]
    assert ops[0].changes == {"name": ("W1", "Week one"), "published": (None, True)}


def test_module_matched_by_id_before_name_and_duplicates_by_position() -> None:
    old = [Module(canvas_id=1, name="Same"), Module(canvas_id=2, name="Same")]
    new = [Module(canvas_id=2, name="Same"), Module(canvas_id=1, name="Same")]
    ops = run_diff(course(old), course(new))
    # one of the two moves; nothing is re-created or deleted
    assert [o.action for o in ops] == [Action.REORDER]
    assert ops[0].changes == {"position": (2, 1)} or ops[0].changes == {"position": (1, 2)}


def test_module_cleared_unlock_is_explicit_none() -> None:
    d = datetime(2026, 9, 8, 9, 0, tzinfo=TZ)
    ops = run_diff(
        course([Module(canvas_id=1, name="W", unlock=d)]), course([Module(canvas_id=1, name="W")])
    )
    assert ops[0].changes == {"unlock": (d, None)}


def test_published_none_is_no_opinion() -> None:
    old = Module(canvas_id=1, name="W", published=True, items=[hdr("A", 10, published=False)])
    new = Module(canvas_id=1, name="W", items=[hdr("A", 10)])
    assert run_diff(course([old]), course([new])) == []


def test_reorder_modules_minimal() -> None:
    old = [Module(canvas_id=i, name=f"M{i}") for i in range(1, 6)]
    new = [old[4], old[0], old[1], old[2], old[3]]  # last moves to the front
    ops = run_diff(course(old), course(new))
    assert len(ops) == 1
    after = cast(plan_modules.PlacedModule, ops[0].after)
    assert ops[0].action is Action.REORDER
    assert after.canvas_id == 5 and after.place and after.pred_id is None


def test_reorder_items_minimal() -> None:
    items = [hdr(f"h{i}", 10 + i) for i in range(6)]
    old = Module(canvas_id=1, name="W", items=items)
    new = Module(canvas_id=1, name="W", items=[items[0], items[2], items[3], items[1], *items[4:]])
    ops = run_diff(course([old]), course([new]))
    assert len(ops) == 1 and ops[0].action is Action.REORDER
    after = cast(plan_modules.PlacedItem, ops[0].after)
    assert after.canvas_id == 11 and after.pred_id == 13


def test_item_create_update_delete_and_replace_on_target_change() -> None:
    k1, p1 = pg("a", "a")
    k2, p2 = pg("b", "b")
    old = Module(
        canvas_id=1,
        name="W",
        items=[
            ModuleItem(page="a", canvas_id=10),
            hdr("keep", 11, indent=0),
            hdr("gone", 12),
        ],
    )
    new = Module(
        canvas_id=1,
        name="W",
        items=[
            ModuleItem(page="b", canvas_id=10),  # points at another page: replaced
            hdr("keep", 11, indent=2),
            ModuleItem(url="https://x.example/", title="X"),
        ],
    )
    ops = run_diff(
        course([old], objects((k1, p1), (k2, p2))), course([new], objects((k1, p1), (k2, p2)))
    )
    assert [(o.action, o.key.path) for o in ops] == [
        (Action.CREATE, "modules.yaml#W/items[0]"),
        (Action.UPDATE, "modules.yaml#W/items[1]"),
        (Action.CREATE, "modules.yaml#W/items[2]"),
        (Action.DELETE, "modules.yaml#W/items[0]"),
        (Action.DELETE, "modules.yaml#W/items[2]"),
    ]
    assert ops[1].changes == {"indent": (0, 2)}
    created = cast(plan_modules.PlacedItem, ops[0].after)
    assert created.canvas_id is None and created.content_id == "b"


def test_page_and_assignment_titles_never_compared() -> None:
    k, p = pg("a", "a")
    old = Module(canvas_id=1, name="W", items=[ModuleItem(page="a", canvas_id=10, title="Custom")])
    new = Module(canvas_id=1, name="W", items=[ModuleItem(page="a", canvas_id=10, title="Other")])
    assert run_diff(course([old], objects((k, p))), course([new], objects((k, p)))) == []


def test_renamed_page_keeps_item() -> None:
    k, p = pg("a", "a")
    k2 = ObjectKey(Kind.PAGE, "pages/renamed.md")
    old = Module(canvas_id=1, name="W", items=[ModuleItem(page="a", canvas_id=10)])
    new = Module(canvas_id=1, name="W", items=[ModuleItem(page="renamed", canvas_id=10)])
    ops = run_diff(course([old], objects((k, p))), course([new], objects((k2, p))))
    assert ops == []


def test_item_moves_between_modules() -> None:
    old = [
        Module(canvas_id=1, name="A", items=[hdr("x", 10), hdr("y", 11)]),
        Module(canvas_id=2, name="B", items=[]),
    ]
    new = [
        Module(canvas_id=1, name="A", items=[hdr("x", 10)]),
        Module(canvas_id=2, name="B", items=[hdr("y", 11)]),
    ]
    ops = run_diff(course(old), course(new))
    assert [o.action for o in ops] == [Action.UPDATE]
    assert ops[0].changes["module"] == ("A", "B")


def test_missing_target_object_raises() -> None:
    new = Module(canvas_id=1, name="W", items=[ModuleItem(page="nope")])
    with pytest.raises(ValidationError, match="not in the course tree"):
        run_diff(course([Module(canvas_id=1, name="W")]), course([new]))


def test_touches_assignment_flags() -> None:
    k, a = asg("hw1", 77)
    objs = objects((k, a))
    old = Module(canvas_id=1, name="W", items=[ModuleItem(assignment="hw1", canvas_id=10)])
    # remove the item
    ops = run_diff(course([old], objs), course([Module(canvas_id=1, name="W")], objs))
    assert [o.action for o in ops] == [Action.DELETE] and ops[0].touches_assignment
    # add the item
    ops = run_diff(course([Module(canvas_id=1, name="W")], objs), course([old], objs))
    assert [o.action for o in ops] == [Action.CREATE] and ops[0].touches_assignment
    # publish the item
    pub = Module(
        canvas_id=1, name="W", items=[ModuleItem(assignment="hw1", canvas_id=10, published=True)]
    )
    ops = run_diff(course([old], objs), course([pub], objs))
    assert ops[0].touches_assignment
    # publishing the module publishes its assignment items
    ops = run_diff(course([old], objs), course([old.model_copy(update={"published": True})], objs))
    assert ops[0].touches_assignment
    # delete the module
    ops = run_diff(course([old], objs), course([], objs))
    assert ops[0].touches_assignment
    # unrelated edits do not
    other = Module(
        canvas_id=1, name="W", items=[ModuleItem(assignment="hw1", canvas_id=10), hdr("h")]
    )
    ops = run_diff(course([old], objs), course([other], objs))
    assert not any(o.touches_assignment for o in ops)


def test_plan_renders() -> None:
    old = Module(canvas_id=1, name="W", items=[hdr("a", 10)])
    new = Module(canvas_id=1, name="W", items=[hdr("a", 10, indent=1), hdr("b")])
    plan = diff(course([old]), course([new]), [plan_modules.diff_type])
    from rich.console import Console

    c = Console(width=200, file=io.StringIO(), color_system=None)
    c.print(plan.render())
    text = c.file.getvalue()  # type: ignore[attr-defined]
    assert "item" in text and "W/items[1]" in text


# ---- unmanaged items and settings (bead awi) ----


def unmanaged_module() -> Module:
    from canvas_sync.model.modules import CompletionRequirement, UnmanagedItem

    return Module(
        canvas_id=1,
        name="W",
        prerequisites=[9],
        require_sequential_progress=True,
        completion_requirements=[CompletionRequirement(item_id=12, type="must_view")],
        items=[
            hdr("a", 10),
            ModuleItem(
                unmanaged=UnmanagedItem(type="ExternalTool", content_id=5),
                title="Clicker",
                canvas_id=12,
            ),
            hdr("b", 11),
        ],
    )


def test_unmanaged_unchanged_is_zero_ops() -> None:
    m = unmanaged_module()
    assert run_diff(course([m]), course([m])) == []


def test_editing_other_item_leaves_unmanaged_and_settings(
    capfd: pytest.CaptureFixture[str],
) -> None:
    old = unmanaged_module()
    items = list(old.items)
    items[0] = hdr("a2", 10)
    new = old.model_copy(update={"items": items})
    ops = run_diff(course([old]), course([new]))
    assert describe(ops) == [("update", "modules.yaml#W/items[0]")]
    assert ops[0].changes == {"header": ("a", "a2")}
    assert capfd.readouterr().err == ""


def test_local_edits_to_read_only_things_warn_and_are_ignored(
    capfd: pytest.CaptureFixture[str],
) -> None:
    from canvas_sync.model.modules import UnmanagedItem

    old = unmanaged_module()
    tool = ModuleItem(
        unmanaged=UnmanagedItem(type="ExternalTool", content_id=5),
        title="Renamed",
        indent=3,
        canvas_id=12,
    )
    new = Module(
        canvas_id=1,
        name="W",
        prerequisites=[],
        require_sequential_progress=False,
        completion_requirements=[],
        # unmanaged moved to the front, edited; plus an unknown unmanaged entry
        items=[
            tool,
            hdr("a", 10),
            ModuleItem(unmanaged=UnmanagedItem(type="ExternalTool"), canvas_id=99),
            hdr("b", 11),
        ],
    )
    assert run_diff(course([old]), course([new])) == []
    err = capfd.readouterr().err
    assert "modules.yaml#W: prerequisites are read-only, local change ignored" in err
    assert "require_sequential_progress is read-only" in err
    assert "completion_requirements are read-only" in err
    assert "items[0]: title, indent of an unmanaged item are read-only" in err
    assert "items[0]: reordering an unmanaged item is read-only" in err
    assert "items[2]: adding an unmanaged item is not supported" in err
    assert all(line.count("\n") == 0 for line in err.splitlines())


def test_deleting_unmanaged_entry_is_not_a_delete(capfd: pytest.CaptureFixture[str]) -> None:
    old = unmanaged_module()
    new = old.model_copy(update={"items": [old.items[0], old.items[2]]})
    assert run_diff(course([old]), course([new])) == []
    assert "deleting an unmanaged item is not supported" in capfd.readouterr().err


# ---- writers against FakeCanvas ----


def apply_ctx(fake: FakeCanvas) -> ApplyContext:
    return ApplyContext(
        config=CFG,
        tz=TZ,
        roster=RosterCache(),
        ids={},
        guard=cast(Any, _NoGuard()),
        blobs=cast(Any, None),
    )


class _NoGuard:
    def __init__(self) -> None:
        self.seen: list[Operation] = []

    def assert_allowed(self, op: Operation, confirmed: set[str]) -> None:
        self.seen.append(op)


def push(
    fake: FakeCanvas, base: Course, target: Course, ctx: ApplyContext | None = None
) -> tuple[Plan, ApplyResult, ApplyContext]:
    plan = diff(base, target, [plan_modules.diff_type])
    ctx = ctx or apply_ctx(fake)
    res = apply(plan, fake.client("write"), ctx, confirmed=set())
    assert res.error is None, res.error
    return plan, res, ctx


def fetch_modules(fake: FakeCanvas, tmp_path: Path, paths: Any = None) -> list[Module]:
    fctx = FetchContext(
        config=CFG,
        tz=TZ,
        bodies=cast(Bodies, None),
        previous=None,
        roster=RosterCache(),
        ignore=IgnoreRules(lines=[]),
        blob_store=BlobStore(tmp_path),
    )
    return remote_modules.read(fake.client("read"), fctx, paths=paths or {})


def order(fake: FakeCanvas, mid: int) -> list[int]:
    st = fake.state("modules")["items"][mid]
    return [i["id"] for i in sorted(st, key=lambda r: r["position"])]


def test_new_objects_and_items_in_one_push(tmp_path: Path) -> None:
    fake = FakeCanvas()
    kp, p = pg("syllabus")  # created earlier in the same push: no canvas_id yet
    ka, a = asg("hw1")
    objs = objects((kp, p), (ka, a))
    target_mod = Module(
        name="Week 1",
        published=True,
        unlock=datetime(2026, 9, 8, 9, 0, tzinfo=TZ),
        items=[
            hdr("Intro"),
            ModuleItem(page="syllabus"),
            ModuleItem(assignment="hw1", indent=1),
            ModuleItem(url="https://x.example/", title="X", new_tab=True),
            ModuleItem(quiz=UnmanagedRef(canvas_id=4321), title="Quiz"),
        ],
    )
    ctx = apply_ctx(fake)
    ctx.ids[kp] = "syllabus"  # the page and assignment were created before the modules
    ctx.ids[ka] = 77
    plan, res, _ = push(fake, course([]), course([target_mod], objs), ctx)
    assert not any(o.touches_assignment for o in plan.ops if o.key.path.endswith("items[1]"))
    assert any(o.touches_assignment for o in plan.ops if o.key.path.endswith("items[2]"))
    assert len(res.done) == 6
    mods = fetch_modules(
        fake,
        tmp_path,
        {(Kind.PAGE, "syllabus"): "syllabus", (Kind.ASSIGNMENT, 77): "hw1"},
    )
    assert len(mods) == 1 and mods[0].published is True
    assert mods[0].unlock == target_mod.unlock
    got = mods[0].items
    assert [i.item_type for i in got] == ["header", "page", "assignment", "url", "quiz"]
    assert got[2].indent == 1 and got[3].new_tab is True
    # push then fetch is a no-op
    written = ctx.ids[ObjectKey(Kind.MODULE, "modules.yaml#Week 1")]
    assert mods[0].canvas_id == written
    assert run_diff(course(mods, objs), course(mods, objs)) == []
    # items carry their ids back, so replanning the pushed tree (ids written back) is empty
    assert run_diff(course(mods, objs), course(mods, objs)) == []


def seed_two(fake: FakeCanvas) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    m = add_module(fake, "W")
    its = [add_item(fake, m["id"], "SubHeader", title=f"h{i}") for i in range(6)]
    return m, its


def test_reorder_sends_one_position_update(tmp_path: Path) -> None:
    fake = FakeCanvas()
    m, its = seed_two(fake)
    base = course(fetch_modules(fake, tmp_path))
    mod = base.modules[0]
    items = mod.items
    new = course(
        [mod.model_copy(update={"items": [items[0], items[2], items[3], items[1], *items[4:]]})]
    )
    push(fake, base, new)
    puts = [w for w in fake.writes()]
    assert len(puts) == 1 and puts[0].method == "PUT"
    ids = [i["id"] for i in its]
    assert order(fake, m["id"]) == [ids[0], ids[2], ids[3], ids[1], ids[4], ids[5]]
    assert (
        run_diff(course(fetch_modules(fake, tmp_path)), course(fetch_modules(fake, tmp_path))) == []
    )


def test_reorder_with_deletes_and_creates_ends_in_target_order(tmp_path: Path) -> None:
    fake = FakeCanvas()
    m, its = seed_two(fake)
    base = course(fetch_modules(fake, tmp_path))
    items = base.modules[0].items
    new_items = [
        items[5],
        hdr("new first"),
        items[2],
        items[0],
        hdr("new mid"),
        items[3],
        hdr("new last"),
    ]  # items 1 and 4 deleted
    target = course([base.modules[0].model_copy(update={"items": new_items})])
    push(fake, base, target)
    got = fetch_modules(fake, tmp_path)
    assert [i.header for i in got[0].items] == [
        "h5",
        "new first",
        "h2",
        "h0",
        "new mid",
        "h3",
        "new last",
    ]
    assert run_diff(course(got), course(got)) == []


def test_module_reorder_and_item_move(tmp_path: Path) -> None:
    fake = FakeCanvas()
    a = add_module(fake, "A")
    b = add_module(fake, "B")
    c = add_module(fake, "C")
    x = add_item(fake, a["id"], "SubHeader", title="x")
    add_item(fake, a["id"], "SubHeader", title="y")
    base = course(fetch_modules(fake, tmp_path))
    ma, mb, mc = base.modules
    new = course(
        [
            mc,
            ma.model_copy(update={"items": [ma.items[1]]}),
            mb.model_copy(update={"items": [ma.items[0]]}),
        ]
    )
    push(fake, base, new)
    got = fetch_modules(fake, tmp_path)
    assert [m.name for m in got] == ["C", "A", "B"]
    assert [i.header for i in got[2].items] == ["x"] and got[2].items[0].canvas_id == x["id"]
    assert [i.header for i in got[1].items] == ["y"]
    assert run_diff(course(got), course(got)) == []
    assert b["id"] == got[2].canvas_id and c["id"] == got[0].canvas_id


def test_updates_send_only_changed_fields_and_explicit_null(tmp_path: Path) -> None:
    fake = FakeCanvas()
    m = add_module(fake, "W", unlock_at="2026-09-08T16:00:00Z", published=False)
    it = add_item(fake, m["id"], "SubHeader", title="h", indent=1)
    base = course(fetch_modules(fake, tmp_path))
    mod = base.modules[0]
    new = course(
        [
            mod.model_copy(
                update={
                    "unlock": None,
                    "published": True,
                    "items": [mod.items[0].model_copy(update={"indent": 0})],
                }
            )
        ]
    )
    push(fake, base, new)
    w = fake.writes()
    assert len(w) == 2
    assert w[0].body == {"module": {"unlock_at": None, "published": True}}
    assert w[1].body == {"module_item": {"indent": 0}}
    assert fake.state("modules")["modules"][m["id"]]["unlock_at"] is None
    assert it["indent"] == 0


def test_writers_never_send_settings(tmp_path: Path) -> None:
    fake = FakeCanvas()
    m1 = add_module(fake, "P")
    m2 = add_module(
        fake,
        "W",
        prerequisite_module_ids=[m1["id"]],
        require_sequential_progress=True,
    )
    tool = add_item(fake, m2["id"], "ExternalTool", title="T", content_id=9)
    add_item(fake, m2["id"], "SubHeader", title="h")
    m2["completion_requirements"] = [{"id": tool["id"], "type": "must_view"}]
    base = course(fetch_modules(fake, tmp_path))
    mod = base.modules[1]
    new_items = [mod.items[0], mod.items[1].model_copy(update={"header": "h2"})]
    new = course([base.modules[0], mod.model_copy(update={"name": "W2", "items": new_items})])
    push(fake, base, new)
    for w in fake.writes():
        text = str(w.body)
        for banned in ("prerequisite", "completion_requirements", "require_sequential_progress"):
            assert banned not in text
    st = fake.state("modules")["modules"][m2["id"]]
    assert st["prerequisite_module_ids"] == [m1["id"]] and st["require_sequential_progress"]
    assert st["completion_requirements"] == [{"id": tool["id"], "type": "must_view"}]
    assert any(i["type"] == "ExternalTool" for i in fake.state("modules")["items"][m2["id"]])
    got = fetch_modules(fake, tmp_path)
    assert run_diff(course(got), course(got)) == []


def test_delete_module_and_item(tmp_path: Path) -> None:
    fake = FakeCanvas()
    a = add_module(fake, "A")
    b = add_module(fake, "B")
    add_item(fake, a["id"], "SubHeader", title="x")
    add_item(fake, a["id"], "SubHeader", title="y")
    add_item(fake, b["id"], "SubHeader", title="z")
    base = course(fetch_modules(fake, tmp_path))
    new = course([base.modules[0].model_copy(update={"items": [base.modules[0].items[0]]})])
    push(fake, base, new)
    got = fetch_modules(fake, tmp_path)
    assert [m.name for m in got] == ["A"] and len(got[0].items) == 1
    assert [w.method for w in fake.writes()] == ["DELETE", "DELETE"]


def test_apply_routes_assignment_items_through_guard(tmp_path: Path) -> None:
    fake = FakeCanvas()
    m = add_module(fake, "W")
    add_item(fake, m["id"], "Assignment", title="HW", content_id=77)
    ka, a = asg("hw1", 77)
    objs = objects((ka, a))
    paths: dict[tuple[Kind, int | str], str] = {(Kind.ASSIGNMENT, 77): "hw1"}
    base = course(fetch_modules(fake, tmp_path, paths), objs)
    new = course([base.modules[0].model_copy(update={"items": []})], objs)
    ctx = apply_ctx(fake)
    push(fake, base, new, ctx)
    guard = cast(_NoGuard, ctx.guard)
    # pre-flight pass plus just-in-time pass
    assert [o.action for o in guard.seen] == [Action.DELETE, Action.DELETE]


def test_fetch_then_plan_is_zero_ops_with_unmanaged_and_settings(tmp_path: Path) -> None:
    fake = FakeCanvas()
    m1 = add_module(fake, "W1")
    m2 = add_module(
        fake, "W2", prerequisite_module_ids=[m1["id"]], require_sequential_progress=True
    )
    add_item(fake, m1["id"], "Page", title="Custom title", page_url="syllabus")
    add_item(fake, m1["id"], "Page", title="Ignored page", page_url="not-fetched")
    add_item(fake, m1["id"], "Assignment", title="Ignored hw", content_id=999)
    tool = add_item(fake, m1["id"], "ExternalTool", title="Clicker", content_id=9)
    add_item(fake, m1["id"], "File", title="slides.pdf", content_id=55)
    quiz = add_item(fake, m2["id"], "Quiz", title="Q", content_id=4)
    m2["completion_requirements"] = [
        {"id": quiz["id"], "type": "min_score", "min_score": 80, "completed": True},
        {"id": tool["id"], "type": "must_view"},
    ]
    paths: dict[tuple[Kind, int | str], str] = {
        (Kind.PAGE, "syllabus"): "syllabus",
        (Kind.FILE, 55): "slides.pdf",
    }
    objs = objects(pg("syllabus", "syllabus"))
    fetched = fetch_modules(fake, tmp_path, paths)
    assert run_diff(course(fetched, objs), course(fetched, objs)) == []
    # edit another item of the module: the unmanaged items and settings are untouched
    first = fetched[0]
    items = list(first.items)
    items[4] = items[4].model_copy(update={"indent": 1})
    edited = [first.model_copy(update={"items": items}), fetched[1]]
    plan, _, _ = push(fake, course(fetched, objs), course(edited, objs))
    assert len(plan.ops) == 1
    assert [w.method for w in fake.writes()] == ["PUT"]
    for w in fake.writes():
        for banned in ("prerequisite", "completion", "sequential", "title"):
            assert banned not in str(w.body)
    after = fetch_modules(fake, tmp_path, paths)
    assert [i.item_type for i in after[0].items] == [i.item_type for i in first.items]
    assert after[1].completion_requirements == fetched[1].completion_requirements
