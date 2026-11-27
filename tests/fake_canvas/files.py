"""FakeCanvas behaviour for files: GET with a configurable replacement chain.

Canvas keeps a replacement chain when a file is overwritten (same folder and name):
the old id stops existing, and ``GET /files/<old>`` with ``replacement_chain_context_type``
and ``replacement_chain_context_id`` returns the replacement. ``replace_file`` models
that overwrite; the writer bead's upload route can call it. State lives in
``fake.state("files")``: ``chain`` (``{old id: new id}``) and ``follow_chain`` (default
True; set False to model a Canvas that does not follow the chain, so old ids are 404).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tests.fake_canvas.core import FakeCanvas, Req


def replace_file(fake: FakeCanvas, old_id: int, content: bytes, **fields: Any) -> dict[str, Any]:
    """Overwrite file ``old_id`` in place: the old file disappears and a new file with a
    new id takes its folder and name; the chain ``old -> new`` is recorded."""
    old = fake.files.pop(old_id)
    fake.file_content.pop(old_id, None)
    keep = {k: old[k] for k in ("hidden", "locked", "unlock_at", "lock_at") if k in old}
    new = fake.add_file(old["folder_id"], old["display_name"], content, **{**keep, **fields})
    chain: dict[int, int] = fake.state("files").setdefault("chain", {})
    chain[old_id] = new["id"]
    return new


def _get_file(q: Req) -> dict[str, Any]:
    from tests.fake_canvas.core import CanvasError  # late: core imports this module

    fid = int(q.match.group(1))
    found = q.fake.files.get(fid)
    if found is not None:
        return found
    state = q.fake.state("files")
    chain: dict[int, int] = state.get("chain", {})
    asks = q.params.get("replacement_chain_context_type") == "course" and str(
        q.params.get("replacement_chain_context_id")
    ) == str(q.fake.course_id)
    if state.get("follow_chain", True) and asks:
        while fid in chain:
            fid = chain[fid]
            if fid in q.fake.files:
                return q.fake.files[fid]
    raise CanvasError(404, "The specified resource does not exist.")


def register(fake: FakeCanvas) -> None:
    """Add this resource's routes with `fake.route(method, regex, handler)`; see core.py."""
    fake.route("GET", r"/files/(\d+)", _get_file)
