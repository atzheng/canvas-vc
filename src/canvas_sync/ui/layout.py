"""Shared page layout and HTML helpers (CONTRACTS.md section 13).

Arguments to ``table`` and ``page`` are HTML the caller already escaped.
"""

from __future__ import annotations

import html
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from urllib.parse import quote

from canvas_sync.config import CourseConfig

NAV = (
    ("overview", "/", "Overview"),
    ("modules", "/modules", "Modules"),
    ("objects", "/objects", "Objects"),
    ("timeline", "/timeline", "Timeline"),
    ("files", "/files", "Files"),
    ("changes", "/changes", "Changes"),
)


def esc(s: object) -> str:
    return html.escape(str(s), quote=True)


def link(href: str, text: str) -> str:
    return f'<a href="{esc(quote(href, safe="/:?&=#%@+,;~"))}">{esc(text)}</a>'


def obj_href(path: str) -> str:
    """``/o/`` + the quoted tree path (a module key ``modules.yaml#name`` keeps its fragment
    percent-encoded so it stays part of the path)."""
    return "/o/" + quote(path, safe="/")


def badge(text: str, kind: str = "") -> str:
    cls = f"badge {esc(kind)}".strip()
    return f'<span class="{cls}">{esc(text)}</span>'


def table(headers: Sequence[str], rows: Iterable[Sequence[str]]) -> str:
    head = "".join(f"<th>{h}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes
    content_type: str = "text/html; charset=utf-8"
    headers: tuple[tuple[str, str], ...] = ()


def page(title: str, body: str, *, current: str, config: CourseConfig | None) -> Response:
    # course.yaml has no title field: name the course by its id.
    course = esc(f"course {config.course_id}") if config else "canvas ui"
    links = " ".join(
        f'<a href="{href}"{" class=current" if key == current else ""}>{label}</a>'
        for key, href, label in NAV
    )
    doc = (
        "<!doctype html>\n<html lang=en><head><meta charset=utf-8>"
        f"<title>{esc(title)} - canvas ui</title>"
        '<link rel=stylesheet href="/static/ui.css"></head><body>'
        f'<nav><span class="brand">canvas ui &middot; {course}</span>{links}</nav>'
        f"<main>{body}</main></body></html>\n"
    )
    return Response(200, doc.encode("utf-8"))


def not_found(msg: str = "not found") -> Response:
    r = page("Not found", f"<h1>404</h1><p>{esc(msg)}</p>", current="", config=None)
    return Response(404, r.body)


def error_page(msg: str, status: int = 500) -> Response:
    r = page("Error", f"<h1>Error</h1><p class=err>{esc(msg)}</p>", current="", config=None)
    return Response(status, r.body)
