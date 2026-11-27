"""File metadata model: ``files/_meta.yaml`` (CONTRACTS.md section 4, SPEC "Files").

``_meta.yaml`` has three sections::

    defaults: {published: true}            # settings for every file
    rules:                                 # globs, later rules win
      - {match: "lectures/*.pdf", hidden: true}
    files:                                 # per-file: ids, hashes, exact overrides
      lectures/01.pdf: {canvas_id: 5551212, sha256: "..."}

Paths under ``files:`` and in ``match`` are relative to ``files/``. In memory,
``FilesMeta.files`` is keyed by *tree path* (``files/lectures/01.pdf``), and
``FileEntry.path`` is the path relative to ``files/``.

Glob semantics of ``match`` (gitignore-like; kept self-contained here)
are documented on ``glob_match``.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict

from canvas_sync.config import CourseConfig
from canvas_sync.dates import LocalDT, parse_local
from canvas_sync.errors import ValidationError
from canvas_sync.model.base import CanvasObject, Kind
from canvas_sync.model.serialize import dump_yaml, load_yaml

META_PATH = "files/_meta.yaml"
_PREFIX = "files/"
_SETTING_FIELDS = ("published", "hidden", "unlock", "lock")
_DATE_FIELDS = ("unlock", "lock")

# One order for every mapping in the document (top level, rules, entries).
KEY_ORDER = [
    "defaults",
    "rules",
    "files",
    "match",
    "canvas_id",
    "sha256",
    "published",
    "hidden",
    "unlock",
    "lock",
    "stable_link",
]
_SHA256 = re.compile(r"[0-9a-f]{64}")
_TOP_KEYS = frozenset({"defaults", "rules", "files"})
_ENTRY_KEYS = frozenset({"canvas_id", "sha256", "stable_link", *_SETTING_FIELDS})


# Date setting: a datetime, None = inherit from the layer below, False = explicitly
# "no date" (overrides earlier layers; ``effective`` turns it into None).
DateSetting = LocalDT | Literal[False] | None


class FileEntry(CanvasObject):
    """Per-file record. ``published/hidden/unlock/lock`` are overrides on top of
    what defaults and rules give (None = no override)."""

    path: str = ""  # relative to files/
    sha256: str = ""
    published: bool | None = None
    hidden: bool | None = None
    unlock: DateSetting = None
    lock: DateSetting = None
    stable_link: bool = False


class FileSettings(BaseModel):
    """Effective settings for one file after defaults, rules and its own entry."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    published: bool | None = None
    hidden: bool | None = None
    unlock: DateSetting = None
    lock: DateSetting = None


class FileRule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    match: str = ""
    settings: FileSettings = FileSettings()


def _resolve(s: FileSettings) -> FileSettings:
    """Turn the explicit "no date" (False) into None."""
    return s.model_copy(update={f: None for f in _DATE_FIELDS if getattr(s, f) is False})


def _overlay(base: FileSettings, over: FileSettings | FileEntry) -> FileSettings:
    """``base`` with every non-None setting of ``over`` applied."""
    update = {f: getattr(over, f) for f in _SETTING_FIELDS if getattr(over, f) is not None}
    return base.model_copy(update=update)


def _nfc(s: str) -> str:
    """Unicode NFC, so macOS (NFD) file names match patterns and keys."""
    return unicodedata.normalize("NFC", s)


# ---------------------------------------------------------------------------
# Glob matching
# ---------------------------------------------------------------------------


def _class_body(body: str) -> str:
    """Regex class body for glob ``[..]`` members: every character escaped, ranges
    validated (``re.error`` for ``z-a`` or ``a--b``)."""
    out: list[str] = []
    i = 0
    while i < len(body):
        c = body[i]
        if i + 2 < len(body) and body[i + 1] == "-":
            hi = body[i + 2]
            if hi < c:
                raise re.error(f"bad character range {c}-{hi}")
            out.append(f"{re.escape(c)}-{re.escape(hi)}")
            i += 3
        else:
            out.append(re.escape(c))
            i += 1
    return "".join(out)


def _translate(pattern: str) -> str:
    """Regex source for one glob."""
    out: list[str] = []
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            j = i
            while j < n and pattern[j] == "*":
                j += 1
            at_start = i == 0 or pattern[i - 1] == "/"
            if j - i >= 2 and at_start and pattern[j : j + 1] == "/":
                out.append("(?:.*/)?")  # "**/": zero or more directories
                i = j + 1
                continue
            if j - i >= 2 and at_start and j == n:
                out.append(".*")  # trailing "/**" (or a bare "**"): everything
            else:
                out.append("[^/]*")  # any other run of stars is a plain "*"
            i = j
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            neg = pattern[i + 1 : i + 2] in ("!", "^")
            first = i + 2 if neg else i + 1  # a ']' right after '[' / '[!' is a member
            end = pattern.find("]", first + 1)
            if end == -1:
                out.append(re.escape(c))
                i += 1
            else:
                body = _class_body(pattern[first:end])
                out.append(f"[^/{body}]" if neg else f"(?!/)[{body}]")
                i = end + 1
        elif c == "\\" and i + 1 < n:
            out.append(re.escape(pattern[i + 1]))
            i += 2
        else:
            out.append(re.escape(c))
            i += 1
    return "".join(out)


@lru_cache(maxsize=1024)
def _compile(pattern: str) -> tuple[re.Pattern[str], bool, bool]:
    """``(regex, anchored, dir_only)`` for a glob."""
    dir_only = pattern.endswith("/") and not pattern.endswith("\\/")
    if dir_only:
        pattern = pattern.rstrip("/")
    anchored = "/" in pattern
    pattern = pattern.lstrip("/")
    try:
        regex = re.compile(_translate(pattern) + r"\Z", re.DOTALL)
    except re.error as e:
        raise ValueError(f"invalid glob {pattern!r}: {e}") from None
    return regex, anchored, dir_only


def glob_match(pattern: str, rel_path: str) -> bool:
    """Does glob ``pattern`` select the file at ``rel_path`` (relative to ``files/``)?

    gitignore-like, case-sensitive, ``/`` separators:

    * ``*`` matches any run of characters except ``/``; ``?`` one character
      except ``/``; ``[abc]`` / ``[!abc]`` a character class (never ``/``);
      ``\\x`` is a literal ``x``.
    * ``**/`` matches zero or more directories, a trailing ``/**`` matches
      everything inside; elsewhere ``**`` is just ``*``.
    * A pattern with no ``/`` (other than a trailing one) matches the *name*
      of the file or of any parent directory, at any depth: ``*.pdf`` selects
      ``a/b/c.pdf``. A pattern containing ``/`` is anchored at ``files/``:
      ``lectures/*.pdf`` selects ``lectures/01.pdf`` but not
      ``x/lectures/01.pdf`` or ``lectures/old/01.pdf``.
    * A pattern that matches a directory selects every file inside it:
      ``old`` and ``old/`` both select ``old/a/b.pdf``. A trailing ``/``
      requires a directory (it never selects a file named like that).
    """
    regex, anchored, dir_only = _compile(_nfc(pattern))
    parts = _nfc(rel_path).split("/")
    last = len(parts) - 1
    for i, part in enumerate(parts):
        if dir_only and i == last:
            break
        if anchored:
            if regex.match("/".join(parts[: i + 1])):
                return True
        elif regex.match(part):
            return True
    return False


# ---------------------------------------------------------------------------
# FilesMeta
# ---------------------------------------------------------------------------


class FilesMeta(BaseModel):
    """Contents of files/_meta.yaml. ``files`` is keyed by tree path."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    defaults: FileSettings = FileSettings()
    rules: list[FileRule] = []
    files: dict[str, FileEntry] = {}

    def _rule_layers(self, path: str) -> FileSettings:
        """Defaults and matching rules layered, False dates kept (internal)."""
        if not path.startswith(_PREFIX):
            raise ValueError(f"{path!r} is not a tree path under {_PREFIX}")
        rel = _nfc(path).removeprefix(_PREFIX)
        s = self.defaults
        for rule in self.rules:
            if glob_match(rule.match, rel):
                s = _overlay(s, rule.settings)
        return s

    def rule_settings(self, path: str) -> FileSettings:
        """Defaults, then every matching rule in order; not the per-file entry."""
        return _resolve(self._rule_layers(path))

    def effective(self, path: str) -> FileSettings:
        """Settings of the file at tree path ``path`` (``files/a/b.pdf``; ValueError
        without the ``files/`` prefix): defaults, then matching rules in order
        (later wins), then its per-file entry. A None in a later layer inherits;
        ``False`` for ``unlock``/``lock`` clears an earlier date and comes back as None."""
        s = self._rule_layers(path)
        entry = self.files.get(_nfc(path))
        if entry is None:
            entry = self.files.get(path)
        return _resolve(_overlay(s, entry) if entry is not None else s)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _fail(msg: str) -> ValidationError:
    return ValidationError(f"{META_PATH}: {msg}")


def _mapping(v: object, where: str) -> dict[str, Any]:
    if v is None:
        return {}
    if not isinstance(v, dict):
        raise _fail(f"{where} must be a mapping")
    m = cast("dict[Any, Any]", v)
    for k in m:
        if not isinstance(k, str):
            raise _fail(f"{where}: key {k!r} is not a string")
    return cast("dict[str, Any]", m)


def _check_keys(m: dict[str, Any], allowed: frozenset[str], where: str) -> None:
    unknown = sorted(set(m) - allowed)
    if unknown:
        raise _fail(f"{where}: unknown key {unknown[0]!r}")


def _settings(m: dict[str, Any], cfg: CourseConfig, where: str) -> FileSettings:
    values: dict[str, Any] = {}
    for f in ("published", "hidden"):
        v = m.get(f)
        if v is not None and not isinstance(v, bool):
            raise _fail(f"{where}: {f} must be true or false")
        values[f] = v
    # date-only values: unlock gets the course unlock time, lock the due time
    times: dict[str, time] = {
        "unlock": cfg.defaults.unlock_time,
        "lock": cfg.defaults.assignment.due_time,
    }
    for f, default in times.items():
        v = m.get(f)
        if v is None or v is False:
            values[f] = v
            continue
        if v is True:
            raise _fail(f"{where}: {f} must be a date or false")
        try:
            values[f] = parse_local(v, cfg.tz, default)
        except ValidationError as e:
            raise _fail(f"{where}: {f}: {e}") from None
    return FileSettings(**values)


def _check_rel_path(p: str) -> None:
    parts = p.split("/")
    if (
        not p
        or p.startswith("/")
        or "\\" in p
        or any(x in ("", ".", "..") for x in parts)
        or "_meta.yaml" in parts
    ):
        raise _fail(f"files: invalid file path {p!r}")


def _entry(rel: str, m: dict[str, Any], cfg: CourseConfig) -> FileEntry:
    where = f"files.{rel}"
    _check_keys(m, _ENTRY_KEYS, where)
    cid = m.get("canvas_id")
    if cid is not None and (isinstance(cid, bool) or not isinstance(cid, int) or cid < 0):
        raise _fail(f"{where}: canvas_id must be a non-negative integer")
    sha = m.get("sha256")
    if sha is not None and not (isinstance(sha, str) and _SHA256.fullmatch(sha)):
        raise _fail(f"{where}: sha256 must be 64 lowercase hex characters")
    link = m.get("stable_link")
    if link is not None and not isinstance(link, bool):
        raise _fail(f"{where}: stable_link must be true or false")
    s = _settings(m, cfg, where)
    return FileEntry(
        canvas_id=cid,
        path=rel,
        sha256=sha or "",
        stable_link=bool(link),
        **s.model_dump(),
    )


def parse_files_meta(text: str, cfg: CourseConfig) -> FilesMeta:
    """Parse ``_meta.yaml`` text (tolerant of date-only values, key order, CRLF)."""
    top = _mapping(load_yaml(text, META_PATH), "document")
    _check_keys(top, _TOP_KEYS, "document")
    raw_defaults = _mapping(top.get("defaults"), "defaults")
    _check_keys(raw_defaults, frozenset(_SETTING_FIELDS), "defaults")
    defaults = _settings(raw_defaults, cfg, "defaults")
    raw_rules = top.get("rules")
    if raw_rules is not None and not isinstance(raw_rules, list):
        raise _fail("rules must be a list")
    rules: list[FileRule] = []
    for i, r in enumerate(cast("list[Any]", raw_rules or [])):
        where = f"rules[{i}]"
        m = _mapping(r, where)
        _check_keys(m, frozenset({"match", *_SETTING_FIELDS}), where)
        match = m.get("match")
        if not isinstance(match, str) or not match.strip():
            raise _fail(f"{where}: match must be a non-empty string")
        try:
            _compile(_nfc(match))
        except ValueError:
            raise _fail(f"{where}: match: invalid glob {match!r}") from None
        rules.append(FileRule(match=match, settings=_settings(m, cfg, where)))
    files: dict[str, FileEntry] = {}
    folded: dict[str, str] = {}
    ids: dict[int, str] = {}
    for raw_rel, v in _mapping(top.get("files"), "files").items():
        _check_rel_path(raw_rel)
        rel = _nfc(raw_rel)
        if rel.casefold() in folded:
            raise _fail(f"files.{rel}: collides with {folded[rel.casefold()]} (case-insensitive)")
        folded[rel.casefold()] = rel
        entry = _entry(rel, _mapping(v, f"files.{rel}"), cfg)
        if entry.canvas_id is not None:
            cid = int(entry.canvas_id)
            if cid in ids:
                raise _fail(f"files.{rel}: duplicate canvas_id {cid} (also in {ids[cid]})")
            ids[cid] = rel
        files[_PREFIX + rel] = entry
    return FilesMeta(defaults=defaults, rules=rules, files=files)


# ---------------------------------------------------------------------------
# Dumping
# ---------------------------------------------------------------------------


def _settings_data(s: FileSettings) -> dict[str, Any]:
    return {f: getattr(s, f) for f in _SETTING_FIELDS if getattr(s, f) is not None}


def _entry_data(meta: FilesMeta, tree_path: str, e: FileEntry) -> dict[str, Any]:
    """ids, hash, stable_link and only the settings that differ from the rules."""
    base = meta.rule_settings(tree_path)
    data: dict[str, Any] = {}
    if e.canvas_id is not None:
        data["canvas_id"] = e.canvas_id
    if e.sha256:
        data["sha256"] = e.sha256
    for f in _SETTING_FIELDS:
        v = getattr(e, f)
        if v is None:
            continue
        # compare what the entry resolves to; False ("no date") only matters over a rule date
        if (None if f in _DATE_FIELDS and v is False else v) != getattr(base, f):
            data[f] = v
    if e.stable_link:
        data["stable_link"] = True
    return data


def render_files_meta(meta: FilesMeta) -> str:
    """Canonical ``_meta.yaml`` text; empty string when there is nothing to say."""
    doc: dict[str, Any] = {}
    defaults = _settings_data(meta.defaults)
    if defaults:
        doc["defaults"] = defaults
    if meta.rules:
        doc["rules"] = [{"match": r.match, **_settings_data(r.settings)} for r in meta.rules]
    if meta.files:
        files: dict[str, Any] = {}
        for tree_path in sorted(meta.files):
            if not tree_path.startswith(_PREFIX):
                raise ValueError(f"file entry key {tree_path!r} is not a tree path under files/")
            files[_nfc(tree_path).removeprefix(_PREFIX)] = _entry_data(
                meta, tree_path, meta.files[tree_path]
            )
        doc["files"] = files
    return dump_yaml(doc, KEY_ORDER) if doc else ""


def _with_disk_hashes(meta: FilesMeta, root: Path) -> FilesMeta:
    """Make ``meta`` reflect the actual bytes under ``files/``.

    A file on disk gets ``sha256`` of its contents (overriding the recorded one, so an
    edited file shows up as a change) and an entry if ``_meta.yaml`` has none (a new
    file). Entries for files that are not on disk keep their recorded hash.
    """
    base = root / "files"
    if not base.is_dir():
        return meta
    entries = dict(meta.files)
    for f in sorted(base.rglob("*")):
        if f.is_symlink() or not f.is_file() or f.name == ".DS_Store":
            continue
        rel = _nfc(f.relative_to(base).as_posix())
        if rel == "_meta.yaml":
            continue
        key = _PREFIX + rel
        sha = hashlib.sha256(f.read_bytes()).hexdigest()
        old = entries.get(key)
        entries[key] = (
            FileEntry(path=rel, sha256=sha)
            if old is None
            else old.model_copy(update={"sha256": sha})
        )
    return meta.model_copy(update={"files": entries})


class _Spec:
    kind = Kind.FILE
    folder = "files"

    def load_files_meta(self, root: Path, cfg: CourseConfig) -> FilesMeta:
        path = root / META_PATH
        meta = (
            parse_files_meta(path.read_text(encoding="utf-8"), cfg)
            if path.exists()
            else FilesMeta()
        )
        return _with_disk_hashes(meta, root)

    def dump_files_meta(self, meta: FilesMeta, cfg: CourseConfig) -> dict[str, bytes]:
        text = render_files_meta(meta)
        return {META_PATH: text.encode("utf-8")} if text else {}

    def load(self, root: Path, cfg: CourseConfig) -> dict[str, CanvasObject]:
        return dict(self.load_files_meta(root, cfg).files)

    def dump(self, objs: dict[str, CanvasObject], cfg: CourseConfig) -> dict[str, bytes]:
        files = {p: o for p, o in objs.items() if isinstance(o, FileEntry)}
        return self.dump_files_meta(FilesMeta(files=files), cfg)


FILE_SPEC = _Spec()
