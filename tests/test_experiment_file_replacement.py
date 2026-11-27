"""Offline guard tests for the sandbox file-replacement experiment script."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from canvas_sync.remote.http import CanvasClient
from tests.integration.experiments import file_replacement as fr

GOOD = {
    "CANVAS_SANDBOX_URL": "https://canvas.ubc.ca",
    "CANVAS_SANDBOX_COURSE": "198014",
    "CANVAS_TOKEN": "x",
}


def test_guard_accepts_sandbox() -> None:
    assert fr.check_guard(GOOD, True) == ("https://canvas.ubc.ca", 198014)


@pytest.mark.parametrize(
    "env,flag",
    [
        ({**GOOD, "CANVAS_SANDBOX_COURSE": "151648"}, True),
        ({**GOOD, "CANVAS_SANDBOX_URL": "https://example.com"}, True),
        (GOOD, False),
        ({k: v for k, v in GOOD.items() if k != "CANVAS_TOKEN"}, True),
    ],
)
def test_guard_refuses(env: dict[str, str], flag: bool) -> None:
    with pytest.raises(fr.Refused):
        fr.check_guard(env, flag)


def test_cleanup_refuses_unrecorded_ids() -> None:
    state = {"file_ids": [1, 2], "module_id": 5, "page_url": "zz-x"}
    fr.assert_in_state(state, "file_ids", 2)
    with pytest.raises(fr.Refused):
        fr.assert_in_state(state, "file_ids", 3)
    with pytest.raises(fr.Refused):
        fr.assert_in_state(state, "module_id", 6)
    with pytest.raises(fr.Refused):
        fr.assert_ours("My real page", "page", "x")


def test_cleanup_deletes_nothing_when_name_wrong(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.path))
        if req.url.path == "/api/v1/files/7":
            return httpx.Response(200, json={"id": 7, "folder_id": 3, "display_name": "real.pdf"})
        if req.url.path == "/api/v1/folders/3":
            return httpx.Response(200, json={"context_type": "Course", "context_id": 198014})
        return httpx.Response(404, json={})

    sp = tmp_path / "s.json"
    state = {"file_ids": [7], "course": 198014}
    sp.write_text(json.dumps(state))
    c = CanvasClient(
        "https://canvas.ubc.ca",
        "t",
        course_id=198014,
        mode="write",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(fr.Refused):
        fr.cleanup(c, sp, state)
    assert not any(m == "DELETE" for m, _ in calls)
