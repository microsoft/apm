"""Environment-only MCP selection stays behind the auth authority."""

import os
from unittest.mock import patch

import pytest

from apm_cli.core.auth import AuthResolver
from apm_cli.core.token_manager import GitHubTokenManager

pytestmark = pytest.mark.unit
SOURCES = (
    "GITHUB_COPILOT_PAT",
    "GITHUB_TOKEN",
    "GITHUB_APM_PAT",
    "GITHUB_PERSONAL_ACCESS_TOKEN",
)


@pytest.mark.parametrize("winner", range(len(SOURCES)))
@pytest.mark.parametrize("source_only", [False, True])
def test_mcp_selection_preserves_precedence(winner: int, source_only: bool) -> None:
    """The selected name and literal value identify the same precedence winner."""
    env = {
        name: f"sentinel-{index}" if index >= winner else "" for index, name in enumerate(SOURCES)
    }
    with patch.dict(os.environ, env, clear=True):
        result = AuthResolver().resolve_github_mcp_token(source_only=source_only)
    assert result == (SOURCES[winner] if source_only else f"sentinel-{winner}")


@pytest.mark.parametrize("source_only", [False, True])
def test_mcp_selection_never_uses_repository_or_helper_sources(source_only: bool) -> None:
    """Neither repository-only variables nor installed credential helpers are MCP sources."""
    with (
        patch.dict(
            os.environ,
            {"GH_TOKEN": "excluded-gh", "GITHUB_APM_PAT_ORG": "excluded-org"},
            clear=True,
        ),
        patch.object(GitHubTokenManager, "resolve_credential_from_gh_cli") as gh,
        patch.object(GitHubTokenManager, "resolve_credential_from_git") as git,
    ):
        assert AuthResolver().resolve_github_mcp_token(source_only=source_only) is None
    gh.assert_not_called()
    git.assert_not_called()


def test_source_only_does_not_request_a_literal_token() -> None:
    """A runtime reference must not call the value-returning token accessor."""
    with (
        patch.dict(os.environ, {"GITHUB_TOKEN": "sentinel-runtime"}, clear=True),
        patch.object(GitHubTokenManager, "get_token_for_purpose") as value_lookup,
    ):
        assert AuthResolver().resolve_github_mcp_token(source_only=True) == "GITHUB_TOKEN"
    value_lookup.assert_not_called()
