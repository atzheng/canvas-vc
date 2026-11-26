"""Scaffold smoke tests: every module imports and the CLI lists every command."""

from __future__ import annotations

import importlib
import pkgutil

import pytest
from typer.testing import CliRunner

import canvas_sync
from canvas_sync.cli import app
from canvas_sync.model import REGISTRY
from canvas_sync.model.base import Kind

COMMANDS = ["clone", "fetch", "pull", "push", "status", "diff", "validate", "open"]


def test_all_modules_import() -> None:
    names = [m.name for m in pkgutil.walk_packages(canvas_sync.__path__, "canvas_sync.")]
    assert len(names) > 30
    for name in names:
        importlib.import_module(name)


def test_help_lists_all_commands() -> None:
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in COMMANDS:
        assert command in result.output


def test_registry_has_all_kinds() -> None:
    assert set(REGISTRY) == set(Kind)


def test_fake_canvas_importable() -> None:
    from tests.fake_canvas import FakeCanvas

    assert FakeCanvas().log == []


@pytest.mark.integration
def test_integration_marker_skipped_without_env() -> None:
    pytest.fail("runs only with the sandbox environment set")
