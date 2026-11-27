"""FakeCanvas behaviour for discussion topics and announcements: GET list.

State lives in ``fake.state("discussion_topics")["topics"]`` (``{id: topic dict}``). Use
``add_topic`` / ``add_announcement``. ``GET /discussion_topics`` honours
``only_announcements=true``. The body is the ``message`` field, as on Canvas. Writes are
not modelled yet.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tests.fake_canvas.core import FakeCanvas, Req


def _store(fake: FakeCanvas) -> dict[int, dict[str, Any]]:
    return fake.state("discussion_topics").setdefault("topics", {})


def add_topic(
    fake: FakeCanvas, title: str, message: str = "", *, announcement: bool = False, **fields: Any
) -> dict[str, Any]:
    topic: dict[str, Any] = {
        "id": fake.next_id(),
        "title": title,
        "message": fake.sanitize(message),
        "is_announcement": announcement,
        "published": True,
        "delayed_post_at": None,
        "posted_at": "2026-01-01T00:00:00Z",
        **fields,
    }
    _store(fake)[topic["id"]] = topic
    return topic


def add_announcement(
    fake: FakeCanvas, title: str, message: str = "", **fields: Any
) -> dict[str, Any]:
    return add_topic(fake, title, message, announcement=True, **fields)


def register(fake: FakeCanvas) -> None:
    def list_topics(q: Req) -> list[dict[str, Any]]:
        only = str(q.arg("only_announcements", "false")).lower() == "true"
        return [dict(t) for t in _store(fake).values() if t["is_announcement"] or not only]

    fake.route("GET", r"/courses/{c}/discussion_topics", list_topics)
