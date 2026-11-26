"""Reusable canonical-form checks for every type bead (CONTRACTS.md sections 4 and 5).

A type bead's tests should call, at minimum::

    assert_spec_fixed_point(MY_SPEC, objs, cfg, tmp_path)    # dump -> load -> dump
    assert_spec_normalizes(MY_SPEC, {"pages/x.md": messy}, {"pages/x.md": canonical}, cfg, tmp_path)

and, once the whole tree can be built, ``assert_course_fixed_point``.

Nothing here touches the network.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path

from canvas_sync.config import CourseConfig
from canvas_sync.model.base import CanvasObject, TypeSpec
from canvas_sync.model.course import Course, dump_tree, load_tree

HASH_SEEDS = ("0", "1", "2", "12345", "4294967295")


def write_files(root: Path, files: Mapping[str, bytes | str]) -> None:
    """Write ``{tree path: content}`` under ``root`` byte-for-byte (no newline translation)."""
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)


def assert_course_fixed_point(course: Course, tmp_path: Path) -> dict[str, bytes]:
    """``dump(load(dump(c))) == dump(c)`` through the real filesystem. Returns ``dump(c)``."""
    first = dump_tree(course)
    root = Path(tempfile.mkdtemp(prefix="fixed-point-", dir=tmp_path))
    write_files(root, first)
    second = dump_tree(load_tree(root))
    assert second == first, _describe_diff(first, second)
    _assert_canonical_bytes(first)
    return first


def assert_spec_fixed_point(
    spec: TypeSpec, objs: dict[str, CanvasObject], cfg: CourseConfig, tmp_path: Path
) -> dict[str, bytes]:
    """dump -> write -> load -> dump is stable, and load returns the objects dumped."""
    first = spec.dump(objs, cfg)
    root = Path(tempfile.mkdtemp(prefix=f"fixed-point-{spec.kind.value}-", dir=tmp_path))
    write_files(root, first)
    loaded = spec.load(root, cfg)
    assert loaded == objs
    second = spec.dump(loaded, cfg)
    assert second == first, _describe_diff(first, second)
    _assert_canonical_bytes(first)
    return first


def assert_spec_normalizes(
    spec: TypeSpec,
    raw: Mapping[str, bytes | str],
    expected: Mapping[str, bytes | str],
    cfg: CourseConfig,
    tmp_path: Path,
) -> None:
    """Tolerant input (date-only, shuffled keys, CRLF, BOM...) dumps to exactly ``expected``."""
    root = Path(tempfile.mkdtemp(prefix=f"normalize-{spec.kind.value}-", dir=tmp_path))
    write_files(root, raw)
    got = spec.dump(spec.load(root, cfg), cfg)
    want = {k: v.encode("utf-8") if isinstance(v, str) else v for k, v in expected.items()}
    assert got == want, _describe_diff(want, got)
    assert_spec_fixed_point(spec, spec.load(root, cfg), cfg, tmp_path)


def assert_deterministic_across_hash_seeds(script: str, seeds: tuple[str, ...] = HASH_SEEDS) -> str:
    """Run ``python -c script`` under several PYTHONHASHSEED values; stdout must be identical.

    ``script`` should print the bytes it wants compared (e.g. ``dump_tree(...)`` as hex).
    Returns that common stdout.
    """
    outputs: dict[str, str] = {}
    for seed in seeds:
        env = {**os.environ, "PYTHONHASHSEED": seed}
        proc = subprocess.run(
            [sys.executable, "-c", script], env=env, capture_output=True, text=True, check=False
        )
        assert proc.returncode == 0, proc.stderr
        outputs[seed] = proc.stdout
    assert len(set(outputs.values())) == 1, f"output depends on hash seed: {outputs}"
    return next(iter(outputs.values()))


def _assert_canonical_bytes(files: Mapping[str, bytes]) -> None:
    for path, data in files.items():
        assert not data.startswith(b"\xef\xbb\xbf"), f"{path}: BOM"
        assert b"\r" not in data, f"{path}: CR in output"
        text = data.decode("utf-8")  # must be valid UTF-8
        if text:
            assert text.endswith("\n") and not text.endswith("\n\n"), f"{path}: trailing newline"


def _describe_diff(a: Mapping[str, bytes], b: Mapping[str, bytes]) -> str:
    if set(a) != set(b):
        return f"paths differ: {sorted(set(a) ^ set(b))}"
    for k in sorted(a):
        if a[k] != b[k]:
            first, second = a[k].decode(errors="replace"), b[k].decode(errors="replace")
            return f"{k} differs:\n--- first\n{first}\n--- second\n{second}"
    return "equal"
