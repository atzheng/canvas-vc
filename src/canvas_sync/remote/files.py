"""Files reader and writer (CONTRACTS.md section 8).

Reader (GET only): walks the Canvas folders under ``files_root`` and maps them
to tree paths ``files/<folder path>/<display_name>``. File contents go into the
``BlobStore``; the entry records their sha256. A file is downloaded again only
when its Canvas ``updated_at`` or ``size`` differ from what was last downloaded.
That is remembered in ``files-index.json`` inside the blob directory (local
cache, never in git): ``FileEntry`` has no room for ``updated_at``, and an
absent or stale cache only costs a download.

Writer (bottom of this file): ``create`` uploads a new file; ``update`` uploads again
only when the content hash changed (``on_duplicate=overwrite``, so Canvas keeps the
replacement chain and itself repoints module items and in-course links) and otherwise
sends a settings-only ``PUT``; ``delete`` removes one file. Every write to an existing
file first runs ``_current_file``: ``GET /files/<old id>`` returns the *current* file of
a replacement chain, so a stale id would otherwise write to a newer version. It refuses
unless Canvas answers with the very id asked for, the file's folder belongs to this
course (``context_type=Course``, ``context_id=course``; a student's submission attachment
lives in a user folder) and the file sits at the path the tree says. The client repeats
the course check before every ``PUT``/``DELETE /files/N``.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from rich.console import Console

from canvas_sync import gitops
from canvas_sync.dates import from_canvas, to_canvas
from canvas_sync.errors import CanvasAPIError, CanvasSyncError, StaleRemote
from canvas_sync.model.base import CanvasObject, Kind, ObjectKey
from canvas_sync.model.files import FileEntry, FileSettings, FilesMeta
from canvas_sync.plan.files import PlacedFile, canvas_settings
from canvas_sync.progress import NullProgress, Progress
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


def read_files_meta(
    client: CanvasClient, ctx: FetchContext, *, progress: Progress | None = None
) -> FilesMeta:
    """Remote files as a ``FilesMeta``: defaults and rules are carried over from
    ``ctx.previous`` (people edit those); ``files`` holds only ids, hashes,
    ``stable_link`` and the settings that differ from what the rules give."""
    prev_meta = ctx.previous.files_meta if ctx.previous else FilesMeta()
    progress = progress or NullProgress()
    progress.phase("files")
    remote, ignored_ids = _list_remote(client, ctx)
    progress.phase("files", len(remote))
    total_bytes = sum(int(r.data.get("size") or 0) for r in remote)
    done_bytes = 0
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
        done_bytes += int(r.data.get("size") or 0)
        progress.bytes(done_bytes, total_bytes)
        progress.advance(label=r.path)
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


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


def _entry(obj: CanvasObject) -> FileEntry:
    if not isinstance(obj, FileEntry):
        raise TypeError(f"expected a FileEntry, got {type(obj).__name__}")
    return obj


def _desired(entry: FileEntry) -> FileSettings:
    """Canvas-level settings the file should have (the differ's effective settings;
    a bare ``FileEntry`` falls back to its own overrides)."""
    if isinstance(entry, PlacedFile):
        return canvas_settings(entry.effective)
    return canvas_settings(
        FileSettings(
            published=entry.published,
            hidden=entry.hidden,
            unlock=entry.unlock or None,
            lock=entry.lock or None,
        )
    )


def _tree_path(entry: FileEntry) -> str:
    return _PREFIX + entry.path


def _folder_parts(ctx: ApplyContext, entry: FileEntry) -> list[str]:
    root = [p for p in ctx.config.files_root.strip().strip("/").split("/") if p]
    return [*root, *_nfc(entry.path).split("/")[:-1]]


def _upload_folder(ctx: ApplyContext, entry: FileEntry) -> str:
    return "/" + "/".join(_folder_parts(ctx, entry))


def _basename(entry: FileEntry) -> str:
    return _nfc(entry.path).rsplit("/", 1)[-1]


def _expected_folder(ctx: ApplyContext, entry: FileEntry) -> str:
    return "/".join([_ROOT_NAME, *_folder_parts(ctx, entry)])


def _content(ctx: ApplyContext, entry: FileEntry) -> bytes:
    """The bytes for ``entry.sha256``: the blob store, else the working tree file; the hash
    is checked either way, so a changed or missing file is never uploaded under the wrong hash."""
    sha = entry.sha256
    data: bytes | None
    try:
        data = ctx.blobs.get(sha)
    except FileNotFoundError:
        data = None
    if data is None:
        local = ctx.blobs.root / _tree_path(entry)
        if local.is_file():
            data = local.read_bytes()
    if data is None or hashlib.sha256(data).hexdigest() != sha:
        raise CanvasSyncError(f"{_tree_path(entry)}: file content for {sha[:8]} is not available")
    return data


def _current_file(client: CanvasClient, ctx: ApplyContext, entry: FileEntry) -> dict[str, Any]:
    """JSON of the file ``entry.canvas_id`` after checking it is safe to write to.

    Refuses (nothing is written) unless Canvas returns this very id, the folder belongs to
    this course, and the file sits at the entry's path."""
    tree = _tree_path(entry)
    if entry.canvas_id is None:
        raise CanvasSyncError(f"{tree}: file has no canvas_id")
    fid = int(entry.canvas_id)
    raw = client.get_one(f"/files/{fid}")
    data = cast("dict[str, Any]", raw) if isinstance(raw, dict) else {}
    got = data.get("id")
    if not isinstance(got, int) or isinstance(got, bool) or got != fid:
        raise StaleRemote(
            f"{tree}: file {fid} was replaced on Canvas (current id {got}); run canvas pull"
        )
    folder_id = data.get("folder_id")
    if not isinstance(folder_id, int) or isinstance(folder_id, bool):
        raise CanvasSyncError(f"{tree}: Canvas gave no folder for file {fid}")
    folder_raw = client.get_one(f"/folders/{folder_id}")
    folder = cast("dict[str, Any]", folder_raw) if isinstance(folder_raw, dict) else {}
    if folder.get("context_type") != "Course" or str(folder.get("context_id")) != str(
        client.course_id
    ):
        raise CanvasSyncError(f"{tree}: file {fid} is not in a folder of this course; not touched")
    name = _nfc(str(data.get("display_name") or data.get("filename") or ""))
    if _nfc(str(folder.get("full_name") or "")) != _expected_folder(ctx, entry) or (
        name != _basename(entry)
    ):
        raise StaleRemote(f"{tree}: file {fid} was moved or renamed on Canvas; run canvas pull")
    return data


def _settings_patch(live: dict[str, Any], want: FileSettings, ctx: ApplyContext) -> dict[str, Any]:
    """Fields of ``PUT /files/N`` that differ between Canvas's ``live`` file and ``want``.
    A date to clear is an explicit null."""
    have = FileSettings(
        published=not bool(live.get("locked")),
        hidden=bool(live.get("hidden")),
        unlock=from_canvas(live.get("unlock_at"), ctx.tz),
        lock=from_canvas(live.get("lock_at"), ctx.tz),
    )
    patch: dict[str, Any] = {}
    if want.published != have.published:
        patch["locked"] = not want.published
    if want.hidden != have.hidden:
        patch["hidden"] = want.hidden
    if want.unlock != have.unlock:
        patch["unlock_at"] = to_canvas(want.unlock or None)
    if want.lock != have.lock:
        patch["lock_at"] = to_canvas(want.lock or None)
    return patch


def _apply_settings(
    client: CanvasClient, ctx: ApplyContext, live: dict[str, Any], entry: FileEntry
) -> None:
    patch = _settings_patch(live, _desired(entry), ctx)
    if patch:
        client.put(f"/files/{int(live['id'])}", json=patch)


def _upload(client: CanvasClient, ctx: ApplyContext, entry: FileEntry) -> dict[str, Any]:
    """Upload ``entry``'s content over the file of the same name and folder; returns Canvas's
    file JSON. Refuses a result stored under another name (Canvas renames on a duplicate it
    did not overwrite)."""
    tree = _tree_path(entry)
    name = _basename(entry)
    result = client.upload_file(
        _upload_folder(ctx, entry), name, _content(ctx, entry), on_duplicate="overwrite"
    )
    fid = result.get("id")
    if not isinstance(fid, int) or isinstance(fid, bool):
        raise CanvasAPIError(0, f"{tree}: upload returned no file id")
    stored = _nfc(str(result.get("display_name") or result.get("filename") or ""))
    if stored != name:
        raise CanvasSyncError(f"{tree}: Canvas stored the upload as {stored!r}; check the course")
    ctx.ids[ObjectKey(Kind.FILE, tree)] = fid
    return result


def _refuse_existing(client: CanvasClient, ctx: ApplyContext, entry: FileEntry) -> None:
    """A new file must not land on a file Canvas already has at that path: the upload
    overwrites by name, so it would replace something this tree never knew about."""
    want = _expected_folder(ctx, entry)
    folders = client.get(f"/courses/{client.course_id}/folders", _PER_PAGE)
    for f in cast("list[dict[str, Any]]", folders):
        if f.get("context_type", "Course") != "Course" or _nfc(str(f.get("full_name"))) != want:
            continue
        for item in cast(
            "list[dict[str, Any]]", client.get(f"/folders/{f['id']}/files", _PER_PAGE)
        ):
            if _nfc(str(item.get("display_name") or "")) == _basename(entry):
                raise StaleRemote(
                    f"{_tree_path(entry)}: a file with this name already exists on Canvas; "
                    "run canvas pull"
                )


def preflight(
    client: CanvasClient, before: CanvasObject | None, after: CanvasObject, ctx: ApplyContext
) -> None:
    """Read-only, before the first write: content is available, an existing file is the
    current one in this course at its path, a new file does not collide."""
    new = _entry(after)
    if before is None:
        _content(ctx, new)
        _refuse_existing(client, ctx, new)
        return
    old = _entry(before)
    if new.sha256 and new.sha256 != old.sha256:
        _content(ctx, new)
    _current_file(client, ctx, old)


def create(client: CanvasClient, obj: CanvasObject, ctx: ApplyContext) -> int | str:
    """Upload a new file; returns its canvas_id. Settings are sent only if Canvas's new file
    differs from the wanted ones."""
    entry = _entry(obj)
    _refuse_existing(client, ctx, entry)
    result = _upload(client, ctx, entry)
    _apply_settings(client, ctx, result, entry)
    return int(result["id"])


def update(
    client: CanvasClient, before: CanvasObject, after: CanvasObject, ctx: ApplyContext
) -> None:
    """Settings-only change: ``PUT /files/<id>`` with the differing fields (id kept).
    Changed content: one upload with ``on_duplicate=overwrite`` (new id, recorded in
    ``ctx.ids``), then a ``PUT`` only if Canvas did not carry the settings over."""
    old, new = _entry(before), _entry(after)
    live = _current_file(client, ctx, old)
    if new.sha256 and new.sha256 != old.sha256:
        live = _upload(client, ctx, new)
    _apply_settings(client, ctx, live, new)


def delete(client: CanvasClient, obj: CanvasObject, ctx: ApplyContext) -> None:
    """Delete one file of this course (CONTRACTS.md section 10: never a submission file: the
    id must be current and in a course folder, else nothing is sent)."""
    entry = _entry(obj)
    live = _current_file(client, ctx, entry)
    client.delete(f"/files/{int(live['id'])}")
