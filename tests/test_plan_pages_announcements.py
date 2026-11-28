"""plan/pages.py, plan/announcements.py and their writers, against FakeCanvas."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from canvas_sync.apply import ApplyResult, apply
from canvas_sync.errors import GuardBlocked, ValidationError
from canvas_sync.model.announcement import Announcement
from canvas_sync.model.base import CanvasObject, Kind, ObjectKey
from canvas_sync.model.course import Course
from canvas_sync.model.files import FilesMeta
from canvas_sync.model.page import Page
from canvas_sync.model.roster import Roster, RosterCache
from canvas_sync.plan import announcements as plan_ann
from canvas_sync.plan import pages as plan_pages
from canvas_sync.plan.core import Action, Operation, Plan, RiskKind, diff
from canvas_sync.remote import announcements as remote_ann
from canvas_sync.remote import pages as remote_pages
from canvas_sync.remote.context import ApplyContext
from canvas_sync.remote.guard import SubmissionGuard
from tests.fake_canvas.core import FakeCanvas
from tests.fake_canvas.discussion_topics import add_announcement, add_reply
from tests.fake_canvas.pages import add_page, rename_page
from tests.test_pages_announcements_reader import ctx_for, make_cfg

CFG = make_cfg()
TZ = CFG.tz
FUTURE = datetime.now(TZ).replace(microsecond=0, second=0) + timedelta(days=30)
PAST = datetime.now(TZ).replace(microsecond=0, second=0) - timedelta(days=30)


def course(objs: dict[str, CanvasObject]) -> Course:
    return Course(
        config=CFG,
        objects={o.key(p): o for p, o in objs.items()},
        files_meta=FilesMeta(),
        modules=[],
        roster=Roster(sections=[], groups_category=None),
    )


def differs() -> list[Any]:
    return [plan_pages.diff_type, plan_ann.diff_type]


def apply_ctx(fake: FakeCanvas, ids: dict[ObjectKey, int | str] | None = None) -> ApplyContext:
    return ApplyContext(
        config=CFG,
        tz=TZ,
        roster=RosterCache(),
        ids=ids if ids is not None else {},
        guard=SubmissionGuard(fake.client("read"), CFG),
        blobs=cast(Any, None),
    )


def push(
    fake: FakeCanvas,
    base: Course,
    target: Course,
    ctx: ApplyContext | None = None,
    confirmed: set[str] | None = None,
) -> tuple[Plan, ApplyResult, ApplyContext]:
    plan = diff(base, target, differs())
    ctx = ctx or apply_ctx(fake)
    res = apply(plan, fake.client("write"), ctx, confirmed=confirmed or set())
    assert res.error is None, res.error
    return plan, res, ctx


def with_ids(target: Course, res: ApplyResult) -> Course:
    """The target tree with ids written back from apply (what push does)."""
    objs: dict[ObjectKey, CanvasObject] = {}
    for key, obj in target.objects.items():
        new = res.new_ids.get(key)
        objs[key] = obj.model_copy(update={"canvas_id": new}) if new is not None else obj
    return Course(CFG, objs, FilesMeta(), [], Roster(sections=[], groups_category=None))


def ops_of(plan: Plan) -> list[tuple[Action, str]]:
    return [(o.action, o.key.path) for o in plan.ops]


# ---- pages: differ ----


def test_page_create_update_delete_ops() -> None:
    base = course(
        {
            "pages/a.md": Page(canvas_id="a", title="A", published=True, body="x"),
            "pages/gone.md": Page(canvas_id="gone", title="Gone"),
        }
    )
    target = course(
        {
            "pages/a.md": Page(canvas_id="a", title="A", published=True, body="y"),
            "pages/new.md": Page(title="New", body="n"),
        }
    )
    plan = diff(base, target, differs())
    assert ops_of(plan) == [
        (Action.UPDATE, "pages/a.md"),
        (Action.CREATE, "pages/new.md"),
        (Action.DELETE, "pages/gone.md"),
    ]
    assert plan.ops[0].changes == {"body": ("x", "y")}


def test_page_unchanged_is_empty_and_published_none_is_no_opinion() -> None:
    base = course({"pages/a.md": Page(canvas_id="a", title="A", published=True)})
    same = course({"pages/a.md": Page(canvas_id="a", title="A", published=None)})
    assert diff(base, base, differs()).is_empty()
    assert diff(base, same, differs()).is_empty()


def test_page_moved_path_same_slug_is_not_create_delete() -> None:
    base = course({"pages/a.md": Page(canvas_id="a", title="A")})
    target = course({"pages/b.md": Page(canvas_id="a", title="A", body="z")})
    assert ops_of(diff(base, target, differs())) == [(Action.UPDATE, "pages/b.md")]


def test_page_rename_is_update() -> None:
    base = course({"pages/a.md": Page(canvas_id="a", title="A")})
    target = course({"pages/a.md": Page(canvas_id="a", title="B")})
    plan = diff(base, target, differs())
    assert ops_of(plan) == [(Action.UPDATE, "pages/a.md")]
    assert plan.ops[0].changes == {"title": ("A", "B")}


# ---- pages: writers ----


def test_page_push_then_fetch_has_no_diff(tmp_path: Path) -> None:
    fake = FakeCanvas()
    target = course(
        {
            "pages/home.md": Page(
                title="Home", published=True, front_page=True, body="Hello *world*\n\n* a\n* b"
            ),
            "pages/later.md": Page(title="Later", published=False, publish_at=FUTURE, body="soon"),
        }
    )
    _, res, ctx = push(fake, course({}), target)
    assert res.new_ids[ObjectKey(Kind.PAGE, "pages/home.md")] == "home"
    tree = with_ids(target, res)
    fctx = ctx_for(tmp_path, {k.path: o for k, o in tree.objects.items()}, ctx.bodies)
    got = remote_pages.read(fake.client("read"), fctx)
    # apply reports only canvas_id; the first fetch adds the stable page_id (one-time)
    assert all(isinstance(p, Page) and p.page_id is not None for p in got.values())
    expected = {
        k.path: o.model_copy(update={"page_id": got[k.path].page_id})  # type: ignore[attr-defined]
        for k, o in tree.objects.items()
    }
    assert got == expected
    assert diff(tree, course(got), differs()).is_empty()


def test_page_bodies_recorded_from_returned_html(tmp_path: Path) -> None:
    fake = FakeCanvas()
    md = "Odd   *markdown*  pandoc would not reproduce"
    _, res, ctx = push(fake, course({}), course({"pages/p.md": Page(title="P", body=md)}))
    html = fake.state("pages")["pages"][next(iter(fake.state("pages")["pages"]))]["body"]
    assert ctx.bodies.matches("pages/p.md", html)
    assert ctx.bodies.md_matches("pages/p.md", md)


def test_page_update_sends_only_changed_fields() -> None:
    fake = FakeCanvas()
    page = add_page(fake, "Syllabus", "<p>old</p>", published=True)
    base = course({"pages/s.md": Page(canvas_id="syllabus", title="Syllabus", published=True)})
    target = course(
        {"pages/s.md": Page(canvas_id="syllabus", title="Syllabus", published=True, body="new")}
    )
    base = course(
        {"pages/s.md": Page(canvas_id="syllabus", title="Syllabus", published=True, body="old")}
    )
    push(fake, base, target)
    (w,) = fake.writes()
    assert w.method == "PUT" and w.path == "/courses/101/pages/syllabus"
    assert set(w.body["wiki_page"]) == {"body"}
    assert page["title"] == "Syllabus"


def test_page_rename_reports_new_slug(tmp_path: Path) -> None:
    fake = FakeCanvas()
    add_page(fake, "Old Name", "<p>x</p>", published=True)
    base = course(
        {"pages/p.md": Page(canvas_id="old-name", title="Old Name", published=True, body="x")}
    )
    target = course(
        {"pages/p.md": Page(canvas_id="old-name", title="New Name", published=True, body="x")}
    )
    _, res, ctx = push(fake, base, target)
    key = ObjectKey(Kind.PAGE, "pages/p.md")
    assert res.new_ids == {key: "new-name"}
    assert ctx.ids[key] == "new-name"
    tree = with_ids(target, res)
    got = remote_pages.read(
        fake.client("read"), ctx_for(tmp_path, {k.path: o for k, o in base.objects.items()})
    )
    page_id = got["pages/p.md"].page_id  # type: ignore[attr-defined]  # added by the fetch
    assert page_id is not None
    assert got == {"pages/p.md": tree.objects[key].model_copy(update={"page_id": page_id})}


def test_page_clearing_publish_at_sends_null() -> None:
    fake = FakeCanvas()
    add_page(fake, "P", published=False, publish_at="2026-09-08T16:00:00Z")
    base = course(
        {"pages/p.md": Page(canvas_id="p", title="P", published=False, publish_at=FUTURE)}
    )
    target = course({"pages/p.md": Page(canvas_id="p", title="P", published=False)})
    push(fake, base, target)
    (w,) = fake.writes()
    assert w.body["wiki_page"] == {"publish_at": None}


def test_page_front_page_moves() -> None:
    fake = FakeCanvas()
    add_page(fake, "A", published=True, front_page=True)
    add_page(fake, "B", published=True)
    base = course(
        {
            "pages/a.md": Page(canvas_id="a", title="A", published=True, front_page=True),
            "pages/b.md": Page(canvas_id="b", title="B", published=True),
        }
    )
    target = course(
        {
            "pages/a.md": Page(canvas_id="a", title="A", published=True),
            "pages/b.md": Page(canvas_id="b", title="B", published=True, front_page=True),
        }
    )
    push(fake, base, target)
    fronts = {p["url"]: p["front_page"] for p in fake.state("pages")["pages"].values()}
    assert fronts == {"a": False, "b": True}


def test_page_create_uses_default_published_and_html_body_verbatim() -> None:
    fake = FakeCanvas()
    html = "<p>raw <b>html</b></p>"
    _, res, ctx = push(
        fake, course({}), course({"pages/h.md": Page(title="H", body_format="html", body=html)})
    )
    (w,) = fake.writes()
    assert w.body["wiki_page"]["published"] is False  # course.yaml default
    assert w.body["wiki_page"]["body"] == html
    assert "pages/h.md" not in ctx.bodies.entries


def test_page_links_render_from_ctx_ids() -> None:
    fake = FakeCanvas()
    add_page(fake, "Other", published=True)
    ids: dict[ObjectKey, int | str] = {ObjectKey(Kind.PAGE, "pages/other.md"): "other"}
    target = course(
        {
            "pages/other.md": Page(canvas_id="other", title="Other", published=True),
            "pages/p.md": Page(title="P", body="[o](other.md)"),
        }
    )
    base = course({"pages/other.md": Page(canvas_id="other", title="Other", published=True)})
    push(fake, base, target, apply_ctx(fake, ids))
    (w,) = fake.writes()
    assert "/courses/101/pages/other" in w.body["wiki_page"]["body"]


def test_page_delete() -> None:
    fake = FakeCanvas()
    add_page(fake, "Gone")
    base = course({"pages/g.md": Page(canvas_id="gone", title="Gone")})
    push(fake, base, course({}))
    assert fake.state("pages")["pages"] == {}


# ---- announcements: differ ----


def ann(**kw: Any) -> Announcement:
    return Announcement(title=kw.pop("title", "Hi"), **kw)


def test_new_announcement_without_future_post_at_needs_confirm_notify() -> None:
    target = course({"announcements/hi.md": ann(body="x")})
    plan = diff(course({}), target, differs())
    (risk,) = plan.ops[0].risks
    assert risk.kind is RiskKind.NOTIFIES_STUDENTS and risk.needs_confirm == "--confirm-notify"
    scheduled = course({"announcements/hi.md": ann(post_at=FUTURE)})
    assert diff(course({}), scheduled, differs()).ops[0].risks == ()
    draft = course({"announcements/hi.md": ann(published=False)})
    assert diff(course({}), draft, differs()).ops[0].risks == ()
    past = course({"announcements/hi.md": ann(post_at=PAST)})
    assert diff(course({}), past, differs()).ops[0].risks[0].kind is RiskKind.NOTIFIES_STUDENTS


def test_publishing_a_draft_notifies_and_editing_posted_warns() -> None:
    draft = course({"announcements/hi.md": ann(canvas_id=5, published=False)})
    live = course({"announcements/hi.md": ann(canvas_id=5, published=True)})
    (op,) = diff(draft, live, differs()).ops
    assert [r.kind for r in op.risks] == [RiskKind.NOTIFIES_STUDENTS]
    edited = course({"announcements/hi.md": ann(canvas_id=5, published=True, body="new")})
    (op2,) = diff(live, edited, differs()).ops
    assert [(r.kind, r.needs_confirm) for r in op2.risks] == [(RiskKind.POSTED_ANNOUNCEMENT, None)]
    sched_a = course({"announcements/hi.md": ann(canvas_id=5, post_at=FUTURE)})
    sched_b = course({"announcements/hi.md": ann(canvas_id=5, post_at=FUTURE, body="b")})
    assert diff(sched_a, sched_b, differs()).ops[0].risks == ()


def test_announcement_delete_is_a_delete_with_posted_warning() -> None:
    base = course({"announcements/hi.md": ann(canvas_id=5)})
    (op,) = diff(base, course({}), differs()).ops
    assert op.action is Action.DELETE and op.after is None
    assert op.key == ObjectKey(Kind.ANNOUNCEMENT, "announcements/hi.md")
    assert op.risks[0].kind is RiskKind.POSTED_ANNOUNCEMENT and op.risks[0].needs_confirm is None
    assert "already posted" in op.risks[0].message


def test_deleting_an_unpublished_announcement_is_a_delete_without_risk() -> None:
    base = course({"announcements/hi.md": ann(canvas_id=5, published=False)})
    (op,) = diff(base, course({}), differs()).ops
    assert op.action is Action.DELETE and op.risks == ()


def test_unpublish_instead_for_no_delete() -> None:
    base = course({"announcements/hi.md": ann(canvas_id=5)})
    (op,) = diff(base, course({}), differs()).ops
    down = plan_ann.unpublish_instead(op)
    assert down is not None and down.action is Action.UNPUBLISH
    assert cast(Announcement, down.after).published is False
    assert down.risks[0].kind is RiskKind.POSTED_ANNOUNCEMENT
    hidden = course({"announcements/hi.md": ann(canvas_id=5, published=False)})
    (op2,) = diff(hidden, course({}), differs()).ops
    assert plan_ann.unpublish_instead(op2) is None


# ---- announcements: writers ----


def test_announcement_create_blocked_without_confirm_notify() -> None:
    fake = FakeCanvas()
    plan = diff(course({}), course({"announcements/hi.md": ann(body="x")}), differs())
    with pytest.raises(GuardBlocked):
        apply(plan, fake.client("write"), apply_ctx(fake), confirmed=set())
    assert fake.writes() == []
    res = apply(plan, fake.client("write"), apply_ctx(fake), confirmed={"--confirm-notify"})
    assert res.error is None and len(fake.writes()) == 1


def test_announcement_push_then_fetch_has_no_diff(tmp_path: Path) -> None:
    fake = FakeCanvas()
    target = course(
        {
            "announcements/a.md": ann(title="Welcome", body="Hello *class*\n\n* one"),
            "announcements/b.md": ann(title="Later", post_at=FUTURE, body="soon"),
        }
    )
    _, res, ctx = push(fake, course({}), target, confirmed={"--confirm-notify"})
    tree = with_ids(target, res)
    assert all(isinstance(o.canvas_id, int) for o in tree.objects.values())
    got = remote_ann.read(
        fake.client("read"),
        ctx_for(tmp_path, {k.path: o for k, o in tree.objects.items()}, ctx.bodies),
    )
    assert got == {k.path: o for k, o in tree.objects.items()}
    assert diff(tree, course(got), differs()).is_empty()


def test_announcement_update_sends_only_changed_fields_and_clears_post_at() -> None:
    fake = FakeCanvas()
    t = add_announcement(fake, "Hi", "<p>x</p>", delayed_post_at="2030-01-01T00:00:00Z")
    base = course({"announcements/hi.md": ann(canvas_id=t["id"], post_at=FUTURE, body="x")})
    target = course({"announcements/hi.md": ann(canvas_id=t["id"], body="x")})
    push(fake, base, target, confirmed={"--confirm-notify"})
    (w,) = fake.writes()
    assert w.body == {"delayed_post_at": None}


def test_announcement_unpublish_writes_only_published_false() -> None:
    fake = FakeCanvas()
    t = add_announcement(fake, "Hi", "<p>x</p>")
    base = course({"announcements/hi.md": ann(canvas_id=t["id"], body="x")})
    (op,) = diff(base, course({}), differs()).ops
    down = plan_ann.unpublish_instead(op)
    assert down is not None
    res = apply(Plan([down]), fake.client("write"), apply_ctx(fake), confirmed=set())
    assert res.error is None
    (w,) = fake.writes()
    assert w.method == "PUT" and w.body == {"published": False}
    assert fake.state("discussion_topics")["topics"][t["id"]]["published"] is False


def test_announcement_delete_without_replies_deletes_the_topic() -> None:
    fake = FakeCanvas()
    t = add_announcement(fake, "Hi", "<p>x</p>")
    base = course({"announcements/hi.md": ann(canvas_id=t["id"], body="x")})
    plan, _, _ = push(fake, base, course({}))
    assert plan.ops[0].action is Action.DELETE
    assert [(w.method, w.path) for w in fake.writes()] == [
        ("DELETE", f"/courses/101/discussion_topics/{t['id']}")
    ]
    assert t["id"] not in fake.state("discussion_topics")["topics"]


def test_announcement_delete_with_a_reply_is_refused_before_any_write() -> None:
    fake = FakeCanvas()
    t = add_announcement(fake, "Hi", "<p>x</p>")
    add_reply(fake, t["id"])
    other = add_page(fake, "Gone", published=True)
    base = course(
        {
            "pages/gone.md": Page(canvas_id=other["url"], title="Gone"),
            "announcements/hi.md": ann(canvas_id=t["id"], body="x"),
        }
    )
    plan = diff(base, course({}), differs())
    with pytest.raises(GuardBlocked, match=r"announcements/hi.md: has 1 replies; .*never deleted"):
        apply(plan, fake.client("write"), apply_ctx(fake), confirmed=set())
    assert fake.writes() == []
    assert t["id"] in fake.state("discussion_topics")["topics"]


def test_announcement_with_missing_reply_count_counts_as_having_replies() -> None:
    fake = FakeCanvas()
    t = add_announcement(fake, "Hi", "<p>x</p>", discussion_subentry_count=None)
    assert "discussion_subentry_count" not in fake.state("discussion_topics")["topics"][t["id"]]
    plan = diff(course({"announcements/hi.md": ann(canvas_id=t["id"])}), course({}), differs())
    with pytest.raises(GuardBlocked, match="announcements/hi.md: reply count unknown"):
        apply(plan, fake.client("write"), apply_ctx(fake), confirmed=set())
    assert fake.writes() == []


def test_announcement_already_gone_is_not_blocked_by_the_guard() -> None:
    fake = FakeCanvas()
    plan = diff(course({"announcements/hi.md": ann(canvas_id=999)}), course({}), differs())
    SubmissionGuard(fake.client("read"), CFG).assert_allowed(plan.ops[0], set())


def test_announcement_delete_writer_sends_one_delete() -> None:
    fake = FakeCanvas()
    t = add_announcement(fake, "Hi", "<p>x</p>")
    remote_ann.delete(fake.client("write"), ann(canvas_id=t["id"]), apply_ctx(fake))
    assert [w.method for w in fake.writes()] == ["DELETE"]


def test_operation_type_hint() -> None:
    assert Operation  # keep the import used for type checkers


def test_duplicate_page_canvas_id_in_target_is_refused() -> None:
    base = course({"pages/a.md": Page(canvas_id="a", title="A")})
    target = course(
        {
            "pages/a.md": Page(canvas_id="a", title="A"),
            "pages/a-copy.md": Page(canvas_id="a", title="Copy", body="x"),
        }
    )
    with pytest.raises(ValidationError, match="pages/a.md.*pages/a-copy.md"):
        diff(base, target, differs())


def test_duplicate_announcement_canvas_id_in_target_is_refused() -> None:
    base = course({"announcements/a.md": ann(canvas_id=5)})
    target = course(
        {"announcements/a.md": ann(canvas_id=5), "announcements/b.md": ann(canvas_id=5, body="x")}
    )
    with pytest.raises(ValidationError, match="announcements/b.md.*announcements/a.md"):
        diff(base, target, differs())


def test_deleting_the_front_page_without_replacement_is_refused() -> None:
    base = course({"pages/h.md": Page(canvas_id="h", title="H", front_page=True)})
    with pytest.raises(ValidationError, match="pages/h.md: is the course front page"):
        diff(base, course({}), differs())
    other = course({"pages/o.md": Page(canvas_id="o", title="O", front_page=True)})
    assert ops_of(diff(base, other, differs()))[-1] == (Action.DELETE, "pages/h.md")


def test_page_differ_pairs_by_page_id_not_reused_slug() -> None:
    base = course({"pages/intro.md": Page(canvas_id="intro", page_id=1, title="Intro")})
    target = course(
        {
            "pages/intro.md": Page(canvas_id="welcome", page_id=1, title="Intro"),
            "pages/new.md": Page(canvas_id="intro", page_id=2, title="Intro"),
        }
    )
    # same slug, different page_id: not paired; the renamed slug alone is no change
    assert ops_of(diff(base, target, differs())) == [(Action.CREATE, "pages/new.md")]


def test_page_write_refused_when_slug_belongs_to_another_page() -> None:
    fake = FakeCanvas()
    a = add_page(fake, "Intro", "<p>a</p>")
    rename_page(fake, a["page_id"], "Welcome")
    add_page(fake, "Intro", "<p>b</p>")
    base = course({"pages/intro.md": Page(canvas_id="intro", page_id=a["page_id"], title="Intro")})
    target = course(
        {"pages/intro.md": Page(canvas_id="intro", page_id=a["page_id"], title="Intro", body="z")}
    )
    plan = diff(base, target, differs())
    res = apply(plan, fake.client("write"), apply_ctx(fake), confirmed=set())
    assert res.error is not None and "another page" in str(res.error)
