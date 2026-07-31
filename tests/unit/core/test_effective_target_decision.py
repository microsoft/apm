"""Regression coverage for the canonical effective install target decision."""

from pathlib import Path
from unittest.mock import patch

import pytest

from apm_cli.core.errors import AmbiguousHarnessError, NoHarnessError
from apm_cli.core.target_detection import resolve_effective_target_decision


def _resolve(
    project_root: Path,
    *,
    explicit_target: str | list[str] | None = None,
    manifest_target: str | list[str] | None = None,
    user_scope: bool = False,
):
    return resolve_effective_target_decision(
        project_root,
        explicit_target=explicit_target,
        manifest_target=manifest_target,
        user_scope=user_scope,
    )


@patch("apm_cli.config.get_install_target", return_value=["cursor", "claude"])
def test_explicit_target_wins_over_manifest_and_saved_default(_saved, tmp_path: Path) -> None:
    decision = _resolve(
        tmp_path,
        explicit_target=["copilot", "claude"],
        manifest_target=["cursor"],
    )

    assert decision.value == ["copilot", "claude"]
    assert decision.source == "--target flag"
    assert decision.canonical_targets == ("copilot", "claude")


@patch("apm_cli.config.get_install_target", return_value="claude")
def test_manifest_target_wins_over_saved_default(_saved, tmp_path: Path) -> None:
    decision = _resolve(tmp_path, manifest_target=["copilot", "cursor"])

    assert decision.value == ["copilot", "cursor"]
    assert decision.source == "apm.yml"
    assert decision.canonical_targets == ("copilot", "cursor")


@patch("apm_cli.config.get_install_target", return_value=["claude", "cursor"])
def test_saved_multi_target_is_the_effective_restriction(_saved, tmp_path: Path) -> None:
    decision = _resolve(tmp_path)

    assert decision.value == ["claude", "cursor"]
    assert decision.source == "apm config target"
    assert decision.canonical_targets == ("claude", "cursor")
    assert decision.runtime_targets == ("claude", "cursor")


@patch("apm_cli.config.get_install_target", return_value=None)
def test_multi_target_runtime_projection_uses_catalog_aliases(_saved, tmp_path: Path) -> None:
    decision = _resolve(
        tmp_path,
        explicit_target=["claude", "copilot", "intellij"],
    )

    assert decision.canonical_targets == ("claude", "copilot")
    assert decision.runtime_targets == ("claude", "copilot", "intellij")


@patch("apm_cli.config.get_install_target", return_value="agents")
def test_deprecated_agents_alias_projects_to_vscode_runtime(_saved, tmp_path: Path) -> None:
    decision = _resolve(tmp_path)

    assert decision.canonical_targets == ("copilot",)
    assert decision.runtime_targets == ("vscode",)
    assert decision.runtime_equivalents == ("agents", "copilot", "vscode")


@patch("apm_cli.config.get_install_target", return_value=None)
def test_mcp_only_target_is_excluded_from_lsp_projection(_saved, tmp_path: Path) -> None:
    decision = _resolve(tmp_path, explicit_target="intellij")

    assert decision.canonical_targets == ("copilot",)
    assert decision.runtime_targets == ("intellij",)
    assert decision.lsp_targets == ()


@patch("apm_cli.config.get_install_target", return_value=None)
def test_unset_or_malformed_saved_target_uses_single_project_detection(
    _saved,
    tmp_path: Path,
) -> None:
    (tmp_path / ".claude").mkdir()

    decision = _resolve(tmp_path)

    assert decision.value == ["claude"]
    assert decision.source == "auto-detect from .claude/"
    assert decision.canonical_targets == ("claude",)


@patch("apm_cli.config.get_install_target", return_value=None)
def test_unrestricted_project_without_harness_fails_closed(_saved, tmp_path: Path) -> None:
    with pytest.raises(NoHarnessError):
        _resolve(tmp_path)


@patch("apm_cli.config.get_install_target", return_value=None)
def test_unrestricted_ambiguous_project_fails_closed(_saved, tmp_path: Path) -> None:
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".cursor").mkdir()

    with pytest.raises(AmbiguousHarnessError):
        _resolve(tmp_path)


@patch("apm_cli.config.get_install_target", return_value=None)
def test_unrestricted_user_scope_preserves_runtime_detection(_saved, tmp_path: Path) -> None:
    decision = _resolve(tmp_path, user_scope=True)

    assert decision.value is None
    assert decision.source == "auto-detect"
    assert decision.canonical_targets is None
