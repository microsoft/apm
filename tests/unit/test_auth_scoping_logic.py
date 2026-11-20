"""Unit tests for auth scoping: tokens are only sent to their matching hosts (pure logic cases)."""

import os
from unittest.mock import patch
from urllib.parse import urlparse

import pytest

from apm_cli.deps.github_downloader import GitHubPackageDownloader
from apm_cli.models.apm_package import DependencyReference

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_downloader(github_token=None, ado_token=None, gitlab_token=None):
    """Create a GitHubPackageDownloader with controlled tokens."""
    with (
        patch.dict(
            os.environ,
            {
                **({"GITHUB_APM_PAT": github_token} if github_token else {}),
                **({"ADO_APM_PAT": ado_token} if ado_token else {}),
                **({"GITLAB_APM_PAT": gitlab_token} if gitlab_token else {}),
            },
            clear=True,
        ),
        patch(
            "apm_cli.core.token_manager.GitHubTokenManager.resolve_credential_from_git",
            return_value=None,
        ),
    ):
        downloader = GitHubPackageDownloader()
    return downloader


def _dep(url_str):
    """Shortcut: parse a DependencyReference from a string."""
    return DependencyReference.parse(url_str)


def _url_host(url: str) -> str:
    """Extract the hostname from an HTTPS or SSH git URL."""
    parsed = urlparse(url)
    if parsed.hostname:
        return parsed.hostname
    # SSH shorthand: git@host:path
    if url.startswith("git@") and ":" in url:
        return url.split("@", 1)[1].split(":", 1)[0]
    raise ValueError(f"Cannot extract host from URL: {url}")


# ===========================================================================
# _build_repo_url - token scoping
# ===========================================================================


class TestBuildRepoUrlTokenScoping:
    """Verify _build_repo_url sends GitHub tokens only to GitHub hosts."""

    def test_github_com_gets_token(self):
        dl = _make_downloader(github_token="ghp_TESTTOKEN")
        dep = _dep("https://github.com/owner/repo.git")
        url = dl._build_repo_url("owner/repo", use_ssh=False, dep_ref=dep)
        assert "ghp_TESTTOKEN" in url
        assert _url_host(url) == "github.com"

    def test_ghe_host_gets_token(self):
        dl = _make_downloader(github_token="ghp_TESTTOKEN")
        dep = _dep("https://company.ghe.com/owner/repo.git")
        url = dl._build_repo_url("owner/repo", use_ssh=False, dep_ref=dep)
        assert "ghp_TESTTOKEN" in url
        assert _url_host(url) == "company.ghe.com"

    def test_gitlab_https_uses_oauth2_not_x_access_token(self):
        """GitLab PAT from GITLAB_APM_PAT is embedded as oauth2:<token>, never x-access-token."""
        dl = _make_downloader(gitlab_token="glpat_GITLABTEST")
        dep = _dep("https://gitlab.com/acme/rules.git")
        dl.auth_resolver._cache.clear()
        with (
            patch.dict(os.environ, {"GITLAB_APM_PAT": "glpat_GITLABTEST"}, clear=True),
            patch(
                "apm_cli.core.token_manager.GitHubTokenManager.resolve_credential_from_git",
                return_value=None,
            ),
        ):
            url = dl._build_repo_url("acme/rules", use_ssh=False, dep_ref=dep)
        assert "x-access-token" not in url.lower()
        assert "oauth2" in url
        assert "glpat_GITLABTEST" in url
        assert _url_host(url) == "gitlab.com"

    def test_gitlab_https_with_token_preserves_custom_port(self):
        dl = _make_downloader(gitlab_token="glpat_GITLABTEST")
        dep = _dep("https://gitlab.corp.example:8443/acme/rules.git")
        dl.auth_resolver._cache.clear()
        with (
            patch.dict(
                os.environ,
                {
                    "GITLAB_HOST": "gitlab.corp.example",
                    "GITLAB_APM_PAT": "glpat_GITLABTEST",
                },
                clear=True,
            ),
            patch(
                "apm_cli.core.token_manager.GitHubTokenManager.resolve_credential_from_git",
                return_value=None,
            ),
        ):
            url = dl._build_repo_url("acme/rules", use_ssh=False, dep_ref=dep)
        parsed = urlparse(url)
        assert parsed.hostname == "gitlab.corp.example"
        assert parsed.port == 8443
        assert parsed.username == "oauth2"
        assert "glpat_GITLABTEST" in url

    def test_gitlab_https_ignores_github_token(self):
        """GitHub module tokens must not be embedded into GitLab clone URLs."""
        dl = _make_downloader(github_token="ghp_SHOULD_NOT_LEAK")
        dep = _dep("https://gitlab.com/acme/rules.git")
        dl.auth_resolver._cache.clear()
        with (
            patch.dict(os.environ, {"GITHUB_APM_PAT": "ghp_SHOULD_NOT_LEAK"}, clear=True),
            patch(
                "apm_cli.core.token_manager.GitHubTokenManager.resolve_credential_from_git",
                return_value=None,
            ),
        ):
            url = dl._build_repo_url("acme/rules", use_ssh=False, dep_ref=dep)
        assert "ghp_SHOULD_NOT_LEAK" not in url
        assert "oauth2" not in url
        assert url == "https://gitlab.com/acme/rules.git"

    def test_bitbucket_does_not_get_github_token(self):
        dl = _make_downloader(github_token="ghp_TESTTOKEN")
        dep = _dep("https://bitbucket.org/team/standards.git")
        url = dl._build_repo_url("team/standards", use_ssh=False, dep_ref=dep)
        assert "ghp_TESTTOKEN" not in url
        assert _url_host(url) == "bitbucket.org"

    def test_self_hosted_does_not_get_github_token(self):
        dl = _make_downloader(github_token="ghp_TESTTOKEN")
        dep = _dep("https://git.company.internal/team/rules.git")
        url = dl._build_repo_url("team/rules", use_ssh=False, dep_ref=dep)
        assert "ghp_TESTTOKEN" not in url

    def test_ssh_url_never_embeds_token(self):
        dl = _make_downloader(github_token="ghp_TESTTOKEN")
        dep = _dep("git@gitlab.com:acme/rules.git")
        url = dl._build_repo_url("acme/rules", use_ssh=True, dep_ref=dep)
        assert "ghp_TESTTOKEN" not in url
        assert _url_host(url) == "gitlab.com"

    def test_github_ssh_also_no_embedded_token(self):
        dl = _make_downloader(github_token="ghp_TESTTOKEN")
        dep = _dep("git@github.com:owner/repo.git")
        url = dl._build_repo_url("owner/repo", use_ssh=True, dep_ref=dep)
        assert "ghp_TESTTOKEN" not in url

    def test_no_token_at_all_plain_url(self):
        dl = _make_downloader()
        dep = _dep("https://github.com/owner/repo.git")
        url = dl._build_repo_url("owner/repo", use_ssh=False, dep_ref=dep)
        assert "@" not in url  # no token embedded


# ===========================================================================
# _clone_with_fallback - env relaxation for generic hosts
# ===========================================================================


# ===========================================================================
# Regression: ssh:// URLs with custom ports (issues #661, #731)
# ===========================================================================


# ===========================================================================
# Object-style dependency entries (parse_from_dict)
# ===========================================================================


class TestParseFromDict:
    """Test DependencyReference.parse_from_dict for object-style entries."""

    def test_basic_git_url(self):
        dep = DependencyReference.parse_from_dict({"git": "https://gitlab.com/acme/rules.git"})
        assert dep.host == "gitlab.com"
        assert dep.repo_url == "acme/rules"
        assert dep.virtual_path is None
        assert dep.reference is None

    def test_git_url_with_path(self):
        dep = DependencyReference.parse_from_dict(
            {
                "git": "https://gitlab.com/acme/rules.git",
                "path": "instructions/security",
            }
        )
        assert dep.host == "gitlab.com"
        assert dep.repo_url == "acme/rules"
        assert dep.virtual_path == "instructions/security"
        assert dep.is_virtual is True

    def test_git_url_with_ref(self):
        dep = DependencyReference.parse_from_dict(
            {
                "git": "https://bitbucket.org/team/standards.git",
                "ref": "v2.0",
            }
        )
        assert dep.host == "bitbucket.org"
        assert dep.reference == "v2.0"

    def test_git_url_with_alias(self):
        dep = DependencyReference.parse_from_dict(
            {
                "git": "git@gitlab.com:acme/rules.git",
                "alias": "my-rules",
            }
        )
        assert dep.alias == "my-rules"
        assert dep.host == "gitlab.com"

    def test_git_url_with_all_fields(self):
        dep = DependencyReference.parse_from_dict(
            {
                "git": "https://gitlab.com/acme/rules.git",
                "path": "prompts/review.prompt.md",
                "ref": "main",
                "alias": "review",
            }
        )
        assert dep.host == "gitlab.com"
        assert dep.repo_url == "acme/rules"
        assert dep.virtual_path == "prompts/review.prompt.md"
        assert dep.is_virtual is True
        assert dep.reference == "main"
        assert dep.alias == "review"

    def test_ssh_git_url(self):
        dep = DependencyReference.parse_from_dict(
            {
                "git": "git@bitbucket.org:team/rules.git",
                "path": "security",
            }
        )
        assert dep.host == "bitbucket.org"
        assert dep.repo_url == "team/rules"
        assert dep.virtual_path == "security"

    def test_path_strips_slashes(self):
        dep = DependencyReference.parse_from_dict(
            {
                "git": "https://gitlab.com/acme/rules.git",
                "path": "/prompts/file.md/",
            }
        )
        assert dep.virtual_path == "prompts/file.md"

    def test_ref_in_url_overridden_by_field(self):
        """'ref' field takes precedence over inline #ref in git URL."""
        dep = DependencyReference.parse_from_dict(
            {
                "git": "https://gitlab.com/acme/rules.git#v1.0",
                "ref": "v2.0",
            }
        )
        assert dep.reference == "v2.0"

    # --- Error cases ---

    def test_missing_git_field(self):
        # With local path support, {"path": "foo"} is treated as a local path attempt.
        # Since "foo" is not a valid local or remote dependency, it raises ValueError.
        with pytest.raises(ValueError):
            DependencyReference.parse_from_dict({"path": "foo"})

    def test_empty_git_field(self):
        with pytest.raises(ValueError, match="non-empty string"):
            DependencyReference.parse_from_dict({"git": ""})

    def test_git_field_not_string(self):
        with pytest.raises(ValueError, match="non-empty string"):
            DependencyReference.parse_from_dict({"git": 42})

    def test_empty_path_field(self):
        with pytest.raises(ValueError, match="'path' field"):
            DependencyReference.parse_from_dict({"git": "https://gitlab.com/a/b.git", "path": ""})

    def test_empty_ref_field(self):
        with pytest.raises(ValueError, match="'ref' field"):
            DependencyReference.parse_from_dict({"git": "https://gitlab.com/a/b.git", "ref": ""})

    def test_empty_alias_field(self):
        with pytest.raises(ValueError, match="'alias' field"):
            DependencyReference.parse_from_dict({"git": "https://gitlab.com/a/b.git", "alias": ""})

    @pytest.mark.parametrize("field", ("alais", "resolved_commit", "version"))
    def test_remote_git_rejects_unknown_fields(self, field: str) -> None:
        expected = (
            "Git dependency field 'version' is unsupported; use 'ref' for a branch, tag, or commit"
            if field == "version"
            else r"Unsupported field\(s\) for git dependency"
        )
        with pytest.raises(ValueError, match=expected):
            DependencyReference.parse_from_dict(
                {
                    "git": "git@gitlab.com:acme/rules.git",
                    field: "unexpected",
                }
            )

    def test_remote_git_rejects_mixed_registry_discriminator(self) -> None:
        with pytest.raises(ValueError, match="cannot mix"):
            DependencyReference.parse_from_dict(
                {
                    "git": "git@gitlab.com:acme/rules.git",
                    "registry": "corp",
                    "id": "rules",
                }
            )


# ===========================================================================
# from_apm_yml - mixed string + dict dependencies
# ===========================================================================


# ===========================================================================
# Dict-style duplicate detection in _validate_and_add_packages_to_apm_yml
# ===========================================================================


# ===========================================================================
# _validate_package_exists env scoping for generic hosts
# ===========================================================================


# ===========================================================================
# GitLab direct host/path shorthand (repo-boundary probing)
# ===========================================================================


# ===========================================================================
# is_github classification edge cases
# ===========================================================================


class TestIsGitHubClassification:
    """Verify is_github is correctly determined for edge-case hosts."""

    def test_empty_host_defaults_to_github(self):
        """When no host is set, packages default to GitHub behavior."""
        _make_downloader(github_token="ghp_test123")
        dep_ref = _dep("microsoft/apm-sample-package")

        # No host set → is_github should be True
        dep_host = dep_ref.host if dep_ref else None
        # Original bug: `dep_host and is_github_hostname(dep_host) or (not dep_host)`
        # With empty/None dep_host, this should return True
        from apm_cli.utils.github_host import is_github_hostname

        is_github = is_github_hostname(dep_host) if dep_host else True
        assert is_github is True

    def test_gitlab_host_is_not_github(self):
        """GitLab host should NOT be classified as GitHub."""
        dep_ref = _dep("gitlab.com/acme/rules")
        from apm_cli.utils.github_host import is_github_hostname

        assert is_github_hostname(dep_ref.host) is False

    def test_ghe_host_is_github(self):
        """GitHub Enterprise host should be classified as GitHub."""
        dep_ref = _dep("https://company.ghe.com/org/repo.git")
        from apm_cli.utils.github_host import is_github_hostname

        assert is_github_hostname(dep_ref.host) is True


# ===========================================================================
# _try_sparse_checkout -- per-dep token resolution
# ===========================================================================
