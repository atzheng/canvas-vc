"""Typer app: registers commands/* as `canvas <command>`."""

from __future__ import annotations

import typer

from canvas_sync.commands import clone, diff, fetch, open, pull, push, status, ui, validate

app = typer.Typer(
    name="canvas",
    help="Keep a Canvas course in sync with a folder of Markdown/YAML files.",
    no_args_is_help=True,
    add_completion=False,
)

app.command("clone")(clone.clone)
app.command("fetch")(fetch.fetch)
app.command("pull")(pull.pull)
app.command("push")(push.push)
app.command("status")(status.status)
app.command("diff")(diff.diff)
app.command("validate")(validate.validate)
app.command("open")(open.open_)
app.command("ui")(ui.ui)
