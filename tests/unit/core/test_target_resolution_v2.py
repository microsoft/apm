"""Unit tests for the target resolution v2 algorithm (#1154).

# Implementation contract assumed by these tests (TDD-red):
#
# Module: apm_cli.core.target_detection
#   - detect_signals(project_root: Path) -> list[Signal]
#       Returns Signal objects for every detection-whitelist marker found
#       under project_root. AGENTS.md and bare .github/ are NOT signals.
#       Each Signal exposes:
#           .target  : canonical target name ('claude', 'copilot', ...)
#           .source  : human-readable signal descriptor
#                      (e.g. 'CLAUDE.md', '.claude/', '.cursorrules',
#                      '.github/copilot-instructions.md')
#
#   - resolve_targets(
#         project_root: Path,
#         *,
#         flag: str | list[str] | None = None,
#         yaml_targets: list[str] | None = None,
#     ) -> ResolvedTargets
#       Priority: flag > yaml_targets > auto-detect signals.
#       Zero auto-detect signals + no flag + no yaml -> raises NoHarnessError.
#       2+ auto-detect signals + no flag + no yaml -> raises AmbiguousHarnessError.
#       Explicit (flag or yaml) -> ResolvedTargets.auto_create == True for
#       every resolved target (three-guard collapse).
#       Auto-detect path also sets auto_create=True for the resolved target
#       so missing-but-detected dirs (e.g. CLAUDE.md without .claude/) get
#       materialized.
#
#   ResolvedTargets fields:
#       .targets   : list[str]  (sorted, canonical names)
#       .source    : str        (one of '--target flag', 'apm.yml',
#                                'auto-detect from <signal_csv>')
#       .auto_create : bool
#
#   - expand_all_targets(
#         project_root: Path,
#         *,
#         yaml_targets: list[str] | None = None,
#     ) -> list[str]
#       Resolves --target all to (signals U yaml_targets), NOT all 7
#       supported targets. Empty result -> NoHarnessError.
#
# Module: apm_cli.core.apm_yml
#   - parse_targets_field(yaml_data: dict) -> list[str]
#       Accepts 'targets:' list OR 'target:' singular sugar; returns
#       canonical list. BOTH present -> ConflictingTargetsError.
#       'target: "claude,copilot"' CSV is also accepted.
#
# Module: apm_cli.core.errors
#   - NoHarnessError, AmbiguousHarnessError, UnknownTargetError,
#     ConflictingTargetsError -- all subclass click.UsageError so Click
#     surfaces exit code 2 automatically.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# These imports are expected to fail on current main; that is the TDD-red point.
from apm_cli.core.apm_yml import parse_targets_field
from apm_cli.core.errors import (
    AmbiguousHarnessError,
    ConflictingTargetsError,
    NoHarnessError,
    UnknownTargetError,
)
from apm_cli.core.target_detection import (
    detect_signals,
    expand_all_targets,
    resolve_targets,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _touch(path: Path, content: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode("utf-8"))


def _signal_targets(project_root: Path) -> list[str]:
    return sorted({s.target for s in detect_signals(project_root)})


# ---------------------------------------------------------------------------
# Signal whitelist
# ---------------------------------------------------------------------------


def test_signal_whitelist_claude_md_is_signal(tmp_path):
    _touch(tmp_path / "CLAUDE.md", "# Claude\n")
    assert "claude" in _signal_targets(tmp_path)


def test_signal_whitelist_gemini_md_is_signal(tmp_path):
    _touch(tmp_path / "GEMINI.md", "# Gemini\n")
    assert "gemini" in _signal_targets(tmp_path)


def test_signal_whitelist_cursorrules_is_signal(tmp_path):
    _touch(tmp_path / ".cursorrules", "# Cursor\n")
    assert "cursor" in _signal_targets(tmp_path)


def test_signal_whitelist_copilot_instructions_is_signal(tmp_path):
    _touch(tmp_path / ".github" / "copilot-instructions.md", "# Copilot\n")
    assert "copilot" in _signal_targets(tmp_path)


def test_signal_whitelist_github_dir_alone_NOT_signal(tmp_path):
    (tmp_path / ".github").mkdir()
    assert "copilot" not in _signal_targets(tmp_path)


def test_signal_whitelist_agents_md_NOT_signal(tmp_path):
    _touch(tmp_path / "AGENTS.md", "# Agents\n")
    assert "codex" not in _signal_targets(tmp_path)
    assert _signal_targets(tmp_path) == []


# ---------------------------------------------------------------------------
# Resolution priority
# ---------------------------------------------------------------------------


def test_resolution_priority_flag_over_yaml(tmp_path):
    _touch(tmp_path / "CLAUDE.md")
    resolved = resolve_targets(tmp_path, flag=["cursor"], yaml_targets=["claude"])
    assert resolved.targets == ["cursor"]
    assert "--target flag" in resolved.source


def test_resolution_priority_yaml_over_autodetect(tmp_path):
    _touch(tmp_path / "CLAUDE.md")
    resolved = resolve_targets(tmp_path, flag=None, yaml_targets=["copilot"])
    assert resolved.targets == ["copilot"]
    assert "apm.yml" in resolved.source


def test_resolution_autodetect_single_signal(tmp_path):
    _touch(tmp_path / "CLAUDE.md")
    resolved = resolve_targets(tmp_path)
    assert resolved.targets == ["claude"]
    assert "auto-detect" in resolved.source
    assert "CLAUDE.md" in resolved.source


def test_resolution_autodetect_zero_signals_error(tmp_path):
    with pytest.raises(NoHarnessError):
        resolve_targets(tmp_path)


def test_resolution_autodetect_multi_signals_error(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".cursor").mkdir()
    with pytest.raises(AmbiguousHarnessError):
        resolve_targets(tmp_path)


# ---------------------------------------------------------------------------
# apm.yml schema
# ---------------------------------------------------------------------------


def test_schema_targets_list_valid():
    out = parse_targets_field({"targets": ["claude", "copilot"]})
    assert sorted(out) == ["claude", "copilot"]


def test_schema_target_singular_sugar():
    out = parse_targets_field({"target": "claude"})
    assert out == ["claude"]


def test_schema_both_target_and_targets_error():
    with pytest.raises(ConflictingTargetsError):
        parse_targets_field({"target": "claude", "targets": ["copilot"]})


# ---------------------------------------------------------------------------
# CSV / unknown-target validation
# ---------------------------------------------------------------------------


def test_csv_target_parsing():
    out = parse_targets_field({"target": "claude,cursor"})
    assert sorted(out) == ["claude", "cursor"]


def test_unknown_target_rejected():
    with pytest.raises(UnknownTargetError) as exc_info:
        parse_targets_field({"target": "unknown"})
    msg = str(exc_info.value).lower()
    # Must list at least one canonical target in the suggestion.
    assert "claude" in msg or "copilot" in msg


# ---------------------------------------------------------------------------
# --target all expansion
# ---------------------------------------------------------------------------


def test_all_expansion_with_single_signal(tmp_path):
    _touch(tmp_path / "CLAUDE.md")
    out = expand_all_targets(tmp_path)
    assert out == ["claude"]


def test_all_expansion_with_yaml_targets(tmp_path):
    out = expand_all_targets(tmp_path, yaml_targets=["claude", "copilot"])
    assert sorted(out) == ["claude", "copilot"]


# ---------------------------------------------------------------------------
# Three-guard collapse: explicit always materializes
# ---------------------------------------------------------------------------


def test_explicit_target_always_materializes(tmp_path):
    """--target claude in greenfield must set auto_create=True."""
    resolved = resolve_targets(tmp_path, flag=["claude"])
    assert resolved.targets == ["claude"]
    assert resolved.auto_create is True
