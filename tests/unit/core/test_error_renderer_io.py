"""Unit tests for the unified error renderer (#1154) (filesystem, process and CLI boundary cases)."""

from __future__ import annotations

import re

import pytest

from apm_cli.core.errors import (
    AmbiguousHarnessError,
    ConflictingTargetsError,
    NoHarnessError,
    UnknownTargetError,
    render_ambiguous_error,
    render_conflicting_schema_error,
    render_no_harness_error,
    render_unknown_target_error,
)

pytestmark = pytest.mark.component

_THREE_CMD_RE = re.compile(r"^\s*apm\s+\S", re.MULTILINE)


def _assert_three_sections(text: str, headline_substrings: list[str]) -> None:
    assert any(s in text.lower() for s in headline_substrings), (
        f"Missing 'what APM saw' headline; want any of {headline_substrings!r} in:\n{text}"
    )
    cmd_count = len(_THREE_CMD_RE.findall(text))
    assert cmd_count >= 3, f"Need >= 3 'apm ...' command lines, got {cmd_count}:\n{text}"
    assert "targets:" in text or "target:" in text, (
        f"Missing apm.yml snippet (targets:/target:) in:\n{text}"
    )


def test_no_harness_error_has_three_parts(tmp_path):
    text = render_no_harness_error(tmp_path)
    _assert_three_sections(text, ["no harness detected"])


def test_ambiguous_error_has_three_parts(tmp_path):
    text = render_ambiguous_error(tmp_path, ["claude", "cursor"])
    _assert_three_sections(text, ["multiple harnesses", "ambiguous"])
    assert "claude" in text and "cursor" in text


def test_all_errors_exit_code_2(tmp_path):
    """Every renderer-backed exception must carry exit_code == 2 (Click UsageError)."""
    for ctor in (
        lambda: NoHarnessError(render_no_harness_error(tmp_path)),
        lambda: AmbiguousHarnessError(render_ambiguous_error(tmp_path, ["claude", "cursor"])),
        lambda: UnknownTargetError(render_unknown_target_error("foo", ["claude"])),
        lambda: ConflictingTargetsError(render_conflicting_schema_error()),
    ):
        exc = ctor()
        assert getattr(exc, "exit_code", None) == 2, (
            f"{type(exc).__name__} must have exit_code=2 (got {getattr(exc, 'exit_code', None)!r})"
        )
