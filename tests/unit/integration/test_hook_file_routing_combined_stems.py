"""Legacy filename classification that feeds the #3226 migration guard."""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.integration.hook_file_routing import (
    _hook_file_allowed_targets,
    configured_hook_file_selection,
    legacy_hook_file_selection,
    legacy_routing_widening,
)

pytestmark = pytest.mark.component


@pytest.mark.parametrize(
    "stem, expected",
    [
        ("claude-codex-hooks", {"claude", "codex"}),
        ("internal-claude-codex-hooks", {"claude", "codex"}),
        ("copilot-hooks", {"copilot", "vscode"}),
        ("my-copilot-hooks", {"copilot", "vscode"}),
        ("pre-claude-launch-hooks", None),
        ("ponytail-hooks", None),
        ("hooks-claude", {"claude"}),
        ("hooks", None),
        ("copilot-vscode-hooks", {"copilot", "vscode"}),
        ("checks.claude", None),
    ],
)
def test_hook_file_allowed_targets(tmp_path: Path, stem: str, expected: set[str] | None) -> None:
    hook_file = tmp_path / f"{stem}.json"

    assert _hook_file_allowed_targets(hook_file) == expected


def test_combined_manifest_legacy_selection_matches_each_named_target(tmp_path: Path) -> None:
    combined = tmp_path / "claude-codex-hooks.json"

    assert legacy_hook_file_selection([combined], "claude") == [combined]
    assert legacy_hook_file_selection([combined], "codex") == [combined]
    assert legacy_hook_file_selection([combined], "copilot") == []


def test_configured_selection_ignores_target_words(tmp_path: Path) -> None:
    files = [tmp_path / "hooks.json", tmp_path / "claude-launch-hooks.json"]

    assert configured_hook_file_selection(files) == files


def test_widening_names_files_the_legacy_convention_hid(tmp_path: Path) -> None:
    generic = tmp_path / "hooks.json"
    claude = tmp_path / "hooks-claude.json"
    combined = tmp_path / "claude-codex-hooks.json"

    assert legacy_routing_widening([generic, claude], "claude") == [generic]
    assert legacy_routing_widening([generic, claude], "codex") == [claude]
    assert legacy_routing_widening([combined], "codex") == []
    assert legacy_routing_widening([combined], "cursor") == [combined]
    assert legacy_routing_widening([generic], "claude") == []
