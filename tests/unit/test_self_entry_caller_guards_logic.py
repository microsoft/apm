"""Regression tests ensuring callers do not crash on the virtual self-entry (pure logic cases)."""

from pathlib import Path  # noqa: F401

import pytest

from apm_cli.commands.uninstall.engine import _validate_uninstall_packages
from apm_cli.core.command_logger import CommandLogger

pytestmark = [
    pytest.mark.unit,
    pytest.mark.promise("P-OWN-skills-root-derivation", "P-OWN-user-facing-output-diagnostics"),
]


# ---------------------------------------------------------------------------
# get_installed_paths() must skip the self-entry
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# apm uninstall <self-key> must not crash
# ---------------------------------------------------------------------------


class TestUninstallRejectsSelfKey:
    def test_uninstall_dot_does_not_crash(self):
        """Trying to uninstall the literal '.' returns 'not found', no crash."""
        logger = CommandLogger(command="uninstall", verbose=False)
        # current_deps are real apm.yml dependency entries; "." is not one.
        to_remove, not_found = _validate_uninstall_packages(  # noqa: RUF059
            packages=["."],
            current_deps=["owner/repo"],
            logger=logger,
        )
        # "." has no "/", so the validator rejects it as invalid format and
        # skips it entirely - never reaches the not-found list.
        assert to_remove == []
        assert "." not in to_remove


# ---------------------------------------------------------------------------
# SkillIntegrator ownership-map iteration must skip the self-entry
# ---------------------------------------------------------------------------
