"""Read-only HTTP server for the explorer UI (CONTRACTS.md section 13)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from canvas_sync.ui.layout import Response, error_page, not_found
from canvas_sync.ui.state import State
from canvas_sync.ui.views import files, overview

Render = Callable[[State, str, Mapping[str, list[str]]], Response]

# Prefix -> view. Later beads add their import above and one line here.
ROUTES: dict[str, Render] = {
    "/": overview.render,
    "/files": files.render,
}


def _css() -> Response:
    data = resources.files("canvas_sync.ui").joinpath("static/ui.css").read_bytes()
    return Response(200, data, "text/css; charset=utf-8")


def _match(path: str) -> tuple[Render, str] | None:
    if path == "/":
        return ROUTES["/"], ""
    for prefix in sorted((p for p in ROUTES if p != "/"), key=len, reverse=True):
        if path == prefix:
            return ROUTES[prefix], ""
        if path.startswith(prefix + "/"):
            return ROUTES[prefix], path[len(prefix) + 1 :]
    return None


def dispatch(root: Path, raw_target: str) -> Response:
    """Request routing: URL in, Response out. Never raises."""
    try:
        parts = urlsplit(raw_target)
        path = unquote(parts.path)  # percent-decode exactly once
        if "\0" in path or ".." in path.split("/"):
            return not_found()
        if path == "/static/ui.css":
            return _css()
        m = _match(path)
        if m is None:
            return not_found()
        render, rest = m
        return render(State.load(root), rest, parse_qs(parts.query))
    except Exception as e:  # one-line error page, never a traceback
        return error_page(" ".join(f"{type(e).__name__}: {e}".split()))


def host_ok(header: str | None) -> bool:
    """Only ``127.0.0.1`` / ``localhost`` (optional port) may be the Host (DNS-rebinding guard)."""
    if not header:
        return False
    name, sep, port = header.rpartition(":")
    if not sep or not port.isdigit():
        name = header
    return name.lower() in ("127.0.0.1", "localhost")


def _handler(root: Path) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _serve(self, head_only: bool) -> None:
            if not host_ok(self.headers.get("Host")):
                r = Response(403, b"forbidden host", "text/plain; charset=utf-8")
            else:
                r = dispatch(root, self.path)
            self.send_response(r.status)
            self.send_header("Content-Type", r.content_type)
            self.send_header("Content-Length", str(len(r.body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in r.headers:
                self.send_header(k, v)
            self.end_headers()
            if not head_only:
                self.wfile.write(r.body)

        def do_GET(self) -> None:
            self._serve(False)

        def do_HEAD(self) -> None:
            self._serve(True)

        def _no(self) -> None:
            self.send_response(405)
            self.send_header("Allow", "GET, HEAD")
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _no

        def log_message(self, format: str, *args: object) -> None:
            pass

    return Handler


def make_server(root: Path, port: int) -> ThreadingHTTPServer:
    """Server bound to 127.0.0.1 (never configurable); ``port`` 0 picks a free one."""
    return ThreadingHTTPServer(("127.0.0.1", port), _handler(root))
