"""Shared pytest setup.

Registers the `integration` marker behaviour: those tests are skipped unless
CANVAS_SANDBOX_URL, CANVAS_SANDBOX_COURSE and CANVAS_TOKEN are all set.
"""

from __future__ import annotations

import os
from collections.abc import Callable

import pytest

from canvas_sync.remote.http import CanvasClient

INTEGRATION_ENV = ("CANVAS_SANDBOX_URL", "CANVAS_SANDBOX_COURSE", "CANVAS_TOKEN")


def _missing_env() -> list[str]:
    return [name for name in INTEGRATION_ENV if not os.environ.get(name)]


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    missing = _missing_env()
    if not missing:
        return
    skip = pytest.mark.skip(reason=f"integration: set {', '.join(missing)}")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def integration() -> Callable[[int], CanvasClient]:
    """Factory for a write client against the sandbox course.

    Course lock (CONTRACTS.md section 7): refuses to build a write client unless the
    course id under test equals CANVAS_SANDBOX_COURSE.
    """
    missing = _missing_env()
    if missing:
        pytest.skip(f"integration: set {', '.join(missing)}")

    def make(course_id: int) -> CanvasClient:
        sandbox = os.environ["CANVAS_SANDBOX_COURSE"]
        if str(course_id) != sandbox:
            raise RuntimeError(
                f"refusing write client: course {course_id} is not the sandbox course"
            )
        return CanvasClient(
            os.environ["CANVAS_SANDBOX_URL"],
            os.environ["CANVAS_TOKEN"],
            course_id=course_id,
            mode="write",
        )

    return make
