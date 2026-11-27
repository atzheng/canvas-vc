"""FakeCanvas behaviour for modules: GET modules and module items.

State lives in ``fake.state("modules")``: ``modules`` (``{id: module dict}``) and
``items`` (``{module id: [item dict]}``). Use ``add_module`` and ``add_item``; item
fields follow the Canvas API (``type``, ``content_id``, ``page_url``,
``external_url``, ``indent``, ``position`` ...).

Writes: POST/PUT/DELETE of modules and items. Bodies may be JSON (``{"module": {...}}``,
``{"module_item": {...}}``) or form fields (``module[name]``). A ``position`` inserts at
that 1-based place and renumbers the rest (like Canvas's acts_as_list); positions stay
contiguous. Deleting a module deletes its items. A module item PUT with ``module_id``
moves it. Module create ignores ``published`` (new modules start unpublished); PUT sets
it. Prerequisites, completion requirements and sequential progress are never touched.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

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

    def fields(q: Req, name: str) -> dict[str, Any]:
        nested = q.body.get(name)
        if isinstance(nested, dict):
            return dict(cast("dict[str, Any]", nested))
        prefix = f"{name}["
        return {k[len(prefix) : -1]: v for k, v in q.body.items() if k.startswith(prefix)}

    def renumber(rows: list[dict[str, Any]]) -> None:
        for n, row in enumerate(rows, start=1):
            row["position"] = n

    def module_list() -> list[dict[str, Any]]:
        return sorted(_store(fake)["modules"].values(), key=lambda m: m["position"])

    def place(rows: list[dict[str, Any]], row: dict[str, Any], pos: Any) -> None:
        if row in rows:
            rows.remove(row)
        index = len(rows) if pos is None else max(0, min(int(pos) - 1, len(rows)))
        rows.insert(index, row)
        renumber(rows)

    def get_module(mid: int) -> dict[str, Any]:
        mod = _store(fake)["modules"].get(mid)
        if mod is None:
            raise CanvasError(404, "The specified resource does not exist.")
        return mod

    def create_module(q: Req) -> dict[str, Any]:
        f = fields(q, "module")
        if not str(f.get("name") or "").strip():
            raise CanvasError(400, "name is required")
        mod = add_module(fake, str(f["name"]), published=False)
        if f.get("unlock_at"):
            mod["unlock_at"] = f["unlock_at"]
        rows = module_list()
        place(rows, mod, f.get("position"))
        return dict(mod)

    def update_module(q: Req) -> dict[str, Any]:
        mod = get_module(int(q.match.group(1)))
        f = fields(q, "module")
        if "name" in f:
            mod["name"] = f["name"]
        if "unlock_at" in f:
            mod["unlock_at"] = f["unlock_at"] or None
        if "published" in f:
            mod["published"] = f["published"] in (True, "true", "1")
        if f.get("position") is not None:
            place(module_list(), mod, f["position"])
        return dict(mod)

    def delete_module(q: Req) -> dict[str, Any]:
        mid = int(q.match.group(1))
        mod = get_module(mid)
        st = _store(fake)
        del st["modules"][mid]
        st["items"].pop(mid, None)
        renumber(module_list())
        return dict(mod)

    def get_item(mid: int, iid: int) -> dict[str, Any]:
        get_module(mid)
        for row in _store(fake)["items"][mid]:
            if row["id"] == iid:
                return row
        raise CanvasError(404, "The specified resource does not exist.")

    def create_item(q: Req) -> dict[str, Any]:
        mid = int(q.match.group(1))
        get_module(mid)
        f = fields(q, "module_item")
        typ = f.get("type")
        extra: dict[str, Any] = {"indent": int(f.get("indent") or 0)}
        if typ == "Page":
            if not f.get("page_url"):
                raise CanvasError(400, "page_url is required")
            extra["page_url"] = f["page_url"]
            extra["title"] = f.get("title") or str(f["page_url"])
        elif typ in ("Assignment", "File", "Quiz", "Discussion"):
            if not f.get("content_id"):
                raise CanvasError(400, "content_id is required")
            extra["content_id"] = int(f["content_id"])
            extra["title"] = f.get("title") or str(typ)
        elif typ == "SubHeader":
            extra["title"] = f.get("title") or ""
        elif typ == "ExternalUrl":
            if not f.get("external_url"):
                raise CanvasError(400, "external_url is required")
            extra["external_url"] = f["external_url"]
            extra["title"] = f.get("title") or f["external_url"]
            extra["new_tab"] = f.get("new_tab") in (True, "true", "1")
        else:
            raise CanvasError(400, f"unsupported module item type {typ!r}")
        item = add_item(fake, mid, str(typ), published=False, **extra)
        place(_store(fake)["items"][mid], item, f.get("position"))
        return dict(item)

    def update_item(q: Req) -> dict[str, Any]:
        mid, iid = int(q.match.group(1)), int(q.match.group(2))
        item = get_item(mid, iid)
        f = fields(q, "module_item")
        for name in ("title", "external_url"):
            if name in f:
                item[name] = f[name]
        if "indent" in f:
            item["indent"] = int(f["indent"] or 0)
        for name in ("published", "new_tab"):
            if name in f:
                item[name] = f[name] in (True, "true", "1")
        st = _store(fake)
        dest = int(f["module_id"]) if f.get("module_id") is not None else mid
        if dest != mid:
            get_module(dest)
            st["items"][mid].remove(item)
            renumber(st["items"][mid])
            st["items"][dest].append(item)
            item["module_id"] = dest
            st["modules"][mid]["items_count"] = len(st["items"][mid])
            st["modules"][dest]["items_count"] = len(st["items"][dest])
        if f.get("position") is not None or dest != mid:
            place(st["items"][dest], item, f.get("position"))
        return dict(item)

    def delete_item(q: Req) -> dict[str, Any]:
        mid, iid = int(q.match.group(1)), int(q.match.group(2))
        item = get_item(mid, iid)
        rows = _store(fake)["items"][mid]
        rows.remove(item)
        renumber(rows)
        _store(fake)["modules"][mid]["items_count"] = len(rows)
        return dict(item)

    fake.route("POST", r"/courses/{c}/modules", create_module)
    fake.route("PUT", r"/courses/{c}/modules/(\d+)", update_module)
    fake.route("DELETE", r"/courses/{c}/modules/(\d+)", delete_module)
    fake.route("POST", r"/courses/{c}/modules/(\d+)/items", create_item)
    fake.route("PUT", r"/courses/{c}/modules/(\d+)/items/(\d+)", update_item)
    fake.route("DELETE", r"/courses/{c}/modules/(\d+)/items/(\d+)", delete_item)
    fake.route("GET", r"/courses/{c}/modules", list_modules)
    fake.route("GET", r"/courses/{c}/modules/(\d+)/items", list_items)
