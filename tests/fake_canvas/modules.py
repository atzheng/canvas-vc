"""FakeCanvas behaviour for modules: GET modules and module items.

State lives in ``fake.state("modules")``: ``modules`` (``{id: module dict}``) and
``items`` (``{module id: [item dict]}``). Use ``add_module`` and ``add_item``; item
fields follow the Canvas API (``type``, ``content_id``, ``page_url``,
``external_url``, ``indent``, ``position`` ...). Writes are not modelled yet.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tests.fake_canvas.core import FakeCanvas, Req


def _store(fake: FakeCanvas) -> dict[str, Any]:
    st = fake.state("modules")
    st.setdefault("modules", {})
    st.setdefault("items", {})
    return st


def add_module(fake: FakeCanvas, name: str, **fields: Any) -> dict[str, Any]:
    st = _store(fake)
    mods: dict[int, dict[str, Any]] = st["modules"]
    mod: dict[str, Any] = {
        "id": fake.next_id(),
        "name": name,
        "position": len(mods) + 1,
        "unlock_at": None,
        "published": True,
        "require_sequential_progress": False,
        "prerequisite_module_ids": [],
        "completion_requirements": [],
        "items_count": 0,
        **fields,
    }
    mods[mod["id"]] = mod
    st["items"][mod["id"]] = []
    return mod


def add_item(fake: FakeCanvas, module_id: int, type: str, **fields: Any) -> dict[str, Any]:
    st = _store(fake)
    items: list[dict[str, Any]] = st["items"][module_id]
    item: dict[str, Any] = {
        "id": fake.next_id(),
        "module_id": module_id,
        "position": len(items) + 1,
        "title": fields.pop("title", type),
        "indent": 0,
        "type": type,
        "published": True,
        **fields,
    }
    items.append(item)
    st["modules"][module_id]["items_count"] = len(items)
    return item


def register(fake: FakeCanvas) -> None:
    from tests.fake_canvas.core import CanvasError  # core imports this module

    def list_modules(q: Req) -> list[dict[str, Any]]:
        return [dict(m) for m in _store(fake)["modules"].values()]

    def list_items(q: Req) -> list[dict[str, Any]]:
        mid = int(q.match.group(1))
        st = _store(fake)
        if mid not in st["modules"]:
            raise CanvasError(404, "The specified resource does not exist.")
        return [dict(i) for i in st["items"][mid]]

    fake.route("GET", r"/courses/{c}/modules", list_modules)
    fake.route("GET", r"/courses/{c}/modules/(\d+)/items", list_items)
