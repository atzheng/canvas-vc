"""Overview (`/`): what is this repo, is it healthy, is it in sync (UI.md section 1)."""

from __future__ import annotations

from collections.abc import Mapping

from canvas_sync import gitops
from canvas_sync.commands.validate import Issue, validate_tree
from canvas_sync.errors import CanvasSyncError
from canvas_sync.model.base import Kind
from canvas_sync.model.course import Course
from canvas_sync.ui.layout import Response, badge, error_page, esc, link, obj_href, page, table
from canvas_sync.ui.state import State

_KINDS = (
    (Kind.PAGE, "Pages"),
    (Kind.ASSIGNMENT, "Assignments"),
    (Kind.ANNOUNCEMENT, "Announcements"),
    (Kind.FILE, "Files"),
    (Kind.MODULE, "Modules"),
)


def _published(course: Course, kind: Kind, path: str, value: bool | None) -> bool:
    if kind is Kind.FILE:
        value = course.files_meta.effective(path).published
    return course.config.defaults.published if value is None else value


def counts(course: Course) -> list[tuple[str, int, int]]:
    """(label, published, unpublished) per kind, in display order."""
    out: list[tuple[str, int, int]] = []
    for kind, label in _KINDS:
        pub = unpub = 0
        if kind is Kind.MODULE:
            states = [_published(course, kind, "", m.published) for m in course.modules]
        else:
            states = [
                _published(course, kind, k.path, getattr(o, "published", None))
                for k, o in course.objects.items()
                if k.kind is kind
            ]
        pub = sum(states)
        unpub = len(states) - pub
        out.append((label, pub, unpub))
    return out


def _short(sha: str | None) -> str:
    return esc(sha[:7]) if sha else "-"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _sync(state: State) -> str:
    root = state.root
    rows = ["<h2>Sync</h2>"]
    if state.remote is None:
        rows.append(
            '<p class="warn">canvas/main: never fetched: run <code>canvas fetch</code></p>'
            f"<p>HEAD: <code>{_short(state.head)}</code></p>"
        )
    else:
        ahead, behind = gitops.ahead_behind(root)
        fetched = gitops.last_fetch_time(root)
        rows.append(
            f"<p>Last fetch: {esc(fetched) if fetched else '-'} &nbsp; "
            f"canvas/main: <code>{_short(state.remote)}</code> &nbsp; "
            f"HEAD: <code>{_short(state.head)}</code></p>"
            f"<p>{_plural(ahead, 'commit')} not pushed, {behind} behind canvas/main</p>"
        )
    op = gitops.operation_in_progress(root)
    if op:
        rows.append(f'<p class="warn">{badge(op, "warning")} in progress</p>')
    changed = gitops.changed_paths(root)
    rows.append(f"<p>Working tree: {_plural(len(changed), 'changed file')}</p>")
    conflicts = gitops.conflict_marker_paths(root)
    if conflicts:
        rows.append(f'<p class="bad">[!] conflict markers in: {", ".join(map(esc, conflicts))}</p>')
    return "".join(rows)


def _issue_row(issue: Issue, course: Course | None) -> list[str]:
    paths: set[str] = {k.path for k in course.objects} if course else set()
    where = link(obj_href(issue.path), issue.path) if issue.path in paths else esc(issue.path)
    return [badge(issue.severity, issue.severity), where, esc(issue.message)]


def _validate(state: State) -> str:
    issues = validate_tree(state.root)
    errors = sum(i.severity == "error" for i in issues)
    warnings = len(issues) - errors
    out = f"<h2>Validate</h2><p>{_plural(errors, 'error')}, {_plural(warnings, 'warning')}</p>"
    if issues:
        out += table(["", "path", "message"], (_issue_row(i, state.course) for i in issues))
    return out


def _course_block(state: State) -> str:
    c = state.course
    if c is None:
        return f'<h1>Course</h1><p class="err">{esc(state.load_error)}</p>'
    d = c.config.defaults
    url = str(c.config.canvas_url)
    return (
        f"<h1>{esc(f'Course {c.config.course_id}')}</h1>"
        f"<p>{link(url, url)} &middot; <code>course.yaml</code></p>"
        f"<p>id {esc(c.config.course_id)} &middot; timezone {esc(c.config.timezone)} &middot; "
        f"defaults: published {esc(str(d.published).lower())}, "
        f"unlock_time {esc(d.unlock_time)}</p>"
    )


def render(state: State, path: str, query: Mapping[str, list[str]]) -> Response:
    try:
        gitops.repo_root(state.root)
    except CanvasSyncError as e:
        return error_page(str(e))
    body = _course_block(state)
    if state.course is not None:
        rows = [[esc(label), str(p + u), str(p), str(u)] for label, p, u in counts(state.course)]
        body += table(["Objects", "total", "published", "unpublished"], rows)
    body += _sync(state) + _validate(state)
    return page("Overview", body, current="overview", config=state.course and state.course.config)
