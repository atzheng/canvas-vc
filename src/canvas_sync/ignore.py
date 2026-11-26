""".canvasignore matching (CONTRACTS.md section 11).

gitignore-style globs over tree paths (POSIX, relative to the tree root):
blank lines and ``#`` comments are skipped, ``!`` negates, a trailing ``/``
matches directories only, a pattern with a ``/`` in the middle (or a leading
one) is anchored to the root, otherwise it matches at any depth. ``*`` and
``?`` do not cross ``/``; ``**`` does. If a parent directory is ignored, so is
everything under it (it cannot be re-included). Tree paths of Markdown objects
also match with the ``.md`` suffix dropped, so ``pages/scratch`` ignores
``pages/scratch.md`` (the form SPEC.md uses).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

IGNORE_FILE = ".canvasignore"


@dataclass(frozen=True)
class _Rule:
    regex: re.Pattern[str]
    negate: bool
    dir_only: bool


def _translate(pat: str) -> str:
    """Glob (already stripped of ``!``, anchors and trailing slash) -> regex source."""
    out: list[str] = []
    i, n = 0, len(pat)
    while i < n:
        c = pat[i]
        if c == "\\" and i + 1 < n:
            out.append(re.escape(pat[i + 1]))
            i += 2
        elif c == "*":
            if pat.startswith("**", i):
                j = i
                while j < n and pat[j] == "*":
                    j += 1
                at_start = i == 0 or pat[i - 1] == "/"
                if at_start and j < n and pat[j] == "/":
                    out.append("(?:.*/)?")  # "**/" : zero or more directories
                    i = j + 1
                    continue
                if at_start and j == n:
                    out.append(".*")  # trailing "/**"
                    i = j
                    continue
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            j = i + 1
            if j < n and pat[j] in "!^":
                j += 1
            if j < n and pat[j] == "]":
                j += 1
            while j < n and pat[j] != "]":
                j += 1
            if j >= n:
                out.append(re.escape(c))
                i += 1
            else:
                body = pat[i + 1 : j].replace("\\", "\\\\")
                if body[:1] in ("!", "^"):
                    body = "^" + body[1:]
                out.append(f"[{body}]")
                i = j + 1
        else:
            out.append(re.escape(c))
            i += 1
    return "".join(out)


def _parse_line(line: str) -> _Rule | None:
    line = line.rstrip("\r\n")
    # trailing spaces are dropped unless escaped
    stripped = line.rstrip(" ")
    if stripped.endswith("\\") and len(stripped) < len(line):
        stripped += " "
    line = stripped
    if not line or line.startswith("#"):
        return None
    negate = line.startswith("!")
    if negate:
        line = line[1:]
    dir_only = line.endswith("/")
    line = line.rstrip("/")
    if not line:
        return None
    anchored = "/" in line
    line = line.lstrip("/")
    if not line:
        return None
    src = _translate(line)
    regex = re.compile(("^" if anchored else "^(?:.*/)?") + src + "$", re.DOTALL)
    return _Rule(regex, negate, dir_only)


def _escape(path: str) -> str:
    out = re.sub(r"([*?\[\]\\])", r"\\\1", path)
    if out[:1] in ("#", "!"):
        out = "\\" + out
    if out.endswith(" "):
        out = out[:-1] + "\\ "
    return "/" + out  # anchored to the root


class IgnoreRules:
    def __init__(self, root: Path | None = None, lines: list[str] | None = None) -> None:
        self._root = root
        self._rules: list[_Rule] = []
        for line in lines or []:
            rule = _parse_line(line)
            if rule is not None:
                self._rules.append(rule)

    @classmethod
    def load(cls, root: Path) -> IgnoreRules:
        path = root / IGNORE_FILE
        try:
            text = path.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            text = ""
        return cls(root, text.splitlines())

    def _matches(self, candidate: str, is_dir: bool) -> bool | None:
        """Last matching rule wins; None if no rule matches."""
        result: bool | None = None
        for rule in self._rules:
            if rule.dir_only and not is_dir:
                continue
            if rule.regex.match(candidate):
                result = not rule.negate
        return result

    def ignored(self, path: str) -> bool:
        parts = [p for p in path.strip("/").split("/") if p]
        for i in range(len(parts)):
            is_dir = i < len(parts) - 1
            cand = "/".join(parts[: i + 1])
            hit = self._matches(cand, is_dir)
            if not is_dir and hit is None and cand.endswith(".md"):
                hit = self._matches(cand[:-3], is_dir)
            if hit:
                return True  # an ignored directory hides everything beneath it
        return False

    def add(self, path: str) -> None:
        """Append an anchored rule for ``path`` to ``.canvasignore`` (idempotent)."""
        if self._root is None:
            raise ValueError("IgnoreRules.add needs rules loaded from a tree")
        if self.ignored(path):
            return
        line = _escape(path.strip("/"))
        file = self._root / IGNORE_FILE
        existing = file.read_text(encoding="utf-8") if file.exists() else ""
        sep = "" if not existing or existing.endswith("\n") else "\n"
        file.write_text(f"{existing}{sep}{line}\n", encoding="utf-8")
        rule = _parse_line(line)
        if rule is not None:
            self._rules.append(rule)
