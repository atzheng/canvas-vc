"""Differ for files (CONTRACTS.md sections 8 and 9; SPEC.md "Replacing files without
breaking links").

Files are paired by tree path (``files/<rel>``). A path only in the target is a CREATE
(an upload); only in the base, a DELETE. A paired file becomes

- an UPLOAD (with ``before``) when its ``sha256`` changed: the writer uploads once with
  ``on_duplicate=overwrite`` and reapplies settings if Canvas did not carry them over.
  This carries a ``FILE_ID_CHANGE`` risk (informational, no flag): Canvas gives the new
  upload a new id;
- an UPDATE when only the effective settings changed (the id is kept);
- nothing otherwise. A different ``canvas_id`` on a paired path, and edits to ``_meta.yaml``
  that do not change a file's effective settings, are ignored.

A file moved to another path (same ``canvas_id``, old path gone) raises ``ValidationError``:
moving is not supported, and planning it as DELETE + CREATE would remove the file from
every module that holds it.

``after`` and ``before`` are ``PlacedFile`` objects: the entry plus its tree path and its
*effective* settings (defaults and rules applied), which the writer needs and the bare
``FileEntry`` (overrides only) does not carry.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

from canvas_sync.errors import ValidationError
from canvas_sync.model.base import Kind, ObjectKey
from canvas_sync.model.files import FileEntry, FileSettings, FilesMeta
from canvas_sync.plan.core import Action, Operation, Risk, RiskKind

if TYPE_CHECKING:
    from canvas_sync.model.course import Course

_SETTING_FIELDS = ("published", "hidden", "unlock", "lock")

# Deleting more files than this, or every file the course has (two or more), needs
# ``--confirm-delete-files`` (e.g. ``files/`` and ``_meta.yaml`` removed by mistake).
MASS_DELETE_LIMIT = 5

ID_CHANGE_MESSAGE = (
    "new upload gets a new Canvas file id; Canvas repoints module items and links inside "
    "the course, but links to the old /files/<id> URL outside the course may stop working"
)


class PlacedFile(FileEntry):
    """A file entry as an operation's ``before``/``after``: ``key_path`` is its tree path
    and ``effective`` its settings after defaults and rules."""

    key_path: str = ""
    effective: FileSettings = FileSettings()


def canvas_settings(s: FileSettings) -> FileSettings:
    """``s`` with Canvas's own defaults filled in (published, not hidden, no dates)."""
    return FileSettings(
        published=True if s.published is None else s.published,
        hidden=False if s.hidden is None else s.hidden,
        unlock=s.unlock or None,
        lock=s.lock or None,
    )


def _placed(meta: FilesMeta, path: str, entry: FileEntry) -> PlacedFile:
    data = entry.model_dump()
    return PlacedFile(key_path=path, effective=canvas_settings(meta.effective(path)), **data)


def _short(sha: str | None) -> str | None:
    return sha[:8] if sha else None


def _setting_changes(old: FileSettings, new: FileSettings) -> dict[str, tuple[object, object]]:
    return {
        f: (getattr(old, f), getattr(new, f))
        for f in _SETTING_FIELDS
        if getattr(old, f) != getattr(new, f)
    }


def _check_moves(base: FilesMeta, target: FilesMeta) -> None:
    by_id = {e.canvas_id: p for p, e in base.files.items() if e.canvas_id is not None}
    for path, entry in sorted(target.files.items()):
        if path in base.files or entry.canvas_id is None:
            continue
        old = by_id.get(entry.canvas_id)
        if old is not None and old not in target.files:
            raise ValidationError(
                f"{path}: moving or renaming a file is not supported (it is {old} on Canvas); "
                "delete it and add it under the new name, or restore the old path"
            )


def diff_type(base: Course, target: Course) -> list[Operation]:
    b, t = base.files_meta, target.files_meta
    _check_moves(b, t)
    ops: list[Operation] = []
    for path in sorted(set(b.files) | set(t.files)):
        key = ObjectKey(Kind.FILE, path)
        old, new = b.files.get(path), t.files.get(path)
        if old is None and new is not None:
            if not new.sha256:
                raise ValidationError(f"{path}: new file has no content on disk")
            after = _placed(t, path, new.model_copy(update={"canvas_id": None}))
            changes: dict[str, tuple[object, object]] = {"content": (None, _short(new.sha256))}
            changes.update(_setting_changes(canvas_settings(FileSettings()), after.effective))
            ops.append(Operation(key, Action.CREATE, None, after, changes))
        elif new is None and old is not None:
            if old.canvas_id is None:
                continue  # never on Canvas: nothing to delete
            ops.append(Operation(key, Action.DELETE, _placed(b, path, old), None, {}))
        elif old is not None and new is not None:
            before, after = _placed(b, path, old), _placed(t, path, new)
            # the id Canvas knows is the base one, whatever the target file says
            after = after.model_copy(update={"canvas_id": old.canvas_id})
            content = bool(new.sha256) and old.sha256 != new.sha256
            settings = _setting_changes(before.effective, after.effective)
            if not content and not settings:
                continue
            changes = {}
            risks: tuple[Risk, ...] = ()
            if content:
                changes["content"] = (_short(old.sha256), _short(new.sha256))
                risks = (Risk(RiskKind.FILE_ID_CHANGE, ID_CHANGE_MESSAGE, None),)
            changes.update(settings)
            action = Action.UPLOAD if content else Action.UPDATE
            ops.append(Operation(key, action, before, after, changes, risks))
    deletes = [o for o in ops if o.action is Action.DELETE]
    if len(deletes) > MASS_DELETE_LIMIT or (len(deletes) > 1 and len(deletes) == len(b.files)):
        risk = Risk(
            RiskKind.FILES_DELETED,
            f"{len(deletes)} files would be deleted from Canvas",
            "--confirm-delete-files",
        )
        ops = [
            dataclasses.replace(o, risks=(risk,)) if o.action is Action.DELETE else o for o in ops
        ]
    return ops
