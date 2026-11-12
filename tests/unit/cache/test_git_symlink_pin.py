"""Unit contract for the canonical ``core.symlinks`` pin (#3457)."""

from __future__ import annotations

import pytest

from apm_cli.utils.git_env import git_symlink_pin_args, git_symlink_pin_entry

pytestmark = [pytest.mark.unit, pytest.mark.windows_compat]


def test_pin_writes_links_as_link_text() -> None:
    """Git never needs OS symlink capability; links are resolved from objects."""
    assert git_symlink_pin_entry() == ("core.symlinks", "false")
    assert git_symlink_pin_args() == ("-c", "core.symlinks=false")
