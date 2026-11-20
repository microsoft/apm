"""Unit tests for the target resolution v2 algorithm (#1154) (filesystem, process and CLI boundary cases)."""

from __future__ import annotations

from pathlib import Path

import pytest

# These imports are expected to fail on current main; that is the TDD-red point.
from apm_cli.core.errors import (
    AmbiguousHarnessError,
    NoHarnessError,
)
from apm_cli.core.target_detection import (
    detect_signals,
    expand_all_targets,
    resolve_targets,
)

pytestmark = pytest.mark.component

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


def test_signal_whitelist_kiro_dir_is_signal(tmp_path):
    (tmp_path / ".kiro").mkdir()
    assert "kiro" in _signal_targets(tmp_path)


def test_signal_whitelist_cursorrules_is_signal(tmp_path):
    _touch(tmp_path / ".cursorrules", "# Cursor\n")
    assert "cursor" in _signal_targets(tmp_path)


def test_signal_whitelist_copilot_instructions_is_signal(tmp_path):
    _touch(tmp_path / ".github" / "copilot-instructions.md", "# Copilot\n")
    assert "copilot" in _signal_targets(tmp_path)


def test_signal_whitelist_github_instructions_dir_is_copilot_signal(tmp_path):
    (tmp_path / ".github" / "instructions").mkdir(parents=True)
    assert "copilot" in _signal_targets(tmp_path)


def test_signal_whitelist_github_agents_dir_is_copilot_signal(tmp_path):
    (tmp_path / ".github" / "agents").mkdir(parents=True)
    assert "copilot" in _signal_targets(tmp_path)


def test_signal_whitelist_github_prompts_dir_is_copilot_signal(tmp_path):
    (tmp_path / ".github" / "prompts").mkdir(parents=True)
    assert "copilot" in _signal_targets(tmp_path)


def test_signal_whitelist_github_hooks_dir_is_copilot_signal(tmp_path):
    (tmp_path / ".github" / "hooks").mkdir(parents=True)
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


def test_flag_source_label_overrides_default_provenance(tmp_path):
    """A config-supplied flag value labels provenance as its own source.

    When the resolved flag value originated from a configured default rather
    than the CLI, the caller passes ``flag_source`` so the provenance line is
    not misattributed to ``--target``.
    """
    resolved = resolve_targets(tmp_path, flag="claude", flag_source="apm config target")
    assert resolved.targets == ["claude"]
    assert resolved.source == "apm config target"


def test_flag_source_defaults_to_cli_flag(tmp_path):
    """Omitting ``flag_source`` keeps the explicit --target provenance label."""
    resolved = resolve_targets(tmp_path, flag="claude")
    assert resolved.source == "--target flag"


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


# ---------------------------------------------------------------------------
# CSV / unknown-target validation
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# YAML list under 'target:' singular key (#1188)
# ---------------------------------------------------------------------------


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
