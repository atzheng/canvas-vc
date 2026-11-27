"""FakeCanvas behaviour for pages: GET list and GET one.

State lives in ``fake.state("pages")``: ``pages`` (``{page_id: page dict}``) and
``aliases`` (``{old slug: page_id}``). Use ``add_page``; ``rename_page`` changes the slug
like Canvas does and keeps the old slug as an alias (Canvas redirects it). The list does
not carry ``body`` (as on Canvas); ``GET /pages/<slug>`` does. Writes are not modelled yet.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tests.fake_canvas.core import FakeCanvas, Req


def _store(fake: FakeCanvas) -> dict[str, Any]:
    st = fake.state("pages")
    st.setdefault("pages", {})
    st.setdefault("aliases", {})
    return st


def _slug(title: str) -> str:
    return "-".join("".join(c if c.isalnum() else " " for c in title.lower()).split())


def add_page(fake: FakeCanvas, title: str, body: str = "", **fields: Any) -> dict[str, Any]:
    st = _store(fake)
    pages: dict[int, dict[str, Any]] = st["pages"]
    page: dict[str, Any] = {
        "page_id": fake.next_id(),
        "url": _slug(title),
        "title": title,
        "body": fake.sanitize(body),
        "published": True,
        "front_page": False,
        "publish_at": None,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        **fields,
    }
    pages[page["page_id"]] = page
    return page


def rename_page(fake: FakeCanvas, page_id: int, title: str) -> None:
    """Rename on Canvas: new title, new slug, old slug kept as a redirect."""
    st = _store(fake)
    page = st["pages"][page_id]
    st["aliases"][page["url"]] = page_id
    page["title"] = title
    page["url"] = _slug(title)


def register(fake: FakeCanvas) -> None:
    from tests.fake_canvas.core import CanvasError  # core imports this module

    def list_pages(q: Req) -> list[dict[str, Any]]:
        return [{k: v for k, v in p.items() if k != "body"} for p in _store(fake)["pages"].values()]

    def get_page(q: Req) -> dict[str, Any]:
        st = _store(fake)
        slug = q.match.group(1)
        for p in st["pages"].values():
            if p["url"] == slug:
                return dict(p)
        if slug in st["aliases"]:
            return dict(st["pages"][st["aliases"][slug]])
        raise CanvasError(404, "The specified resource does not exist.")

    fake.route("GET", r"/courses/{c}/pages", list_pages)
    fake.route("GET", r"/courses/{c}/pages/([^/]+)", get_page)
