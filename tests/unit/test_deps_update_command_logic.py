"""Tests for `apm deps update` command (pure logic cases)."""

import pytest

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# Common patch targets -- the update() function uses lazy imports.


class TestDeadCodeRemoval:
    """Verify broken update helpers have been removed."""

    def test_update_single_package_removed(self):
        """_update_single_package deleted from _utils.py."""
        from apm_cli.commands.deps import _utils as utils

        assert not hasattr(utils, "_update_single_package")

    def test_update_all_packages_removed(self):
        """_update_all_packages deleted from _utils.py."""
        from apm_cli.commands.deps import _utils as utils

        assert not hasattr(utils, "_update_all_packages")

    def test_not_in_package_exports(self):
        """Dead functions not exported from __init__.py."""
        import apm_cli.commands.deps as deps_mod

        all_names = getattr(deps_mod, "__all__", [])
        assert "_update_single_package" not in all_names
        assert "_update_all_packages" not in all_names
