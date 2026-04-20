"""Unit tests for generic git URL support in dependency parsing.

Tests that APM can parse dependency references from any git host using
standard git protocol URLs (HTTPS and SSH), including GitLab, Bitbucket,
and self-hosted instances.
"""

from pathlib import Path

import pytest

from src.apm_cli.models.apm_package import DependencyReference
from src.apm_cli.deps.lockfile import LockedDependency
from src.apm_cli.utils.github_host import (
    build_https_clone_url,
    build_ssh_url,
    is_supported_git_host,
)


class TestGenericHostSupport:
    """Test that any valid FQDN is accepted as a git host."""

    def test_gitlab_com_is_supported(self):
        assert is_supported_git_host("gitlab.com")

    def test_bitbucket_org_is_supported(self):
        assert is_supported_git_host("bitbucket.org")

    def test_self_hosted_gitlab_is_supported(self):
        assert is_supported_git_host("gitlab.company.internal")

    def test_self_hosted_gitea_is_supported(self):
        assert is_supported_git_host("gitea.myorg.com")

    def test_custom_git_server_is_supported(self):
        assert is_supported_git_host("git.example.com")

    def test_localhost_not_supported(self):
        """Single-label hostnames are not valid FQDNs."""
        assert not is_supported_git_host("localhost")

    def test_empty_not_supported(self):
        assert not is_supported_git_host("")
        assert not is_supported_git_host(None)


class TestGitLabHTTPS:
    """Test HTTPS git URL parsing for GitLab repositories."""

    def test_gitlab_https_url(self):
        dep = DependencyReference.parse("https://gitlab.com/acme/coding-standards.git")
        assert dep.host == "gitlab.com"
        assert dep.repo_url == "acme/coding-standards"
        assert dep.reference is None

    def test_gitlab_https_url_no_git_suffix(self):
        dep = DependencyReference.parse("https://gitlab.com/acme/coding-standards")
        assert dep.host == "gitlab.com"
        assert dep.repo_url == "acme/coding-standards"

    def test_gitlab_https_url_with_ref(self):
        dep = DependencyReference.parse("https://gitlab.com/acme/coding-standards.git#v2.0")
        assert dep.host == "gitlab.com"
        assert dep.repo_url == "acme/coding-standards"
        assert dep.reference == "v2.0"

    def test_gitlab_https_url_with_alias_shorthand_removed(self):
        """Shorthand @alias on HTTPS URLs is no longer supported."""
        with pytest.raises(ValueError):
            DependencyReference.parse("https://gitlab.com/acme/coding-standards.git@my-rules")

    def test_gitlab_https_url_with_ref_and_alias_shorthand_not_parsed(self):
        """Shorthand #ref@alias on HTTPS URLs — @ is no longer parsed as alias separator."""
        dep = DependencyReference.parse("https://gitlab.com/acme/coding-standards.git#main@rules")
        assert dep.host == "gitlab.com"
        assert dep.repo_url == "acme/coding-standards"
        assert dep.reference == "main@rules"
        assert dep.alias is None

    def test_gitlab_fqdn_format(self):
        """Test gitlab.com/owner/repo format (without https://)."""
        dep = DependencyReference.parse("gitlab.com/acme/coding-standards")
        assert dep.host == "gitlab.com"
        assert dep.repo_url == "acme/coding-standards"

    def test_self_hosted_gitlab_https(self):
        dep = DependencyReference.parse("https://gitlab.company.internal/team/rules.git")
        assert dep.host == "gitlab.company.internal"
        assert dep.repo_url == "team/rules"

    def test_self_hosted_gitlab_fqdn(self):
        dep = DependencyReference.parse("gitlab.company.internal/team/rules")
        assert dep.host == "gitlab.company.internal"
        assert dep.repo_url == "team/rules"


class TestGitLabSSH:
    """Test SSH git URL parsing for GitLab repositories."""

    def test_gitlab_ssh_git_at(self):
        dep = DependencyReference.parse("git@gitlab.com:acme/coding-standards.git")
        assert dep.host == "gitlab.com"
        assert dep.repo_url == "acme/coding-standards"

    def test_gitlab_ssh_git_at_no_suffix(self):
        dep = DependencyReference.parse("git@gitlab.com:acme/coding-standards")
        assert dep.host == "gitlab.com"
        assert dep.repo_url == "acme/coding-standards"

    def test_gitlab_ssh_git_at_with_ref(self):
        dep = DependencyReference.parse("git@gitlab.com:acme/coding-standards.git#v1.0")
        assert dep.host == "gitlab.com"
        assert dep.repo_url == "acme/coding-standards"
        assert dep.reference == "v1.0"

    def test_gitlab_ssh_protocol(self):
        """Test ssh:// protocol URL normalization."""
        dep = DependencyReference.parse("ssh://git@gitlab.com/acme/coding-standards.git")
        assert dep.host == "gitlab.com"
        assert dep.repo_url == "acme/coding-standards"

    def test_gitlab_ssh_protocol_with_ref(self):
        dep = DependencyReference.parse("ssh://git@gitlab.com/acme/coding-standards.git#main")
        assert dep.host == "gitlab.com"
        assert dep.repo_url == "acme/coding-standards"
        assert dep.reference == "main"

    def test_self_hosted_gitlab_ssh(self):
        dep = DependencyReference.parse("git@gitlab.company.internal:team/rules.git")
        assert dep.host == "gitlab.company.internal"
        assert dep.repo_url == "team/rules"

    def test_self_hosted_ssh_protocol(self):
        dep = DependencyReference.parse("ssh://git@gitlab.company.internal/team/rules.git")
        assert dep.host == "gitlab.company.internal"
        assert dep.repo_url == "team/rules"

    def test_ssh_protocol_with_port(self):
        """Non-default ssh:// ports are preserved on the dep_ref.port field."""
        dep = DependencyReference.parse("ssh://git@gitlab.com:2222/acme/repo.git")
        assert dep.host == "gitlab.com"
        assert dep.port == 2222
        assert dep.repo_url == "acme/repo"


class TestBitbucketHTTPS:
    """Test HTTPS git URL parsing for Bitbucket repositories."""

    def test_bitbucket_https_url(self):
        dep = DependencyReference.parse("https://bitbucket.org/acme/security-rules.git")
        assert dep.host == "bitbucket.org"
        assert dep.repo_url == "acme/security-rules"

    def test_bitbucket_https_no_suffix(self):
        dep = DependencyReference.parse("https://bitbucket.org/acme/security-rules")
        assert dep.host == "bitbucket.org"
        assert dep.repo_url == "acme/security-rules"

    def test_bitbucket_https_with_ref(self):
        dep = DependencyReference.parse("https://bitbucket.org/acme/security-rules.git#v1.0")
        assert dep.host == "bitbucket.org"
        assert dep.repo_url == "acme/security-rules"
        assert dep.reference == "v1.0"

    def test_bitbucket_fqdn_format(self):
        dep = DependencyReference.parse("bitbucket.org/acme/security-rules")
        assert dep.host == "bitbucket.org"
        assert dep.repo_url == "acme/security-rules"


class TestBitbucketSSH:
    """Test SSH git URL parsing for Bitbucket repositories."""

    def test_bitbucket_ssh_git_at(self):
        dep = DependencyReference.parse("git@bitbucket.org:acme/security-rules.git")
        assert dep.host == "bitbucket.org"
        assert dep.repo_url == "acme/security-rules"

    def test_bitbucket_ssh_protocol(self):
        dep = DependencyReference.parse("ssh://git@bitbucket.org/acme/security-rules.git")
        assert dep.host == "bitbucket.org"
        assert dep.repo_url == "acme/security-rules"


class TestGitHubURLs:
    """Test that GitHub URLs still work correctly with generic support."""

    def test_github_https_url(self):
        dep = DependencyReference.parse("https://github.com/microsoft/apm.git")
        assert dep.host == "github.com"
        assert dep.repo_url == "microsoft/apm"

    def test_github_https_no_suffix(self):
        dep = DependencyReference.parse("https://github.com/microsoft/apm")
        assert dep.host == "github.com"
        assert dep.repo_url == "microsoft/apm"

    def test_github_ssh_url(self):
        dep = DependencyReference.parse("git@github.com:microsoft/apm.git")
        assert dep.host == "github.com"
        assert dep.repo_url == "microsoft/apm"

    def test_github_ssh_protocol(self):
        dep = DependencyReference.parse("ssh://git@github.com/microsoft/apm.git")
        assert dep.host == "github.com"
        assert dep.repo_url == "microsoft/apm"

    def test_github_shorthand_still_works(self):
        dep = DependencyReference.parse("microsoft/apm")
        assert dep.host == "github.com"
        assert dep.repo_url == "microsoft/apm"

    def test_github_fqdn_format(self):
        dep = DependencyReference.parse("github.com/microsoft/apm")
        assert dep.host == "github.com"
        assert dep.repo_url == "microsoft/apm"


class TestCustomPortParsing:
    """Port preservation for self-hosted git servers (issues #661, #731).

    Non-default SSH/HTTPS ports must be captured on dep_ref.port so downstream
    URL builders can emit them instead of silently falling back to default ports.
    SCP shorthand (``git@host:path``) cannot carry a port because ``:`` is the
    path separator, so the SCP path must stay port-less.
    """

    def test_ssh_protocol_url_preserves_port(self):
        """``ssh://host:7999/path`` captures port=7999 without intermediate SCP form."""
        dep = DependencyReference.parse("ssh://git@bitbucket.domain.ext:7999/project/repo.git")
        assert dep.host == "bitbucket.domain.ext"
        assert dep.port == 7999
        assert dep.repo_url == "project/repo"

    def test_ssh_protocol_url_no_port(self):
        """ssh:// without a port leaves ``port=None``."""
        dep = DependencyReference.parse("ssh://git@bitbucket.domain.ext/project/repo.git")
        assert dep.host == "bitbucket.domain.ext"
        assert dep.port is None
        assert dep.repo_url == "project/repo"

    def test_https_url_preserves_port(self):
        """Covers #731: ``https://host:8443/path`` captures port=8443."""
        dep = DependencyReference.parse("https://bitbucket.domain.ext:8443/project/repo")
        assert dep.host == "bitbucket.domain.ext"
        assert dep.port == 8443
        assert dep.repo_url == "project/repo"

    def test_https_url_with_git_suffix_preserves_port(self):
        dep = DependencyReference.parse("https://bitbucket.domain.ext:8443/project/repo.git")
        assert dep.host == "bitbucket.domain.ext"
        assert dep.port == 8443
        assert dep.repo_url == "project/repo"

    def test_scp_shorthand_port_is_none(self):
        """SCP shorthand ``git@host:path`` cannot carry a port — no behaviour change."""
        dep = DependencyReference.parse("git@bitbucket.org:acme/rules.git")
        assert dep.host == "bitbucket.org"
        assert dep.port is None
        assert dep.repo_url == "acme/rules"

    def test_shorthand_default_host_port_is_none(self):
        """Bare ``owner/repo`` shorthand has no port."""
        dep = DependencyReference.parse("microsoft/apm")
        assert dep.host == "github.com"
        assert dep.port is None

    def test_ssh_protocol_url_with_ref_and_alias(self):
        """``ssh://host:7999/path.git#main@alias`` splits fragment cleanly."""
        dep = DependencyReference.parse(
            "ssh://git@bitbucket.domain.ext:7999/project/repo.git#main@my-alias"
        )
        assert dep.host == "bitbucket.domain.ext"
        assert dep.port == 7999
        assert dep.repo_url == "project/repo"
        assert dep.reference == "main"
        assert dep.alias == "my-alias"

    def test_ssh_protocol_url_with_bare_alias(self):
        """``ssh://host/path.git@alias`` (no #ref) still extracts the alias."""
        dep = DependencyReference.parse(
            "ssh://git@bitbucket.domain.ext/project/repo.git@my-alias"
        )
        assert dep.host == "bitbucket.domain.ext"
        assert dep.port is None
        assert dep.alias == "my-alias"
        assert dep.reference is None

    def test_custom_port_round_trips_through_lockfile(self):
        """port survives to_dict()/from_dict()."""
        dep = DependencyReference.parse("ssh://git@bitbucket.domain.ext:7999/project/repo.git")
        locked = LockedDependency.from_dependency_ref(
            dep, resolved_commit="abc123", depth=1, resolved_by=None
        )
        assert locked.port == 7999
        restored = LockedDependency.from_dict(locked.to_dict())
        assert restored.port == 7999

    def test_lockfile_omits_port_when_none(self):
        """Default-port deps do not emit a ``port`` key (backwards compatibility)."""
        dep = DependencyReference.parse("https://bitbucket.domain.ext/project/repo.git")
        locked = LockedDependency.from_dependency_ref(
            dep, resolved_commit="abc123", depth=1, resolved_by=None
        )
        assert locked.port is None
        assert "port" not in locked.to_dict()

    def test_same_repo_different_ports_dedup_by_repo_url(self):
        """Two refs to the same logical repo via different ports still collide on repo_url.

        Port is a transport detail, not an identity component — dedup stays on repo_url.
        """
        dep_a = DependencyReference.parse("ssh://git@bitbucket.domain.ext:7999/project/repo.git")
        dep_b = DependencyReference.parse("https://bitbucket.domain.ext:8443/project/repo")
        assert dep_a.get_unique_key() == dep_b.get_unique_key()

    def test_lockfile_rejects_garbage_port_string(self):
        restored = LockedDependency.from_dict({"repo_url": "owner/repo", "port": "not-a-number"})
        assert restored.port is None

    def test_lockfile_rejects_port_out_of_range(self):
        for bad in (99999, -1, 0):
            restored = LockedDependency.from_dict({"repo_url": "owner/repo", "port": bad})
            assert restored.port is None, f"port={bad!r} should be rejected"

    def test_lockfile_accepts_numeric_port_string(self):
        """YAML tolerance: numeric strings coerce to int when in range."""
        restored = LockedDependency.from_dict({"repo_url": "owner/repo", "port": "7999"})
        assert restored.port == 7999


class TestCloneURLBuilding:
    """Test that clone URLs are correctly built for generic hosts."""

    def test_gitlab_https_clone_url(self):
        url = build_https_clone_url("gitlab.com", "acme/repo")
        assert url == "https://gitlab.com/acme/repo"

    def test_gitlab_https_clone_url_with_token(self):
        url = build_https_clone_url("gitlab.com", "acme/repo", token="glpat-xxx")
        assert url == "https://x-access-token:glpat-xxx@gitlab.com/acme/repo.git"

    def test_bitbucket_https_clone_url(self):
        url = build_https_clone_url("bitbucket.org", "acme/repo")
        assert url == "https://bitbucket.org/acme/repo"

    def test_gitlab_ssh_clone_url(self):
        url = build_ssh_url("gitlab.com", "acme/repo")
        assert url == "git@gitlab.com:acme/repo.git"

    def test_bitbucket_ssh_clone_url(self):
        url = build_ssh_url("bitbucket.org", "acme/repo")
        assert url == "git@bitbucket.org:acme/repo.git"

    def test_self_hosted_ssh_clone_url(self):
        url = build_ssh_url("git.company.internal", "team/repo")
        assert url == "git@git.company.internal:team/repo.git"

    def test_ssh_clone_url_with_custom_port_uses_ssh_scheme(self):
        """SCP shorthand cannot carry a port, so a port switches to ``ssh://`` form."""
        url = build_ssh_url("bitbucket.domain.ext", "team/repo", port=7999)
        assert url == "ssh://git@bitbucket.domain.ext:7999/team/repo.git"

    def test_ssh_clone_url_port_none_keeps_scp_shorthand(self):
        url = build_ssh_url("bitbucket.domain.ext", "team/repo", port=None)
        assert url == "git@bitbucket.domain.ext:team/repo.git"

    def test_https_clone_url_with_custom_port(self):
        url = build_https_clone_url("bitbucket.domain.ext", "team/repo", port=8443)
        assert url == "https://bitbucket.domain.ext:8443/team/repo"

    def test_https_clone_url_with_token_and_port(self):
        url = build_https_clone_url(
            "bitbucket.domain.ext", "team/repo", token="pat-xxx", port=8443
        )
        assert url == "https://x-access-token:pat-xxx@bitbucket.domain.ext:8443/team/repo.git"


class TestToGithubURLGenericHosts:
    """Test that to_github_url works correctly for generic hosts."""

    def test_gitlab_to_url(self):
        dep = DependencyReference.parse("https://gitlab.com/acme/repo.git")
        assert dep.to_github_url() == "https://gitlab.com/acme/repo"

    def test_bitbucket_to_url(self):
        dep = DependencyReference.parse("git@bitbucket.org:acme/repo.git")
        assert dep.to_github_url() == "https://bitbucket.org/acme/repo"

    def test_self_hosted_to_url(self):
        dep = DependencyReference.parse("git@git.company.internal:team/rules.git")
        assert dep.to_github_url() == "https://git.company.internal/team/rules"


class TestGetInstallPathGenericHosts:
    """Test that install paths work correctly for generic hosts."""

    def test_gitlab_install_path(self):
        dep = DependencyReference.parse("https://gitlab.com/acme/repo.git")
        path = dep.get_install_path(Path("apm_modules"))
        assert path == Path("apm_modules/acme/repo")

    def test_bitbucket_install_path(self):
        dep = DependencyReference.parse("git@bitbucket.org:team/rules.git")
        path = dep.get_install_path(Path("apm_modules"))
        assert path == Path("apm_modules/team/rules")

    def test_self_hosted_install_path(self):
        dep = DependencyReference.parse("git@git.company.internal:team/rules.git")
        path = dep.get_install_path(Path("apm_modules"))
        assert path == Path("apm_modules/team/rules")


class TestSecurityWithGenericHosts:
    """Test that security protections still work with generic host support."""

    def test_protocol_relative_rejected(self):
        with pytest.raises(ValueError, match="Protocol-relative"):
            DependencyReference.parse("//evil.com/user/repo")

    def test_control_characters_rejected(self):
        with pytest.raises(ValueError, match="control characters"):
            DependencyReference.parse("gitlab.com/user/repo\n")

    def test_empty_string_rejected(self):
        with pytest.raises(ValueError, match="Empty"):
            DependencyReference.parse("")

    def test_path_injection_still_rejected(self):
        """Embedding a hostname in a sub-path position is valid with nested groups.
        
        With nested group support on generic hosts, all path segments are part
        of the repo path. The host is correctly identified from the first segment.
        """
        dep = DependencyReference.parse("evil.com/github.com/user/repo")
        assert dep.host == "evil.com"
        assert dep.repo_url == "github.com/user/repo"
        assert dep.is_virtual is False

    def test_invalid_characters_rejected(self):
        with pytest.raises(ValueError, match="Invalid repository path component"):
            DependencyReference.parse("https://gitlab.com/user/repo$bad")


class TestFQDNVirtualPaths:
    """Test FQDN shorthand with virtual paths on generic hosts.

    Git protocol URLs (https://, git@) are repo-level and cannot embed paths.
    Use FQDN shorthand (host/owner/repo/path) for virtual packages on any host.
    """

    def test_gitlab_virtual_file(self):
        dep = DependencyReference.parse("gitlab.com/acme/repo/prompts/file.prompt.md")
        assert dep.host == "gitlab.com"
        assert dep.repo_url == "acme/repo"
        assert dep.virtual_path == "prompts/file.prompt.md"
        assert dep.is_virtual is True
        assert dep.is_virtual_file() is True

    def test_bitbucket_virtual_collection(self):
        dep = DependencyReference.parse("bitbucket.org/team/rules/collections/security")
        assert dep.host == "bitbucket.org"
        assert dep.repo_url == "team/rules"
        assert dep.virtual_path == "collections/security"
        assert dep.is_virtual is True
        assert dep.is_virtual_collection() is True

    def test_self_hosted_virtual_subdirectory(self):
        """Without virtual indicators, all segments are repo path on generic hosts.
        
        Virtual subdirectory packages on generic hosts with nested groups
        require the dict format: {git: 'host/group/repo', path: 'subdir'}
        """
        dep = DependencyReference.parse("git.company.internal/team/skills/brand-guidelines")
        assert dep.host == "git.company.internal"
        assert dep.repo_url == "team/skills/brand-guidelines"
        assert dep.is_virtual is False

    def test_gitlab_virtual_file_with_ref(self):
        dep = DependencyReference.parse("gitlab.com/acme/repo/prompts/file.prompt.md#v2.0")
        assert dep.host == "gitlab.com"
        assert dep.repo_url == "acme/repo"
        assert dep.virtual_path == "prompts/file.prompt.md"
        assert dep.reference == "v2.0"

    def test_https_url_with_path_rejected(self):
        """HTTPS git URLs can't embed virtual paths — use dict format instead."""
        with pytest.raises(ValueError, match="virtual file extension"):
            DependencyReference.parse("https://gitlab.com/acme/repo/prompts/file.prompt.md")

    def test_ssh_url_with_path_rejected(self):
        """SSH git URLs can't embed virtual paths — use dict format instead."""
        with pytest.raises(ValueError, match="virtual file extension"):
            DependencyReference.parse("git@gitlab.com:acme/repo/prompts/code-review.prompt.md")


class TestNestedGroupSupport:
    """Test nested group/subgroup support for generic hosts (GitLab, Gitea, etc.).

    GitLab supports up to 20 levels of nested groups: gitlab.com/group/subgroup/.../repo.
    For generic hosts (non-GitHub, non-ADO), ALL path segments are treated as repo path
    unless virtual indicators (file extensions, /collections/) are present.
    """

    # --- FQDN shorthand ---

    def test_gitlab_two_level_group(self):
        dep = DependencyReference.parse("gitlab.com/group/subgroup/repo")
        assert dep.host == "gitlab.com"
        assert dep.repo_url == "group/subgroup/repo"
        assert dep.is_virtual is False

    def test_gitlab_three_level_group(self):
        dep = DependencyReference.parse("gitlab.com/org/team/project/repo")
        assert dep.host == "gitlab.com"
        assert dep.repo_url == "org/team/project/repo"
        assert dep.is_virtual is False

    def test_gitlab_simple_owner_repo_unchanged(self):
        dep = DependencyReference.parse("gitlab.com/owner/repo")
        assert dep.host == "gitlab.com"
        assert dep.repo_url == "owner/repo"
        assert dep.is_virtual is False

    def test_nested_group_with_ref(self):
        dep = DependencyReference.parse("gitlab.com/group/subgroup/repo#v2.0")
        assert dep.host == "gitlab.com"
        assert dep.repo_url == "group/subgroup/repo"
        assert dep.reference == "v2.0"
        assert dep.is_virtual is False

    def test_nested_group_with_alias_shorthand_removed(self):
        """Shorthand @alias on nested groups is no longer supported."""
        with pytest.raises(ValueError):
            DependencyReference.parse("gitlab.com/group/subgroup/repo@my-alias")

    def test_nested_group_with_ref_and_alias_shorthand_not_parsed(self):
        """Shorthand #ref@alias on nested groups — @ is no longer parsed as alias separator."""
        dep = DependencyReference.parse("gitlab.com/group/subgroup/repo#main@alias")
        assert dep.repo_url == "group/subgroup/repo"
        assert dep.reference == "main@alias"
        assert dep.alias is None

    # --- SSH URLs ---

    def test_ssh_nested_group(self):
        dep = DependencyReference.parse("git@gitlab.com:group/subgroup/repo.git")
        assert dep.host == "gitlab.com"
        assert dep.repo_url == "group/subgroup/repo"
        assert dep.is_virtual is False

    def test_ssh_three_level_group(self):
        dep = DependencyReference.parse("git@gitlab.com:org/team/project/repo.git")
        assert dep.host == "gitlab.com"
        assert dep.repo_url == "org/team/project/repo"

    def test_ssh_nested_group_no_git_suffix(self):
        dep = DependencyReference.parse("git@gitlab.com:group/subgroup/repo")
        assert dep.repo_url == "group/subgroup/repo"

    def test_ssh_nested_group_with_ref(self):
        dep = DependencyReference.parse("git@gitlab.com:group/subgroup/repo.git#v1.0")
        assert dep.repo_url == "group/subgroup/repo"
        assert dep.reference == "v1.0"

    # --- HTTPS URLs ---

    def test_https_nested_group(self):
        dep = DependencyReference.parse("https://gitlab.com/group/subgroup/repo.git")
        assert dep.host == "gitlab.com"
        assert dep.repo_url == "group/subgroup/repo"
        assert dep.is_virtual is False

    def test_https_three_level_group(self):
        dep = DependencyReference.parse("https://gitlab.com/org/team/project/repo.git")
        assert dep.host == "gitlab.com"
        assert dep.repo_url == "org/team/project/repo"

    def test_https_nested_group_no_git_suffix(self):
        dep = DependencyReference.parse("https://gitlab.com/group/subgroup/repo")
        assert dep.host == "gitlab.com"
        assert dep.repo_url == "group/subgroup/repo"

    # --- ssh:// protocol URLs ---

    def test_ssh_protocol_nested_group(self):
        dep = DependencyReference.parse("ssh://git@gitlab.com/group/subgroup/repo.git")
        assert dep.host == "gitlab.com"
        assert dep.repo_url == "group/subgroup/repo"

    # --- Virtual packages with nested groups ---

    def test_nested_group_simple_repo_with_virtual_file(self):
        """Simple 2-segment repo on generic host with virtual file extension."""
        dep = DependencyReference.parse("gitlab.com/acme/repo/design.prompt.md")
        assert dep.host == "gitlab.com"
        assert dep.repo_url == "acme/repo"
        assert dep.virtual_path == "design.prompt.md"
        assert dep.is_virtual is True

    def test_nested_group_simple_repo_with_collection(self):
        """Simple 2-segment repo on generic host with collections path."""
        dep = DependencyReference.parse("gitlab.com/acme/repo/collections/security")
        assert dep.host == "gitlab.com"
        assert dep.repo_url == "acme/repo"
        assert dep.virtual_path == "collections/security"
        assert dep.is_virtual is True

    def test_nested_group_virtual_requires_dict_format(self):
        """For nested groups + virtual, dict format is required."""
        dep = DependencyReference.parse_from_dict({
            "git": "gitlab.com/group/subgroup/repo",
            "path": "prompts/review.prompt.md"
        })
        assert dep.host == "gitlab.com"
        assert dep.repo_url == "group/subgroup/repo"
        assert dep.virtual_path == "prompts/review.prompt.md"
        assert dep.is_virtual is True

    # --- Install paths ---

    def test_install_path_nested_group(self):
        dep = DependencyReference.parse("gitlab.com/group/subgroup/repo")
        path = dep.get_install_path(Path("/apm_modules"))
        assert path == Path("/apm_modules/group/subgroup/repo")

    def test_install_path_three_level_group(self):
        dep = DependencyReference.parse("gitlab.com/org/team/project/repo")
        path = dep.get_install_path(Path("/apm_modules"))
        assert path == Path("/apm_modules/org/team/project/repo")

    def test_install_path_simple_generic_host(self):
        dep = DependencyReference.parse("gitlab.com/owner/repo")
        path = dep.get_install_path(Path("/apm_modules"))
        assert path == Path("/apm_modules/owner/repo")

    # --- Canonical form ---

    def test_canonical_nested_group(self):
        dep = DependencyReference.parse("gitlab.com/group/subgroup/repo")
        assert dep.to_canonical() == "gitlab.com/group/subgroup/repo"

    def test_canonical_nested_group_with_ref(self):
        dep = DependencyReference.parse("gitlab.com/group/subgroup/repo#v2.0")
        assert dep.to_canonical() == "gitlab.com/group/subgroup/repo#v2.0"

    def test_canonical_ssh_nested_group(self):
        dep = DependencyReference.parse("git@gitlab.com:group/subgroup/repo.git")
        assert dep.to_canonical() == "gitlab.com/group/subgroup/repo"

    def test_canonical_https_nested_group(self):
        dep = DependencyReference.parse("https://gitlab.com/group/subgroup/repo.git")
        assert dep.to_canonical() == "gitlab.com/group/subgroup/repo"

    # --- to_github_url (clone URL) ---

    def test_to_github_url_nested_group(self):
        dep = DependencyReference.parse("gitlab.com/group/subgroup/repo")
        assert dep.to_github_url() == "https://gitlab.com/group/subgroup/repo"

    # --- GitHub unchanged ---

    def test_github_shorthand_unchanged(self):
        """GitHub 2-segment shorthand is unchanged by nested group support."""
        dep = DependencyReference.parse("owner/repo")
        assert dep.host == "github.com"
        assert dep.repo_url == "owner/repo"
        assert dep.is_virtual is False

    def test_github_virtual_unchanged(self):
        """GitHub 3+ segments still mean virtual package."""
        dep = DependencyReference.parse("owner/repo/file.prompt.md")
        assert dep.repo_url == "owner/repo"
        assert dep.virtual_path == "file.prompt.md"
        assert dep.is_virtual is True

    # --- Rejection cases ---

    # --- Ambiguity: nested group + virtual path (shorthand vs dict) ---

    def test_shorthand_ambiguity_virtual_ext_collapses_repo(self):
        """Shorthand with virtual extension treats owner/repo as 2-segment base.

        gitlab.com/group/subgroup/repo/file.prompt.md → the parser sees the
        .prompt.md extension and assumes a 2-segment repo (group/subgroup)
        with virtual path repo/file.prompt.md. This is WRONG if the user
        meant repo=group/subgroup/repo. That's why dict format is required.
        """
        dep = DependencyReference.parse("gitlab.com/group/subgroup/repo/file.prompt.md")
        # Parser sees virtual indicator → assumes 2-segment base
        assert dep.repo_url == "group/subgroup"
        assert dep.virtual_path == "repo/file.prompt.md"
        assert dep.is_virtual is True

    def test_dict_format_resolves_ambiguity(self):
        """Dict format makes nested-group + virtual path unambiguous.

        The dict format explicitly separates the repo URL from the virtual
        path, so there's no ambiguity about where the repo path ends.
        """
        dep = DependencyReference.parse_from_dict({
            "git": "gitlab.com/group/subgroup/repo",
            "path": "file.prompt.md"
        })
        assert dep.repo_url == "group/subgroup/repo"
        assert dep.virtual_path == "file.prompt.md"
        assert dep.is_virtual is True
        assert dep.host == "gitlab.com"

    def test_dict_format_nested_group_with_collection(self):
        """Dict format works for nested-group repos with collections."""
        dep = DependencyReference.parse_from_dict({
            "git": "gitlab.com/acme/platform/infra/repo",
            "path": "collections/security"
        })
        assert dep.repo_url == "acme/platform/infra/repo"
        assert dep.virtual_path == "collections/security"
        assert dep.is_virtual is True

    def test_dict_format_nested_group_install_path_subdir(self):
        """Install path for dict-based virtual subdirectory nested-group dep."""
        dep = DependencyReference.parse_from_dict({
            "git": "gitlab.com/group/subgroup/repo",
            "path": "skills/code-review"
        })
        path = dep.get_install_path(Path("/apm_modules"))
        # Subdirectory virtual: repo path + virtual path
        assert path == Path("/apm_modules/group/subgroup/repo/skills/code-review")

    def test_dict_format_nested_group_install_path_file(self):
        """Install path for dict-based virtual file nested-group dep."""
        dep = DependencyReference.parse_from_dict({
            "git": "gitlab.com/group/subgroup/repo",
            "path": "prompts/review.prompt.md"
        })
        path = dep.get_install_path(Path("/apm_modules"))
        # Virtual file: first segment / sanitized package name
        assert path == Path("/apm_modules/group/" + dep.get_virtual_package_name())

    def test_dict_format_nested_group_canonical(self):
        """Canonical form for dict-based nested-group dep includes virtual path."""
        dep = DependencyReference.parse_from_dict({
            "git": "gitlab.com/group/subgroup/repo",
            "path": "prompts/review.prompt.md"
        })
        # Canonical includes virtual path since it's a virtual package
        assert dep.to_canonical() == "gitlab.com/group/subgroup/repo/prompts/review.prompt.md"

    def test_dict_format_nested_group_clone_url(self):
        """Clone URL for dict-based nested-group dep."""
        dep = DependencyReference.parse_from_dict({
            "git": "gitlab.com/group/subgroup/repo",
            "path": "prompts/review.prompt.md"
        })
        assert dep.to_github_url() == "https://gitlab.com/group/subgroup/repo"

    def test_dict_format_nested_group_with_ref_and_alias(self):
        """Dict format with all fields on nested-group repo."""
        dep = DependencyReference.parse_from_dict({
            "git": "https://gitlab.com/acme/team/project/repo.git",
            "path": "instructions/security",
            "ref": "v2.0",
            "alias": "sec-rules"
        })
        assert dep.host == "gitlab.com"
        assert dep.repo_url == "acme/team/project/repo"
        assert dep.virtual_path == "instructions/security"
        assert dep.reference == "v2.0"
        assert dep.alias == "sec-rules"
        assert dep.is_virtual is True

    # --- SSH/HTTPS rejection for nested groups with virtual extensions ---

    def test_ssh_nested_group_with_virtual_ext_rejected(self):
        """SSH URLs can't embed virtual paths even with nested groups."""
        with pytest.raises(ValueError, match="virtual file extension"):
            DependencyReference.parse("git@gitlab.com:group/subgroup/file.prompt.md")

    def test_https_nested_group_with_virtual_ext_rejected(self):
        """HTTPS URLs can't embed virtual paths even with nested groups."""
        with pytest.raises(ValueError, match="virtual file extension"):
            DependencyReference.parse("https://gitlab.com/group/subgroup/file.prompt.md")


class TestSCPPortDetection:
    """Detect port-like first path segment in SCP shorthand (git@host:port/path).

    SCP shorthand uses ':' as the path separator and cannot carry a port.
    When the first path segment is a valid TCP port (1-65535), APM should
    raise a ValueError with an actionable suggestion to use ssh:// instead.
    """

    def test_scp_with_port_7999_raises(self):
        """Bitbucket Datacenter: git@host:7999/project/repo.git."""
        with pytest.raises(ValueError, match="ssh://"):
            DependencyReference.parse("git@bitbucket.example.com:7999/project/repo.git")

    def test_scp_with_port_22_raises(self):
        """Default SSH port 22 should still be detected."""
        with pytest.raises(ValueError, match="ssh://"):
            DependencyReference.parse("git@host.example.com:22/owner/repo.git")

    def test_scp_with_port_65535_raises(self):
        """Max valid TCP port should trigger detection."""
        with pytest.raises(ValueError, match="ssh://"):
            DependencyReference.parse("git@host.example.com:65535/owner/repo.git")

    def test_scp_with_port_1_raises(self):
        """Min valid TCP port should trigger detection."""
        with pytest.raises(ValueError, match="ssh://"):
            DependencyReference.parse("git@host.example.com:1/owner/repo.git")

    def test_scp_with_leading_zeros_raises(self):
        """Leading zeros: 007999 -> int 7999, still a valid port."""
        with pytest.raises(ValueError, match="ssh://"):
            DependencyReference.parse("git@host.example.com:007999/project/repo.git")

    def test_scp_port_only_no_path_raises(self):
        """git@host:7999 with no repo path after the port."""
        with pytest.raises(ValueError, match="no repository path follows"):
            DependencyReference.parse("git@host.example.com:7999")

    def test_scp_port_trailing_slash_no_path_raises(self):
        """git@host:7999/ — trailing slash but empty remaining path."""
        with pytest.raises(ValueError, match="no repository path follows"):
            DependencyReference.parse("git@host.example.com:7999/")

    def test_scp_port_with_ref_raises_and_preserves_ref(self):
        """Port-like segment with #ref should be caught; suggestion preserves the ref."""
        with pytest.raises(
            ValueError,
            match=r"ssh://git@host\.example\.com:7999/project/repo\.git#main",
        ):
            DependencyReference.parse("git@host.example.com:7999/project/repo.git#main")

    def test_scp_port_with_alias_raises_and_preserves_alias(self):
        """Port-like segment with @alias should be caught; suggestion preserves the alias."""
        with pytest.raises(
            ValueError,
            match=r"ssh://git@host\.example\.com:7999/project/repo\.git@my-alias",
        ):
            DependencyReference.parse("git@host.example.com:7999/project/repo.git@my-alias")

    def test_scp_port_with_ref_and_alias_preserves_both(self):
        """Suggestion should include both #ref and @alias when present."""
        with pytest.raises(
            ValueError,
            match=r"ssh://git@host\.example\.com:7999/project/repo\.git#v1\.0@my-alias",
        ):
            DependencyReference.parse("git@host.example.com:7999/project/repo.git#v1.0@my-alias")

    def test_suggestion_includes_git_suffix(self):
        """When the user wrote .git, the suggestion should preserve it."""
        with pytest.raises(
            ValueError,
            match=r"ssh://git@host\.example\.com:7999/project/repo\.git",
        ):
            DependencyReference.parse("git@host.example.com:7999/project/repo.git")

    def test_suggestion_omits_git_suffix_when_absent(self):
        """When the user omitted .git, the suggestion should not add it."""
        with pytest.raises(ValueError) as excinfo:
            DependencyReference.parse("git@host.example.com:7999/project/repo")
        msg = str(excinfo.value)
        assert "ssh://git@host.example.com:7999/project/repo" in msg
        assert not msg.endswith(".git")

    def test_port_zero_not_detected(self):
        """Port 0 is invalid -- should NOT trigger port detection, parses as org name."""
        dep = DependencyReference.parse("git@host.example.com:0/repo")
        assert dep.repo_url == "0/repo"
        assert dep.port is None

    def test_port_out_of_range_not_detected(self):
        """99999 > 65535 -- not a valid port, should NOT trigger port detection."""
        dep = DependencyReference.parse("git@host.example.com:99999/repo")
        assert dep.repo_url == "99999/repo"
        assert dep.port is None

    def test_normal_org_name_not_detected(self):
        """Non-numeric org name should parse normally."""
        dep = DependencyReference.parse("git@gitlab.com:acme/repo.git")
        assert dep.repo_url == "acme/repo"
        assert dep.port is None

    def test_alphanumeric_first_segment_not_detected(self):
        """'v2' is not purely numeric -- should parse normally."""
        dep = DependencyReference.parse("git@gitlab.com:v2/repo.git")
        assert dep.repo_url == "v2/repo"
        assert dep.port is None

    def test_ssh_protocol_with_port_still_works(self):
        """ssh:// URL form with port must continue working (regression guard)."""
        dep = DependencyReference.parse("ssh://git@bitbucket.example.com:7999/project/repo.git")
        assert dep.host == "bitbucket.example.com"
        assert dep.port == 7999
        assert dep.repo_url == "project/repo"
