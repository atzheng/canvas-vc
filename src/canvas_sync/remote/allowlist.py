"""Endpoints canvas-sync may call (CONTRACTS.md section 7), safety layer 1.

Never widen this without saying why in the PR body; reviewers check it every
time. Entries are exact (METHOD, path template) pairs, matched with
``re.fullmatch`` (ASCII-only, so ``\\d`` is 0-9) against a *normalised* path
relative to ``/api/v1``. ``{c}`` is replaced by the client's own course id, so
a client can never reach another course.
"""

from __future__ import annotations

import re
from functools import cache
from urllib.parse import unquote

from canvas_sync.errors import ForbiddenEndpoint

_COURSE_READ = (
    "pages|assignments|assignment_groups|discussion_topics|files|folders|modules|"
    "sections|groups|group_categories|users|enrollments"
)

ALLOWED: list[tuple[str, str]] = [  # (METHOD, path template; {c} = course id)
    # --- reads: the tool mirrors these resource types ---
    ("GET", r"/courses/{c}"),
    ("GET", rf"/courses/{{c}}/({_COURSE_READ})(/[^/]+)*"),
    ("GET", r"/courses/{c}/late_policy"),  # the submission guard needs it
    ("GET", r"/folders/\d+(/[^/]+)*"),
    ("GET", r"/files/\d+"),
    ("GET", r"/files/\d+/create_success"),  # upload step 3 (confirm), see CanvasClient
    ("GET", r"/users/self"),
    # --- creates ---
    ("POST", r"/courses/{c}/pages"),
    ("POST", r"/courses/{c}/assignments"),
    ("POST", r"/courses/{c}/assignment_groups"),
    ("POST", r"/courses/{c}/discussion_topics"),
    ("POST", r"/courses/{c}/modules"),
    ("POST", r"/courses/{c}/modules/\d+/items"),
    ("POST", r"/courses/{c}/assignments/\d+/overrides"),
    ("POST", r"/courses/{c}/files"),  # upload step 1
    ("POST", r"/folders/\d+/files"),  # upload step 1 (into a folder)
    ("POST", r"/courses/{c}/folders"),
    # --- updates ---
    ("PUT", r"/courses/{c}/pages/[^/]+"),
    ("PUT", r"/courses/{c}/assignments/\d+"),
    ("PUT", r"/courses/{c}/assignments/\d+/overrides/\d+"),
    ("PUT", r"/courses/{c}/discussion_topics/\d+"),
    ("PUT", r"/courses/{c}/modules/\d+"),
    ("PUT", r"/courses/{c}/modules/\d+/items/\d+"),
    ("PUT", r"/files/\d+"),
    # --- deletes (assignment deletes are further gated by the live guard) ---
    ("DELETE", r"/courses/{c}/pages/[^/]+"),
    ("DELETE", r"/courses/{c}/assignments/\d+"),
    ("DELETE", r"/courses/{c}/assignments/\d+/overrides/\d+"),
    ("DELETE", r"/courses/{c}/modules/\d+"),
    ("DELETE", r"/courses/{c}/modules/\d+/items/\d+"),
    ("DELETE", r"/files/\d+"),
]

# Non-API GET paths (relative to the host root) that `CanvasClient.download` may fetch
# from the Canvas host itself. Other hosts (signed CDN URLs) get no credentials.
DOWNLOAD_PATHS: list[str] = [
    r"/files/\d+/download",
    r"/courses/{c}/files/\d+/download",
]

# Defence in depth: for any non-GET call these path segments are refused even if a
# future ALLOWED entry were to match them.
_WRITE_DENIED_SEGMENTS = frozenset(
    {"submissions", "quizzes", "new_quizzes", "quiz_submissions", "enrollments"}
)
_DELETE_DENIED = [
    r"/courses/[^/]+",  # deleting / concluding a course
    r"/courses/[^/]+/discussion_topics/[^/]+",  # announcements are never deleted
    r"/courses/[^/]+/assignment_groups/[^/]+",  # groups are never deleted
]


_RESOURCE_PARENTS = frozenset({"courses", "assignments", "sections", "users", "quizzes", "folders"})


def _hits_denied_resource(path: str) -> bool:
    """True if a denied name sits in a *resource position*: a top-level segment, or
    right after ``/<parent>/<id>/``. Page slugs (``/pages/quizzes``) are not resources."""
    segs = path.split("/")[1:]
    for i, seg in enumerate(segs):
        if seg.lower() not in _WRITE_DENIED_SEGMENTS:
            continue
        if i == 0:
            return True
        if i >= 2 and segs[i - 2].lower() in _RESOURCE_PARENTS and segs[i - 1].isdigit():
            return True
    return False


def normalize_path(path: str, *, strip_api_prefix: bool = True) -> str:
    """Return the canonical decoded path, or raise ForbiddenEndpoint.

    The result is what gets matched *and* what gets sent, so the allowlist
    decision applies to exactly the bytes on the wire. Rejects: query strings,
    fragments, encoded slashes/backslashes, double encoding, control characters,
    empty / ``.`` / ``..`` segments. One trailing slash is dropped.
    """
    if not path.startswith("/") or path.startswith("//"):
        raise ForbiddenEndpoint(f"refusing malformed path {path!r}")
    if "?" in path or "#" in path:
        raise ForbiddenEndpoint(f"refusing path with query or fragment {path!r}")
    low = path.lower()
    if "%2f" in low or "%5c" in low:
        raise ForbiddenEndpoint(f"refusing path with encoded separator {path!r}")
    decoded = unquote(path)
    if "%" in decoded or "\\" in decoded or any(ord(ch) < 32 or ord(ch) == 127 for ch in decoded):
        raise ForbiddenEndpoint(f"refusing path with suspicious characters {path!r}")
    if strip_api_prefix and (decoded == "/api/v1" or decoded.startswith("/api/v1/")):
        decoded = decoded[len("/api/v1") :] or "/"
    segments = decoded.split("/")[1:]
    if len(segments) > 1 and segments[-1] == "":
        segments.pop()
    if segments != [""] and any(seg in ("", ".", "..") for seg in segments):
        raise ForbiddenEndpoint(f"refusing non-canonical path {path!r}")
    return "/" + "/".join(segments)


@cache
def _rules(course_id: int) -> list[tuple[str, re.Pattern[str]]]:
    c = str(int(course_id))
    return [(m, re.compile(p.replace("{c}", c), re.ASCII)) for m, p in ALLOWED]


@cache
def _download_rules(course_id: int) -> list[re.Pattern[str]]:
    c = str(int(course_id))
    return [re.compile(p.replace("{c}", c), re.ASCII) for p in DOWNLOAD_PATHS]


def assert_allowed(method: str, path: str, course_id: int) -> str:
    """Check ``(method, path)`` for a client bound to ``course_id``.

    ``path`` must already be normalised. Returns it, or raises ForbiddenEndpoint.
    """
    method = method.upper()
    if method != "GET":
        if _hits_denied_resource(path):
            raise ForbiddenEndpoint(f"{method} {path} is never allowed")
        if method == "DELETE" and any(re.fullmatch(p, path, re.ASCII) for p in _DELETE_DENIED):
            raise ForbiddenEndpoint(f"{method} {path} is never allowed")
    for m, rx in _rules(course_id):
        if m == method and rx.fullmatch(path):
            return path
    raise ForbiddenEndpoint(f"{method} {path} is not on the allowlist for course {course_id}")


def assert_download_allowed(path: str, course_id: int) -> str:
    """Same-host, non-API file download paths (``path`` normalised, no API prefix strip)."""
    for rx in _download_rules(course_id):
        if rx.fullmatch(path):
            return path
    raise ForbiddenEndpoint(f"GET {path} is not an allowed download path for course {course_id}")
