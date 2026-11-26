"""The only module that talks HTTP (CONTRACTS.md section 7).

Every call is checked against ``remote/allowlist.py`` (and the client mode)
before any I/O. The path that is checked is the path that is sent.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, Literal
from urllib.parse import quote, urljoin, urlsplit

import httpx

from canvas_sync.errors import CanvasAPIError, ForbiddenEndpoint
from canvas_sync.remote.allowlist import (
    assert_allowed,
    assert_download_allowed,
    normalize_path,
)

API_PREFIX = "/api/v1"
RATE_LIMIT_LOW_WATER = 50.0
RATE_LIMIT_PAUSE = 1.0
MAX_RETRIES = 5
BACKOFF_BASE = 1.0
MAX_PAGES = 10_000
MAX_REDIRECTS = 5
_BODY_LIMIT = 500
_SAFE = "/:@!$&'()*+,=-._~"


def _origin(url: str) -> tuple[str, str, int]:
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    port = parts.port or (443 if scheme == "https" else 80)
    return scheme, host, port


class CanvasClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        course_id: int,
        mode: Literal["read", "write"],
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        root = base_url.rstrip("/")
        if root.endswith(API_PREFIX):
            root = root[: -len(API_PREFIX)]
        parts = urlsplit(root)
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username:
            raise ValueError("base_url must be an http(s) URL without credentials")
        if mode not in ("read", "write"):
            raise ValueError(f"unknown mode {mode!r}")
        self.base_url = root
        self.course_id = int(course_id)
        self.mode = mode
        self._token = token
        self._origin = _origin(root)
        self._sleep = sleep
        self._http = httpx.Client(transport=transport, follow_redirects=False, timeout=60.0)

    def __repr__(self) -> str:  # never include the token
        return f"CanvasClient({self.base_url!r}, course_id={self.course_id}, mode={self.mode!r})"

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> CanvasClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- public JSON API -------------------------------------------------

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """JSON; follows Link pagination, returns the full list."""
        url = self._resolve("GET", path)
        merged: dict[str, Any] = {"per_page": 100}
        merged.update(params or {})
        resp = self._api("GET", url, params=merged)
        body = self._json(resp)
        if not isinstance(body, list):
            return body
        items: list[Any] = list(body)  # pyright: ignore[reportUnknownArgumentType]
        seen = {url}
        for _ in range(MAX_PAGES):
            nxt = resp.links.get("next", {}).get("url")
            if not nxt:
                return items
            # The next link comes from the server: re-check it like any other call.
            nxt_url = self._resolve("GET", nxt)
            if nxt_url in seen:
                raise CanvasAPIError(0, "pagination loop detected")
            seen.add(nxt_url)
            resp = self._api("GET", nxt_url)
            page = self._json(resp)
            if not isinstance(page, list):
                raise CanvasAPIError(resp.status_code, "unexpected non-list page")
            items.extend(page)  # pyright: ignore[reportUnknownArgumentType]
        raise CanvasAPIError(0, "too many pages")

    def get_one(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._json(self._api("GET", self._resolve("GET", path), params=params))

    def post(self, path: str, data: Any = None, json: Any = None) -> Any:
        return self._write("POST", path, data, json)

    def put(self, path: str, data: Any = None, json: Any = None) -> Any:
        return self._write("PUT", path, data, json)

    def delete(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = self._resolve("DELETE", path)
        return self._json(self._api("DELETE", url, params=params))

    def _write(self, method: str, path: str, data: Any, json: Any) -> Any:
        url = self._resolve(method, path)
        return self._json(self._api(method, url, data=data, json=json))

    # ---- files -----------------------------------------------------------

    def upload_file(
        self,
        folder_path: str,
        name: str,
        content: bytes,
        on_duplicate: Literal["overwrite"] = "overwrite",
    ) -> dict[str, Any]:
        """Canvas 3-step upload; returns the file JSON.

        1. POST ``/courses/{c}/files`` (allowlisted, authenticated) -> ``upload_url``.
        2. POST multipart to ``upload_url`` (often another host) with NO credentials,
           no redirect following. Must be https (or this Canvas origin).
        3. If step 2 answers with a redirect, GET its Location (authenticated). It
           must be on the Canvas origin and match the allowlist
           (``GET /files/\\d+/create_success``). A 201 JSON answer ends the flow.
        """
        info = self.post(
            f"/courses/{self.course_id}/files",
            data={
                "name": name,
                "size": len(content),
                "parent_folder_path": folder_path,
                "on_duplicate": on_duplicate,
            },
        )
        info_d: dict[str, Any] = info if isinstance(info, dict) else {}  # pyright: ignore[reportUnknownVariableType]
        upload_url = info_d.get("upload_url")
        params = info_d.get("upload_params")
        if not isinstance(upload_url, str) or not isinstance(params, dict):
            raise CanvasAPIError(0, "upload step 1 returned no upload_url")
        self._check_external(upload_url)
        resp = self._send(
            "POST",
            upload_url,
            auth=False,
            data={k: str(v) for k, v in params.items()},  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
            files={"file": (name, content)},
        )
        if resp.is_redirect or resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("location")
            if not location:
                raise CanvasAPIError(resp.status_code, "upload step 2 redirect without Location")
            confirm = self._resolve("GET", urljoin(upload_url, location))
            return self._json(self._api("GET", confirm))
        result = self._json(resp)
        if not isinstance(result, dict):
            raise CanvasAPIError(resp.status_code, "upload returned no file JSON")
        return result  # pyright: ignore[reportUnknownVariableType]

    def download(self, url: str) -> bytes:
        """GET a file's content URL.

        On the Canvas host only ``/files/<id>/download`` (this course or
        course-less) is allowed, with credentials. Any other host (signed CDN
        URLs) must be https and never receives the token. Redirects are followed
        manually (max 5) and every hop is re-checked.
        """
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            parts = urlsplit(current)
            if parts.username or parts.password or parts.scheme not in ("http", "https"):
                raise ForbiddenEndpoint("refusing download URL with credentials or odd scheme")
            if _origin(current) == self._origin:
                path = normalize_path(parts.path, strip_api_prefix=False)
                assert_download_allowed(path, self.course_id)
                target = self._origin_url(path, parts.query)
                auth = True
            else:
                if parts.scheme != "https" or not parts.hostname:
                    raise ForbiddenEndpoint("refusing non-https download from another host")
                target = current
                auth = False
            resp = self._send("GET", target, auth=auth)
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("location")
                if not location:
                    raise CanvasAPIError(resp.status_code, "redirect without Location")
                current = urljoin(target, location)
                continue
            return resp.content
        raise CanvasAPIError(0, "too many redirects")

    # ---- internals -------------------------------------------------------

    def _origin_url(self, path: str, query: str = "") -> str:
        url = f"{self.base_url}{quote(path, safe=_SAFE)}"
        return f"{url}?{query}" if query else url

    def _resolve(self, method: str, target: str) -> str:
        """Check mode + allowlist and return the URL to send. No I/O."""
        method = method.upper()
        if method not in ("GET", "POST", "PUT", "DELETE"):
            raise ForbiddenEndpoint(f"method {method} is not supported")
        if self.mode == "read" and method != "GET":
            raise ForbiddenEndpoint(f"{method} refused: client is in read mode")
        query = ""
        if target.startswith("/") and not target.startswith("//"):
            raw_path = target
        else:
            parts = urlsplit(target)
            if (
                _origin(target) != self._origin
                or parts.username
                or parts.password
                or parts.fragment
                or not parts.path.startswith(API_PREFIX + "/")
            ):
                raise ForbiddenEndpoint("refusing URL outside this Canvas API")
            raw_path, query = parts.path, parts.query
        path = normalize_path(raw_path)
        assert_allowed(method, path, self.course_id)
        return self._origin_url(API_PREFIX + path, query)

    def _check_external(self, url: str) -> None:
        parts = urlsplit(url)
        if parts.username or parts.password or not parts.hostname or parts.fragment:
            raise ForbiddenEndpoint("refusing upload URL with credentials")
        if parts.scheme != "https" and _origin(url) != self._origin:
            raise ForbiddenEndpoint("refusing non-https upload URL on another host")
        if parts.scheme not in ("http", "https"):
            raise ForbiddenEndpoint("refusing upload URL with odd scheme")

    def _json(self, resp: httpx.Response) -> Any:
        if not resp.content.strip():
            return None
        try:
            return resp.json()
        except ValueError:
            raise CanvasAPIError(resp.status_code, "response was not JSON") from None

    def _api(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        resp = self._send(method, url, auth=True, **kwargs)
        if resp.status_code >= 300:
            raise CanvasAPIError(resp.status_code, "unexpected redirect")
        return resp

    def _send(self, method: str, url: str, *, auth: bool, **kwargs: Any) -> httpx.Response:
        """One logical request with rate-limit pauses and retry/backoff.

        Returns responses with status < 400 (3xx included; callers decide).
        """
        headers = {"Authorization": f"Bearer {self._token}"} if auth else {}
        for attempt in range(MAX_RETRIES + 1):
            try:
                resp = self._http.request(method, url, headers=headers, **kwargs)
            except httpx.HTTPError as exc:
                # Not chained: the exception text must never carry request headers.
                if attempt < MAX_RETRIES:
                    self._sleep(BACKOFF_BASE * 2**attempt)
                    continue
                raise CanvasAPIError(0, f"network error: {type(exc).__name__}") from None
            body = resp.text[:_BODY_LIMIT] if resp.status_code >= 400 else ""
            retryable = resp.status_code >= 500 or (
                resp.status_code == 403 and "rate limit exceeded" in body.lower()
            )
            if retryable and attempt < MAX_RETRIES:
                self._sleep(BACKOFF_BASE * 2**attempt)
                continue
            if resp.status_code >= 400:
                raise CanvasAPIError(resp.status_code, body)
            remaining = resp.headers.get("X-Rate-Limit-Remaining")
            try:
                if remaining is not None and float(remaining) < RATE_LIMIT_LOW_WATER:
                    self._sleep(RATE_LIMIT_PAUSE)
            except ValueError:
                pass
            return resp
        raise AssertionError("unreachable")  # pragma: no cover
