"""Unit tests for apm_cli.install.gitlab_resolver."""

from __future__ import annotations

from unittest.mock import MagicMock, patch


class TestTryResolveGitlabDirectShorthand:
    """Tests for _try_resolve_gitlab_direct_shorthand."""

    def test_returns_none_when_not_gitlab_shorthand(self) -> None:
        """Non-shorthand strings return None immediately."""
        from apm_cli.install.gitlab_resolver import _try_resolve_gitlab_direct_shorthand

        with patch(
            "apm_cli.install.gitlab_resolver.DependencyReference"
            ".split_gitlab_direct_shorthand_parts",
            return_value=None,
        ):
            result = _try_resolve_gitlab_direct_shorthand("github.com/owner/repo", MagicMock())
        assert result is None

    def test_returns_none_when_no_boundary_candidate_validates(self) -> None:
        """When no boundary candidate passes validation, return None."""
        from apm_cli.install.gitlab_resolver import _try_resolve_gitlab_direct_shorthand

        mock_dep_ref = MagicMock()

        with (
            patch(
                "apm_cli.install.gitlab_resolver.DependencyReference"
                ".split_gitlab_direct_shorthand_parts",
                return_value=("gitlab.com", ["owner", "repo"], "main"),
            ),
            patch(
                "apm_cli.install.gitlab_resolver.DependencyReference"
                ".iter_gitlab_direct_shorthand_boundary_candidates",
                return_value=[("owner/repo", "")],
            ),
            patch(
                "apm_cli.install.gitlab_resolver.DependencyReference.from_gitlab_shorthand_probe",
                return_value=mock_dep_ref,
            ),
            patch(
                "apm_cli.install.gitlab_resolver._validate_package_exists",
                return_value=False,
            ),
        ):
            result = _try_resolve_gitlab_direct_shorthand("gitlab.com/owner/repo", MagicMock())
        assert result is None

    def test_returns_first_valid_candidate(self) -> None:
        """Returns the first candidate that passes validation."""
        from apm_cli.install.gitlab_resolver import _try_resolve_gitlab_direct_shorthand

        candidate_a = MagicMock(name="candidate_a")
        candidate_b = MagicMock(name="candidate_b")

        boundary_candidates = [("owner/repo", ""), ("owner/repo/sub", "")]
        probe_results = [candidate_a, candidate_b]

        with (
            patch(
                "apm_cli.install.gitlab_resolver.DependencyReference"
                ".split_gitlab_direct_shorthand_parts",
                return_value=("gitlab.com", ["owner", "repo"], None),
            ),
            patch(
                "apm_cli.install.gitlab_resolver.DependencyReference"
                ".iter_gitlab_direct_shorthand_boundary_candidates",
                return_value=iter(boundary_candidates),
            ),
            patch(
                "apm_cli.install.gitlab_resolver.DependencyReference.from_gitlab_shorthand_probe",
                side_effect=probe_results,
            ),
            patch(
                "apm_cli.install.gitlab_resolver._validate_package_exists",
                return_value=True,
            ),
        ):
            result = _try_resolve_gitlab_direct_shorthand("gitlab.com/owner/repo", MagicMock())
        assert result is candidate_a

    def test_skips_failed_candidates_and_returns_second(self) -> None:
        """Skips non-validating candidates and returns the next one that validates."""
        from apm_cli.install.gitlab_resolver import _try_resolve_gitlab_direct_shorthand

        candidate_a = MagicMock(name="candidate_a")
        candidate_b = MagicMock(name="candidate_b")

        boundary_candidates = [("owner/repo", ""), ("owner/repo/sub", "")]
        probe_results = [candidate_a, candidate_b]
        # First fails, second succeeds
        validate_results = [False, True]

        with (
            patch(
                "apm_cli.install.gitlab_resolver.DependencyReference"
                ".split_gitlab_direct_shorthand_parts",
                return_value=("gitlab.com", ["owner", "repo"], None),
            ),
            patch(
                "apm_cli.install.gitlab_resolver.DependencyReference"
                ".iter_gitlab_direct_shorthand_boundary_candidates",
                return_value=iter(boundary_candidates),
            ),
            patch(
                "apm_cli.install.gitlab_resolver.DependencyReference.from_gitlab_shorthand_probe",
                side_effect=probe_results,
            ),
            patch(
                "apm_cli.install.gitlab_resolver._validate_package_exists",
                side_effect=validate_results,
            ),
        ):
            result = _try_resolve_gitlab_direct_shorthand("gitlab.com/owner/repo", MagicMock())
        assert result is candidate_b

    def test_creates_auth_resolver_when_none_provided(self) -> None:
        """When auth_resolver is None, AuthResolver() is instantiated."""
        from apm_cli.install.gitlab_resolver import _try_resolve_gitlab_direct_shorthand

        with (
            patch(
                "apm_cli.install.gitlab_resolver.DependencyReference"
                ".split_gitlab_direct_shorthand_parts",
                return_value=None,
            ),
            patch("apm_cli.install.gitlab_resolver.AuthResolver") as mock_auth_cls,
        ):
            _try_resolve_gitlab_direct_shorthand("gitlab.com/owner/repo", None)
        mock_auth_cls.assert_called_once()

    def test_verbose_flag_forwarded_to_validate(self) -> None:
        """verbose=True is passed through to _validate_package_exists."""
        from apm_cli.install.gitlab_resolver import _try_resolve_gitlab_direct_shorthand

        mock_dep_ref = MagicMock()

        with (
            patch(
                "apm_cli.install.gitlab_resolver.DependencyReference"
                ".split_gitlab_direct_shorthand_parts",
                return_value=("gitlab.com", ["owner", "repo"], None),
            ),
            patch(
                "apm_cli.install.gitlab_resolver.DependencyReference"
                ".iter_gitlab_direct_shorthand_boundary_candidates",
                return_value=[("owner/repo", "")],
            ),
            patch(
                "apm_cli.install.gitlab_resolver.DependencyReference.from_gitlab_shorthand_probe",
                return_value=mock_dep_ref,
            ),
            patch(
                "apm_cli.install.gitlab_resolver._validate_package_exists",
                return_value=True,
            ) as mock_validate,
        ):
            _try_resolve_gitlab_direct_shorthand("gitlab.com/owner/repo", MagicMock(), verbose=True)
        call_kwargs = mock_validate.call_args[1]
        assert call_kwargs.get("verbose") is True
