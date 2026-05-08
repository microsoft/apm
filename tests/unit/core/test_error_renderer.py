"""Unit tests for the unified error renderer (#1154).

# Implementation contract assumed by these tests (TDD-red):
#
# Module: apm_cli.core.errors
#   - render_no_harness_error(project_root: Path) -> str
#   - render_ambiguous_error(project_root: Path, detected: list[str]) -> str
#   - render_unknown_target_error(value: str, valid: list[str]) -> str
#   - render_conflicting_schema_error() -> str
#
# All renderers MUST emit ASCII-only output (U+0020 .. U+007E) and
# include three sections:
#   (a) "what APM saw" headline (e.g. 'no harness detected', 'multiple
#        harnesses detected', 'unknown target', 'cannot use both ...')
#   (b) at least three actionable commands, each line beginning with
#        'apm ' (e.g. 'apm install --target claude', 'apm targets',
#        'apm install --dry-run')
#   (c) an apm.yml snippet containing 'targets:' (or 'target:' for the
#        singular-sugar example).
#
# All exception classes that wrap these renderers subclass
# click.UsageError so Click surfaces exit code 2 automatically.
"""

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


def test_unknown_target_error_lists_valid():
    text = render_unknown_target_error("foo", ["claude", "copilot", "cursor"])
    _assert_three_sections(text, ["unknown target", "invalid target"])
    # Suggestion must list at least one canonical target.
    assert "claude" in text


def test_unknown_target_error_suggests_copilot_not_first_alphabetical():
    """Suggestion must be a sensible default (#1188), not sorted-first."""
    valid = ["agent-skills", "claude", "copilot", "cursor"]
    text = render_unknown_target_error("foo", valid)
    # 'agent-skills' is alphabetically first but is the wrong default to
    # surface to a user who typed 'foo'. Prefer 'copilot'.
    assert "--target copilot" in text
    assert "    - copilot" in text
    assert "--target agent-skills" not in text


def test_unknown_target_error_sanitizes_garbled_value():
    """Headline must not show Python list-repr noise (#1188)."""
    # Simulates the pre-fix garbled token "['copilot'" leaking through.
    text = render_unknown_target_error("['copilot'", ["claude", "copilot"])
    headline = text.splitlines()[0]
    # Bracket and quote noise is stripped from the headline value.
    assert headline == "[x] Unknown target 'copilot'"


def test_unknown_target_error_falls_back_when_strip_empties_value():
    """If sanitization removes everything, fall back so headline stays actionable."""
    # All-noise input: stripping yields empty string.
    text = render_unknown_target_error("[]'\"", ["claude", "copilot"])
    headline = text.splitlines()[0]
    # Must not render `Unknown target ''`. We accept either the raw
    # (un-stripped) value or a `<empty>` placeholder.
    assert headline != "[x] Unknown target ''"
    assert "Unknown target '" in headline


def test_unknown_target_error_hides_agent_skills_meta_target():
    """agent-skills is a meta-target; do not advertise it as a recovery
    path here when ``apm targets`` won't list it (#1208).

    The canonical set still ACCEPTS ``agent-skills`` via --target or
    apm.yml; this test only pins that the unknown-target error message
    does not steer users to it. Discoverability lives in
    ``apm targets --json --all``.
    """
    valid = ["agent-skills", "claude", "copilot", "cursor"]
    text = render_unknown_target_error("foo", valid)
    # 'agent-skills' must not appear in the rendered "Valid targets:" CSV
    # nor in any of the three suggested commands or the apm.yml snippet.
    assert "agent-skills" not in text, (
        f"agent-skills meta-target leaked into unknown-target suggestions:\n{text}"
    )
    # Sanity: the surviving harness targets are still listed.
    assert "claude" in text and "copilot" in text and "cursor" in text


def test_unknown_target_error_falls_back_when_only_meta_target_visible():
    """If the caller filters to only agent-skills, the renderer must
    not crash and must emit a sane default suggestion (claude) -- and
    the 'Valid targets:' line must not render as a bare colon."""
    text = render_unknown_target_error("foo", ["agent-skills"])
    # No agent-skills in suggestions...
    assert "--target agent-skills" not in text
    assert "    - agent-skills" not in text
    # ...and the safety-net default 'claude' surfaces instead.
    assert "--target claude" in text
    # The 'Valid targets:' line must have non-empty content (#1215 review).
    assert "Valid targets: \n" not in text
    assert "Valid targets: claude" in text


def test_conflicting_schema_error_has_three_parts():
    text = render_conflicting_schema_error()
    _assert_three_sections(text, ["cannot use both", "conflicting"])


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


@pytest.mark.parametrize(
    "renderer",
    [
        lambda: render_no_harness_error(),
        lambda: render_ambiguous_error(None, ["claude", "cursor"]),
        lambda: render_unknown_target_error("foo", ["claude", "copilot"]),
        lambda: render_conflicting_schema_error(),
    ],
    ids=["no_harness", "ambiguous", "unknown", "conflict"],
)
def test_error_output_ascii_only(renderer):
    """No char > U+007E may appear in any error message (Windows cp1252)."""
    try:
        text = renderer()
    except TypeError:
        # Some renderers require a project_root; retry with a placeholder
        # only if the signature truly demands it.
        from pathlib import Path

        text = renderer.__wrapped__(Path(".")) if hasattr(renderer, "__wrapped__") else ""
    bad = [(i, ch) for i, ch in enumerate(text) if ord(ch) > 0x7E]
    assert not bad, f"Non-ASCII chars in error output: {bad[:5]!r}"
