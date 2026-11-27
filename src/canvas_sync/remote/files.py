"""Files reader and writer (CONTRACTS.md section 8).

Reader (GET only): walks the Canvas folders under ``files_root`` and maps them
to tree paths ``files/<folder path>/<display_name>``. File contents go into the
``BlobStore``; the entry records their sha256. A file is downloaded again only
when its Canvas ``updated_at`` or ``size`` differ from what was last downloaded.
That is remembered in ``files-index.json`` inside the blob directory (local
cache, never in git): ``FileEntry`` has no room for ``updated_at``, and an
absent or stale cache only costs a download.
"""

from __future__ import annotations

import json
import os
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from rich.console import Console

from canvas_sync import gitops
from canvas_sync.dates import from_canvas
from canvas_sync.errors import CanvasAPIError
from canvas_sync.model.base import CanvasObject
from canvas_sync.model.files import FileEntry, FileSettings, FilesMeta
from canvas_sync.remote.context import ApplyContext, FetchContext
from canvas_sync.remote.http import CanvasClient

INDEX_NAME = "files-index.json"
_PREFIX = "files/"
_ROOT_NAME = "course files"
_PER_PAGE = {"per_page": 100}


@dataclass(frozen=True)
class _Remote:
    data: dict[str, Any]
    path: str  # tree path


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def fold_settings(remote: FileSettings, base: FileSettings) -> dict[str, Any]:
    """Entry overrides needed so that ``base`` plus them gives ``remote``.

    A setting that equals what defaults and rules already give is left out
    (None = inherit). A missing date over a rule's date becomes ``False``. An
    unset ``published``/``hidden`` in ``base`` counts as the Canvas default
    (published, not hidden).
    """
    out: dict[str, Any] = {}
    for name, canvas_default in (("published", True), ("hidden", False)):
        want = getattr(remote, name)
        have = getattr(base, name)
        if want != (canvas_default if have is None else have):
            out[name] = want
    for name in ("unlock", "lock"):
        want = getattr(remote, name)
        have = getattr(base, name)
        if want != have:
            out[name] = False if want is None else want
    return out


def _remote_settings(data: dict[str, Any], ctx: FetchContext) -> FileSettings:
    return FileSettings(
        published=not bool(data.get("locked")),
        hidden=bool(data.get("hidden")),
        unlock=from_canvas(data.get("unlock_at"), ctx.tz),
        lock=from_canvas(data.get("lock_at"), ctx.tz),
    )


def _folder_paths(folders: list[dict[str, Any]], root: str) -> dict[int, str]:
    """``{folder id: path relative to files/}`` for the folders under ``root``
    (the ``files_root`` setting; ``/`` is the course's top folder)."""
    wanted = [p for p in root.strip().strip("/").split("/") if p]
    base = "/".join([_ROOT_NAME, *wanted])
    out: dict[int, str] = {}
    for f in folders:
        full = str(f.get("full_name") or "")
        if full == base:
            out[int(f["id"])] = ""
        elif full.startswith(base + "/"):
            out[int(f["id"])] = full[len(base) + 1 :]
    return out


# ---------------------------------------------------------------------------
# Download cache
# ---------------------------------------------------------------------------


def _index_path(ctx: FetchContext) -> Path:
    return gitops.blob_dir(ctx.blob_store.root) / INDEX_NAME


def _load_index(path: Path) -> dict[str, dict[str, Any]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    items = cast("dict[Any, Any]", raw)
    return {str(k): cast("dict[str, Any]", v) for k, v in items.items() if isinstance(v, dict)}


def _save_index(path: Path, index: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(index, f, sort_keys=True, indent=1)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------


def _warn(msg: str) -> None:
    Console(stderr=True).print(f"warning: {msg}", markup=False, highlight=False)


def _list_remote(client: CanvasClient, ctx: FetchContext) -> tuple[list[_Remote], set[int]]:
    """Remote files (NFC tree paths, ignored ones left out) and the ids of ignored files.

    Names that cannot be stored (bad name, or a case-insensitive collision with
    another file) are skipped with a warning; of a colliding pair the lowest id wins.
    """
    folders = client.get(f"/courses/{client.course_id}/folders", _PER_PAGE)
    paths = _folder_paths(
        [f for f in folders if f.get("context_type", "Course") == "Course"],
        ctx.config.files_root,
    )
    cands: list[_Remote] = []
    ignored_ids: set[int] = set()
    for folder_id in sorted(paths, key=lambda i: paths[i]):
        prefix = paths[folder_id]
        for f in client.get(f"/folders/{folder_id}/files", _PER_PAGE):
            name = str(f.get("display_name") or f.get("filename") or "")
            rel = _nfc(f"{prefix}/{name}" if prefix else name)
            tree = _PREFIX + rel
            if not name or "/" in name or "_meta.yaml" in tree.split("/"):
                _warn(f"{tree}: Canvas file name cannot be stored in the tree; skipped")
                continue
            if ctx.ignore.ignored(tree):
                ignored_ids.add(int(f["id"]))
                continue
            cands.append(_Remote(f, tree))
    found: list[_Remote] = []
    seen: dict[str, str] = {}
    for r in sorted(cands, key=lambda r: int(r.data["id"])):
        folded = r.path.casefold()
        if folded in seen:
            _warn(f"{r.path}: collides with {seen[folded]} (case-insensitive); skipped")
            continue
        seen[folded] = r.path
        found.append(r)
    return found, ignored_ids


def _match_previous(
    client: CanvasClient, remote: list[_Remote], previous: dict[str, FileEntry]
) -> dict[str, FileEntry]:
    """Previous entry for each remote tree path: by canvas_id, then by following
    the replacement chain of previous ids that vanished, then by path."""
    by_remote_id = {int(r.data["id"]): r for r in remote}
    matched: dict[str, FileEntry] = {}
    leftover: list[FileEntry] = []
    for entry in previous.values():
        try:
            cid = int(entry.canvas_id) if entry.canvas_id is not None else None
        except ValueError:
            cid = None
        if cid is not None and cid in by_remote_id:
            matched[by_remote_id[cid].path] = entry
        else:
            leftover.append(entry)
    claims: dict[str, list[tuple[bool, str, FileEntry]]] = {}
    for entry in sorted(leftover, key=lambda e: _nfc(e.path)):
        if entry.canvas_id is None:
            continue
        try:
            new = client.get_one(
                f"/files/{entry.canvas_id}",
                {
                    "replacement_chain_context_type": "course",
                    "replacement_chain_context_id": client.course_id,
                },
            )
        except CanvasAPIError as e:
            if e.status == 404:
                continue
            raise
        new_id = cast("dict[str, Any]", new).get("id") if isinstance(new, dict) else None
        hit = by_remote_id.get(int(new_id)) if new_id is not None else None
        if hit is not None and hit.path not in matched:
            same = _nfc(_PREFIX + entry.path) == hit.path
            claims.setdefault(hit.path, []).append((not same, _nfc(entry.path), entry))
    for path, found in claims.items():
        matched[path] = min(found, key=lambda t: (t[0], t[1]))[2]  # same path, then lowest path
    by_path = {_nfc(_PREFIX + e.path): e for e in leftover}
    used = {id(e) for e in matched.values()}
    for r in remote:
        e = by_path.get(r.path)
        if r.path not in matched and e is not None and id(e) not in used:
            matched[r.path] = e
            used.add(id(e))
    return matched


def read_files_meta(client: CanvasClient, ctx: FetchContext) -> FilesMeta:
    """Remote files as a ``FilesMeta``: defaults and rules are carried over from
    ``ctx.previous`` (people edit those); ``files`` holds only ids, hashes,
    ``stable_link`` and the settings that differ from what the rules give."""
    prev_meta = ctx.previous.files_meta if ctx.previous else FilesMeta()
    remote, ignored_ids = _list_remote(client, ctx)
    matched = _match_previous(client, remote, prev_meta.files)
    index_file = _index_path(ctx)
    index = _load_index(index_file)
    new_index: dict[str, dict[str, Any]] = {
        i: index[i] for i in map(str, ignored_ids) if i in index
    }
    files: dict[str, FileEntry] = {}
    base_meta = FilesMeta(defaults=prev_meta.defaults, rules=prev_meta.rules)
    for r in remote:
        fid = int(r.data["id"])
        stamp = {"updated_at": r.data.get("updated_at"), "size": r.data.get("size")}
        prev = matched.get(r.path)
        cached = index.get(str(fid), {})
        sha = ""
        if (
            prev is not None
            and prev.sha256
            and ctx.blob_store.has(prev.sha256)
            and cached.get("sha256") == prev.sha256
            and {k: cached.get(k) for k in stamp} == stamp
            and stamp["updated_at"] is not None
        ):
            sha = prev.sha256
        else:
            url = r.data.get("url") or f"{client.base_url}/files/{fid}/download"
            sha = ctx.blob_store.put(client.download(str(url)))
        new_index[str(fid)] = {**stamp, "sha256": sha}
        overrides = fold_settings(_remote_settings(r.data, ctx), base_meta.rule_settings(r.path))
        files[r.path] = FileEntry(
            canvas_id=fid,
            path=r.path.removeprefix(_PREFIX),
            sha256=sha,
            stable_link=prev.stable_link if prev is not None else False,
            **overrides,
        )
    _save_index(index_file, new_index)
    return FilesMeta(defaults=prev_meta.defaults, rules=prev_meta.rules, files=files)


def read(client: CanvasClient, ctx: FetchContext) -> dict[str, CanvasObject]:
    """``{tree path: FileEntry}``; use ``read_files_meta`` for the full ``FilesMeta``."""
    return dict(read_files_meta(client, ctx).files)


def create(client: CanvasClient, obj: CanvasObject, ctx: ApplyContext) -> int | str:
    """Returns the new canvas_id."""
    raise NotImplementedError


def update(
    client: CanvasClient, before: CanvasObject, after: CanvasObject, ctx: ApplyContext
) -> None:
    """Only changed fields are sent."""
    raise NotImplementedError


def delete(client: CanvasClient, obj: CanvasObject, ctx: ApplyContext) -> None:
    """Goes through the submission guard."""
    raise NotImplementedError
