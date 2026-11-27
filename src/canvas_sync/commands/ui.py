"""canvas ui: serve the read-only, offline course explorer (CONTRACTS.md section 13)."""

from __future__ import annotations

import webbrowser
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from canvas_sync import gitops
from canvas_sync.errors import CanvasSyncError
from canvas_sync.ui.server import make_server


def ui(
    port: Annotated[int, typer.Option(help="Port on 127.0.0.1 (0 picks a free one)")] = 8765,
    no_open: Annotated[bool, typer.Option("--no-open", help="Do not open a browser")] = False,
) -> None:
    """Explore the course repo in a browser (read-only, offline, no token)."""
    console = Console()
    try:
        root = gitops.repo_root(Path.cwd())
        server = make_server(root, port)
    except (CanvasSyncError, OSError) as e:
        console.print(str(e), style="red", markup=False, highlight=False, soft_wrap=True)
        raise typer.Exit(1) from None
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    console.print(
        f"Serving {root} at {url} (Ctrl-C to stop)", markup=False, highlight=False, soft_wrap=True
    )
    if not no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
