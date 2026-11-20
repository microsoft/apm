"""Install logger residue reporting for a failed install."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from apm_cli.core.command_logger import InstallLogger
from apm_cli.install.failure_residue import (
    FailedInstallResidue,
)
from apm_cli.models.results import InstallDisposition

pytestmark = pytest.mark.unit

_ROLLBACK_WORDING = ("rolled back", "roll back", "rollback", "restored", "restore", "reverted")


def _assert_no_rollback_claim(text: str) -> None:
    lowered = text.lower()
    for phrase in _ROLLBACK_WORDING:
        assert phrase not in lowered, f"unexpected rollback wording {phrase!r} in: {text}"


def _render(residue: FailedInstallResidue) -> str:
    with (
        patch("apm_cli.core.command_logger._rich_warning") as warning,
        patch("apm_cli.core.command_logger._rich_echo") as echo,
        patch("apm_cli.core.command_logger._rich_info") as info,
    ):
        InstallLogger().failed_install_residue(residue)
    return " ".join(
        str(call.args[0]) for mock in (warning, echo, info) for call in mock.call_args_list
    )


class TestInstallLoggerResidue:
    def test_residue_lists_paths_and_one_next_step(self) -> None:
        rendered = _render(
            FailedInstallResidue(
                files=(".claude/agents/a.md", ".claude/skills/s/SKILL.md"),
                entries=(),
                user_scope=False,
            )
        )

        assert "2 native target files" in rendered
        assert ".claude/agents/a.md" in rendered
        assert ".claude/skills/s/SKILL.md" in rendered
        assert "Remove these files, or re-run 'apm install' (add --force" in rendered
        _assert_no_rollback_claim(rendered)

    def test_user_scope_next_step_reruns_global_install(self) -> None:
        rendered = _render(
            FailedInstallResidue(files=("~/.claude/agents/a.md",), entries=(), user_scope=True)
        )

        assert "~/.claude/agents/a.md" in rendered
        assert "re-run 'apm install -g' (add --force" in rendered

    def test_entries_are_not_called_files(self) -> None:
        rendered = _render(
            FailedInstallResidue(
                files=(), entries=("copilot-app-db://workflows/42",), user_scope=False
            )
        )

        assert (
            "1 native target entry that apm.lock.yaml does not track (not plain files)" in rendered
        )
        assert "copilot-app-db://workflows/42" in rendered
        assert "Remove these files" not in rendered
        assert "Re-run 'apm install' (add --force" in rendered
        _assert_no_rollback_claim(rendered)

    @patch("apm_cli.core.command_logger._rich_error")
    def test_failed_summary_with_residue_drops_uncommitted_claim(self, mock_error):
        logger = InstallLogger()
        with (
            patch("apm_cli.core.command_logger._rich_warning"),
            patch("apm_cli.core.command_logger._rich_echo"),
            patch("apm_cli.core.command_logger._rich_info"),
        ):
            logger.failed_install_residue(
                FailedInstallResidue(files=(".claude/agents/a.md",), entries=(), user_scope=False)
            )

        logger.install_summary(
            apm_count=0, mcp_count=0, errors=1, disposition=InstallDisposition.FAILED
        )

        msg = mock_error.call_args[0][0]
        assert "No install transaction changes were committed" not in msg
        assert "1 native target item listed above remains in place" in msg
        _assert_no_rollback_claim(msg)

    @patch("apm_cli.core.command_logger._rich_error")
    def test_failed_summary_without_residue_is_unchanged(self, mock_error):
        InstallLogger().install_summary(
            apm_count=0, mcp_count=0, errors=1, disposition=InstallDisposition.FAILED
        )

        msg = mock_error.call_args[0][0]
        assert (
            msg
            == "Installation failed with 1 error(s). No install transaction changes were committed."
        )
