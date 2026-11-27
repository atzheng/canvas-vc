"""FakeCanvas behaviour for files: GET, upload, PUT, DELETE, and the replacement chain.

Modelled on what the sandbox experiment found (2026-11-27):

- Uploading the same name into the same folder with ``on_duplicate=overwrite`` gives a NEW
  file id; ``hidden``, ``locked``, ``unlock_at`` and ``lock_at`` carry over (``replace_file``).
- ``GET /files/<any old id>`` returns the CURRENT file (Canvas follows the replacement chain),
  whatever the query says. ``follow_chain`` False (in ``fake.state("files")``) models a Canvas
  that does not: old ids are 404. PUT and DELETE resolve ids the same way, so a write by a
  stale id lands on the current file (a trap for writers that do not check the id).
- Canvas itself repoints module items (``content_id``) to the new file and rewrites file links
  in page, assignment and announcement bodies to the new id, adding a fresh ``verifier``
  query parameter. The fake does both in ``replace_file``.

Upload is the 3-step flow: ``POST /courses/<c>/files`` answers with an ``upload_url`` on
another host (``https://uploads.test``) and params; the multipart POST there (an
``upload_target`` route) stores the file and redirects to ``GET /files/<id>/create_success``.
A name collision is overwritten only with ``on_duplicate=overwrite`` (else Canvas renames the
new file ``name-1.ext``). Missing folders of ``parent_folder_path`` are created.
``carry_settings`` False models a Canvas that drops them.
State: ``chain`` (``{old id: new id}``), ``follow_chain``, ``uploads`` (pending uploads).
"""

from __future__ import annotations

import re
from email.parser import BytesParser
from email.policy import HTTP
from typing import TYPE_CHECKING, Any, cast

import httpx

if TYPE_CHECKING:
    from tests.fake_canvas.core import FakeCanvas, Req

UPLOAD_HOST = "https://uploads.test"
_FILE_LINK = re.compile(r"(/files/)(\d+)(?=\D|$)([^\"'\s<>]*)")


def _rewrite_bodies(fake: FakeCanvas, old_id: int, new_id: int) -> None:
    """Point file links in every body at ``new_id`` and give them a fresh ``verifier``."""
    state = fake.state("files")

    def fix(html: Any) -> Any:
        if not isinstance(html, str):
            return html

        def sub(m: re.Match[str]) -> str:
            if int(m.group(2)) != old_id:
                return m.group(0)
            state["verifiers"] = state.get("verifiers", 0) + 1
            rest = re.sub(r"[?&]verifier=[^&\"'\s<>]*", "", m.group(3))
            sep = "&" if "?" in rest else "?"
            return f"{m.group(1)}{new_id}{rest}{sep}verifier=fake{state['verifiers']:04d}"

        return _FILE_LINK.sub(sub, html)

    for page in fake.state("pages").get("pages", {}).values():
        page["body"] = fix(page.get("body"))
    for a in fake.state("assignments").get("assignments", {}).values():
        a["description"] = fix(a.get("description"))
    for t in fake.state("discussion_topics").get("topics", {}).values():
        t["message"] = fix(t.get("message"))


def _repoint_modules(fake: FakeCanvas, old_id: int, new_id: int) -> None:
    for items in fake.state("modules").get("items", {}).values():
        for item in items:
            if item.get("type") == "File" and item.get("content_id") == old_id:
                item["content_id"] = new_id


def replace_file(fake: FakeCanvas, old_id: int, content: bytes, **fields: Any) -> dict[str, Any]:
    """Overwrite file ``old_id`` in place: the old file disappears and a new file with a
    new id takes its folder and name; the chain ``old -> new`` is recorded, module items and
    body links are repointed, and the settings carry over."""
    old = fake.files.pop(old_id)
    fake.file_content.pop(old_id, None)
    carry = fake.state("files").get("carry_settings", True)
    keep = {k: old[k] for k in ("hidden", "locked", "unlock_at", "lock_at") if k in old and carry}
    new = fake.add_file(old["folder_id"], old["display_name"], content, **{**keep, **fields})
    chain: dict[int, int] = fake.state("files").setdefault("chain", {})
    chain[old_id] = new["id"]
    _repoint_modules(fake, old_id, new["id"])
    _rewrite_bodies(fake, old_id, new["id"])
    return new


def _resolve(fake: FakeCanvas, fid: int) -> dict[str, Any] | None:
    found = fake.files.get(fid)
    if found is not None:
        return found
    state = fake.state("files")
    chain: dict[int, int] = state.get("chain", {})
    if state.get("follow_chain", True):
        while fid in chain:
            fid = chain[fid]
            if fid in fake.files:
                return fake.files[fid]
    return None


def _get_file(q: Req) -> dict[str, Any]:
    from tests.fake_canvas.core import CanvasError  # late: core imports this module

    found = _resolve(q.fake, int(q.match.group(1)))
    if found is None:
        raise CanvasError(404, "The specified resource does not exist.")
    return found


def _folder_for(fake: FakeCanvas, path: str) -> dict[str, Any]:
    """The course folder at ``path`` (relative to the course root), created if missing."""
    folder = fake.folders[fake.root_folder_id]
    for part in [p for p in path.strip("/").split("/") if p]:
        nxt = next(
            (
                f
                for f in fake.folders.values()
                if f["parent_folder_id"] == folder["id"]
                and f["name"] == part
                and f["context_type"] == "Course"
            ),
            None,
        )
        folder = nxt or fake.add_folder(part, parent_folder_id=folder["id"])
    return folder


def _start_upload(q: Req) -> dict[str, Any]:
    from tests.fake_canvas.core import CanvasError

    name = q.arg("name")
    if not name or q.arg("size") is None:
        raise CanvasError(400, "name and size are required")
    uploads: dict[str, dict[str, Any]] = q.fake.state("files").setdefault("uploads", {})
    token = f"up{len(uploads) + 1}"
    uploads[token] = {
        "name": str(name),
        "folder": str(q.arg("parent_folder_path", "/")),
        "on_duplicate": str(q.arg("on_duplicate", "overwrite")),
    }
    return {
        "upload_url": f"{UPLOAD_HOST}/fake-upload/{token}",
        "upload_params": {"token": token, "filename": str(name)},
    }


def _unused_name(fake: FakeCanvas, folder_id: int, name: str) -> str:
    taken = {f["display_name"] for f in fake.files.values() if f["folder_id"] == folder_id}
    stem, dot, ext = name.partition(".")
    n = 1
    while name in taken:
        name = f"{stem}-{n}{dot}{ext}"
        n += 1
    return name


def _upload_target(q: Req) -> httpx.Response:
    from tests.fake_canvas.core import CanvasError

    token = q.match.group(1)
    info = q.fake.state("files").get("uploads", {}).get(token)
    if info is None:
        raise CanvasError(404, "unknown upload")
    request = q.entry.request
    if "Authorization" in request.headers:
        raise CanvasError(400, "credentials sent to the upload host")
    msg = BytesParser(policy=HTTP).parsebytes(
        b"Content-Type: " + request.headers["content-type"].encode() + b"\r\n\r\n" + request.content
    )
    content = b""
    for part in msg.iter_parts():
        if part.get_filename() is not None:
            content = cast("bytes", part.get_payload(decode=True))
    fake = q.fake
    folder = _folder_for(fake, info["folder"])
    same = next(
        (
            f
            for f in fake.files.values()
            if f["folder_id"] == folder["id"] and f["display_name"] == info["name"]
        ),
        None,
    )
    if same is not None and info["on_duplicate"] == "overwrite":
        new = replace_file(fake, same["id"], content)
    else:
        name = _unused_name(fake, folder["id"], info["name"])
        new = fake.add_file(folder["id"], name, content)
    return httpx.Response(
        301, headers={"Location": f"{fake.base_url}/api/v1/files/{new['id']}/create_success?uuid=x"}
    )


def _set_fields(q: Req, f: dict[str, Any]) -> None:
    from tests.fake_canvas.core import CanvasError

    for name in ("hidden", "locked", "unlock_at", "lock_at"):
        if name in q.body:
            f[name] = q.body[name]
        elif f"{name}" in q.params:
            f[name] = q.params[name]
    if "name" in q.body:
        raise CanvasError(400, "the fake does not rename files")


def _put_file(q: Req) -> dict[str, Any]:
    from tests.fake_canvas.core import CanvasError

    found = _resolve(q.fake, int(q.match.group(1)))
    if found is None:
        raise CanvasError(404, "The specified resource does not exist.")
    _set_fields(q, found)
    return found


def _delete_file(q: Req) -> dict[str, Any]:
    from tests.fake_canvas.core import CanvasError

    found = _resolve(q.fake, int(q.match.group(1)))
    if found is None:
        raise CanvasError(404, "The specified resource does not exist.")
    fid = found["id"]
    q.fake.files.pop(fid)
    q.fake.file_content.pop(fid, None)
    for items in q.fake.state("modules").get("items", {}).values():
        items[:] = [
            i for i in items if not (i.get("type") == "File" and i.get("content_id") == fid)
        ]
    return found


def register(fake: FakeCanvas) -> None:
    """Add this resource's routes with `fake.route(method, regex, handler)`; see core.py."""
    fake.route("GET", r"/files/(\d+)", _get_file)
    fake.route("GET", r"/files/(\d+)/create_success", _get_file)
    fake.route("POST", r"/courses/{c}/files", _start_upload)
    fake.route("POST", r"/fake-upload/(\w+)", _upload_target, upload_target=True)
    fake.route("PUT", r"/files/(\d+)", _put_file)
    fake.route("DELETE", r"/files/(\d+)", _delete_file)
