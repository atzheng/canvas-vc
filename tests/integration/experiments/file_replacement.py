"""File-replacement experiment (bead canvas-sync-y01.3, SPEC.md "Replacing files
without breaking links").

RUN BY THE OWNER ONLY, by hand, against the sandbox course. Never from tests or CI.
Every object created is named ``zz-canvas-sync-experiment...`` and is unpublished or
hidden. All HTTP goes through CanvasClient (no allowlist changes).

Usage (see the PR body for the full run sheet)::

    export CANVAS_SANDBOX_URL=https://canvas.ubc.ca CANVAS_SANDBOX_COURSE=198014
    export CANVAS_TOKEN=...   # never printed
    uv run python -m tests.integration.experiments.file_replacement \
        --i-am-the-owner --state ~/scratch/fr-state.json setup
    ... observe | overwrite-api | observe | (web UI overwrite) | observe | report | cleanup
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from canvas_sync.errors import CanvasSyncError
from canvas_sync.remote.http import CanvasClient

REQUIRED_URL = "https://canvas.ubc.ca"
REQUIRED_COURSE = "198014"
PREFIX = "zz-canvas-sync-experiment"
FILE_NAME = f"{PREFIX}.txt"
MODULE_NAME = f"{PREFIX} (delete me)"
PAGE_TITLE = f"{PREFIX} page"
UNLOCK_AT = "2027-01-05T00:00:00Z"
LOCK_AT = "2027-12-31T00:00:00Z"
STEPS = ("setup", "observe", "overwrite-api", "report", "cleanup")


class Refused(SystemExit):
    pass


def check_guard(env: dict[str, str], owner_flag: bool) -> tuple[str, int]:
    """Hard-coded sandbox check. Returns (base_url, course_id) or raises Refused."""
    if not owner_flag:
        raise Refused("refusing: pass --i-am-the-owner (this writes to a real Canvas course)")
    if env.get("CANVAS_SANDBOX_URL", "").rstrip("/") != REQUIRED_URL:
        raise Refused(f"refusing: CANVAS_SANDBOX_URL must be {REQUIRED_URL}")
    if env.get("CANVAS_SANDBOX_COURSE", "") != REQUIRED_COURSE:
        raise Refused(f"refusing: CANVAS_SANDBOX_COURSE must be {REQUIRED_COURSE}")
    if not env.get("CANVAS_TOKEN"):
        raise Refused("refusing: CANVAS_TOKEN is not set")
    return REQUIRED_URL, int(REQUIRED_COURSE)


def assert_in_state(state: dict[str, Any], kind: str, ident: Any) -> None:
    """Cleanup may only touch ids this experiment recorded."""
    recorded = state.get(kind)
    ids = recorded if isinstance(recorded, list) else [recorded]
    if ident is None or str(ident) not in {str(i) for i in ids if i is not None}:  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
        raise Refused(f"refusing: {kind} {ident!r} is not recorded in the state file")


def assert_ours(name: str | None, kind: str, ident: Any) -> None:
    if not (name or "").startswith(PREFIX):
        raise Refused(f"refusing: {kind} {ident} is not named {PREFIX}*")


def load_state(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.write_text(json.dumps(state, indent=2, sort_keys=True))


def file_links(html: str) -> list[str]:
    return re.findall(r'(?:href|src)="([^"]*?/files/\d+[^"]*)"', html)


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()[:12]


def content(version: str) -> bytes:
    return f"{PREFIX} content {version}\n".encode()


_SUMMARY_KEYS = (
    "id",
    "display_name",
    "folder_id",
    "size",
    "hidden",
    "locked",
    "unlock_at",
    "lock_at",
    "hidden_for_user",
    "locked_for_user",
    "visibility_level",
    "modified_at",
    "url",
    "replacement_chain_context_type",
    "replacement_chain_context_id",
)


def _file_summary(f: dict[str, Any]) -> dict[str, Any]:
    return {k: f.get(k) for k in _SUMMARY_KEYS if k in f}


def _try(fn: Any) -> Any:
    try:
        return fn()
    except CanvasSyncError as e:
        return {"error": f"{type(e).__name__}: {str(e)[:120]}"}


def _dl(c: CanvasClient, url: str) -> dict[str, Any]:
    data = c.download(url)
    return {
        "ok": True,
        "bytes": len(data),
        "sha256_12": sha(data),
        "text": data[:60].decode(errors="replace"),
    }


def setup(c: CanvasClient, state_path: Path, state: dict[str, Any]) -> None:
    cid = c.course_id
    folder = f"{PREFIX}-folder"
    f = c.upload_file(folder, FILE_NAME, content("v1"))
    state.update(
        file_ids=[f["id"]],
        original_file_id=f["id"],
        folder_id=f.get("folder_id"),
        folder_path=folder,
    )
    save_state(state_path, state)
    c.put(f"/files/{f['id']}", data={"hidden": "true", "unlock_at": UNLOCK_AT, "lock_at": LOCK_AT})
    mod = c.post(
        f"/courses/{cid}/modules",
        data={"module[name]": MODULE_NAME, "module[published]": "false"},
    )
    state["module_id"] = mod["id"]
    save_state(state_path, state)
    item = c.post(
        f"/courses/{cid}/modules/{mod['id']}/items",
        data={
            "module_item[title]": FILE_NAME,
            "module_item[type]": "File",
            "module_item[content_id]": f["id"],
        },
    )
    state["module_item_id"] = item["id"]
    link = f"/courses/{cid}/files/{f['id']}/download?wrap=1"
    page = c.post(
        f"/courses/{cid}/pages",
        data={
            "wiki_page[title]": PAGE_TITLE,
            "wiki_page[published]": "false",
            "wiki_page[body]": f'<p><a href="{link}">the file</a></p>',
        },
    )
    state["page_url"] = page["url"]
    state["observations"] = {}
    save_state(state_path, state)
    ui = state_path.parent / FILE_NAME
    ui.write_bytes(content("v3-web-ui"))
    print(f"setup done. File to upload in the web UI later: {ui}")


def observe(c: CanvasClient, state_path: Path, state: dict[str, Any], label: str) -> None:
    cid = c.course_id
    snap: dict[str, Any] = {}
    listing = _try(lambda: c.get(f"/folders/{state['folder_id']}/files"))
    if isinstance(listing, list):
        for f in listing:
            if f.get("display_name", "").startswith(PREFIX) and f["id"] not in state["file_ids"]:
                state["file_ids"].append(f["id"])
        snap["folder_files"] = [_file_summary(f) for f in listing]
    else:
        snap["folder_files"] = listing
    snap["files_by_id"] = {}
    snap["old_download"] = {}
    for fid in state["file_ids"]:
        snap["files_by_id"][str(fid)] = _try(
            lambda fid=fid: _file_summary(c.get_one(f"/files/{fid}"))
        )
        snap["old_download"][str(fid)] = _try(
            lambda fid=fid: _dl(c, f"{c.base_url}/files/{fid}/download?download_frd=1")
        )
    mid = state["module_id"]
    items = _try(lambda: c.get(f"/courses/{cid}/modules/{mid}/items"))
    if isinstance(items, list):
        keys = ("id", "type", "content_id", "title", "published")
        snap["module_items"] = [{k: i.get(k) for k in keys} for i in items]
    else:
        snap["module_items"] = items
    page = _try(lambda: c.get_one(f"/courses/{cid}/pages/{state['page_url']}"))
    if isinstance(page, dict) and "body" in page:
        links = file_links(page["body"] or "")
        snap["page_links"] = links
        snap["page_link_fetch"] = {
            ln: _try(lambda ln=ln: _dl(c, ln if ln.startswith("http") else c.base_url + ln))
            for ln in links
        }
    else:
        snap["page_links"] = page
    state.setdefault("observations", {})[label] = snap
    save_state(state_path, state)
    print(json.dumps({label: snap}, indent=2, default=str))


def overwrite_api(c: CanvasClient, state_path: Path, state: dict[str, Any]) -> None:
    f = c.upload_file(state["folder_path"], FILE_NAME, content("v2-api"), on_duplicate="overwrite")
    if f["id"] not in state["file_ids"]:
        state["file_ids"].append(f["id"])
    state["api_overwrite_file_id"] = f["id"]
    save_state(state_path, state)
    print(
        f"overwrite via API: upload returned file id {f['id']} "
        f"(original {state['original_file_id']}); run observe --label after-api."
    )
    print(json.dumps(_file_summary(f), indent=2, default=str))


def report(state: dict[str, Any]) -> None:
    obs: dict[str, Any] = state.get("observations", {})
    orig = str(state.get("original_file_id"))

    def get(label: str, *path: str) -> Any:
        v: Any = obs.get(label, {})
        for p in path:
            v = v.get(p, "") if isinstance(v, dict) else ""
        return v

    for label in ("before", "after-api", "after-ui"):
        if label not in obs:
            continue
        files = get(label, "folder_files")
        cur: dict[str, Any] = files[-1] if isinstance(files, list) and files else {}
        mi = get(label, "module_items")
        rows = [
            ("current file id in folder", cur.get("id", "")),
            ("new id differs from original?", str(cur.get("id")) != orig if cur else ""),
            ("old id GET /files/<old>", json.dumps(get(label, "files_by_id", orig))),
            ("old download URL resolves", json.dumps(get(label, "old_download", orig))),
            (
                "module item content_id",
                [i.get("content_id") for i in mi] if isinstance(mi, list) else mi,
            ),
            ("page link(s) + fetch", json.dumps(get(label, "page_link_fetch"))),
            (
                "hidden/unlock_at/lock_at on current",
                {k: cur.get(k) for k in ("hidden", "unlock_at", "lock_at", "locked")},
            ),
        ]
        print(f"\n== {label} ==")
        for k, v in rows:
            print(f"{k:38} {v}")
    print("\n== owner only (Student View) ==")
    for q in (
        "Student View: file visible in Files list after API overwrite?",
        "Student View: file visible in Files list after web-UI overwrite?",
        "Student View: module item openable after each overwrite?",
        "Student View: page link opens file after each overwrite?",
        "Web UI replace: did Canvas ask rename/replace, and was the id kept?",
    ):
        print(f"{q:70} ____")


def cleanup(c: CanvasClient, state_path: Path, state: dict[str, Any]) -> None:
    cid = c.course_id
    for fid in list(state.get("file_ids", [])):
        assert_in_state(state, "file_ids", fid)
        r = _try(lambda fid=fid: c.get_one(f"/files/{fid}"))
        if isinstance(r, dict) and "error" in r:
            print(f"file {fid}: {r['error']} (already gone?)")
            continue
        assert_ours(r.get("display_name"), "file", fid)
        c.delete(f"/files/{fid}")
        print(f"deleted file {fid}")
    mid = state.get("module_id")
    if mid:
        assert_in_state(state, "module_id", mid)
        m = c.get_one(f"/courses/{cid}/modules/{mid}")
        assert_ours(m.get("name"), "module", mid)
        c.delete(f"/courses/{cid}/modules/{mid}")
        print(f"deleted module {mid}")
    slug = state.get("page_url")
    if slug:
        assert_in_state(state, "page_url", slug)
        p = c.get_one(f"/courses/{cid}/pages/{slug}")
        assert_ours(p.get("title"), "page", slug)
        c.delete(f"/courses/{cid}/pages/{slug}")
        print(f"deleted page {slug}")
    print(
        f"NOT deleted: folder {state.get('folder_id')} ({state.get('folder_path')}); "
        "DELETE /folders is not on the allowlist. Delete it by hand in the Files UI."
    )
    state["cleaned"] = True
    save_state(state_path, state)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="file_replacement")
    ap.add_argument("--i-am-the-owner", action="store_true")
    ap.add_argument("--state", required=True, type=Path, help="JSON state file (outside the repo)")
    ap.add_argument("--label", help="observe label: before | after-api | after-ui")
    ap.add_argument("step", choices=STEPS)
    a = ap.parse_args(argv)
    base, course = check_guard(dict(os.environ), a.i_am_the_owner)
    state_path = a.state.expanduser()
    if a.step == "report":
        report(load_state(state_path))
        return
    if a.step == "setup":
        if state_path.exists():
            raise Refused(f"refusing: {state_path} already exists")
        state: dict[str, Any] = {"course": course}
    else:
        state = load_state(state_path)
        if state.get("course") != course:
            raise Refused("refusing: state file is for a different course")
    with CanvasClient(base, os.environ["CANVAS_TOKEN"], course_id=course, mode="write") as c:
        if a.step == "setup":
            setup(c, state_path, state)
        elif a.step == "observe":
            observe(c, state_path, state, a.label or "before")
        elif a.step == "overwrite-api":
            overwrite_api(c, state_path, state)
        elif a.step == "cleanup":
            cleanup(c, state_path, state)


if __name__ == "__main__":
    main(sys.argv[1:])
