"""Safety layer 1: forbidden calls must raise before any request is sent."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from canvas_sync.errors import ForbiddenEndpoint
from canvas_sync.remote.allowlist import ALLOWED, normalize_path
from canvas_sync.remote.http import CanvasClient

C = 42
OTHER = 43
BASE = "https://canvas.example.edu"


class Recorder:
    """MockTransport that records every request that reaches the wire."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json={})


def make(mode: str = "write", course: int = C) -> tuple[CanvasClient, Recorder]:
    rec = Recorder()
    client = CanvasClient(
        BASE,
        "secret-token",
        course_id=course,
        mode=mode,  # pyright: ignore[reportArgumentType]
        transport=httpx.MockTransport(rec),
    )
    return client, rec


def call(client: CanvasClient, method: str, path: str) -> Any:
    if method == "GET":
        return client.get_one(path)
    if method == "POST":
        return client.post(path, data={"x": 1})
    if method == "PUT":
        return client.put(path, data={"x": 1})
    return client.delete(path)


FORBIDDEN: list[tuple[str, str]] = [
    # writes under submissions
    ("POST", f"/courses/{C}/assignments/1/submissions"),
    ("PUT", f"/courses/{C}/assignments/1/submissions/2"),
    ("DELETE", f"/courses/{C}/assignments/1/submissions/2"),
    ("PUT", f"/courses/{C}/assignments/1/submissions/update_grades"),
    ("POST", f"/courses/{C}/assignments/1/submissions/2/comments"),
    ("PUT", f"/courses/{C}/students/submissions"),
    ("DELETE", f"/courses/{C}/assignments/1/submissions/2/files/3"),
    # course
    ("DELETE", f"/courses/{C}"),
    ("PUT", f"/courses/{C}"),
    ("POST", f"/courses/{C}/conclude"),
    ("DELETE", f"/courses/{C}/"),
    # discussion topics / announcements
    ("DELETE", f"/courses/{C}/discussion_topics/9"),
    # quizzes
    ("POST", f"/courses/{C}/quizzes"),
    ("PUT", f"/courses/{C}/quizzes/5"),
    ("DELETE", f"/courses/{C}/quizzes/5"),
    ("POST", f"/courses/{C}/quizzes/5/submissions"),
    ("GET", f"/courses/{C}/quizzes"),
    # enrollments writes
    ("POST", f"/courses/{C}/enrollments"),
    ("PUT", f"/courses/{C}/enrollments/3"),
    ("DELETE", f"/courses/{C}/enrollments/3"),
    ("POST", "/sections/1/enrollments"),
    # assignment groups: never deleted (with or without move_assignments_to)
    ("DELETE", f"/courses/{C}/assignment_groups/7"),
    ("DELETE", f"/courses/{C}/assignment_groups/7?move_assignments_to=8"),
    # other courses
    ("GET", f"/courses/{OTHER}"),
    ("GET", f"/courses/{OTHER}/assignments"),
    ("POST", f"/courses/{OTHER}/assignments"),
    ("PUT", f"/courses/{OTHER}/pages/x"),
    ("DELETE", f"/courses/{OTHER}/assignments/1"),
    ("GET", f"/courses/{C}0/assignments"),  # 420 is not 42
    ("GET", f"/courses/{C}x/assignments"),
    # unknown things
    ("GET", "/accounts/1"),
    ("GET", "/users/1/profile"),
    ("POST", "/users/self/files"),
    ("DELETE", "/users/self"),
    ("PUT", "/courses/42/assignment_groups/7"),
    ("POST", "/folders/3/folders"),
    ("DELETE", "/folders/3"),
    ("GET", "/"),
    ("GET", ""),
    # normalisation tricks
    ("GET", f"/courses/{C}/assignments/../../{OTHER}"),
    ("GET", f"/courses/{C}/pages/.."),
    ("GET", f"/courses/{C}/pages/%2e%2e/%2e%2e/{OTHER}"),
    ("GET", f"/courses/{C}/pages/..%2f..%2f{OTHER}"),
    ("GET", f"/courses/{C}//assignments"),
    ("GET", f"//courses/{C}/assignments"),
    ("GET", f"/courses/{C}/assignments/./1"),
    ("GET", f"/courses/{C}/assignments?x=1"),  # query belongs in params
    ("GET", f"/courses/{C}/assignments#frag"),
    ("GET", "/courses/%34%33/assignments"),  # %43 -> course 43 after decoding
    ("GET", f"/courses/{C}/%2561ssignments"),  # double encoding
    ("DELETE", f"/courses/{C}/assignments/1/%73ubmissions/2"),  # encoded 's'
    ("DELETE", f"/courses/{C}/assignments/1/SUBMISSIONS/2"),
    ("DELETE", f"/courses/{C}/pages/a\\b"),
    ("GET", f"/courses/{C}/assignments/\x00"),
    ("GET", f"/api/v1/api/v1/courses/{C}"),
    ("DELETE", f"/api/v1/courses/{C}"),
    ("GET", f"api/v1/courses/{C}"),
    ("PUT", f"/courses/{C}/assignments/١"),  # non-ASCII digit
    # absolute URLs
    ("GET", f"https://evil.example.com/api/v1/courses/{C}"),
    ("GET", f"http://canvas.example.edu/api/v1/courses/{C}"),  # wrong scheme
    ("GET", f"https://canvas.example.edu:8443/api/v1/courses/{C}"),
    ("GET", f"https://user:pw@canvas.example.edu/api/v1/courses/{C}"),
    ("GET", f"https://canvas.example.edu/courses/{C}"),  # no /api/v1
    ("GET", f"https://canvas.example.edu.evil.com/api/v1/courses/{C}"),
    ("GET", f"https://canvas.example.edu/api/v1/courses/{OTHER}"),
    ("GET", f"ftp://canvas.example.edu/api/v1/courses/{C}"),
    ("DELETE", f"https://canvas.example.edu/api/v1/courses/{C}/assignment_groups/1"),
]


@pytest.mark.parametrize(("method", "path"), FORBIDDEN)
def test_forbidden_calls_raise_before_io(method: str, path: str) -> None:
    client, rec = make()
    with pytest.raises(ForbiddenEndpoint):
        call(client, method, path)
    assert rec.requests == []


@pytest.mark.parametrize(("method", "path"), FORBIDDEN)
def test_forbidden_calls_raise_for_pagination_get(method: str, path: str) -> None:
    if method != "GET":
        pytest.skip("GET only")
    client, rec = make()
    with pytest.raises(ForbiddenEndpoint):
        client.get(path)
    assert rec.requests == []


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE"])
def test_read_mode_rejects_every_non_get(method: str) -> None:
    client, rec = make(mode="read")
    # Even paths that are allowed in write mode.
    paths = {
        "POST": f"/courses/{C}/pages",
        "PUT": f"/courses/{C}/assignments/1",
        "DELETE": f"/courses/{C}/assignments/1",
    }
    with pytest.raises(ForbiddenEndpoint):
        call(client, method, paths[method])
    assert rec.requests == []


def test_read_mode_rejects_upload() -> None:
    client, rec = make(mode="read")
    with pytest.raises(ForbiddenEndpoint):
        client.upload_file("/", "a.txt", b"x")
    assert rec.requests == []


def test_every_allowed_entry_is_course_locked_or_id_scoped() -> None:
    for method, template in ALLOWED:
        assert method in {"GET", "POST", "PUT", "DELETE"}
        assert not template.endswith(".*"), template
        assert "{c}" in template or template.startswith(("/folders/", "/files/", "/users/self"))
    # no allowed entry mentions the never-allowed resources
    for method, template in ALLOWED:
        if method != "GET":
            for word in ("submissions", "quizzes", "enrollments"):
                assert word not in template
    assert not any(
        m == "DELETE"
        and ("assignment_groups" in t or "discussion_topics" in t or t.endswith("{c}"))
        for m, t in ALLOWED
    )


ALLOWED_CALLS: list[tuple[str, str]] = [
    ("GET", f"/courses/{C}"),
    ("GET", f"/courses/{C}/assignments"),
    ("GET", f"/courses/{C}/assignments/1/submissions"),  # reads are fine (backup)
    ("GET", f"/courses/{C}/assignments/1/overrides"),
    ("GET", f"/courses/{C}/late_policy"),
    ("GET", f"/courses/{C}/enrollments"),
    ("GET", f"/courses/{C}/pages/my-page/"),  # trailing slash dropped
    ("GET", f"/api/v1/courses/{C}/pages"),  # prefix tolerated
    ("GET", f"https://canvas.example.edu/api/v1/courses/{C}/modules?page=2"),
    ("GET", "/users/self"),
    ("GET", "/folders/3/files"),
    ("GET", "/files/10"),
    ("POST", f"/courses/{C}/assignment_groups"),
    ("PUT", f"/courses/{C}/pages/my%20page"),
    ("PUT", f"/courses/{C}/assignments/1"),
    ("DELETE", f"/courses/{C}/modules/1/items/2"),
    ("DELETE", "/files/5"),
]


@pytest.mark.parametrize(("method", "path"), ALLOWED_CALLS)
def test_allowed_calls_reach_the_wire(method: str, path: str) -> None:
    client, rec = make()
    call(client, method, path)
    assert len(rec.requests) == 1
    assert rec.requests[0].url.host == "canvas.example.edu"
    assert rec.requests[0].url.path.startswith("/api/v1/")


def test_wire_path_is_the_checked_path() -> None:
    client, rec = make()
    client.get_one(f"/courses/{C}/pages/a%20b/")
    assert rec.requests[0].url.raw_path == f"/api/v1/courses/{C}/pages/a%20b".encode()


def test_course_lock_other_client() -> None:
    client, rec = make(course=OTHER)
    client.get_one(f"/courses/{OTHER}/pages")
    with pytest.raises(ForbiddenEndpoint):
        client.get_one(f"/courses/{C}/pages")
    assert len(rec.requests) == 1


def test_normalize_path_cases() -> None:
    assert normalize_path("/api/v1/courses/1") == "/courses/1"
    assert normalize_path("/courses/1/") == "/courses/1"
    assert normalize_path("/") == "/"
    for bad in ["courses/1", "//x", "/a//b", "/a/../b", "/a/%2E%2E/b", "/a?b", "/a%2Fb", "/a%252e"]:
        with pytest.raises(ForbiddenEndpoint):
            normalize_path(bad)


def test_repr_and_errors_hide_token() -> None:
    client, _ = make()
    assert "secret-token" not in repr(client)
    with pytest.raises(ForbiddenEndpoint) as ei:
        client.delete(f"/courses/{C}")
    assert "secret-token" not in str(ei.value)
