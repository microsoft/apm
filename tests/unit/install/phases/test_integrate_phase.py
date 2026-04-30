"""Tests for _check_cowork_caps in apm_cli.install.phases.integrate."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, Dict  # noqa: F401, UP035
from unittest.mock import MagicMock

import pytest

from apm_cli.install.phases.integrate import _check_cowork_caps
from apm_cli.integration.targets import KNOWN_TARGETS

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_config_cache():
    """Reset the in-process config cache before and after every test."""
    from apm_cli.config import _invalidate_config_cache

    _invalidate_config_cache()
    yield
    _invalidate_config_cache()


def _make_cowork_target(cowork_root: Path) -> Any:
    """Return a frozen TargetProfile with resolved_deploy_root for cowork.

    Args:
        cowork_root: The resolved cowork skills root directory.

    Returns:
        A frozen TargetProfile suitable for cowork tests.
    """
    return replace(KNOWN_TARGETS["copilot-cowork"], resolved_deploy_root=cowork_root)


def _make_ctx(
    cowork_root: Path | None = None,
    include_copilot: bool = False,
) -> MagicMock:
    """Build a minimal ctx mock for cap check tests.

    Args:
        cowork_root: If set, adds a cowork target with this root.
        include_copilot: If True, also adds the copilot target.

    Returns:
        A MagicMock configured as an InstallContext.
    """
    ctx = MagicMock()
    ctx.targets = []
    if cowork_root is not None:
        ctx.targets.append(_make_cowork_target(cowork_root))
    if include_copilot:
        ctx.targets.append(KNOWN_TARGETS["copilot"])
    ctx.logger = MagicMock()
    ctx.diagnostics = MagicMock()
    return ctx


def _create_skills(cowork_root: Path, count: int, size: int = 100) -> None:
    """Create N skill directories with SKILL.md files.

    Args:
        cowork_root: Root directory for skills.
        count: Number of skill dirs to create.
        size: Size of each SKILL.md in bytes.
    """
    for i in range(count):
        skill_dir = cowork_root / f"skill-{i:04d}"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_bytes(b"x" * size)


# ---------------------------------------------------------------------------
# TestCheckCoworkCaps
# ---------------------------------------------------------------------------


class TestCheckCoworkCaps:
    """Tests for _check_cowork_caps capacity checks."""

    def test_count_cap_warning_fires_at_51_skills(self, tmp_path: Path) -> None:
        cowork_root = tmp_path / "cowork"
        cowork_root.mkdir()
        _create_skills(cowork_root, 51)
        ctx = _make_ctx(cowork_root)
        _check_cowork_caps(ctx)
        warning_calls = ctx.logger.warning.call_args_list
        assert len(warning_calls) >= 1
        msg = str(warning_calls[0])
        assert "51" in msg
        assert "50" in msg

    def test_count_cap_no_warning_at_50_skills(self, tmp_path: Path) -> None:
        cowork_root = tmp_path / "cowork"
        cowork_root.mkdir()
        _create_skills(cowork_root, 50)
        ctx = _make_ctx(cowork_root)
        _check_cowork_caps(ctx)
        warning_calls = ctx.logger.warning.call_args_list
        # No warning about count
        count_warnings = [c for c in warning_calls if "50" in str(c) and "cap" in str(c).lower()]
        assert len(count_warnings) == 0

    def test_size_cap_warning_fires_for_oversized_skill_md(self, tmp_path: Path) -> None:
        cowork_root = tmp_path / "cowork"
        cowork_root.mkdir()
        _create_skills(cowork_root, 1, size=1_048_577)
        ctx = _make_ctx(cowork_root)
        _check_cowork_caps(ctx)
        warning_calls = ctx.logger.warning.call_args_list
        assert len(warning_calls) >= 1
        msg = str(warning_calls[0])
        assert "MB" in msg

    def test_size_cap_no_warning_at_exactly_1mb(self, tmp_path: Path) -> None:
        cowork_root = tmp_path / "cowork"
        cowork_root.mkdir()
        _create_skills(cowork_root, 1, size=1_048_576)
        ctx = _make_ctx(cowork_root)
        _check_cowork_caps(ctx)
        size_warnings = [c for c in ctx.logger.warning.call_args_list if "MB" in str(c)]
        assert len(size_warnings) == 0

    def test_cap_check_skipped_when_no_cowork_target(self, tmp_path: Path) -> None:
        ctx = _make_ctx(cowork_root=None, include_copilot=True)
        _check_cowork_caps(ctx)
        ctx.logger.warning.assert_not_called()

    def test_cap_check_skipped_when_cowork_root_nonexistent(self, tmp_path: Path) -> None:
        nonexistent = tmp_path / "nonexistent"
        ctx = _make_ctx(cowork_root=nonexistent)
        _check_cowork_caps(ctx)
        ctx.logger.warning.assert_not_called()

    def test_package_100_skills_all_deploy_cap_warns_but_completes(self, tmp_path: Path) -> None:
        cowork_root = tmp_path / "cowork"
        cowork_root.mkdir()
        _create_skills(cowork_root, 100)
        ctx = _make_ctx(cowork_root)
        # Should warn but NOT raise
        _check_cowork_caps(ctx)
        warning_calls = ctx.logger.warning.call_args_list
        assert len(warning_calls) >= 1

    def test_cap_check_skipped_when_targets_empty(self) -> None:
        ctx = MagicMock()
        ctx.targets = []
        ctx.logger = MagicMock()
        _check_cowork_caps(ctx)
        ctx.logger.warning.assert_not_called()
