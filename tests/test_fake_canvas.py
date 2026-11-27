"""FakeCanvas core behaviour, driven through the real CanvasClient (no network)."""

from __future__ import annotations

import httpx
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from canvas_sync.errors import CanvasAPIError, ForbiddenEndpoint
from canvas_sync.remote.http import MAX_RETRIES
from tests.fake_canvas import FakeCanvas
from tests.fake_canvas.core import CanvasError, FakeCanvasError, Req, sanitize_html

C = 101


@pytest.fixture
def fake() -> FakeCanvas:
    return FakeCanvas(course_id=C)


def test_course_and_sections(fake: FakeCanvas) -> None:
    client = fake.client("read")
    course = client.get_one(f"/courses/{C}")
    assert course["id"] == C
    assert [s["name"] for s in client.get(f"/courses/{C}/sections")] == ["Section 001"]
    sec = fake.add_section("Section 002")
    assert client.get_one(f"/courses/{C}/sections/{sec['id']}")["name"] == "Section 002"


def test_users_and_enrollments(fake: FakeCanvas) -> None:
    fake.add_user("Ada Example")
    fake.add_user("Tess Teacher", role="TeacherEnrollment")
    client = fake.client("read")
    assert len(client.get(f"/courses/{C}/users")) == 2
    students = client.get(f"/courses/{C}/users", {"enrollment_type[]": "student"})
    assert [u["name"] for u in students] == ["Ada Example"]
    with_enr = client.get(f"/courses/{C}/users", {"include[]": "enrollments"})
    assert with_enr[0]["enrollments"][0]["type"] == "StudentEnrollment"
    teachers = client.get(f"/courses/{C}/enrollments", {"type[]": "TeacherEnrollment"})
    assert len(teachers) == 1


def test_group_categories_and_groups(fake: FakeCanvas) -> None:
    cat = fake.add_group_category("Teams")
    other = fake.add_group_category("Labs")
    u = fake.add_user("Ada Example")
    g = fake.add_group(cat["id"], "Team A", members=[u["id"]])
    fake.add_group(other["id"], "Lab 1")
    client = fake.client("read")
    assert [c["name"] for c in client.get(f"/courses/{C}/group_categories")] == ["Teams", "Labs"]
    in_cat = client.get(f"/courses/{C}/group_categories/{cat['id']}/groups")
    assert [x["id"] for x in in_cat] == [g["id"]]
    assert in_cat[0]["members_count"] == 1
    assert len(client.get(f"/courses/{C}/groups")) == 2


def test_pagination_with_small_pages(fake: FakeCanvas) -> None:
    fake.max_per_page = 3
    for i in range(8):
        fake.add_user(f"Person {i}")
    client = fake.client("read")
    users = client.get(f"/courses/{C}/users")  # client asks per_page=100; fake caps to 3
    assert len(users) == 8
    pages = [e for e in fake.log if e.path == f"/courses/{C}/users"]
    assert len(pages) == 3
    assert pages[1].params["page"] == "2"


def test_link_headers(fake: FakeCanvas) -> None:
    fake.max_per_page = 2
    for i in range(5):
        fake.add_user(f"Person {i}")
    resp = httpx.Client(transport=fake).get(
        f"{fake.base_url}/api/v1/courses/{C}/users",
        params={"per_page": 2, "page": 2},
        headers={"Authorization": "Bearer x"},
    )
    assert set(resp.links) == {"current", "next", "first", "last"}
    assert "page=3" in resp.links["next"]["url"]
    assert "per_page=2" in resp.links["next"]["url"]
    last = httpx.Client(transport=fake).get(
        f"{fake.base_url}/api/v1/courses/{C}/users",
        params={"per_page": 2, "page": 3},
        headers={"Authorization": "Bearer x"},
    )
    assert "next" not in last.links


def test_log_records_method_path_params(fake: FakeCanvas) -> None:
    client = fake.client("read")
    client.get_one(f"/courses/{C}/sections", {"per_page": 5, "include[]": "students"})
    entry = fake.log[-1]
    assert (entry.method, entry.path) == ("GET", f"/courses/{C}/sections")
    assert entry.params["per_page"] == "5"
    assert entry.params["include[]"] == ["students"]
    assert entry.authorized


def test_unknown_get_is_404_and_unknown_write_fails_test(fake: FakeCanvas) -> None:
    client = fake.client("write")
    with pytest.raises(CanvasAPIError) as err:
        client.get_one(f"/courses/{C}/pages/nope")
    assert err.value.status == 404
    with pytest.raises(FakeCanvasError, match="no route"):
        client.post(f"/courses/{C}/folders", json={"name": "x"})


def test_missing_auth_is_401(fake: FakeCanvas) -> None:
    resp = httpx.Client(transport=fake).get(f"{fake.base_url}/api/v1/courses/{C}")
    assert resp.status_code == 401


def test_write_off_allowlist_never_reaches_fake(fake: FakeCanvas) -> None:
    client = fake.client("write")
    with pytest.raises(ForbiddenEndpoint):
        client.put(f"/courses/{C}/assignments/1/submissions/2", json={})
    assert fake.writes() == []
    # A raw request that bypasses the client is reported as a test failure.
    raw = httpx.Client(transport=fake)
    with pytest.raises(FakeCanvasError, match="allowlist"):
        raw.put(f"{fake.base_url}/api/v1/courses/{C}/assignments/1/submissions/2", json={})


def test_resource_modules_can_plug_in_routes(fake: FakeCanvas) -> None:
    topics = fake.state("scratch")

    def create(q: Req) -> dict[str, str]:
        topic = {"id": "7", "message": fake.sanitize(str(q.body["message"]))}
        topics["7"] = topic
        return topic

    def get(q: Req) -> object:
        if q.match.group(1) not in topics:
            raise CanvasError(404, "missing")
        return topics[q.match.group(1)]

    fake.route("POST", r"/courses/{c}/folders", create)
    fake.route("GET", r"/courses/{c}/folders/(\d+)", get)
    client = fake.client("write")
    client.post(f"/courses/{C}/folders", json={"message": "<p>Hi<br></p>"})
    assert client.get_one(f"/courses/{C}/folders/7")["message"] == "<p>Hi<br /></p>\n"


def test_sanitizer_is_stable_and_not_identity() -> None:
    samples = [
        "<p>Hello</p>",
        "<p>a<br>b<br/>c</p>",
        '<p><b>bold</b> and <i>it</i> <a href="https://x.test">x</a></p>',
        '<img src="a.png"><script>alert(1)</script>',
        '<a href="/x" rel="nofollow">y</a>\n\n',
        "",
    ]
    for s in samples:
        once = sanitize_html(s)
        assert sanitize_html(once) == once, s
        if s:
            assert once != s, s
    assert "<script" not in sanitize_html(samples[3])
    assert "<strong>bold</strong>" in sanitize_html(samples[2])
    assert sanitize_html("<p>x</p>") == sanitize_html("<p>x</p>")


def test_seeded_submissions_snapshot(fake: FakeCanvas) -> None:
    fake.seed_submissions(7, [{"user_id": 5, "body": "<p>mine</p>"}])
    snap = fake.snapshot_submissions()
    client = fake.client("read")
    got = client.get(f"/courses/{C}/assignments/7/submissions")
    assert got[0]["body"] == "<p>mine</p>"
    fake.assert_submissions_intact(snap)
    fake.submissions[7][0]["body"] = "changed"
    with pytest.raises(AssertionError, match="submissions changed"):
        fake.assert_submissions_intact(snap)


def test_submissions_cannot_be_written_through_client(fake: FakeCanvas) -> None:
    fake.seed_submissions(7, [{"user_id": 5}])
    client = fake.client("write")
    with pytest.raises(ForbiddenEndpoint):
        client.delete(f"/courses/{C}/assignments/7/submissions/1")
    assert fake.writes() == []


def test_folder_and_file_context(fake: FakeCanvas) -> None:
    course_file = fake.add_file(fake.root_folder_id, "syllabus.txt", b"hello")
    student_folder = fake.add_folder("submissions", context_type="User", context_id=999)
    student_file = fake.add_file(student_folder["id"], "essay.txt", b"mine")
    client = fake.client("write")
    f = client.get_one(f"/files/{course_file['id']}")
    assert f["folder_id"] == fake.root_folder_id
    assert client.get_one(f"/folders/{fake.root_folder_id}")["context_type"] == "Course"
    assert client.download(course_file["url"]) == b"hello"
    assert [x["id"] for x in client.get(f"/courses/{C}/files")] == [course_file["id"]]
    # The client's course lock refuses to write a student's file; the fake never saw a PUT.
    with pytest.raises(ForbiddenEndpoint):
        client.delete(f"/files/{student_file['id']}")
    assert fake.writes() == []


def test_throttling_is_retried(fake: FakeCanvas) -> None:
    fake.throttle(2)
    fake.throttle(1, status=429, retry_after=7)
    client = fake.client("read")
    assert client.get_one(f"/courses/{C}")["id"] == C
    assert fake.sleeps == [1.0, 2.0, 7.0]
    assert len(fake.log) == 4


def test_5xx_retried_for_get_not_for_post(fake: FakeCanvas) -> None:
    fake.fail(503, method="GET", times=1)
    client = fake.client("write")
    assert client.get_one(f"/courses/{C}")["id"] == C
    assert len(fake.sleeps) == 1

    fake.route("POST", r"/courses/{c}/pages", lambda q: {"url": "x"})
    fake.fail(500, method="POST", times=1)
    before = len(fake.log)
    with pytest.raises(CanvasAPIError):
        client.post(f"/courses/{C}/pages", json={})
    assert len(fake.log) == before + 1  # sent exactly once


def test_persistent_failure_and_network_error(fake: FakeCanvas) -> None:
    fake.fail(500, times=None, path=f"/courses/{C}/sections")
    client = fake.client("read")
    with pytest.raises(CanvasAPIError):
        client.get(f"/courses/{C}/sections")
    assert len(fake.log) == MAX_RETRIES + 1
    fake.fail(0, exc=httpx.ConnectError, times=1, path=f"/courses/{C}")
    assert client.get_one(f"/courses/{C}")["id"] == C


def test_rate_limit_header(fake: FakeCanvas) -> None:
    fake.rate_limit_remaining = 10
    client = fake.client("read")
    client.get_one(f"/courses/{C}")
    assert fake.sleeps  # the client paused because the budget was low


def test_non_api_write_to_base_host_is_refused(fake: FakeCanvas) -> None:
    raw = httpx.Client(transport=fake)
    with pytest.raises(FakeCanvasError, match="outside /api/v1"):
        raw.post(f"{fake.base_url}/courses/{C}/assignments/5/submissions/7")
    # Even a registered ordinary non-API route does not accept writes.
    fake.route("POST", r"/anything", lambda q: {}, api=False)
    with pytest.raises(FakeCanvasError):
        raw.post(f"{fake.base_url}/anything")
    # Upload targets (any host) are the explicit exception.
    fake.route("POST", r"/upload/\d+", lambda q: {"ok": True}, upload_target=True)
    assert raw.post("https://uploads.test/upload/3").json() == {"ok": True}
    assert raw.post(f"{fake.base_url}/upload/3").status_code == 200


def test_seed_submissions_copies_nested_data(fake: FakeCanvas) -> None:
    seed = {"user_id": 5, "attachments": [{"id": 1, "display_name": "a.txt"}]}
    fake.seed_submissions(7, [seed])
    seed["attachments"][0]["display_name"] = "changed"
    seed["attachments"].append({"id": 2})
    assert fake.submissions[7][0]["attachments"] == [{"id": 1, "display_name": "a.txt"}]


def test_users_self_is_dedicated_teacher(fake: FakeCanvas) -> None:
    fake.add_user("Ada Example")
    me = fake.client("read").get_one("/users/self")
    assert me["name"] == "Tess Teacher"
    assert me["id"] not in fake.users


def test_sanitizer_known_nested_cases() -> None:
    for s in ["<scr<script></script>ipt>x</script>", "<br<a>", "<brx>", "<scripty>a</scripty>"]:
        once = sanitize_html(s)
        assert sanitize_html(once) == once, s


@settings(max_examples=500, deadline=None)
@given(
    st.lists(
        st.sampled_from(
            [
                "<",
                ">",
                "/",
                " ",
                "\n",
                "b",
                "i",
                "a",
                "br",
                "img",
                "hr",
                "script",
                "</script>",
                "<script>",
                "<br>",
                "<b>",
                "</b>",
                "<i>",
                "<a ",
                "rel=",
                "x",
                '"',
                "=",
            ]
        )
    ).map("".join)
)
def test_sanitizer_idempotent_property(s: str) -> None:
    once = sanitize_html(s)
    assert sanitize_html(once) == once
