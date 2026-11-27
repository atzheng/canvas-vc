"""FakeCanvas transport (CONTRACTS.md section 12).

`FakeCanvas` is an `httpx.BaseTransport` that behaves like Canvas for the endpoints in
CONTRACTS section 7. This module owns routing, pagination with `Link` headers, the request
log, fault injection (throttling, 5xx, network errors), seeded student submissions, a simple
folder/file context store, and the HTML sanitizer. Core resources (course, sections, users,
enrollments, group categories, groups) are served here.

Per-resource modules (`pages.py`, `assignments.py`, ...) plug in with::

    def register(fake: FakeCanvas) -> None:
        fake.route("GET", r"/courses/\\d+/pages", list_pages)

A route is `(method, regex, handler)`. The regex is matched with `fullmatch` against the path
relative to `/api/v1` (or the decoded raw path for `api=False` routes, which match any host
and are for non-API URLs such as downloads; non-GET non-API routes must be `upload_target=True`).
A handler takes a `Req` and returns a
`list` (paginated for you), any other JSON value, an `httpx.Response`, or `None`; or raises
`CanvasError`. Routes registered by resource modules win over the defaults in this file. Modules
keep their data in `fake.state("<name>")` and draw ids from `fake.next_id()`.

Strictness: a write the fake has no route for, or one that is off the client allowlist, raises
`FakeCanvasError` (an `AssertionError`) so the test fails. An unknown GET answers 404.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote

import httpx

from canvas_sync.errors import ForbiddenEndpoint
from canvas_sync.remote.allowlist import assert_allowed, normalize_path
from tests.fake_canvas import assignments, discussion_topics, files, modules, pages

if TYPE_CHECKING:
    from canvas_sync.remote.http import CanvasClient

API_PREFIX = "/api/v1"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "canvas"
DEFAULT_PER_PAGE = 10  # Canvas default when no per_page is sent
Handler = Callable[["Req"], Any]

_ENROLLMENT_TYPES = {
    "student_view": "StudentViewEnrollment",
    "student": "StudentEnrollment",
    "teacher": "TeacherEnrollment",
    "ta": "TaEnrollment",
    "observer": "ObserverEnrollment",
    "designer": "DesignerEnrollment",
}


class FakeCanvasError(AssertionError):
    """The code under test did something the fake refuses (a failing test, not a 4xx)."""


class CanvasError(Exception):
    """Raised by a handler to answer with a Canvas-style error response."""

    def __init__(self, status: int, message: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.message = message


# ---- HTML sanitizer ------------------------------------------------------------------


_SCRIPT = re.compile(r"<script(?=[\s/>]).*?</script\s*>", re.IGNORECASE | re.DOTALL)
_VOID = re.compile(r"<(br|hr|img)(?=[\s/>])([^>]*?)\s*/?>", re.IGNORECASE)
_BOLD = re.compile(r"<(/?)b(?=[\s>])", re.IGNORECASE)
_ITALIC = re.compile(r"<(/?)i(?=[\s>])", re.IGNORECASE)
_ANCHOR = re.compile(r"<a\b(?![^>]*\brel=)([^>]*)>", re.IGNORECASE)


def sanitize_html(html: str) -> str:
    """Mimic Canvas rewriting HTML on save: deterministic, idempotent, not the identity.

    Drops `<script>`, closes void tags (`<br />`), turns `<b>`/`<i>` into `<strong>`/`<em>`,
    adds `rel="noopener noreferrer"` to links and ends non-empty bodies with one newline.
    """
    out = html
    for _ in range(100):  # each rewrite can expose another (`<scr<script></script>ipt>`)
        prev = out
        out = _SCRIPT.sub("", out)
        out = _VOID.sub(lambda m: f"<{m.group(1).lower()}{m.group(2)} />", out)
        out = _BOLD.sub(r"<\1strong", out)
        out = _ITALIC.sub(r"<\1em", out)
        out = _ANCHOR.sub(r'<a\1 rel="noopener noreferrer">', out)
        out = out.rstrip()
        out = out + "\n" if out else ""
        if out == prev:
            break
    return out


# ---- request plumbing ----------------------------------------------------------------


@dataclass
class LoggedRequest:
    """One entry of `fake.log`.

    `path` is relative to `/api/v1` for API calls (the raw path otherwise). `params` holds the
    query: a string, or a list when the key repeats or ends in `[]`.
    """

    method: str
    path: str
    params: dict[str, str | list[str]]
    body: dict[str, Any]
    request: httpx.Request
    api: bool = True

    @property
    def authorized(self) -> bool:
        return self.request.headers.get("Authorization", "").startswith("Bearer ")


@dataclass
class Req:
    """What a route handler sees."""

    fake: FakeCanvas
    entry: LoggedRequest
    match: re.Match[str]

    @property
    def method(self) -> str:
        return self.entry.method

    @property
    def params(self) -> dict[str, str | list[str]]:
        return self.entry.params

    @property
    def body(self) -> dict[str, Any]:
        return self.entry.body

    def arg(self, name: str, default: Any = None) -> Any:
        """A body field if present, else a query param, else `default`."""
        if name in self.body:
            return self.body[name]
        return self.params.get(name, default)

    def values(self, name: str) -> list[str]:
        """Query values for `name` or `name[]`, always as a list."""
        out: list[str] = []
        for key in (name, f"{name}[]"):
            v = self.params.get(key)
            if v is not None:
                out.extend(v if isinstance(v, list) else [v])
        return out


@dataclass
class _Route:
    method: str
    regex: re.Pattern[str]
    handler: Handler
    api: bool
    upload_target: bool = False


@dataclass
class _Fault:
    method: str | None
    path: re.Pattern[str] | None
    status: int
    times: int | None
    headers: dict[str, str]
    body: str
    exc: type[httpx.TransportError] | None


def _query(request: httpx.Request) -> dict[str, str | list[str]]:
    out: dict[str, str | list[str]] = {}
    for key, value in request.url.params.multi_items():
        if key in out or key.endswith("[]"):
            existing = out.get(key, [])
            lst = existing if isinstance(existing, list) else [existing]
            out[key] = [*lst, value]
        else:
            out[key] = value
    return out


def _parse_body(request: httpx.Request) -> dict[str, Any]:
    try:
        request.read()
        raw = request.content
    except Exception:  # streaming bodies (multipart) are left to the handler
        return {}
    ctype = request.headers.get("content-type", "")
    if not raw:
        return {}
    if "json" in ctype:
        data: Any = json.loads(raw)
        return data if isinstance(data, dict) else {}  # pyright: ignore[reportUnknownVariableType]
    if "x-www-form-urlencoded" in ctype:
        out: dict[str, Any] = {}
        for key, value in httpx.QueryParams(raw.decode()).multi_items():
            if key in out or key.endswith("[]"):
                existing = out.get(key, [])
                out[key] = [*(existing if isinstance(existing, list) else [existing]), value]
            else:
                out[key] = value
        return out
    return {}


def _error(status: int, message: str, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, json={"errors": [{"message": message}]}, headers=headers)


class FakeCanvas(httpx.BaseTransport):
    def __init__(
        self,
        *,
        course_id: int = 101,
        base_url: str = "https://canvas.test",
        max_per_page: int = 100,
        rate_limit_remaining: float | None = None,
        require_auth: bool = True,
    ) -> None:
        self.course_id = course_id
        self.base_url = base_url.rstrip("/")
        self.max_per_page = max_per_page  # server-side cap on per_page (set small to paginate)
        self.rate_limit_remaining = rate_limit_remaining  # sent as X-Rate-Limit-Remaining
        self.require_auth = require_auth
        self.log: list[LoggedRequest] = []
        self.sleeps: list[float] = []  # filled by clients made with `client()`
        self._routes: list[_Route] = []
        self._faults: list[_Fault] = []
        self._ids = 1000
        self._state: dict[str, dict[str, Any]] = {}

        self.course: dict[str, Any] = self.fixture("course", id=course_id)
        self.sections: dict[int, dict[str, Any]] = {}
        self.users: dict[int, dict[str, Any]] = {}
        self.enrollments: dict[int, dict[str, Any]] = {}
        self.group_categories: dict[int, dict[str, Any]] = {}
        self.groups: dict[int, dict[str, Any]] = {}
        self.group_members: dict[int, list[int]] = {}
        # Seeded student work. Never written by the tool; the guard bead's tests snapshot it.
        self.submissions: dict[int, list[dict[str, Any]]] = {}
        self.folders: dict[int, dict[str, Any]] = {}
        self.files: dict[int, dict[str, Any]] = {}
        self.file_content: dict[int, bytes] = {}

        self.self_user: dict[str, Any] = self.fixture(
            "user", id=self.next_id(), name="Tess Teacher", short_name="Tess"
        )
        self.root_folder_id = self.add_folder("course files")["id"]
        self.add_section("Section 001")

        # Resource modules register first so their routes override the defaults below.
        for module in (pages, discussion_topics, assignments, files, modules):
            module.register(self)
        self._register_defaults()

    # ---- extension API for resource modules ---------------------------------------

    def route(
        self,
        method: str,
        pattern: str,
        handler: Handler,
        *,
        api: bool = True,
        upload_target: bool = False,
    ) -> None:
        """Register a route. `{c}` in `pattern` stands for this fake's course id.

        `pattern` is matched against the decoded path. Non-GET requests outside `/api/v1/`
        (any host) are refused unless a route with `upload_target=True` (which implies
        `api=False`) matches: that is how a files module models the upload URL of step 2.
        """
        regex = re.compile(pattern.replace("{c}", str(self.course_id)), re.ASCII)
        self._routes.append(
            _Route(method.upper(), regex, handler, api and not upload_target, upload_target)
        )

    def next_id(self) -> int:
        self._ids += 1
        return self._ids

    def state(self, name: str) -> dict[str, Any]:
        """A per-module scratch dict (created on first use)."""
        return self._state.setdefault(name, {})

    def sanitize(self, html: str) -> str:
        return sanitize_html(html)

    @staticmethod
    def fixture(name: str, /, **overrides: Any) -> dict[str, Any]:
        """A fresh copy of `tests/fixtures/canvas/<name>.json` with fields overridden."""
        data: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text())
        data.update(overrides)
        return data

    def client(self, mode: str = "write", token: str = "test-token") -> CanvasClient:
        """A `CanvasClient` wired to this fake; its sleeps are recorded in `self.sleeps`."""
        from canvas_sync.remote.http import CanvasClient

        return CanvasClient(
            self.base_url,
            token,
            course_id=self.course_id,
            mode=mode,  # pyright: ignore[reportArgumentType]
            transport=self,
            sleep=self.sleeps.append,
        )

    # ---- seeding helpers -------------------------------------------------------------

    def add_section(self, name: str, **fields: Any) -> dict[str, Any]:
        sid = self.next_id()
        sec = self.fixture("section", id=sid, course_id=self.course_id, name=name, **fields)
        self.sections[sid] = sec
        return sec

    def add_user(
        self,
        name: str,
        *,
        role: str = "StudentEnrollment",
        section_id: int | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        """Add a (made-up) user and enroll them in the course. Returns the user JSON."""
        uid = self.next_id()
        short = name.split()[0]
        user = self.fixture(
            "user", id=uid, name=name, short_name=short, sortable_name=name, **fields
        )
        self.users[uid] = user
        eid = self.next_id()
        sec = section_id if section_id is not None else next(iter(self.sections))
        self.enrollments[eid] = self.fixture(
            "enrollment",
            id=eid,
            user_id=uid,
            course_id=self.course_id,
            course_section_id=sec,
            type=role,
            role=role,
        )
        return user

    def add_group_category(self, name: str, **fields: Any) -> dict[str, Any]:
        cid = self.next_id()
        cat = self.fixture("group_category", id=cid, name=name, course_id=self.course_id, **fields)
        self.group_categories[cid] = cat
        return cat

    def add_group(
        self, category_id: int, name: str, members: list[int] | None = None, **fields: Any
    ) -> dict[str, Any]:
        gid = self.next_id()
        ids = list(members or [])
        group = self.fixture(
            "group",
            id=gid,
            name=name,
            group_category_id=category_id,
            course_id=self.course_id,
            members_count=len(ids),
            **fields,
        )
        self.groups[gid] = group
        self.group_members[gid] = ids
        return group

    def add_folder(
        self,
        name: str,
        *,
        context_type: str = "Course",
        context_id: int | None = None,
        parent_folder_id: int | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        """Add a folder. Use `context_type="User"` to model a student's submission folder."""
        fid = self.next_id()
        cid = self.course_id if context_id is None else context_id
        parent = self.folders.get(parent_folder_id) if parent_folder_id is not None else None
        full = f"{parent['full_name']}/{name}" if parent else name
        folder = self.fixture(
            "folder",
            id=fid,
            name=name,
            full_name=full,
            context_type=context_type,
            context_id=cid,
            parent_folder_id=parent_folder_id,
            **fields,
        )
        self.folders[fid] = folder
        return folder

    def add_file(
        self, folder_id: int, display_name: str, content: bytes = b"", **fields: Any
    ) -> dict[str, Any]:
        fid = self.next_id()
        folder = self.folders[folder_id]
        data = self.fixture(
            "file",
            id=fid,
            folder_id=folder_id,
            display_name=display_name,
            filename=display_name,
            url=f"{self.base_url}/files/{fid}/download",
            size=len(content),
            **fields,
        )
        data["context_type"] = folder["context_type"]
        data["context_id"] = folder["context_id"]
        self.files[fid] = data
        self.file_content[fid] = content
        return data

    def seed_submissions(self, assignment_id: int, submissions: list[dict[str, Any]]) -> None:
        """Store student submissions for an assignment (copied; the tool must never change them)."""
        store = self.submissions.setdefault(assignment_id, [])
        for sub in submissions:
            sid = sub.get("id") or self.next_id()
            store.append(
                self.fixture(
                    "submission", assignment_id=assignment_id, **{**copy.deepcopy(sub), "id": sid}
                )
            )

    def snapshot_submissions(self) -> dict[int, list[dict[str, Any]]]:
        """A deep copy of every seeded submission, to compare after the code under test ran."""
        return copy.deepcopy(self.submissions)

    def assert_submissions_intact(self, snapshot: dict[int, list[dict[str, Any]]]) -> None:
        if self.submissions != snapshot:
            gone = sorted(set(snapshot) - set(self.submissions))
            changed = sorted(a for a in snapshot if self.submissions.get(a) != snapshot[a])
            raise AssertionError(
                f"submissions changed: assignments missing {gone}, differing {changed}"
            )

    # ---- fault injection -------------------------------------------------------------

    def fail(
        self,
        status: int = 500,
        *,
        method: str | None = None,
        path: str | None = None,
        times: int | None = 1,
        headers: dict[str, str] | None = None,
        body: str = "injected failure",
        exc: type[httpx.TransportError] | None = None,
    ) -> None:
        """Answer the next `times` matching requests with `status` (or raise `exc`).

        `path` is a regex matched in full against the API-relative path; `times=None` means
        every matching request. Faults fire before routing, in the order injected.
        """
        self._faults.append(
            _Fault(
                method.upper() if method else None,
                re.compile(path) if path else None,
                status,
                times,
                headers or {},
                body,
                exc,
            )
        )

    def throttle(
        self,
        times: int = 1,
        *,
        status: int = 403,
        retry_after: int | None = None,
        method: str | None = None,
        path: str | None = None,
    ) -> None:
        """Canvas throttling: 403 "Rate Limit Exceeded" (default) or 429 with Retry-After."""
        headers = {"X-Rate-Limit-Remaining": "0"}
        if retry_after is not None:
            headers["Retry-After"] = str(retry_after)
        self.fail(
            status,
            method=method,
            path=path,
            times=times,
            headers=headers,
            body="403 Forbidden (Rate Limit Exceeded)" if status == 403 else "Too Many Requests",
        )

    def _take_fault(self, entry: LoggedRequest) -> _Fault | None:
        for fault in self._faults:
            if fault.method and fault.method != entry.method:
                continue
            if fault.path and not fault.path.fullmatch(entry.path):
                continue
            if fault.times is not None:
                fault.times -= 1
                if fault.times <= 0:
                    self._faults.remove(fault)
            return fault
        return None

    # ---- request handling ------------------------------------------------------------

    def requests(self, method: str | None = None) -> list[LoggedRequest]:
        return [e for e in self.log if method is None or e.method == method.upper()]

    def writes(self) -> list[LoggedRequest]:
        return [e for e in self.log if e.method != "GET"]

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        raw_path = unquote(request.url.path)
        same_host = f"{request.url.scheme}://{request.url.netloc.decode()}" == self.base_url
        api = same_host and raw_path.startswith(API_PREFIX + "/")
        rel = raw_path[len(API_PREFIX) :] if api else raw_path
        entry = LoggedRequest(
            request.method.upper(), rel, _query(request), _parse_body(request), request, api
        )
        self.log.append(entry)

        if api and entry.method != "GET":
            try:
                assert_allowed(entry.method, normalize_path(rel), self.course_id)
            except ForbiddenEndpoint as exc:
                raise FakeCanvasError(f"write off the allowlist reached Canvas: {exc}") from None

        fault = self._take_fault(entry)
        if fault is not None:
            if fault.exc is not None:
                raise fault.exc("injected network error", request=request)
            return self._finish(_error(fault.status, fault.body, fault.headers))

        if api and self.require_auth and not entry.authorized:
            return self._finish(_error(401, "user authorization required"))

        for route in self._routes:
            if route.method != entry.method or (route.api and not api):
                continue
            if not api and entry.method != "GET" and not route.upload_target:
                continue
            m = route.regex.fullmatch(rel)
            if m is None:
                continue
            try:
                result = route.handler(Req(self, entry, m))
            except CanvasError as err:
                return self._finish(_error(err.status, err.message))
            return self._finish(self._respond(result, entry))

        if entry.method != "GET":
            where = "" if api else " outside /api/v1 (only upload_target routes accept writes)"
            raise FakeCanvasError(f"FakeCanvas has no route for {entry.method} {rel}{where}")
        return self._finish(_error(404, "The specified resource does not exist."))

    def _finish(self, response: httpx.Response) -> httpx.Response:
        if self.rate_limit_remaining is not None:
            response.headers.setdefault("X-Rate-Limit-Remaining", str(self.rate_limit_remaining))
        return response

    def _respond(self, result: Any, entry: LoggedRequest) -> httpx.Response:
        if isinstance(result, httpx.Response):
            return result
        if isinstance(result, list):
            return self.paginate(entry, result)  # pyright: ignore[reportUnknownArgumentType]
        if result is None:
            return httpx.Response(200, content=b"")
        return httpx.Response(200, json=result)

    def paginate(self, entry: LoggedRequest, items: list[Any]) -> httpx.Response:
        """One page of `items` with Canvas `Link` headers (current/next/prev/first/last)."""
        raw = entry.params.get("per_page")
        try:
            per_page = int(raw if isinstance(raw, str) else DEFAULT_PER_PAGE)
        except ValueError:
            per_page = DEFAULT_PER_PAGE
        per_page = max(1, min(per_page, self.max_per_page))
        rawp = entry.params.get("page")
        try:
            page = max(1, int(rawp)) if isinstance(rawp, str) else 1
        except ValueError:
            page = 1
        last = max(1, -(-len(items) // per_page))
        url = entry.request.url.copy_remove_param("page")

        def link(n: int, rel: str) -> str:
            return f'<{url.copy_add_param("page", str(n))}>; rel="{rel}"'

        links = [link(page, "current")]
        if page < last:
            links.append(link(page + 1, "next"))
        links += [link(1, "first"), link(last, "last")]
        chunk = items[(page - 1) * per_page : page * per_page]
        return httpx.Response(200, json=chunk, headers={"Link": ", ".join(links)})

    # ---- default routes: course, sections, users, groups, files, submissions -----------

    def _register_defaults(self) -> None:
        r = self.route
        r("GET", r"/courses/{c}", lambda q: self.course)
        r("GET", r"/users/self", lambda q: self.self_user)

        r("GET", r"/courses/{c}/sections", lambda q: list(self.sections.values()))
        r("GET", r"/courses/{c}/sections/(\d+)", lambda q: _one(self.sections, q))

        r("GET", r"/courses/{c}/users", self._list_users)
        r("GET", r"/courses/{c}/enrollments", self._list_enrollments)

        r("GET", r"/courses/{c}/group_categories", lambda q: list(self.group_categories.values()))
        r("GET", r"/courses/{c}/group_categories/(\d+)", lambda q: _one(self.group_categories, q))
        r("GET", r"/courses/{c}/group_categories/(\d+)/groups", self._category_groups)
        r("GET", r"/courses/{c}/groups", lambda q: list(self.groups.values()))

        r("GET", r"/courses/{c}/assignments/(\d+)/submissions", self._list_submissions)

        r("GET", r"/folders/(\d+)", lambda q: _one(self.folders, q))
        r("GET", r"/folders/(\d+)/folders", self._subfolders)
        r("GET", r"/folders/(\d+)/files", self._folder_files)
        r("GET", r"/courses/{c}/folders", lambda q: self._course_items(self.folders))
        r("GET", r"/courses/{c}/files", lambda q: self._course_items(self.files))
        r("GET", r"/files/(\d+)", lambda q: _one(self.files, q))
        r("GET", r"/(?:courses/{c}/)?files/(\d+)/download", self._download, api=False)

    def _course_items(self, store: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            v
            for v in store.values()
            if v["context_type"] == "Course" and str(v["context_id"]) == str(self.course_id)
        ]

    def _list_users(self, q: Req) -> list[dict[str, Any]]:
        wanted = {_ENROLLMENT_TYPES.get(t.lower(), t) for t in q.values("enrollment_type")}
        states = set(q.values("enrollment_state"))
        out: list[dict[str, Any]] = []
        for user in self.users.values():
            mine = [e for e in self.enrollments.values() if e["user_id"] == user["id"]]
            if not any(
                (not wanted or e["type"] in wanted)
                and (not states or e["enrollment_state"] in states)
                for e in mine
            ):
                continue
            row = dict(user)
            if "enrollments" in q.values("include"):
                row["enrollments"] = mine
            out.append(row)
        return out

    def _list_enrollments(self, q: Req) -> list[dict[str, Any]]:
        types = set(q.values("type"))
        return [e for e in self.enrollments.values() if not types or e["type"] in types]

    def _category_groups(self, q: Req) -> list[dict[str, Any]]:
        cid = int(q.match.group(1))
        if cid not in self.group_categories:
            raise CanvasError(404, "The specified resource does not exist.")
        return [g for g in self.groups.values() if g["group_category_id"] == cid]

    def _list_submissions(self, q: Req) -> list[dict[str, Any]]:
        return list(self.submissions.get(int(q.match.group(1)), []))

    def _subfolders(self, q: Req) -> list[dict[str, Any]]:
        fid = int(q.match.group(1))
        if fid not in self.folders:
            raise CanvasError(404, "The specified resource does not exist.")
        return [f for f in self.folders.values() if f["parent_folder_id"] == fid]

    def _folder_files(self, q: Req) -> list[dict[str, Any]]:
        fid = int(q.match.group(1))
        if fid not in self.folders:
            raise CanvasError(404, "The specified resource does not exist.")
        return [f for f in self.files.values() if f["folder_id"] == fid]

    def _download(self, q: Req) -> httpx.Response:
        fid = int(q.match.group(1))
        if fid not in self.file_content:
            raise CanvasError(404, "The specified resource does not exist.")
        return httpx.Response(200, content=self.file_content[fid])


def _one(store: dict[int, dict[str, Any]], q: Req) -> dict[str, Any]:
    found = store.get(int(q.match.group(q.match.lastindex or 1)))
    if found is None:
        raise CanvasError(404, "The specified resource does not exist.")
    return found
