"""Reference index over a Course (CONTRACTS.md section 13). Pure: no I/O, no git.

Outbound links come from ``markdown-it-py`` tokens (``link_open``, ``image``,
``href``/``src`` inside raw HTML); ``body_format: html`` bodies are scanned for
``href``/``src`` with the stdlib HTML parser. Targets are resolved with
``render.links.resolve_relative``, so the rules match what push renders.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Literal

from markdown_it import MarkdownIt
from markdown_it.token import Token

from canvas_sync.model.base import CanvasObject, Kind, ObjectKey
from canvas_sync.model.course import Course
from canvas_sync.model.modules import Module
from canvas_sync.render.links import LinkContext, is_relative_ref, resolve_relative

LinkKind = Literal["link", "image", "module_item"]


@dataclass(frozen=True)
class Link:
    source: str  # tree path of the referrer ("modules.yaml#<name>" for a module item)
    target: str  # resolved tree path, or the raw relative href when broken
    kind: LinkKind
    line: int | None  # 1-based line in the body when known
    broken: bool  # no such object or file in the tree


@dataclass(frozen=True)
class Index:
    outbound: dict[str, tuple[Link, ...]]  # source tree path -> its links, document order
    backlinks: dict[str, tuple[Link, ...]]  # target tree path -> links to it (modules included)
    broken: tuple[Link, ...]
    orphans: tuple[str, ...]  # non-file objects with no backlinks, in no module
    unreferenced_files: tuple[str, ...]  # files/** with no backlinks, in no module


@dataclass(frozen=True)
class _Raw:
    href: str
    image: bool
    line: int


class _HrefParser(HTMLParser):
    def __init__(self, base_line: int) -> None:
        super().__init__(convert_charrefs=True)
        self.base_line = base_line
        self.found: list[_Raw] = []

    def _tag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        want = {"a": "href", "img": "src"}.get(tag)
        if want is None:
            return
        for name, value in attrs:
            if name == want and value:
                line = self.base_line + self.getpos()[0] - 1
                self.found.append(_Raw(value, tag == "img", line))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._tag(tag, attrs)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._tag(tag, attrs)


def _html_refs(html: str, base_line: int) -> list[_Raw]:
    p = _HrefParser(base_line)
    p.feed(html)
    p.close()
    return p.found


def _md_refs(body: str) -> list[_Raw]:
    out: list[_Raw] = []
    for block in MarkdownIt("commonmark").enable(["table", "strikethrough"]).parse(body):
        start = (block.map[0] + 1) if block.map else 1
        if block.type == "html_block":
            out.extend(_html_refs(block.content, start))
        elif block.type == "inline":
            out.extend(_inline_refs(block.children or [], start))
    return out


def _inline_refs(tokens: list[Token], start: int) -> Iterator[_Raw]:
    line = start
    for t in tokens:
        if t.type in ("softbreak", "hardbreak"):
            line += 1
        elif t.type == "link_open":
            yield _Raw(str(t.attrGet("href") or ""), False, line)
        elif t.type == "image":
            yield _Raw(str(t.attrGet("src") or ""), True, line)
        elif t.type == "html_inline":
            yield from _html_refs(t.content, line)


def _module_keys(modules: list[Module]) -> list[tuple[str, Module]]:
    """Same scheme as ``dump_tree``: ``modules.yaml#<name>``, ``#<name>#2`` for repeats."""
    taken: set[str] = set()
    out: list[tuple[str, Module]] = []
    for m in modules:
        key = f"modules.yaml#{m.name}"
        n = 2
        while key in taken:
            key = f"modules.yaml#{m.name}#{n}"
            n += 1
        taken.add(key)
        out.append((key, m))
    return out


def _body(obj: CanvasObject) -> tuple[str, bool] | None:
    """(body, is_html) for body-bearing objects."""
    body = getattr(obj, "body", None)
    if not isinstance(body, str):
        return None
    return body, getattr(obj, "body_format", "md") == "html"


def build_index(course: Course) -> Index:
    exists = {k.path for k in course.objects}

    def links_from(source: str, body: str, is_html: bool) -> list[Link]:
        seen: list[str] = []

        def ids(key: ObjectKey) -> int | None:
            seen.append(key.path)
            return 0 if key.path in exists else None

        ctx = LinkContext(
            course_id=course.config.course_id,
            from_path=source,
            ids=ids,
            paths=lambda _kind, _cid: None,
        )
        refs = _html_refs(body, 1) if is_html else _md_refs(body)
        out: list[Link] = []
        for r in refs:
            if not is_relative_ref(r.href):
                continue
            seen.clear()
            res = resolve_relative(r.href, ctx, image=r.image)
            ok = res.status == "ok" and bool(seen)
            target = seen[-1] if ok else r.href
            out.append(Link(source, target, "image" if r.image else "link", r.line, not ok))
        return out

    outbound: dict[str, list[Link]] = {}
    for key in sorted(course.objects):
        if key.kind in (Kind.FILE, Kind.MODULE):
            continue
        b = _body(course.objects[key])
        if b is not None:
            found = links_from(key.path, *b)
            if found:
                outbound[key.path] = found

    for mkey, mod in _module_keys(course.modules):
        items: list[Link] = []
        for item in mod.items:
            target: str | None = None
            if item.page is not None:
                target = f"pages/{item.page}.md"
            elif item.assignment is not None:
                target = f"assignments/{item.assignment}.md"
            elif item.file is not None:
                target = f"files/{item.file}"
            if target is not None:
                items.append(Link(mkey, target, "module_item", None, target not in exists))
        if items:
            outbound[mkey] = items

    back: dict[str, list[Link]] = {}
    broken: list[Link] = []
    for src in sorted(outbound):
        for lk in outbound[src]:
            if lk.broken:
                broken.append(lk)
            elif lk.target != lk.source:  # a self-link does not make an object referenced
                back.setdefault(lk.target, []).append(lk)

    orphans: list[str] = []
    unreferenced: list[str] = []
    for key in sorted(course.objects):
        if key.path in back:
            continue
        if key.kind is Kind.FILE:
            unreferenced.append(key.path)
        elif key.kind is not Kind.MODULE:
            orphans.append(key.path)

    return Index(
        outbound={k: tuple(v) for k, v in sorted(outbound.items())},
        backlinks={k: tuple(v) for k, v in sorted(back.items())},
        broken=tuple(broken),
        orphans=tuple(orphans),
        unreferenced_files=tuple(unreferenced),
    )
