"""FakeCanvas behaviour for pages: GET list, GET one, POST, PUT, DELETE.

State lives in ``fake.state("pages")``: ``pages`` (``{page_id: page dict}``) and
``aliases`` (``{old slug: page_id}``). Use ``add_page``; ``rename_page`` changes the slug
like Canvas does and keeps the old slug as an alias (Canvas redirects it). The list does
not carry ``body`` (as on Canvas); ``GET /pages/<slug>`` does. POST creates
(unpublished unless ``published`` is sent; the slug gets ``-2`` on collision), PUT updates
only the fields sent (a new title renames the page like Canvas does; ``front_page`` true
clears it on every other page), DELETE removes the page (the front page: 400). Bodies are
JSON ``{"wiki_page": {...}}`` or form fields ``wiki_page[title]``. Responses carry the
sanitized body.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

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

    def fields(q: Req) -> dict[str, Any]:
        nested = q.body.get("wiki_page")
        if isinstance(nested, dict):
            return dict(cast("dict[str, Any]", nested))
        return {k[10:-1]: v for k, v in q.body.items() if k.startswith("wiki_page[")}

    def truthy(v: Any) -> bool:
        return v in (True, "true", "1")

    def find(slug: str) -> dict[str, Any]:
        st = _store(fake)
        for p in st["pages"].values():
            if p["url"] == slug:
                return p
        if slug in st["aliases"]:
            return st["pages"][st["aliases"][slug]]
        raise CanvasError(404, "The specified resource does not exist.")

    def set_front(page: dict[str, Any]) -> None:
        for other in _store(fake)["pages"].values():
            other["front_page"] = other is page

    def create_page(q: Req) -> dict[str, Any]:
        f = fields(q)
        title = str(f.get("title") or "").strip()
        if not title:
            raise CanvasError(400, "title is required")
        taken = {p["url"] for p in _store(fake)["pages"].values()}
        base, n = _slug(title), 1
        slug = base
        while slug in taken:
            n += 1
            slug = f"{base}-{n}"
        page = add_page(
            fake,
            title,
            str(f.get("body") or ""),
            url=slug,
            published=truthy(f.get("published")),
            publish_at=f.get("publish_at") or None,
        )
        if truthy(f.get("front_page")):
            set_front(page)
        return dict(page)

    def update_page(q: Req) -> dict[str, Any]:
        page = find(q.match.group(1))
        f = fields(q)
        if "title" in f and f["title"] != page["title"]:
            rename_page(fake, page["page_id"], str(f["title"]))
        if "body" in f:
            page["body"] = fake.sanitize(str(f["body"] or ""))
        if "published" in f:
            page["published"] = truthy(f["published"])
        if "publish_at" in f:
            page["publish_at"] = f["publish_at"] or None
        if "front_page" in f:
            if truthy(f["front_page"]):
                set_front(page)
            else:
                page["front_page"] = False
        return dict(page)

    def delete_page(q: Req) -> dict[str, Any]:
        page = find(q.match.group(1))
        if page["front_page"]:
            raise CanvasError(400, "The front page cannot be deleted.")
        del _store(fake)["pages"][page["page_id"]]
        return dict(page)

    fake.route("GET", r"/courses/{c}/pages", list_pages)
    fake.route("POST", r"/courses/{c}/pages", create_page)
    fake.route("PUT", r"/courses/{c}/pages/([^/]+)", update_page)
    fake.route("DELETE", r"/courses/{c}/pages/([^/]+)", delete_page)
    fake.route("GET", r"/courses/{c}/pages/([^/]+)", get_page)
