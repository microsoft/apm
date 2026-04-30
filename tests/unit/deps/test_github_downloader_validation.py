"""Regression tests for round-2 panel findings on PR #941.

Covers the security gates added to ``github_downloader_validation``:

* finding 6 -- ``ls-remote`` no longer fails open; a successful ref
  resolution must be paired with a positive shallow-fetch + ``ls-tree``
  path probe before validation returns ``True``.
* finding 7 -- ``virtual_path`` is screened by
  ``validate_path_segments`` before any URL interpolation, so traversal
  segments cannot leak into Contents-API or archive URLs.
* finding 8 -- Azure DevOps tokens are injected via
  ``http.extraheader`` (``Authorization: Bearer ...``) and never
  embedded in the clone URL or visible on the subprocess argv.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from apm_cli.deps import github_downloader_validation as gdv
from apm_cli.deps.github_downloader import GitHubPackageDownloader
from apm_cli.models.apm_package import DependencyReference


def _make_subdir_dep(
    repo_url: str = "owner/repo",
    vpath: str = "skills/my-skill",
    ref: str | None = "main",
    host: str | None = None,
) -> DependencyReference:
    """Build a virtual-subdirectory ``DependencyReference`` for tests."""
    return DependencyReference(
        repo_url=repo_url,
        host=host,
        reference=ref,
        virtual_path=vpath,
        is_virtual=True,
    )


# ---------------------------------------------------------------------------
# Finding 7: path traversal rejection
# ---------------------------------------------------------------------------


class TestVirtualPathTraversalRejection:
    """``..`` segments in ``virtual_path`` MUST be rejected before any HTTP."""

    @pytest.mark.parametrize(
        "bad_path",
        [
            "../etc/passwd",
            "skills/../../../secret",
            "..\\windows\\system32",
            "ok/../bad",
        ],
    )
    def test_traversal_segment_rejected_without_network(self, bad_path: str) -> None:
        downloader = GitHubPackageDownloader()
        dep_ref = _make_subdir_dep(vpath=bad_path)

        # Patch download_raw_file to assert it is never reached: validation
        # must fail BEFORE any URL interpolation occurs.
        with patch.object(downloader, "download_raw_file") as raw_mock:
            ok = gdv.validate_virtual_package_exists(downloader, dep_ref)

        assert ok is False
        raw_mock.assert_not_called()

    def test_clean_path_not_rejected(self) -> None:
        """A normal path falls through to the marker probes (which we mock)."""
        downloader = GitHubPackageDownloader()
        dep_ref = _make_subdir_dep(vpath="skills/clean")

        with patch.object(downloader, "download_raw_file") as raw_mock:
            raw_mock.side_effect = RuntimeError("404")
            with (
                patch.object(gdv, "_directory_exists_at_ref", return_value=False),
                patch.object(gdv, "_ref_exists_via_ls_remote", return_value=False),
            ):
                ok = gdv.validate_virtual_package_exists(downloader, dep_ref)

        assert ok is False
        # Marker probes ran (proving we got past the path-security gate).
        assert raw_mock.call_count >= 1


# ---------------------------------------------------------------------------
# Finding 6: fail-open close
# ---------------------------------------------------------------------------


class TestLsRemoteFailOpenClose:
    """ls-remote success alone MUST NOT validate a typo'd subdirectory."""

    def _patch_marker_misses(self, downloader: GitHubPackageDownloader):
        """Make every download_raw_file probe miss (404)."""
        return patch.object(downloader, "download_raw_file", side_effect=RuntimeError("404"))

    def test_ls_remote_alone_does_not_validate_when_path_missing(self) -> None:
        """Round-2 finding 6: typo'd vpath after a valid ref must return False.

        Reproduces the security regression: previously, a successful
        ls-remote on the ref bypassed all path validation. Now the
        shallow-fetch + ls-tree probe must also confirm vpath.
        """
        downloader = GitHubPackageDownloader()
        dep_ref = _make_subdir_dep(vpath="skills/typo-not-real", ref="main")

        with (
            self._patch_marker_misses(downloader),
            patch.object(gdv, "_directory_exists_at_ref", return_value=False),
            patch.object(gdv, "_ref_exists_via_ls_remote", return_value=True),
            patch.object(gdv, "_path_exists_in_tree_at_ref", return_value=False) as path_probe,
        ):
            ok = gdv.validate_virtual_package_exists(downloader, dep_ref)

        assert ok is False, (
            "Validation must not pass when ls-remote sees the ref but "
            "the subdirectory is absent from the tree."
        )
        path_probe.assert_called_once()

    def test_ls_remote_plus_path_probe_validates(self) -> None:
        """Both gates pass -> validation succeeds, with a deferred-probe warning."""
        downloader = GitHubPackageDownloader()
        dep_ref = _make_subdir_dep(vpath="skills/exists", ref="v1.0.0")
        warnings: list[str] = []

        with (
            self._patch_marker_misses(downloader),
            patch.object(gdv, "_directory_exists_at_ref", return_value=False),
            patch.object(gdv, "_ref_exists_via_ls_remote", return_value=True),
            patch.object(gdv, "_path_exists_in_tree_at_ref", return_value=True),
        ):
            ok = gdv.validate_virtual_package_exists(
                downloader, dep_ref, warn_callback=warnings.append
            )

        assert ok is True
        assert len(warnings) == 1, "expected exactly one deferred-probe warning"
        # Round-2 finding 3: warning text must NOT include literal '[!]'
        # (the logger prepends the symbol).
        assert "[!]" not in warnings[0]
        # Round-2 finding 4: warning must end with an actionable next step.
        assert "To fix" in warnings[0] or "to fix" in warnings[0].lower()

    def test_ls_remote_only_runs_when_explicit_ref(self) -> None:
        """Without an explicit ``#ref`` the lenient fallback is skipped."""
        downloader = GitHubPackageDownloader()
        dep_ref = _make_subdir_dep(vpath="skills/x", ref=None)

        with (
            self._patch_marker_misses(downloader),
            patch.object(gdv, "_directory_exists_at_ref", return_value=False),
            patch.object(gdv, "_ref_exists_via_ls_remote", return_value=True) as ls_remote_mock,
            patch.object(gdv, "_path_exists_in_tree_at_ref", return_value=True) as path_mock,
        ):
            ok = gdv.validate_virtual_package_exists(downloader, dep_ref)

        assert ok is False
        ls_remote_mock.assert_not_called()
        path_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Finding 8: ADO bearer header injection
# ---------------------------------------------------------------------------


class TestAdoBearerHeaderInjection:
    """ADO tokens must travel via ``http.extraheader``, never the URL."""

    def _make_ado_dep(self) -> DependencyReference:
        return DependencyReference(
            repo_url="myorg/myproj/myrepo",
            host="dev.azure.com",
            reference="main",
            virtual_path="skills/x",
            is_virtual=True,
            ado_organization="myorg",
            ado_project="myproj",
            ado_repo="myrepo",
        )

    def test_ado_token_injected_as_header_not_url_in_validation(self) -> None:
        """The token must appear in the env's GIT_CONFIG_VALUE_0, not the URL."""
        downloader = GitHubPackageDownloader()
        dep_ref = self._make_ado_dep()
        secret = "ADO_PAT_SECRET_VALUE_DO_NOT_LEAK"

        ado_mock_ctx = MagicMock()
        ado_mock_ctx.auth_scheme = "basic"
        ado_mock_ctx.git_env = {}

        with (
            patch.object(downloader, "_resolve_dep_token", return_value=secret),
            patch.object(downloader, "_resolve_dep_auth_ctx", return_value=ado_mock_ctx),
            patch.object(
                downloader,
                "_build_repo_url",
                return_value="https://dev.azure.com/myorg/myproj/_git/myrepo",
            ),
            patch.object(
                downloader,
                "_build_noninteractive_git_env",
                return_value={},
            ),
        ):
            attempts = gdv._build_validation_attempts(downloader, dep_ref, log=lambda _m: None)

        assert attempts, "expected at least the token attempt"
        labels = [label for label, _url, _env in attempts]
        # First attempt is the ADO header-injected one.
        assert any("bearer header" in label.lower() for label in labels), labels

        # Find the ADO attempt and assert: token NOT in URL, token IN env header.
        ado_attempts = [a for a in attempts if "bearer header" in a[0].lower()]
        assert len(ado_attempts) == 1
        _label, url, env = ado_attempts[0]

        assert secret not in url, (
            "ADO token must NOT be embedded in the clone URL "
            "(round-2 finding 8 -- prevents leakage to process table / git logs)."
        )

        # The env must carry the token via the http.extraheader mechanism.
        assert env.get("GIT_CONFIG_KEY_0") == "http.extraheader"
        header_value = env.get("GIT_CONFIG_VALUE_0", "")
        assert header_value.startswith("Authorization: Bearer ")
        assert secret in header_value, (
            "Token must travel as an HTTP Authorization header, not via URL."
        )

    def test_non_ado_path_unchanged(self) -> None:
        """GitHub deps still use the existing auth chain (no ADO header overlay)."""
        downloader = GitHubPackageDownloader()
        dep_ref = _make_subdir_dep(repo_url="owner/repo", host="github.com")
        secret = "GH_PAT_SECRET"

        with (
            patch.object(downloader, "_resolve_dep_token", return_value=secret),
            patch.object(downloader, "_resolve_dep_auth_ctx", return_value=None),
            patch.object(
                downloader,
                "_build_repo_url",
                return_value=f"https://x-access-token:{secret}@github.com/owner/repo.git",
            ),
            patch.object(
                downloader,
                "_build_noninteractive_git_env",
                return_value={},
            ),
        ):
            attempts = gdv._build_validation_attempts(downloader, dep_ref, log=lambda _m: None)

        labels = [label for label, _u, _e in attempts]
        assert "authenticated HTTPS" in labels
        # Crucially: no ADO bearer-header attempt for non-ADO deps.
        assert not any("bearer header" in lbl.lower() for lbl in labels)


# ---------------------------------------------------------------------------
# Mechanical guards
# ---------------------------------------------------------------------------


class TestSplitOwnerRepoGuard:
    """Round-2 finding 2: ``repo_url`` without a slash must not raise."""

    def test_returns_none_on_missing_slash(self) -> None:
        assert gdv._split_owner_repo("just-one-segment") is None

    def test_returns_none_on_empty_owner(self) -> None:
        assert gdv._split_owner_repo("/repo") is None

    def test_returns_none_on_empty_repo(self) -> None:
        assert gdv._split_owner_repo("owner/") is None

    def test_returns_pair_for_valid(self) -> None:
        assert gdv._split_owner_repo("owner/repo") == ("owner", "repo")

    def test_directory_probe_returns_false_on_malformed_repo_url(self) -> None:
        """Malformed ``repo_url`` falls through to a clean ``False``."""
        downloader = GitHubPackageDownloader()
        dep_ref = DependencyReference(
            repo_url="malformed-no-slash",
            host="github.com",
            reference="main",
            virtual_path="skills/x",
            is_virtual=True,
        )
        ok = gdv._directory_exists_at_ref(
            downloader, dep_ref, "skills/x", "main", log=lambda _m: None
        )
        assert ok is False
