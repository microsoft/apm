"""Tests for combined multi-target hook manifest stem routing."""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.integration.hook_file_routing import (
    _hook_file_allowed_targets,
    filter_hook_files_for_target,
)


@pytest.mark.parametrize(
    "stem, expected",
    [
        ("claude-codex-hooks", {"claude", "codex"}),
        ("copilot-hooks", {"copilot", "vscode"}),
        ("my-copilot-hooks", {"copilot", "vscode"}),
        ("pre-claude-launch-hooks", None),
        ("ponytail-hooks", None),
        ("hooks-claude", {"claude"}),
        ("hooks", None),
        ("copilot-vscode-hooks", {"copilot", "vscode"}),
    ],
)
def test_hook_file_allowed_targets(tmp_path: Path, stem: str, expected: set[str] | None) -> None:
    hook_file = tmp_path / f"{stem}.json"

    assert _hook_file_allowed_targets(hook_file) == expected


def test_combined_manifest_selected_for_all_its_targets_and_not_others(tmp_path: Path) -> None:
    combined = tmp_path / "claude-codex-hooks.json"

    assert [p.name for p in filter_hook_files_for_target([combined], "claude")] == [combined.name]
    assert [p.name for p in filter_hook_files_for_target([combined], "codex")] == [combined.name]
    assert filter_hook_files_for_target([combined], "copilot") == []
