"""Credential-selection characterization for parsed dependency references."""

from __future__ import annotations

import os
from typing import NamedTuple
from unittest.mock import patch

import pytest

from apm_cli.core.auth import AuthResolver
from apm_cli.core.token_manager import GitHubTokenManager
from apm_cli.models.dependency.reference import DependencyReference

pytestmark = pytest.mark.unit

HELPER_TOKEN = "helper-token"
GH_CLI_TOKEN = "gh-cli-token"


class CredentialCase(NamedTuple):
    """One dependency reference credential-selection scenario."""

    shape: str
    reference: str
    env: dict[str, str]
    expected_host: str
    expected_repo_url: str
    expected_org: str
    expected_source: str
    expected_token: str
    expected_virtual_path: str | None = None
    expected_artifactory_prefix: str | None = None


SUPPORTED_SHAPE_CASES: tuple[CredentialCase, ...] = (
    CredentialCase(
        "github.com standard owner/repo",
        "github.com/acme/repo",
        {"GITHUB_APM_PAT_ACME": "org-token", "GITHUB_APM_PAT": "global-token"},
        "github.com",
        "acme/repo",
        "acme",
        "GITHUB_APM_PAT_ACME",
        "org-token",
    ),
    CredentialCase(
        "GitHub Enterprise Server FQDN",
        "github.corp.example.com/acme/repo",
        {
            "GITHUB_HOST": "github.corp.example.com",
            "GITHUB_APM_PAT_ACME": "org-token",
            "GITHUB_APM_PAT": "global-token",
        },
        "github.corp.example.com",
        "acme/repo",
        "acme",
        "GITHUB_APM_PAT_ACME",
        "org-token",
    ),
    CredentialCase(
        "FQDN with virtual subpath",
        "git.example.com/team/repo/prompts/review.prompt.md",
        {},
        "git.example.com",
        "team/repo",
        "team",
        "git-credential-fill",
        HELPER_TOKEN,
        "prompts/review.prompt.md",
    ),
    CredentialCase(
        "Azure DevOps dev.azure.com _git",
        "dev.azure.com/acme/project/_git/repo",
        {"ADO_APM_PAT": "ado-token"},
        "dev.azure.com",
        "acme/project/repo",
        "acme",
        "ADO_APM_PAT",
        "ado-token",
    ),
    CredentialCase(
        "Azure DevOps legacy visualstudio.com",
        "acme.visualstudio.com/project/_git/repo",
        {"ADO_APM_PAT": "ado-token"},
        "acme.visualstudio.com",
        "acme/project/repo",
        "acme",
        "ADO_APM_PAT",
        "ado-token",
    ),
    CredentialCase(
        "GitLab top-level group",
        "gitlab.com/group/repo",
        {
            "GITLAB_APM_PAT": "gitlab-token",
            "GITHUB_APM_PAT_GROUP": "wrong-token",
            "GITHUB_APM_PAT": "wrong-global",
        },
        "gitlab.com",
        "group/repo",
        "group",
        "GITLAB_APM_PAT",
        "gitlab-token",
    ),
    CredentialCase(
        "GitLab nested subgroup",
        "gitlab.com/group/subgroup/repo",
        {
            "GITLAB_APM_PAT": "gitlab-token",
            "GITHUB_APM_PAT_GROUP": "wrong-token",
            "GITHUB_APM_PAT": "wrong-global",
        },
        "gitlab.com",
        "group/subgroup/repo",
        "group",
        "GITLAB_APM_PAT",
        "gitlab-token",
    ),
    CredentialCase(
        "Artifactory virtual-repo shorthand",
        "artifactory.example.com/artifactory/github/acme/repo/.agents/skills/foo",
        {},
        "artifactory.example.com",
        "acme/repo",
        "acme",
        "git-credential-fill",
        HELPER_TOKEN,
        ".agents/skills/foo",
        "artifactory/github",
    ),
    CredentialCase(
        "ssh:// reference",
        "ssh://git@github.com/acme/repo.git",
        {"GITHUB_APM_PAT_ACME": "org-token", "GITHUB_APM_PAT": "global-token"},
        "github.com",
        "acme/repo",
        "acme",
        "GITHUB_APM_PAT_ACME",
        "org-token",
    ),
    CredentialCase(
        "SCP-style reference",
        "git@github.com:acme/repo.git",
        {"GITHUB_APM_PAT_ACME": "org-token", "GITHUB_APM_PAT": "global-token"},
        "github.com",
        "acme/repo",
        "acme",
        "GITHUB_APM_PAT_ACME",
        "org-token",
    ),
)


@pytest.mark.parametrize(
    "case",
    SUPPORTED_SHAPE_CASES,
    ids=[case.shape for case in SUPPORTED_SHAPE_CASES],
)
def test_credential_selection_supported_host_shapes(case: CredentialCase) -> None:
    with (
        patch.dict(os.environ, case.env, clear=True),
        patch.object(
            GitHubTokenManager,
            "resolve_credential_from_gh_cli",
            return_value=None,
        ),
        patch.object(
            GitHubTokenManager,
            "resolve_credential_from_git",
            return_value=HELPER_TOKEN,
        ),
    ):
        dep = DependencyReference.parse(case.reference)
        resolver = AuthResolver()
        ctx = resolver.resolve_for_dep(dep)
        cached_again = resolver.resolve(
            case.expected_host,
            case.expected_org,
            port=dep.port,
            host_type=dep.host_type,
        )

    cache_keys = list(resolver._cache)
    assert dep.host == case.expected_host
    assert dep.repo_url == case.expected_repo_url
    assert dep.virtual_path == case.expected_virtual_path
    assert dep.artifactory_prefix == case.expected_artifactory_prefix
    assert ctx.host_info.host == case.expected_host
    assert ctx.source == case.expected_source
    assert ctx.token == case.expected_token
    assert cached_again is ctx
    assert len(cache_keys) == 1
    assert (cache_keys[0].host, cache_keys[0].org) == (
        case.expected_host,
        case.expected_org,
    )


@pytest.mark.parametrize(
    ("env", "gh_cli_token", "git_helper_token", "expected_source", "expected_token"),
    (
        (
            {"GITHUB_APM_PAT": "apm-token", "GITHUB_TOKEN": "github-token"},
            GH_CLI_TOKEN,
            HELPER_TOKEN,
            "GITHUB_APM_PAT",
            "apm-token",
        ),
        (
            {"GITHUB_TOKEN": "github-token", "GH_TOKEN": "gh-env-token"},
            GH_CLI_TOKEN,
            HELPER_TOKEN,
            "GITHUB_TOKEN",
            "github-token",
        ),
        ({}, GH_CLI_TOKEN, HELPER_TOKEN, "gh-auth-token", GH_CLI_TOKEN),
        ({}, None, HELPER_TOKEN, "git-credential-fill", HELPER_TOKEN),
        ({}, None, None, "none", None),
    ),
)
def test_github_credential_source_precedence_is_unchanged(
    env: dict[str, str],
    gh_cli_token: str | None,
    git_helper_token: str | None,
    expected_source: str,
    expected_token: str | None,
) -> None:
    with (
        patch.dict(os.environ, env, clear=True),
        patch.object(
            GitHubTokenManager,
            "resolve_credential_from_gh_cli",
            return_value=gh_cli_token,
        ),
        patch.object(
            GitHubTokenManager,
            "resolve_credential_from_git",
            return_value=git_helper_token,
        ),
    ):
        dep = DependencyReference.parse("github.com/acme/repo")
        ctx = AuthResolver().resolve_for_dep(dep)

    assert ctx.source == expected_source
    assert ctx.token == expected_token


def test_unknown_platform_virtual_host_fails_before_credential_lookup() -> None:
    with (
        patch.dict(os.environ, {"GITHUB_APM_PAT": "must-not-use"}, clear=True),
        patch.object(
            GitHubTokenManager,
            "resolve_credential_from_gh_cli",
            return_value=GH_CLI_TOKEN,
        ) as gh_cli,
        patch.object(
            GitHubTokenManager,
            "resolve_credential_from_git",
            return_value=HELPER_TOKEN,
        ) as git_fill,
        pytest.raises(ValueError) as excinfo,
    ):
        DependencyReference.parse(
            "github.corp.example.com/acme/internal-skills/.agents/skills/jdk-installer"
        )

    assert excinfo.value.__class__.__name__ == "UnsupportedHostQualifiedVirtualPackageError"
    assert excinfo.value.host == "github.corp.example.com"
    gh_cli.assert_not_called()
    git_fill.assert_not_called()
