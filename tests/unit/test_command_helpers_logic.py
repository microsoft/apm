"""Tests for shared CLI helper functions in apm_cli.commands._helpers (pure logic cases)."""

import os
import tempfile  # noqa: F401
from pathlib import Path  # noqa: F401
from unittest.mock import MagicMock, patch  # noqa: F401

import pytest
import yaml  # noqa: F401

from apm_cli.commands._helpers import (
    _check_and_notify_updates,
    _expand_with_ancestors,
)
from apm_cli.install.orphans import _find_orphaned_packages

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-orphan-package-selection")]

# ---------------------------------------------------------------------------
# _atomic_write
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _update_gitignore_for_apm_modules
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _load_apm_config / _get_default_script / _list_available_scripts
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _scan_installed_packages
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _expand_with_ancestors
# ---------------------------------------------------------------------------


class TestExpandWithAncestors:
    """Tests for _expand_with_ancestors."""

    def test_adds_intermediate_ancestors(self):
        """Subdirectory path produces install-root ancestors only.

        Depth-cap security contract: ancestors past depth 3 are NOT
        emitted (see _expand_with_ancestors docstring). For a path
        like ``owner/repo/.apm/skills/my-skill`` only the 2-segment
        and 3-segment prefixes are added.
        """
        paths = {"owner/repo/.apm/skills/my-skill"}
        result = _expand_with_ancestors(paths)
        assert "owner/repo" in result
        assert "owner/repo/.apm" in result
        assert "owner/repo/.apm/skills/my-skill" in result
        # Depth cap: deeper ancestors are intentionally not emitted.
        assert "owner/repo/.apm/skills" not in result

    def test_two_segment_path_unchanged(self):
        """A 2-segment path has no intermediate ancestors to add."""
        paths = {"owner/repo"}
        result = _expand_with_ancestors(paths)
        assert result == {"owner/repo"}

    def test_empty_input(self):
        """Empty set returns empty set."""
        assert _expand_with_ancestors(set()) == set()

    def test_three_segment_ado_path(self):
        """ADO-style org/project/repo produces org/project as ancestor."""
        paths = {"org/project/repo"}
        result = _expand_with_ancestors(paths)
        assert "org/project/repo" in result
        assert "org/project" in result

    def test_no_false_prefix_overlap(self):
        """owner/repo-extra does not collide with owner/repo."""
        paths = {"owner/repo-extra/.apm/skills/x"}
        result = _expand_with_ancestors(paths)
        assert "owner/repo-extra" in result
        assert "owner/repo" not in result

    def test_skips_path_traversal(self):
        """Paths containing '..' are skipped during expansion."""
        paths = {"owner/../etc/passwd"}
        result = _expand_with_ancestors(paths)
        # Original path is kept (it's in the input set), but no ancestors are generated
        assert "owner/../etc/passwd" in result
        assert "owner/.." not in result

    def test_skips_backslash_traversal(self):
        """Backslash-encoded traversal cannot bypass the '..' guard.

        Regression for the supply-chain finding: prior implementation
        called ``p.split('/')`` directly, so a token like
        ``owner\\..\\evil/sub`` parsed as a single segment containing
        ``..`` and slipped past the guard. The fix normalises ``\\``
        -> ``/`` before splitting.
        """
        paths = {"owner\\..\\evil/sub"}
        result = _expand_with_ancestors(paths)
        # Original kept (membership semantics), but NO ancestor must
        # leak into the expansion.
        assert "owner\\..\\evil/sub" in result
        assert "owner" not in result
        assert "owner/.." not in result
        assert "owner\\..\\evil" not in result

    def test_installed_guard_protects_real_orphan(self):
        """When ``installed`` lists a real standalone package that is
        also an ancestor of an expected subdir dep, the ancestor is
        NOT added to the expansion -- so the real package can still
        be detected as an orphan.
        """
        paths = {"owner/repo/.apm/skills/foo"}
        result = _expand_with_ancestors(paths, installed={"owner/repo"})
        assert "owner/repo" not in result, (
            "Real installed package must not be masked by ancestor expansion"
        )

    def test_depth_cap_bounds_ancestor_emission(self):
        """Ancestors past depth 3 are not emitted (security cap).

        ``_scan_installed_packages`` skips dotted dirs and doesn't see
        intermediates past depth 3, so emitting them would only widen
        the orphan-suppression surface.
        """
        paths = {"owner/repo/.apm/skills/foo/extra/deeper"}
        result = _expand_with_ancestors(paths)
        assert "owner/repo" in result
        # Cap stops emission at depth 3 (exclusive index 4).
        assert "owner/repo/.apm/skills" not in result
        assert "owner/repo/.apm/skills/foo" not in result


# ---------------------------------------------------------------------------
# _check_orphaned_packages -- subdirectory ancestor false-positive fix
# ---------------------------------------------------------------------------


@pytest.mark.windows_compat
@pytest.mark.parametrize("separator", ["/", "\\"])
def test_orphan_selector_uses_segment_bounded_paths(separator: str) -> None:
    """Normalize Windows tokens without confusing adjacent roots with descendants."""
    expected = {
        "alias/skills/child".replace("/", separator),
        "owner/bundle".replace("/", separator),
    }
    installed = ["alias", "alias-other", "owner/bundle/skills/embedded", "owner/bundle-other"]
    assert _find_orphaned_packages(installed, expected) == ["alias-other", "owner/bundle-other"]


@pytest.mark.parametrize("invalid", ["owner/../repo", "owner/./repo"])
def test_orphan_selector_invalid_tokens_do_not_protect_ancestors(invalid: str) -> None:
    """Malformed tokens cannot widen retention beyond an exact match."""
    assert _find_orphaned_packages(["owner", "owner/repo"], {invalid}) == ["owner", "owner/repo"]


# ---------------------------------------------------------------------------
# _check_and_notify_updates
# ---------------------------------------------------------------------------


class TestCheckAndNotifyUpdates:
    """Tests for _check_and_notify_updates."""

    def test_skips_when_self_update_disabled(self):
        """Returns immediately when distribution disables self-update."""
        with patch("apm_cli.commands._helpers.is_self_update_enabled", return_value=False):
            with patch("apm_cli.commands._helpers.check_for_updates") as mock_check:
                _check_and_notify_updates()
                mock_check.assert_not_called()

    def test_skips_in_e2e_test_mode(self):
        """Returns immediately when APM_E2E_TESTS=1 is set."""
        with patch.dict(os.environ, {"APM_E2E_TESTS": "1"}):
            with patch("apm_cli.commands._helpers.check_for_updates") as mock_check:
                _check_and_notify_updates()
                mock_check.assert_not_called()

    def test_skips_for_unknown_version(self):
        """Returns immediately when current version is 'unknown' (dev)."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("APM_E2E_TESTS", None)
            with patch("apm_cli.commands._helpers.get_version", return_value="unknown"):
                with patch("apm_cli.commands._helpers.check_for_updates") as mock_check:
                    _check_and_notify_updates()
                    mock_check.assert_not_called()

    def test_no_output_when_up_to_date(self):
        """Does not warn when check_for_updates returns None."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("APM_E2E_TESTS", None)
            with patch("apm_cli.commands._helpers.get_version", return_value="1.0.0"):
                with patch("apm_cli.commands._helpers.check_for_updates", return_value=None):
                    with patch("apm_cli.commands._helpers._rich_warning") as mock_warn:
                        _check_and_notify_updates()
                        mock_warn.assert_not_called()

    def test_warns_when_update_available(self):
        """Calls _rich_warning when a newer version is found."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("APM_E2E_TESTS", None)
            with patch("apm_cli.commands._helpers.get_version", return_value="1.0.0"):
                with patch("apm_cli.commands._helpers.check_for_updates", return_value="1.1.0"):
                    with patch("apm_cli.commands._helpers._rich_warning") as mock_warn:
                        _check_and_notify_updates()
                        mock_warn.assert_called_once()
                        call_args = mock_warn.call_args[0][0]
                        assert "1.1.0" in call_args

    def test_silently_ignores_check_exception(self):
        """Does not raise when check_for_updates throws."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("APM_E2E_TESTS", None)
            with patch("apm_cli.commands._helpers.get_version", return_value="1.0.0"):
                with patch(
                    "apm_cli.commands._helpers.check_for_updates",
                    side_effect=RuntimeError("network error"),
                ):
                    # Should not raise
                    _check_and_notify_updates()


# ---------------------------------------------------------------------------
# Round-3 panel regressions:
#   - traversal guard must route through canonical validate_path_segments
#   - _standalone_installed_packages must NOT swallow corruption errors
# ---------------------------------------------------------------------------


class TestExpandWithAncestorsRoutesThroughCanonicalGuard:
    """Round-3 supply-chain finding: ad-hoc ``..`` check must be replaced
    by ``apm_cli.utils.path_security.validate_path_segments`` so the
    project keeps a single chokepoint for path-segment validation and
    also catches single-dot (``.``) traversal segments.
    """

    def test_helpers_traversal_uses_validate_path_segments(self):
        """``_expand_with_ancestors`` calls the canonical guard once per
        input path. Mocking the guard and asserting it was called proves
        the hand-rolled ``".." in parts`` check is gone.
        """
        with patch("apm_cli.commands._helpers.validate_path_segments") as mock_guard:
            _expand_with_ancestors({"owner/repo/.apm/skills/foo"})
            assert mock_guard.called, (
                "Ancestor expansion must route every input through "
                "validate_path_segments rather than a hand-rolled '..' check"
            )
            called_paths = {call.args[0] for call in mock_guard.call_args_list}
            assert "owner/repo/.apm/skills/foo" in called_paths

    def test_single_dot_segment_now_rejected(self):
        """Single-dot segments are rejected by the canonical guard
        (which the prior ad-hoc ``".." in parts`` check missed). The
        path is kept in the result (membership semantics) but no
        ancestors are emitted.
        """
        result = _expand_with_ancestors({"owner/./repo"})
        assert "owner/./repo" in result
        assert "owner" not in result
        assert "owner/." not in result
