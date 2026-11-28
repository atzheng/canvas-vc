"""FakeCanvas behaviour for discussion topics and announcements (GET, POST, PUT, DELETE).

State lives in ``fake.state("discussion_topics")["topics"]`` (``{id: topic dict}``). Use
``add_topic`` / ``add_announcement``. ``GET /discussion_topics`` honours
``only_announcements=true``. The body is the ``message`` field, as on Canvas.
POST creates a topic (an announcement with ``is_announcement``) and PUT
updates only the fields sent (JSON or form). DELETE removes the topic (whatever its replies:
the guard is what protects them). Every topic carries ``discussion_subentry_count`` (0 by
default); ``add_reply`` raises it, and ``add_topic(..., discussion_subentry_count=None)``
drops the field to model a response without it. Responses carry the sanitized ``message``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

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
        "discussion_subentry_count": 0,
        **fields,
    }
    if topic["discussion_subentry_count"] is None:
        del topic["discussion_subentry_count"]
    _store(fake)[topic["id"]] = topic
    return topic


def add_announcement(
    fake: FakeCanvas, title: str, message: str = "", **fields: Any
) -> dict[str, Any]:
    return add_topic(fake, title, message, announcement=True, **fields)


def add_reply(fake: FakeCanvas, topic_id: int, n: int = 1) -> None:
    """Seed ``n`` replies (any author) on a topic."""
    topic = _store(fake)[topic_id]
    topic["discussion_subentry_count"] = int(topic.get("discussion_subentry_count", 0)) + n


def register(fake: FakeCanvas) -> None:
    def list_topics(q: Req) -> list[dict[str, Any]]:
        only = str(q.arg("only_announcements", "false")).lower() == "true"
        return [dict(t) for t in _store(fake).values() if t["is_announcement"] or not only]

    fake.route("GET", r"/courses/{c}/discussion_topics", list_topics)
    from tests.fake_canvas.core import CanvasError  # core imports this module

    def truthy(v: Any) -> bool:
        return v in (True, "true", "1")

    def find(q: Req) -> dict[str, Any]:
        topic = _store(fake).get(int(q.match.group(1)))
        if topic is None:
            raise CanvasError(404, "The specified resource does not exist.")
        return topic

    def apply_fields(topic: dict[str, Any], f: dict[str, Any]) -> None:
        if "title" in f:
            topic["title"] = f["title"]
        if "message" in f:
            topic["message"] = fake.sanitize(str(f["message"] or ""))
        if "published" in f:
            topic["published"] = truthy(f["published"])
        if "delayed_post_at" in f:
            topic["delayed_post_at"] = f["delayed_post_at"] or None

    def create_topic(q: Req) -> dict[str, Any]:
        f = dict(q.body)
        if not str(f.get("title") or "").strip():
            raise CanvasError(400, "title is required")
        topic = add_topic(
            fake, str(f["title"]), announcement=truthy(f.get("is_announcement")), published=False
        )
        apply_fields(topic, f)
        if "published" not in f:
            topic["published"] = True
        return dict(topic)

    def update_topic(q: Req) -> dict[str, Any]:
        topic = find(q)
        apply_fields(topic, dict(cast("dict[str, Any]", q.body)))
        return dict(topic)

    def delete_topic(q: Req) -> dict[str, Any]:
        topic = find(q)
        del _store(fake)[topic["id"]]
        return dict(topic)

    def get_topic(q: Req) -> dict[str, Any]:
        return dict(find(q))

    fake.route("GET", r"/courses/{c}/discussion_topics/(\d+)", get_topic)
    fake.route("POST", r"/courses/{c}/discussion_topics", create_topic)
    fake.route("DELETE", r"/courses/{c}/discussion_topics/(\d+)", delete_topic)
    fake.route("PUT", r"/courses/{c}/discussion_topics/(\d+)", update_topic)
