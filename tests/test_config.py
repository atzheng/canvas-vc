from pathlib import Path

import pytest

from canvas_sync.config import get_token, load_config
from canvas_sync.errors import ConfigError

GOOD = """\
canvas_url: https://canvas.example.edu
course_id: 123
timezone: America/Vancouver
defaults:
  unlock_time: "08:30"
  assignment:
    due_time: "17:00"
"""


def write(tmp_path: Path, text: str) -> Path:
    (tmp_path / "course.yaml").write_text(text)
    return tmp_path


def test_load_ok(tmp_path: Path) -> None:
    cfg = load_config(write(tmp_path, GOOD))
    assert cfg.course_id == 123
    assert cfg.tz.key == "America/Vancouver"
    assert cfg.defaults.unlock_time.hour == 8
    assert cfg.defaults.assignment.due_time.hour == 17
    assert cfg.token_env == "CANVAS_TOKEN"


def test_bad_timezone(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as e:
        load_config(write(tmp_path, GOOD.replace("America/Vancouver", "Mars/Olympus")))
    assert "\n" not in str(e.value)
    assert "timezone" in str(e.value)


def test_missing_file_and_bad_yaml(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(tmp_path)
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, "a: [unclosed"))
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, "- a\n- b\n"))


def test_missing_field(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="course_id"):
        load_config(write(tmp_path, "canvas_url: https://x.edu\ntimezone: UTC\n"))


def test_get_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = load_config(write(tmp_path, GOOD + "token_env: MY_TOK\n"))
    monkeypatch.delenv("MY_TOK", raising=False)
    with pytest.raises(ConfigError, match="MY_TOK"):
        get_token(cfg)
    monkeypatch.setenv("MY_TOK", "dummy")
    assert get_token(cfg) == "dummy"
