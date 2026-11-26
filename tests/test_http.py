"""CanvasClient behaviour with httpx.MockTransport (no network, no real sleeping)."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from canvas_sync.errors import CanvasAPIError, ForbiddenEndpoint
from canvas_sync.remote.http import MAX_RETRIES, CanvasClient

C = 42
BASE = "https://canvas.example.edu"
API = f"{BASE}/api/v1"

Handler = Callable[[httpx.Request], httpx.Response]


def make(handler: Handler, mode: str = "write") -> tuple[CanvasClient, list[float]]:
    sleeps: list[float] = []
    client = CanvasClient(
        BASE,
        "secret-token",
        course_id=C,
        mode=mode,  # pyright: ignore[reportArgumentType]
        transport=httpx.MockTransport(handler),
        sleep=sleeps.append,
    )
    return client, sleeps


def test_auth_header_and_json() -> None:
    seen: list[httpx.Request] = []

    def h(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"id": 1})

    client, _ = make(h)
    assert client.get_one(f"/courses/{C}") == {"id": 1}
    assert seen[0].headers["Authorization"] == "Bearer secret-token"


def test_empty_body_returns_none() -> None:
    client, _ = make(lambda r: httpx.Response(204))
    assert client.delete(f"/courses/{C}/pages/x") is None


def test_pagination_follows_link_headers() -> None:
    def h(req: httpx.Request) -> httpx.Response:
        page = int(req.url.params.get("page", "1"))
        headers = {}
        if page < 3:
            headers["Link"] = f'<{API}/courses/{C}/pages?page={page + 1}&per_page=100>; rel="next"'
        return httpx.Response(200, json=[page * 10, page * 10 + 1], headers=headers)

    client, _ = make(h)
    assert client.get(f"/courses/{C}/pages") == [10, 11, 20, 21, 30, 31]


def test_pagination_next_link_to_other_host_refused() -> None:
    calls: list[str] = []

    def h(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        return httpx.Response(
            200,
            json=[1],
            headers={
                "Link": f'<https://evil.example.com/api/v1/courses/{C}/pages?page=2>; rel="next"'
            },
        )

    client, _ = make(h)
    with pytest.raises(ForbiddenEndpoint):
        client.get(f"/courses/{C}/pages")
    assert len(calls) == 1


@pytest.mark.parametrize(
    "nxt",
    [
        f"{API}/courses/43/pages?page=2",  # other course
        f"{API}/courses/{C}/assignments/1/../../../43?page=2",
        f"{BASE}/courses/{C}/pages?page=2",  # no /api/v1
        f"{API}/courses/{C}/quizzes?page=2",
    ],
)
def test_pagination_next_link_rechecked(nxt: str) -> None:
    calls: list[str] = []

    def h(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        return httpx.Response(200, json=[1], headers={"Link": f'<{nxt}>; rel="next"'})

    client, _ = make(h)
    with pytest.raises(ForbiddenEndpoint):
        client.get(f"/courses/{C}/pages")
    assert len(calls) == 1


def test_pagination_loop_detected() -> None:
    def h(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=[1], headers={"Link": f'<{API}/courses/{C}/pages?x=1>; rel="next"'}
        )

    client, _ = make(h)
    with pytest.raises(CanvasAPIError):
        client.get(f"/courses/{C}/pages")


def test_rate_limit_403_retried_then_succeeds() -> None:
    n = {"i": 0}

    def h(req: httpx.Request) -> httpx.Response:
        n["i"] += 1
        if n["i"] <= 2:
            return httpx.Response(403, text="403 Forbidden (Rate Limit Exceeded)")
        return httpx.Response(200, json={"ok": True})

    client, sleeps = make(h)
    assert client.get_one(f"/courses/{C}") == {"ok": True}
    assert sleeps == [1.0, 2.0]


def test_5xx_retried_then_api_error_get() -> None:
    n = {"i": 0}

    def h(req: httpx.Request) -> httpx.Response:
        n["i"] += 1
        return httpx.Response(503, text="down")

    client, sleeps = make(h)
    with pytest.raises(CanvasAPIError) as ei:
        client.get_one(f"/courses/{C}")
    assert ei.value.status == 503
    assert n["i"] == MAX_RETRIES + 1
    assert sleeps == [1.0, 2.0, 4.0, 8.0, 16.0]
    assert "secret-token" not in str(ei.value)


def test_plain_403_not_retried() -> None:
    n = {"i": 0}

    def h(req: httpx.Request) -> httpx.Response:
        n["i"] += 1
        return httpx.Response(403, text="user not authorized")

    client, sleeps = make(h)
    with pytest.raises(CanvasAPIError) as ei:
        client.get_one(f"/courses/{C}")
    assert ei.value.status == 403
    assert n["i"] == 1 and sleeps == []


def test_404_not_retried() -> None:
    client, sleeps = make(lambda r: httpx.Response(404, text="nope"))
    with pytest.raises(CanvasAPIError) as ei:
        client.get_one(f"/courses/{C}")
    assert ei.value.status == 404 and sleeps == []


def test_network_error_retried_without_leaking_token() -> None:
    def h(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=req)

    client, sleeps = make(h)
    with pytest.raises(CanvasAPIError) as ei:
        client.get_one(f"/courses/{C}")
    assert len(sleeps) == MAX_RETRIES
    assert "secret-token" not in str(ei.value)
    assert ei.value.__cause__ is None


def test_low_rate_limit_remaining_sleeps() -> None:
    client, sleeps = make(
        lambda r: httpx.Response(200, json={}, headers={"X-Rate-Limit-Remaining": "10.5"})
    )
    client.get_one(f"/courses/{C}")
    assert sleeps == [1.0]
    client2, sleeps2 = make(
        lambda r: httpx.Response(200, json={}, headers={"X-Rate-Limit-Remaining": "600"})
    )
    client2.get_one(f"/courses/{C}")
    assert sleeps2 == []


def test_upload_three_steps() -> None:
    log: list[tuple[str, str, bool]] = []

    def h(req: httpx.Request) -> httpx.Response:
        log.append((req.method, str(req.url), "Authorization" in req.headers))
        if req.url.host == "canvas.example.edu" and req.method == "POST":
            return httpx.Response(
                200,
                json={
                    "upload_url": "https://uploads.example.com/up?x=1",
                    "upload_params": {"key": "abc", "filename": "a.txt"},
                },
            )
        if req.url.host == "uploads.example.com":
            assert b'name="file"' in req.content and b"hello" in req.content
            return httpx.Response(
                301, headers={"Location": f"{API}/files/77/create_success?uuid=u"}
            )
        return httpx.Response(201, json={"id": 77, "display_name": "a.txt"})

    client, _ = make(h)
    out = client.upload_file("course files/x", "a.txt", b"hello")
    assert out == {"id": 77, "display_name": "a.txt"}
    assert [(m, a) for m, _, a in log] == [("POST", True), ("POST", False), ("GET", True)]
    assert log[0][1] == f"{API}/courses/{C}/files"
    assert log[2][1] == f"{API}/files/77/create_success?uuid=u"


def test_upload_step2_201_json_ends_flow() -> None:
    def h(req: httpx.Request) -> httpx.Response:
        if req.url.host == "canvas.example.edu":
            return httpx.Response(
                200, json={"upload_url": "https://up.example.com/", "upload_params": {}}
            )
        return httpx.Response(201, json={"id": 5})

    client, _ = make(h)
    assert client.upload_file("/", "a", b"x") == {"id": 5}


@pytest.mark.parametrize(
    ("upload_url", "location"),
    [
        ("http://up.example.com/", None),  # plain http to another host
        ("https://user:pw@up.example.com/", None),
        ("ftp://up.example.com/", None),
        ("https://up.example.com/", "https://evil.example.com/api/v1/files/1/create_success"),
        ("https://up.example.com/", f"{API}/courses/{C}/quizzes"),
        ("https://up.example.com/", f"{API}/courses/43/files"),
    ],
)
def test_upload_refuses_unsafe_urls(upload_url: str, location: str | None) -> None:
    seen: list[str] = []

    def h(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        if req.url.host == "canvas.example.edu":
            return httpx.Response(200, json={"upload_url": upload_url, "upload_params": {}})
        return httpx.Response(302, headers={"Location": location or "/"})

    client, _ = make(h)
    with pytest.raises(ForbiddenEndpoint):
        client.upload_file("/", "a", b"x")
    # Only step 1 (and possibly the harmless step-2 post) ever happened; never the bad URL.
    assert all("evil" not in s and "quizzes" not in s and "/courses/43" not in s for s in seen)
    if location is None:
        assert len(seen) == 1


def test_download_same_host_with_auth_and_cdn_without() -> None:
    log: list[tuple[str, bool]] = []

    def h(req: httpx.Request) -> httpx.Response:
        log.append((req.url.host, "Authorization" in req.headers))
        if req.url.host == "canvas.example.edu":
            return httpx.Response(302, headers={"Location": "https://cdn.example.com/f?sig=1"})
        return httpx.Response(200, content=b"bytes")

    client, _ = make(h)
    assert client.download(f"{BASE}/files/9/download?download_frd=1&verifier=v") == b"bytes"
    assert log == [("canvas.example.edu", True), ("cdn.example.com", False)]


@pytest.mark.parametrize(
    "url",
    [
        f"{BASE}/api/v1/courses/{C}/assignments",
        f"{BASE}/courses/43/files/9/download",
        f"{BASE}/files/9/../../api/v1/courses/{C}",
        "http://cdn.example.com/f",
        "https://user:pw@cdn.example.com/f",
        "file:///etc/passwd",
    ],
)
def test_download_refuses(url: str) -> None:
    calls: list[str] = []

    def h(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        return httpx.Response(200, content=b"x")

    client, _ = make(h)
    with pytest.raises(ForbiddenEndpoint):
        client.download(url)
    assert calls == []


def test_download_redirect_back_to_canvas_rechecked() -> None:
    def h(req: httpx.Request) -> httpx.Response:
        if req.url.host == "cdn.example.com":
            return httpx.Response(302, headers={"Location": f"{API}/courses/{C}/assignments"})
        return httpx.Response(302, headers={"Location": "https://cdn.example.com/x"})

    client, _ = make(h)
    with pytest.raises(ForbiddenEndpoint):
        client.download(f"{BASE}/files/1/download")


def test_base_url_with_api_suffix_and_bad_base() -> None:
    c = CanvasClient(f"{BASE}/api/v1/", "t", course_id=C, mode="read")
    assert c.base_url == BASE
    with pytest.raises(ValueError):
        CanvasClient("ftp://x", "t", course_id=C, mode="read")


@pytest.mark.parametrize("method", ["POST", "PUT"])
def test_non_idempotent_5xx_sent_once(method: str) -> None:
    n = {"i": 0}

    def h(req: httpx.Request) -> httpx.Response:
        n["i"] += 1
        return httpx.Response(500, text="oops")

    client, sleeps = make(h)
    path = f"/courses/{C}/pages" if method == "POST" else f"/courses/{C}/pages/x"
    with pytest.raises(CanvasAPIError) as ei:
        client.post(path, data={}) if method == "POST" else client.put(path, data={})
    assert ei.value.status == 500 and n["i"] == 1 and sleeps == []


def test_post_network_error_not_retried() -> None:
    n = {"i": 0}

    def h(req: httpx.Request) -> httpx.Response:
        n["i"] += 1
        raise httpx.ReadTimeout("t", request=req)

    client, sleeps = make(h)
    with pytest.raises(CanvasAPIError):
        client.post(f"/courses/{C}/pages", data={})
    assert n["i"] == 1 and sleeps == []


def test_delete_5xx_retried() -> None:
    n = {"i": 0}

    def h(req: httpx.Request) -> httpx.Response:
        n["i"] += 1
        return httpx.Response(502) if n["i"] < 3 else httpx.Response(200, json={})

    client, sleeps = make(h)
    client.delete(f"/courses/{C}/pages/x")
    assert n["i"] == 3 and len(sleeps) == 2


def test_post_throttle_429_retry_after_honoured_and_capped() -> None:
    n = {"i": 0}

    def h(req: httpx.Request) -> httpx.Response:
        n["i"] += 1
        if n["i"] == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        if n["i"] == 2:
            return httpx.Response(429, headers={"Retry-After": "9999"})
        if n["i"] == 3:
            return httpx.Response(403, text="Rate Limit Exceeded")
        return httpx.Response(200, json={"id": 1})

    client, sleeps = make(h)
    assert client.post(f"/courses/{C}/pages", data={}) == {"id": 1}
    assert sleeps == [7.0, 60.0, 4.0]


def folder_handler(context_type: str, context_id: int, log: list[tuple[str, str]]) -> Handler:
    def h(req: httpx.Request) -> httpx.Response:
        log.append((req.method, req.url.path))
        if req.method == "GET" and req.url.path == "/api/v1/files/5":
            return httpx.Response(200, json={"id": 5, "folder_id": 8})
        if req.method == "GET" and req.url.path == "/api/v1/folders/8":
            return httpx.Response(
                200, json={"id": 8, "context_type": context_type, "context_id": context_id}
            )
        return httpx.Response(200, json={"ok": True})

    return h


@pytest.mark.parametrize(
    ("op", "target"),
    [("PUT", "/files/5"), ("DELETE", "/files/5"), ("POST", "/folders/8/files")],
)
def test_course_file_and_folder_writes_allowed(op: str, target: str) -> None:
    log: list[tuple[str, str]] = []
    client, _ = make(folder_handler("Course", C, log))
    {"PUT": client.put, "DELETE": client.delete, "POST": client.post}[op](target)
    assert log[-1][0] == op
    assert ("GET", "/api/v1/folders/8") in log[:-1]


@pytest.mark.parametrize(
    ("op", "target"),
    [("PUT", "/files/5"), ("DELETE", "/files/5"), ("POST", "/folders/8/files")],
)
@pytest.mark.parametrize("ctx", [("User", C), ("Course", 43), ("Group", C)])
def test_foreign_file_and_folder_writes_refused(op: str, target: str, ctx: tuple[str, int]) -> None:
    """A student's submission attachment lives in a user folder: never writable."""
    log: list[tuple[str, str]] = []
    client, _ = make(folder_handler(ctx[0], ctx[1], log))
    with pytest.raises(ForbiddenEndpoint):
        {"PUT": client.put, "DELETE": client.delete, "POST": client.post}[op](target)
    assert all(m == "GET" for m, _ in log)


def test_file_without_folder_refused() -> None:
    client, _ = make(lambda r: httpx.Response(200, json={"id": 5}))
    with pytest.raises(ForbiddenEndpoint):
        client.delete("/files/5")


def test_upload_url_on_canvas_origin_refused() -> None:
    seen: list[str] = []

    def h(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        return httpx.Response(
            200, json={"upload_url": f"{BASE}/api/v1/courses/{C}/pages", "upload_params": {}}
        )

    client, _ = make(h)
    with pytest.raises(ForbiddenEndpoint):
        client.upload_file("/", "a", b"x")
    assert len(seen) == 1


def test_upload_params_method_override_refused() -> None:
    def h(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"upload_url": "https://up.example.com/", "upload_params": {"_method": "x"}}
        )

    client, _ = make(h)
    with pytest.raises(ForbiddenEndpoint):
        client.upload_file("/", "a", b"x")
