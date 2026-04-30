"""Unit tests for compilation/output_writer.py.

Verifies the chokepoint contract: every persisted compiled file must have
its ``BUILD_ID_PLACEHOLDER`` resolved before reaching disk, and the writer
asserts this invariant defensively so future code paths cannot bypass it.
"""

import re
from pathlib import Path

import pytest

from apm_cli.compilation.constants import BUILD_ID_PLACEHOLDER
from apm_cli.compilation.output_writer import CompiledOutputWriter

_HASH_LINE_RE = re.compile(r"<!-- Build ID: [a-f0-9]{12} -->")


def test_stabilizes_build_id_before_writing(tmp_path: Path):
    target = tmp_path / "AGENTS.md"
    content = f"# AGENTS.md\n{BUILD_ID_PLACEHOLDER}\n<!-- APM Version: 1.0.0 -->\n"

    CompiledOutputWriter().write(target, content)

    written = target.read_text(encoding="utf-8")
    assert BUILD_ID_PLACEHOLDER not in written
    assert _HASH_LINE_RE.search(written), written


def test_creates_parent_directories(tmp_path: Path):
    target = tmp_path / "deep" / "nested" / "AGENTS.md"
    content = f"# A\n{BUILD_ID_PLACEHOLDER}\n"

    CompiledOutputWriter().write(target, content)

    assert target.exists()


def test_writes_utf8_encoded(tmp_path: Path):
    target = tmp_path / "AGENTS.md"
    title = "\u4e2d\u6587\u6a19\u984c"
    content = f"# {title}\n{BUILD_ID_PLACEHOLDER}\nbody\n"

    CompiledOutputWriter().write(target, content)

    assert target.read_text(encoding="utf-8").startswith(f"# {title}")


def test_passes_through_content_without_placeholder(tmp_path: Path):
    target = tmp_path / "AGENTS.md"
    content = "# A\n<!-- no placeholder here -->\nbody\n"

    CompiledOutputWriter().write(target, content)

    assert target.read_text(encoding="utf-8") == content


def test_raises_when_placeholder_unresolvable(tmp_path: Path, monkeypatch):
    """Defense-in-depth: if a future code path mutates content so the
    placeholder survives stabilization, the writer must refuse to persist.
    """
    target = tmp_path / "AGENTS.md"
    # Inject a sentinel that simulates a stabilization failure: stub
    # ``stabilize_build_id`` to a no-op so the placeholder survives.
    import apm_cli.compilation.output_writer as ow

    monkeypatch.setattr(ow, "stabilize_build_id", lambda c: c)

    with pytest.raises(RuntimeError, match="build_id stabilization bypassed"):
        CompiledOutputWriter().write(target, f"# A\n{BUILD_ID_PLACEHOLDER}\n")

    assert not target.exists()


def test_atomic_write_no_partial_file_on_failure(tmp_path: Path, monkeypatch):
    """Failed writes must not leave a half-written file at the target path."""
    target = tmp_path / "AGENTS.md"
    target.write_text("PRE-EXISTING\n", encoding="utf-8")

    import apm_cli.utils.atomic_io as atomic_io  # noqa: PLR0402

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(atomic_io.os, "replace", boom)

    with pytest.raises(OSError):
        CompiledOutputWriter().write(target, f"# A\n{BUILD_ID_PLACEHOLDER}\n")

    # Pre-existing file must remain untouched
    assert target.read_text(encoding="utf-8") == "PRE-EXISTING\n"
