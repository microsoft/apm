"""GitLab routing and credential-scope contract tests."""

import os
from unittest.mock import Mock, patch
from urllib.parse import urlparse

from apm_cli.deps.github_downloader import GitHubPackageDownloader
from apm_cli.models.apm_package import DependencyReference

_CRED_FILL_PATCH = patch(
    "apm_cli.core.token_manager.GitHubTokenManager.resolve_credential_from_git",
    return_value=None,
)


def _download_from_bespoke_gitlab_host(env: dict[str, str]) -> tuple[str, dict[str, str]]:
    """Route an object-form GitLab dependency and return its HTTP request."""
    dep_ref = DependencyReference.parse_from_dict(
        {"git": "https://code.acme.com/group/sub/repo.git", "type": "gitlab"}
    )
    response = Mock(status_code=200, content=b"gitlab raw", headers={})

    with patch.dict(os.environ, env, clear=True), _CRED_FILL_PATCH:
        downloader = GitHubPackageDownloader()
        with patch.object(downloader, "_resilient_get", return_value=response) as mock_get:
            result = downloader._download_github_file(dep_ref, "SKILL.md", "main")

    assert result == b"gitlab raw"
    assert dep_ref.host_type == "gitlab"
    return mock_get.call_args[0][0], mock_get.call_args[1]["headers"]


def test_type_gitlab_routes_untrusted_bespoke_host_without_private_token() -> None:
    """Explicit GitLab type selects its API without trusting the host for auth."""
    request_url, headers = _download_from_bespoke_gitlab_host({"GITLAB_APM_PAT": "glpat-bespoke"})

    parsed = urlparse(request_url)
    assert parsed.hostname == "code.acme.com"
    assert parsed.path.endswith("/repository/files/SKILL.md/raw")
    assert "PRIVATE-TOKEN" not in headers


def test_type_gitlab_routes_trusted_bespoke_host_with_private_token() -> None:
    """APM_GITLAB_HOSTS opts the bespoke host into GitLab token delivery."""
    request_url, headers = _download_from_bespoke_gitlab_host(
        {
            "APM_GITLAB_HOSTS": "code.acme.com",
            "GITLAB_APM_PAT": "glpat-bespoke",
        }
    )

    parsed = urlparse(request_url)
    assert parsed.hostname == "code.acme.com"
    assert parsed.path.endswith("/repository/files/SKILL.md/raw")
    assert headers.get("PRIVATE-TOKEN") == "glpat-bespoke"
