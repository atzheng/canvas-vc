"""Changes (`/changes`): the plan push would run, and recent history (UI.md section 6)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from typing import Any

from canvas_sync import gitops
from canvas_sync.commands.diff import build_plan
from canvas_sync.errors import CanvasSyncError
from canvas_sync.plan.core import Action, Operation, Plan
from canvas_sync.ui.layout import Response, badge, esc, link, obj_href, page, table
from canvas_sync.ui.state import State

HISTORY_LIMIT = 10
MAX_PATHS = 8


def _fmt(v: Any) -> str:
    if v is None:
        return "(none)"
    if hasattr(v, "strftime"):
        return str(v.strftime("%Y-%m-%d %H:%M"))
    return " ".join(str(v).split())


def _short(v: Any, n: int = 30) -> str:
    s = _fmt(v)
    return s if len(s) <= n else s[:n] + "…"


def _changes(op: Operation) -> str:
    if op.action in (Action.DELETE, Action.UNPUBLISH):
        return ""
    return "; ".join(f"{f}: {_short(old)} → {_short(new)}" for f, (old, new) in op.changes.items())


def _label(op: Operation) -> str:
    path = op.key.path
    if "#" in path:
        return path.split("#", 1)[1]
    return path.rsplit("/", 1)[-1]


def _op_row(op: Operation) -> list[str]:
    where = esc(op.key.path)
    if op.action is not Action.DELETE:
        where = link(obj_href(op.key.path), op.key.path)
    risks = "<br>".join(
        f'<span class="warn">! {esc(r.message)}</span>'
        + (f" (needs <code>{esc(r.needs_confirm)}</code>)" if r.needs_confirm else "")
        for r in op.risks
    )
    return [
        badge(op.action.value),
        esc(_label(op)),
        esc(_changes(op)),
        where,
        risks,
    ]


def _pending(plan: Plan) -> str:
    n = len(plan.ops)
    out = f"<h2>Pending (canvas/main..HEAD): {n} operation{'' if n == 1 else 's'}</h2>"
    if not plan.ops:
        return out + "<p>In sync: nothing to push.</p>"
    groups: dict[str, list[Operation]] = defaultdict(list)
    for op in plan.ops:
        groups[op.key.kind.value].append(op)
    for kind, ops in groups.items():
        out += f"<h3>{esc(kind)} ({len(ops)})</h3>"
        out += table(["action", "object", "changes", "path", "risk"], (_op_row(o) for o in ops))
    return out


def _commits(commits: list[gitops.CommitInfo], empty: str) -> str:
    if not commits:
        return f"<p>{esc(empty)}</p>"
    rows: list[list[str]] = []
    for c in commits:
        shown = [esc(p) for p in c.paths[:MAX_PATHS]]
        if len(c.paths) > MAX_PATHS:
            shown.append(f"+{len(c.paths) - MAX_PATHS} more")
        rows.append(
            [
                f"<code>{esc(c.sha[:7])}</code>",
                esc(c.time),
                esc(c.subject),
                "<br>".join(shown),
            ]
        )
    return table(["commit", "time", "message", "changed paths"], rows)


def render(state: State, path: str, query: Mapping[str, list[str]]) -> Response:
    root = state.root
    cfg = state.course.config if state.course else None
    body = "<h1>Changes</h1>"
    if state.remote is None:
        body += (
            '<p class="warn">canvas/main: never fetched: run <code>canvas fetch</code></p>'
            "<p>Nothing to compare against yet.</p>"
        )
        return page("Changes", body, current="changes", config=cfg)
    fetched = gitops.last_fetch_time(root)
    when = esc(fetched) if fetched else "-"
    body += f"<p>As of the last fetch ({when}); nothing is read from Canvas.</p>"
    try:
        plan = build_plan(root).plan
    except CanvasSyncError as e:
        body += f'<p class="err">cannot compute the plan: {esc(" ".join(str(e).split()))}</p>'
    else:
        body += _pending(plan)
    changed = gitops.changed_paths(root)
    if changed:
        body += (
            f'<p class="warn">Uncommitted: {len(changed)} changed file'
            f"{'' if len(changed) == 1 else 's'} (not part of the plan; the plan is from "
            "committed HEAD)</p>"
        )
    try:
        unpushed = gitops.log_commits(root, "HEAD", exclude=state.remote, limit=HISTORY_LIMIT)
        history = gitops.log_commits(root, state.remote, limit=HISTORY_LIMIT)
    except CanvasSyncError as e:
        body += f'<p class="err">cannot read history: {esc(" ".join(str(e).split()))}</p>'
    else:
        body += "<h2>Unpushed commits on main</h2>"
        body += _commits(unpushed, "None: everything on main is in canvas/main.")
        body += f"<h2>canvas/main history (last {HISTORY_LIMIT})</h2>"
        body += _commits(history, "No commits.")
    return page("Changes", body, current="changes", config=cfg)
