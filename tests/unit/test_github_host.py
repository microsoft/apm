import re

import pytest

from apm_cli.utils import github_host
from apm_cli.utils.github_host import build_raw_content_url, is_valid_fqdn


def test_build_raw_content_url():
    """build_raw_content_url returns the correct raw.githubusercontent.com URL."""
    url = build_raw_content_url("microsoft", "apm", "main", "README.md")
    assert url == "https://raw.githubusercontent.com/microsoft/apm/main/README.md"


def test_build_raw_content_url_nested_path():
    """build_raw_content_url handles nested file paths."""
    url = build_raw_content_url("owner", "repo", "v1.0.0", "agents/api-architect.agent.md")
    assert (
        url == "https://raw.githubusercontent.com/owner/repo/v1.0.0/agents/api-architect.agent.md"
    )


def test_build_raw_content_url_slashed_ref():
    """build_raw_content_url encodes slashes in refs (e.g. feature/foo)."""
    url = build_raw_content_url("owner", "repo", "feature/foo", "README.md")
    assert url == "https://raw.githubusercontent.com/owner/repo/feature%2Ffoo/README.md"


def test_valid_fqdns():
    valid_hosts = [
        "github.com",
        "github.com/user/repo",
        "example.com",
        "sub.example.co.uk",
        "a1b2.example",
        "xn--example.com",  # punycode-like label
        "my-service.localdomain.com",
    ]

    for host in valid_hosts:
        assert is_valid_fqdn(host), f"Expected '{host}' to be valid FQDN"


def test_invalid_fqdns():
    invalid_hosts = [
        "",
        None,  # function treats falsy values as invalid
        "localhost",
        "no_dot",
        "-startdash.com",
        "enddash-.com",
        "two..dots.com",
        "a.-b.com",
        "invalid_domain",
    ]

    for host in invalid_hosts:
        # allow passing None without raising (function handles falsy)
        assert not is_valid_fqdn(host), f"Expected '{host}' to be invalid FQDN"


def test_default_host_env_override(monkeypatch):
    monkeypatch.setenv("GITHUB_HOST", "example.ghe.com")
    assert github_host.default_host() == "example.ghe.com"
    monkeypatch.delenv("GITHUB_HOST", raising=False)


def test_is_github_hostname_defaults():
    assert github_host.is_github_hostname(github_host.default_host())
    assert github_host.is_github_hostname("org.ghe.com")
    assert not github_host.is_github_hostname("example.com")


def test_is_gitlab_hostname_saas():
    assert github_host.is_gitlab_hostname("gitlab.com")
    assert github_host.is_gitlab_hostname("GitLab.COM")


def test_is_gitlab_hostname_env_gitlab_host(monkeypatch):
    monkeypatch.setenv("GITLAB_HOST", "git.selfmanaged.example.org")
    assert github_host.is_gitlab_hostname("git.selfmanaged.example.org")
    assert not github_host.is_gitlab_hostname("other.example.org")
    monkeypatch.delenv("GITLAB_HOST", raising=False)


def test_is_gitlab_hostname_env_apm_gitlab_hosts(monkeypatch):
    monkeypatch.setenv("APM_GITLAB_HOSTS", "a.example.com, b.example.com")
    assert github_host.is_gitlab_hostname("a.example.com")
    assert github_host.is_gitlab_hostname("b.example.com")
    monkeypatch.delenv("APM_GITLAB_HOSTS", raising=False)


def test_is_gitlab_hostname_ghes_precedence_over_gitlab_list(monkeypatch):
    """GITHUB_HOST match is GHES, not GitLab, even if also listed for GitLab."""
    monkeypatch.setenv("GITHUB_HOST", "git.company.com")
    monkeypatch.setenv("APM_GITLAB_HOSTS", "git.company.com, other.gitlab.org")
    assert not github_host.is_gitlab_hostname("git.company.com")
    assert github_host.is_gitlab_hostname("other.gitlab.org")
    monkeypatch.delenv("GITHUB_HOST", raising=False)
    monkeypatch.delenv("APM_GITLAB_HOSTS", raising=False)


def test_has_github_gitlab_host_env_conflict_gitlab_host(monkeypatch):
    monkeypatch.setenv("GITHUB_HOST", "git.epam.com")
    monkeypatch.setenv("GITLAB_HOST", "git.epam.com")
    assert github_host.has_github_gitlab_host_env_conflict("git.epam.com")
    monkeypatch.delenv("GITHUB_HOST", raising=False)
    monkeypatch.delenv("GITLAB_HOST", raising=False)


def test_has_github_gitlab_host_env_conflict_apm_gitlab_hosts(monkeypatch):
    monkeypatch.setenv("GITHUB_HOST", "git.company.com")
    monkeypatch.setenv("APM_GITLAB_HOSTS", "git.company.com, other.gitlab.org")
    assert github_host.has_github_gitlab_host_env_conflict("git.company.com")
    assert not github_host.has_github_gitlab_host_env_conflict("other.gitlab.org")
    monkeypatch.delenv("GITHUB_HOST", raising=False)
    monkeypatch.delenv("APM_GITLAB_HOSTS", raising=False)


def test_has_github_gitlab_host_env_conflict_false_gitlab_only(monkeypatch):
    monkeypatch.setenv("GITLAB_HOST", "git.epam.com")
    assert not github_host.has_github_gitlab_host_env_conflict("git.epam.com")
    monkeypatch.delenv("GITLAB_HOST", raising=False)


def test_maybe_raise_bare_fqdn_two_segments_after_host_no_raise(monkeypatch):
    monkeypatch.setenv("GITHUB_HOST", "git.epam.com")
    monkeypatch.setenv("GITLAB_HOST", "git.epam.com")
    github_host.maybe_raise_bare_fqdn_github_gitlab_conflict("git.epam.com/epm-ease/apm-registry")
    monkeypatch.delenv("GITHUB_HOST", raising=False)
    monkeypatch.delenv("GITLAB_HOST", raising=False)


def test_maybe_raise_bare_fqdn_ambiguous_raises(monkeypatch):
    monkeypatch.setenv("GITHUB_HOST", "git.epam.com")
    monkeypatch.setenv("GITLAB_HOST", "git.epam.com")
    with pytest.raises(ValueError, match="both GitHub Enterprise"):
        github_host.maybe_raise_bare_fqdn_github_gitlab_conflict(
            "git.epam.com/epm-ease/apm-registry/agents/ai-run-ba-flow"
        )
    monkeypatch.delenv("GITHUB_HOST", raising=False)
    monkeypatch.delenv("GITLAB_HOST", raising=False)


def test_is_azure_devops_hostname():
    """Test Azure DevOps hostname detection."""
    # Valid Azure DevOps hosts
    assert github_host.is_azure_devops_hostname("dev.azure.com")
    assert github_host.is_azure_devops_hostname("ssh.dev.azure.com")
    assert github_host.is_azure_devops_hostname("mycompany.visualstudio.com")
    assert github_host.is_azure_devops_hostname("contoso.visualstudio.com")

    # Invalid hosts
    assert not github_host.is_azure_devops_hostname("github.com")
    assert not github_host.is_azure_devops_hostname("example.com")
    assert not github_host.is_azure_devops_hostname("azure.com")
    assert not github_host.is_azure_devops_hostname("visualstudio.com")  # Must have org prefix
    assert not github_host.is_azure_devops_hostname(None)
    assert not github_host.is_azure_devops_hostname("")


def test_is_supported_git_host():
    """Test unified Git host detection supporting all platforms."""
    # GitHub hosts
    assert github_host.is_supported_git_host("github.com")
    assert github_host.is_supported_git_host("company.ghe.com")

    # Azure DevOps hosts
    assert github_host.is_supported_git_host("dev.azure.com")
    assert github_host.is_supported_git_host("mycompany.visualstudio.com")

    # Generic git hosts (supported via valid FQDN)
    assert github_host.is_supported_git_host("gitlab.com")
    assert github_host.is_supported_git_host("bitbucket.org")
    assert github_host.is_supported_git_host("gitea.example.com")
    assert github_host.is_supported_git_host("git.company.internal")

    # Invalid hostnames (not valid FQDNs)
    assert not github_host.is_supported_git_host("localhost")
    assert not github_host.is_supported_git_host(None)
    assert not github_host.is_supported_git_host("")


def test_is_supported_git_host_with_custom_host(monkeypatch):
    """Test that GITHUB_HOST env var adds custom host to supported list."""
    # Set a custom Azure DevOps Server host
    monkeypatch.setenv("GITHUB_HOST", "ado.mycompany.internal")

    # Custom host should now be supported
    assert github_host.is_supported_git_host("ado.mycompany.internal")

    # Standard hosts should still work
    assert github_host.is_supported_git_host("github.com")
    assert github_host.is_supported_git_host("dev.azure.com")

    monkeypatch.delenv("GITHUB_HOST", raising=False)


def test_sanitize_token_url_in_message():
    host = github_host.default_host()
    msg = f"fatal: Authentication failed for 'https://ghp_secret@{host}/user/repo.git'"
    sanitized = github_host.sanitize_token_url_in_message(msg, host=host)
    assert f"***@{host}" in sanitized


def test_build_gitlab_https_clone_url_encodes_token_specials():
    from urllib.parse import unquote, urlsplit

    url = github_host.build_gitlab_https_clone_url("gitlab.com", "group/sub/repo", "x:y/z")
    assert "x-access-token" not in url.lower()
    sp = urlsplit(url)
    assert sp.hostname == "gitlab.com"
    assert sp.username == "oauth2"
    assert unquote(sp.password) == "x:y/z"
    assert sp.path == "/group/sub/repo.git"


def test_build_gitlab_https_clone_url_preserves_port():
    from urllib.parse import urlsplit

    url = github_host.build_gitlab_https_clone_url(
        "gitlab.corp.example",
        "group/repo",
        "glpat-secret",
        port=8443,
    )
    sp = urlsplit(url)
    assert sp.hostname == "gitlab.corp.example"
    assert sp.port == 8443
    assert sp.username == "oauth2"


def test_unsupported_host_error_message():
    """Test that unsupported host error provides actionable guidance."""
    error_msg = github_host.unsupported_host_error("github.company.com")

    # Should mention the hostname
    assert "github.company.com" in error_msg

    # Should list supported hosts
    assert "github.com" in error_msg
    assert "*.ghe.com" in error_msg
    assert "dev.azure.com" in error_msg

    # Should provide fix instructions for all platforms
    assert "export GITHUB_HOST=" in error_msg
    assert "$env:GITHUB_HOST" in error_msg
    assert "set GITHUB_HOST=" in error_msg


def test_unsupported_host_error_shows_current_host(monkeypatch):
    """Test that error shows current GITHUB_HOST if set."""
    monkeypatch.setenv("GITHUB_HOST", "other.company.com")

    error_msg = github_host.unsupported_host_error("github.company.com")

    # Should show the mismatch
    assert "other.company.com" in error_msg
    assert "github.company.com" in error_msg

    monkeypatch.delenv("GITHUB_HOST", raising=False)


# Azure DevOps URL builder tests


def test_build_ado_https_clone_url():
    """Test Azure DevOps HTTPS URL construction."""
    # Without token
    url = github_host.build_ado_https_clone_url("dmeppiel-org", "market-js-app", "compliance-rules")
    assert url == "https://dev.azure.com/dmeppiel-org/market-js-app/_git/compliance-rules"

    # With token
    url = github_host.build_ado_https_clone_url(
        "dmeppiel-org", "market-js-app", "compliance-rules", token="mytoken"
    )
    assert url == "https://mytoken@dev.azure.com/dmeppiel-org/market-js-app/_git/compliance-rules"

    # With custom host (ADO Server)
    url = github_host.build_ado_https_clone_url(
        "myorg", "myproject", "myrepo", host="ado.company.internal"
    )
    assert url == "https://ado.company.internal/myorg/myproject/_git/myrepo"


def test_build_ado_ssh_url():
    """Test Azure DevOps SSH URL construction."""
    url = github_host.build_ado_ssh_url("dmeppiel-org", "market-js-app", "compliance-rules")
    assert url == "git@ssh.dev.azure.com:v3/dmeppiel-org/market-js-app/compliance-rules"


def test_build_ado_ssh_url_server():
    """Test Azure DevOps Server SSH URL construction for on-premises."""
    # Custom host should use server format
    url = github_host.build_ado_ssh_url("myorg", "myproject", "myrepo", host="ado.company.internal")
    assert url == "ssh://git@ado.company.internal/myorg/myproject/_git/myrepo"

    # Cloud host should use cloud format
    url = github_host.build_ado_ssh_url("myorg", "myproject", "myrepo", host="ssh.dev.azure.com")
    assert url == "git@ssh.dev.azure.com:v3/myorg/myproject/myrepo"


def test_build_ado_api_url():
    """Test Azure DevOps API URL construction."""
    url = github_host.build_ado_api_url(
        "dmeppiel-org", "market-js-app", "compliance-rules", "apm.yml", "main"
    )
    assert "/_apis/git/repositories/compliance-rules/items" in url
    assert "path=apm.yml" in url
    assert "versionDescriptor.version=main" in url
    assert "api-version=7.0" in url

    encoded_url = github_host.build_ado_api_url("my org", "My Project", "repo/name", "path/file.md")
    assert "my%20org/My%20Project/_apis/git/repositories/repo%2Fname/items" in encoded_url
    encoded_ref_url = github_host.build_ado_api_url(
        "myorg", "project", "repo", "path/file.md", ref="feature/a&b"
    )
    assert "versionDescriptor.version=feature%2Fa%26b" in encoded_ref_url

    legacy_url = github_host.build_ado_api_url(
        "contoso", "_apm", "_apm", "apm-policy.yml", host="contoso.visualstudio.com"
    )
    assert legacy_url.startswith(
        "https://contoso.visualstudio.com/_apm/_apis/git/repositories/_apm/items"
    )

    ssh_url = github_host.build_ado_api_url(
        "contoso", "_apm", "_apm", "apm-policy.yml", host="ssh.dev.azure.com"
    )
    assert ssh_url.startswith("https://dev.azure.com/contoso/_apm/_apis/")


def test_build_authorization_header_git_env_bearer():
    """Bearer scheme produces correct GIT_CONFIG_* env overlay."""
    env = github_host.build_authorization_header_git_env("Bearer", "eyJabc.def.ghi")
    assert env == {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraheader",
        "GIT_CONFIG_VALUE_0": "Authorization: Bearer eyJabc.def.ghi",
    }


def test_build_authorization_header_git_env_basic():
    """Basic scheme works the same way; helper is scheme-agnostic."""
    env = github_host.build_authorization_header_git_env("Basic", "dXNlcjpwYXNz")
    assert env["GIT_CONFIG_VALUE_0"] == "Authorization: Basic dXNlcjpwYXNz"
    assert env["GIT_CONFIG_KEY_0"] == "http.extraheader"
    assert env["GIT_CONFIG_COUNT"] == "1"


def test_build_ado_bearer_git_env():
    """ADO bearer wrapper delegates to the generic helper with 'Bearer' scheme."""
    token = "eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9.payload.signature"
    env = github_host.build_ado_bearer_git_env(token)
    assert env["GIT_CONFIG_VALUE_0"] == f"Authorization: Bearer {token}"
    assert env["GIT_CONFIG_KEY_0"] == "http.extraheader"
    assert env["GIT_CONFIG_COUNT"] == "1"


def test_build_ado_bearer_git_env_does_not_url_encode():
    """Tokens are passed through verbatim; git handles header value as-is."""
    token = "abc/def+ghi=jkl"
    env = github_host.build_ado_bearer_git_env(token)
    assert env["GIT_CONFIG_VALUE_0"] == f"Authorization: Bearer {token}"


def test_unsupported_host_error_with_context():
    """Test that context message is included when provided."""
    error_msg = github_host.unsupported_host_error(
        "//evil.com", context="Protocol-relative URLs are not supported"
    )

    # Should include the context
    assert "Protocol-relative URLs are not supported" in error_msg

    # Should still include standard guidance
    assert re.search(r"\bgithub\.com\b", error_msg)
    assert "GITHUB_HOST" in error_msg


# ---------------------------------------------------------------------------
# is_ssh_auth_failure_signal
# ---------------------------------------------------------------------------


class TestIsSshAuthFailureSignal:
    """is_ssh_auth_failure_signal classifies OpenSSH stderr correctly."""

    # --- known auth rejection strings -> True ---

    def test_permission_denied(self):
        assert github_host.is_ssh_auth_failure_signal(
            "git@git.corp.internal: Permission denied (publickey)."
        )

    def test_publickey_substring(self):
        assert github_host.is_ssh_auth_failure_signal(
            "debug1: Offering public key: /home/user/.ssh/id_ed25519"
            "\nno more authentication methods to try"
        )

    def test_no_more_authentication_methods(self):
        assert github_host.is_ssh_auth_failure_signal(
            "Permission denied (publickey).\r\nfatal: Could not read from remote repository."
        )

    def test_host_key_verification_failed(self):
        assert github_host.is_ssh_auth_failure_signal(
            "Host key verification failed.\nfatal: Could not read from remote repository."
        )

    def test_no_supported_authentication_methods(self):
        assert github_host.is_ssh_auth_failure_signal(
            "no supported authentication methods available (server sent: publickey)"
        )

    def test_too_many_authentication_failures(self):
        assert github_host.is_ssh_auth_failure_signal(
            "Received disconnect from 10.0.0.1 port 22:2: Too many authentication failures"
        )

    def test_agent_refused_operation(self):
        assert github_host.is_ssh_auth_failure_signal(
            "sign_and_send_pubkey: signing failed: agent refused operation"
        )

    def test_case_insensitive(self):
        assert github_host.is_ssh_auth_failure_signal("PERMISSION DENIED (PUBLICKEY).")

    # --- connectivity errors -> False (must NOT be classified as auth failures) ---

    def test_could_not_resolve_hostname(self):
        """DNS failure is NOT an auth failure -- preflight must defer."""
        assert not github_host.is_ssh_auth_failure_signal(
            "ssh: Could not resolve hostname git.internal.corp: nodename nor servname provided"
        )

    def test_connection_refused(self):
        """Firewall/service-down is NOT an auth failure -- preflight must defer."""
        assert not github_host.is_ssh_auth_failure_signal(
            "ssh: connect to host git.internal.corp port 22: Connection refused"
        )

    def test_network_unreachable(self):
        assert not github_host.is_ssh_auth_failure_signal(
            "connect to host git.internal.corp port 22: Network unreachable"
        )

    # --- edge cases -> False ---

    def test_none_input(self):
        assert not github_host.is_ssh_auth_failure_signal(None)

    def test_empty_string(self):
        assert not github_host.is_ssh_auth_failure_signal("")

    def test_unrelated_stderr(self):
        assert not github_host.is_ssh_auth_failure_signal(
            "warning: remote HEAD refers to nonexistent ref, unable to checkout."
        )


class TestIsGitHubHostnameGHES:
    """is_github_hostname recognises custom GHES hosts via GITHUB_HOST env var."""

    def test_ghes_host_via_env_var(self, monkeypatch):
        monkeypatch.setenv("GITHUB_HOST", "ghe.example.com")
        assert github_host.is_github_hostname("ghe.example.com") is True

    def test_ghes_host_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("GITHUB_HOST", "GHE.Example.COM")
        assert github_host.is_github_hostname("ghe.example.com") is True

    def test_ghes_host_not_matched_when_env_unset(self, monkeypatch):
        monkeypatch.delenv("GITHUB_HOST", raising=False)
        assert github_host.is_github_hostname("ghe.example.com") is False

    def test_ghes_host_does_not_claim_ado(self, monkeypatch):
        monkeypatch.setenv("GITHUB_HOST", "dev.azure.com")
        assert github_host.is_github_hostname("dev.azure.com") is False

    def test_ghes_host_does_not_claim_visualstudio(self, monkeypatch):
        monkeypatch.setenv("GITHUB_HOST", "myorg.visualstudio.com")
        assert github_host.is_github_hostname("myorg.visualstudio.com") is False

    def test_ghes_host_does_not_claim_gitlab_com(self, monkeypatch):
        monkeypatch.setenv("GITHUB_HOST", "gitlab.com")
        assert github_host.is_github_hostname("gitlab.com") is False

    def test_github_com_still_works_with_ghes_env(self, monkeypatch):
        monkeypatch.setenv("GITHUB_HOST", "ghe.example.com")
        assert github_host.is_github_hostname("github.com") is True

    def test_ghe_cloud_still_works_with_ghes_env(self, monkeypatch):
        monkeypatch.setenv("GITHUB_HOST", "ghe.example.com")
        assert github_host.is_github_hostname("corp.ghe.com") is True

    def test_ghes_does_not_match_different_host(self, monkeypatch):
        monkeypatch.setenv("GITHUB_HOST", "ghe.example.com")
        assert github_host.is_github_hostname("git.other.com") is False

    def test_ghes_rejects_invalid_fqdn(self, monkeypatch):
        monkeypatch.setenv("GITHUB_HOST", "not-a-fqdn")
        assert github_host.is_github_hostname("not-a-fqdn") is False
