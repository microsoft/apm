"""Filename retirement must never widen previously selected executable sources."""

from pathlib import Path

import pytest

from apm_cli.hook_contract import HookContractError
from apm_cli.integration.hook_file_routing import filter_hook_files_for_target

pytestmark = pytest.mark.unit

_TARGETS = (
    "copilot",
    "vscode",
    "claude",
    "cursor",
    "codex",
    "gemini",
    "antigravity",
    "windsurf",
    "kiro",
)


@pytest.mark.parametrize("target", _TARGETS)
def test_generic_discovery_is_returned_whole_without_target_requirements(target: str) -> None:
    files = [Path("hooks/checks.json"), Path("hooks/claude.audit.json")]
    assert filter_hook_files_for_target(files, target) == files


@pytest.mark.parametrize("target", _TARGETS)
def test_formerly_shadowed_generic_descriptor_requires_migration(target: str) -> None:
    files = [Path("hooks/checks.json"), Path(f"hooks/{target}-hooks.json")]
    with pytest.raises(HookContractError, match=r"checks\.json.*previously excluded"):
        filter_hook_files_for_target(files, target, package_name="checks")


@pytest.mark.parametrize("target", _TARGETS)
def test_other_client_descriptor_is_not_silently_activated(target: str) -> None:
    old_target = "claude" if target != "claude" else "codex"
    files = [Path(f"hooks/{old_target}-hooks.json")]
    with pytest.raises(HookContractError, match=r"apm\.yml"):
        filter_hook_files_for_target(files, target, package_name="checks")


@pytest.mark.parametrize("target", _TARGETS)
def test_compatible_historical_name_does_not_select_a_subset(target: str) -> None:
    files = [Path(f"hooks/{target}-hooks.json")]
    assert filter_hook_files_for_target(files, target) == files


def test_migration_does_not_mutate_the_discovery_inventory() -> None:
    files = [Path("hooks/checks.json"), Path("hooks/claude-hooks.json")]
    original = list(files)
    with pytest.raises(HookContractError):
        filter_hook_files_for_target(files, "claude")
    assert files == original
