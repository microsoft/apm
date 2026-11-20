"""Unit tests for the ``apm outdated`` command (pure logic cases)."""

import pytest

from apm_cli.deps.lockfile import LockedDependency

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-read-only-lockfile-path")]

# ---------------------------------------------------------------------------
# Patch targets -- imports are lazy (inside function body), so we patch
# at the source module level.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------


class TestPackageBasename:
    """Direct unit tests for _package_basename edge cases."""

    @pytest.mark.parametrize(
        "virtual_path,expected",
        [
            ("packages/agent-dev-workflow", "agent-dev-workflow"),
            ("packages/agent-dev-workflow/", "agent-dev-workflow"),
            ("packages/deep/nested/pkg", "pkg"),
            ("pkg", "pkg"),
        ],
    )
    def test_virtual_subdir_basename_variants(self, virtual_path, expected):
        """Trailing slashes, deep nesting, and simple names all resolve correctly."""
        dep = LockedDependency(
            repo_url="org/monorepo",
            resolved_ref="v1.0.0",
            is_virtual=True,
            virtual_path=virtual_path,
        )
        from apm_cli.commands.outdated import _package_basename

        assert _package_basename(dep) == expected
