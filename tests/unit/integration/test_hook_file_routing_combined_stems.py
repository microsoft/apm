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
        ("internal-claude-codex-hooks", {"claude", "codex"}),
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


def test_unknown_suffix_with_known_token_warns_before_universal_fallback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mixed = tmp_path / "claude-launch-hooks.json"
    warned_packages: set[str] = set()

    assert filter_hook_files_for_target(
        [mixed],
        "codex",
        package_name="ponytail",
        package_identity="DietrichGebert/ponytail",
        warned_packages=warned_packages,
    ) == [mixed]

    output = capsys.readouterr().out
    assert "hook filename routing could not resolve" in output
    assert "claude-launch-hooks.json" in output
    assert "contains target token(s) [claude]" in output
    assert "universal fallback" in output


def test_known_token_outside_target_suffix_warns_when_routed_to_last_token(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mixed = tmp_path / "claude-approval-codex-hooks.json"
    warned_packages: set[str] = set()

    assert [
        p.name
        for p in filter_hook_files_for_target(
            [mixed],
            "codex",
            package_name="ponytail",
            package_identity="DietrichGebert/ponytail",
            warned_packages=warned_packages,
        )
    ] == [mixed.name]
    assert (
        filter_hook_files_for_target(
            [mixed],
            "claude",
            package_name="ponytail",
            package_identity="DietrichGebert/ponytail",
            warned_packages=warned_packages,
        )
        == []
    )

    output = capsys.readouterr().out
    assert "hook filename routing could not resolve" in output
    assert "claude-approval-codex-hooks.json" in output
    assert "contains target token(s) [claude]" in output
    assert "uses the suffix" in output
