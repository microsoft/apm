"""Remediation hints name commands that work, and ref updates can repair them (#3837).

* ``PROXY_REGISTRY_ONLY`` blocks replay of a lockfile locked to direct VCS
  sources, but a ref update re-resolves through the registry instead of
  failing on the stale on-disk entries.
* Auth pre-flight failures name the command the user actually invoked.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import click
import pytest

from apm_cli.core.scope import InstallScope
from apm_cli.install.source_helpers import (
    lockfile_refresh_command,
    ref_update_operation_label,
    supply_chain_mismatch_hint,
)

pytestmark = pytest.mark.unit


class TestRemediationCommands:
    def test_refresh_command_follows_scope(self) -> None:
        assert lockfile_refresh_command(InstallScope.PROJECT) == "apm update"
        assert lockfile_refresh_command(InstallScope.USER) == "apm update -g"

    def test_mismatch_hint_targets_a_direct_dependency(self) -> None:
        ctx = MagicMock(scope=InstallScope.USER, declared_direct_refs={"owner/repo": ()})
        hint = supply_chain_mismatch_hint(ctx, "owner/repo")
        assert "Use 'apm update -g owner/repo' to accept new content for this package" in hint

    def test_mismatch_hint_for_transitive_dependency_names_full_refresh(self) -> None:
        ctx = MagicMock(scope=InstallScope.PROJECT, declared_direct_refs={"owner/root": ()})
        hint = supply_chain_mismatch_hint(ctx, "owner/child")
        assert "Use 'apm update' to accept new content" in hint
        assert "owner/child" not in hint

    @pytest.mark.parametrize(
        ("command_path", "expected"),
        [
            ("apm update", "update"),
            ("apm install", "install --update"),
            ("apm deps update", "deps update"),
        ],
    )
    def test_auth_label_names_invoked_command(self, command_path: str, expected: str) -> None:
        root = click.Context(click.Group("apm"), info_name="apm")
        ctx = root
        for name in command_path.split()[1:]:
            ctx = click.Context(click.Command(name), parent=ctx, info_name=name)
        with ctx:
            assert ref_update_operation_label() == expected

    def test_auth_label_without_click_context(self) -> None:
        assert ref_update_operation_label() == "update"
