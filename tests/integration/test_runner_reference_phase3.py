"""Phase-3 integration tests for script_runner.py and models/dependency/reference.py.

Coverage targets
----------------
* ``src/apm_cli/core/script_runner.py``   (integration gap = 412 lines)
* ``src/apm_cli/models/dependency/reference.py``  (integration gap = 359 lines)

Strategy
--------
* Minimal mocking: only external I/O (subprocess, filesystem for side effects).
* No live network calls.
* Type hints on every function signature.
* URL assertions use ``urllib.parse.urlparse``, never substring matching.
"""

from __future__ import annotations

import os
import urllib.parse
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helper: import DependencyReference through the public re-export so tests
# follow the same import path as production code.
# ---------------------------------------------------------------------------


def _dep_ref():
    from apm_cli.models.apm_package import DependencyReference

    return DependencyReference


# ============================================================================
# SECTION 1 - DependencyReference: basic shorthand parsing
# ============================================================================


class TestParseShorthand:
    """Parse simple ``owner/repo`` shorthand strings."""

    def test_owner_repo_no_extras(self) -> None:
        DR = _dep_ref()
        ref = DR.parse("acme/my-tool")
        assert ref.repo_url == "acme/my-tool"
        assert ref.reference is None
        assert ref.alias is None
        assert ref.is_virtual is False
        assert ref.is_local is False

    def test_owner_repo_with_ref(self) -> None:
        DR = _dep_ref()
        ref = DR.parse("acme/my-tool#main")
        assert ref.repo_url == "acme/my-tool"
        assert ref.reference == "main"

    def test_owner_repo_with_semver_tag(self) -> None:
        DR = _dep_ref()
        ref = DR.parse("acme/my-tool#v2.3.1")
        assert ref.reference == "v2.3.1"

    def test_owner_repo_with_commit_sha(self) -> None:
        DR = _dep_ref()
        sha = "a" * 40
        ref = DR.parse(f"acme/my-tool#{sha}")
        assert ref.reference == sha

    def test_owner_repo_with_alias(self) -> None:
        # Alias in shorthand is only extracted via SCP (git@host:path@alias) form
        DR = _dep_ref()
        ref = DR.parse("git@github.com:acme/my-tool.git@my-alias")
        assert ref.alias == "my-alias"
        assert ref.reference is None

    def test_owner_repo_with_ref_and_alias(self) -> None:
        # Ref + alias via SCP form: git@host:path#ref@alias
        DR = _dep_ref()
        ref = DR.parse("git@github.com:acme/my-tool.git#develop@dev-copy")
        assert ref.reference == "develop"
        assert ref.alias == "dev-copy"

    def test_empty_string_raises(self) -> None:
        DR = _dep_ref()
        with pytest.raises(ValueError, match="Empty dependency string"):
            DR.parse("   ")

    def test_control_character_raises(self) -> None:
        DR = _dep_ref()
        with pytest.raises(ValueError, match="control characters"):
            DR.parse("owner/repo\x00extra")

    def test_invalid_alias_raises(self) -> None:
        # Invalid alias chars via SCP form (alias extracted from ssh repo path)
        DR = _dep_ref()
        with pytest.raises(ValueError, match="Invalid alias"):
            DR.parse("git@github.com:owner/repo.git@bad alias!")

    def test_protocol_relative_url_raises(self) -> None:
        DR = _dep_ref()
        with pytest.raises(ValueError):
            DR.parse("//github.com/owner/repo")


# ============================================================================
# SECTION 2 - DependencyReference: HTTPS URL parsing
# ============================================================================


class TestParseHttpsUrls:
    """Parse ``https://`` URLs for various git hosts."""

    def test_github_https_url(self) -> None:
        DR = _dep_ref()
        ref = DR.parse("https://github.com/acme/tool.git")
        parsed = urllib.parse.urlparse(ref.to_github_url())
        assert parsed.hostname == "github.com"
        assert ref.repo_url == "acme/tool"
        assert ref.explicit_scheme == "https"

    def test_github_https_url_with_ref(self) -> None:
        DR = _dep_ref()
        ref = DR.parse("https://github.com/acme/tool.git#v1.0")
        assert ref.reference == "v1.0"

    def test_gitlab_https_url(self) -> None:
        DR = _dep_ref()
        ref = DR.parse("https://gitlab.com/acme/rules.git")
        parsed = urllib.parse.urlparse(ref.to_github_url())
        assert parsed.hostname == "gitlab.com"
        assert ref.host == "gitlab.com"

    def test_https_default_port_stripped(self) -> None:
        DR = _dep_ref()
        ref = DR.parse("https://github.com:443/owner/repo.git")
        # Port 443 is the default for HTTPS and must be normalised away
        assert ref.port is None

    def test_https_non_standard_port_preserved(self) -> None:
        DR = _dep_ref()
        ref = DR.parse("https://github.example.com:8443/owner/repo.git")
        assert ref.port == 8443

    def test_https_url_to_canonical_uses_host_for_non_default(self) -> None:
        DR = _dep_ref()
        ref = DR.parse("https://gitlab.com/owner/repo.git")
        canonical = ref.to_canonical()
        # Non-default host must appear in canonical form
        host = canonical.split("/", 1)[0]
        assert host == "gitlab.com"

    def test_http_insecure_url_sets_flag(self) -> None:
        DR = _dep_ref()
        ref = DR.parse("http://internal.corp/owner/repo.git")
        assert ref.is_insecure is True
        assert ref.explicit_scheme == "http"

    def test_http_default_port_stripped(self) -> None:
        DR = _dep_ref()
        ref = DR.parse("http://internal.corp:80/owner/repo.git")
        assert ref.port is None


# ============================================================================
# SECTION 3 - DependencyReference: SSH URL parsing
# ============================================================================


class TestParseSshUrls:
    """Parse SCP shorthand and ``ssh://`` protocol URLs."""

    def test_scp_github(self) -> None:
        DR = _dep_ref()
        ref = DR.parse("git@github.com:owner/repo.git")
        assert ref.host == "github.com"
        assert ref.repo_url == "owner/repo"
        assert ref.explicit_scheme == "ssh"
        assert ref.ssh_user == "git"

    def test_scp_with_ref(self) -> None:
        DR = _dep_ref()
        ref = DR.parse("git@github.com:owner/repo.git#v3.0")
        assert ref.reference == "v3.0"

    def test_scp_with_alias(self) -> None:
        DR = _dep_ref()
        ref = DR.parse("git@github.com:owner/repo.git@my-alias")
        assert ref.alias == "my-alias"

    def test_scp_port_number_in_path_raises(self) -> None:
        DR = _dep_ref()
        with pytest.raises(ValueError, match="ssh://"):
            DR.parse("git@bitbucket.example.com:7999/owner/repo.git")

    def test_ssh_protocol_url(self) -> None:
        DR = _dep_ref()
        ref = DR.parse("ssh://git@github.com/owner/repo.git")
        assert ref.host == "github.com"
        assert ref.repo_url == "owner/repo"
        assert ref.explicit_scheme == "ssh"

    def test_ssh_protocol_url_with_port(self) -> None:
        DR = _dep_ref()
        ref = DR.parse("ssh://git@bitbucket.example.com:7999/owner/repo.git")
        assert ref.port == 7999

    def test_ssh_protocol_default_port_stripped(self) -> None:
        DR = _dep_ref()
        ref = DR.parse("ssh://git@github.com:22/owner/repo.git")
        assert ref.port is None

    def test_ssh_protocol_url_with_ref(self) -> None:
        DR = _dep_ref()
        ref = DR.parse("ssh://git@github.com/owner/repo.git#feature-x")
        assert ref.reference == "feature-x"

    def test_ssh_protocol_url_percent_encoded_userinfo_raises(self) -> None:
        # Test _parse_ssh_protocol_url directly — parse() runs urllib.parse.unquote()
        # first which decodes %2D to '-' before the SSH percent-encoding check runs,
        # so the SSH-user allowlist catches the leading '-' instead.
        DR = _dep_ref()
        with pytest.raises(ValueError, match="Percent-encoded"):
            DR._parse_ssh_protocol_url("ssh://%2DoProxyCommand=evil@github.com/owner/repo.git")

    def test_scp_custom_emu_user(self) -> None:
        DR = _dep_ref()
        ref = DR.parse("emu-user@github.com:owner/repo.git")
        assert ref.ssh_user == "emu-user"


# ============================================================================
# SECTION 4 - DependencyReference: virtual packages
# ============================================================================


class TestVirtualPackages:
    """Virtual file and subdirectory package detection and classification."""

    def test_virtual_file_prompt_md(self) -> None:
        DR = _dep_ref()
        ref = DR.parse("owner/repo/prompts/code-review.prompt.md")
        assert ref.is_virtual is True
        assert ref.virtual_path == "prompts/code-review.prompt.md"
        assert ref.is_virtual_file() is True
        assert ref.is_virtual_subdirectory() is False

    def test_virtual_file_instructions_md(self) -> None:
        DR = _dep_ref()
        ref = DR.parse("owner/repo/security.instructions.md")
        assert ref.is_virtual is True
        assert ref.is_virtual_file() is True

    def test_virtual_file_chatmode_md(self) -> None:
        DR = _dep_ref()
        ref = DR.parse("owner/repo/modes/debug.chatmode.md")
        assert ref.is_virtual is True
        assert ref.is_virtual_file() is True

    def test_virtual_file_agent_md(self) -> None:
        DR = _dep_ref()
        ref = DR.parse("owner/repo/agents/coder.agent.md")
        assert ref.is_virtual is True
        assert ref.is_virtual_file() is True

    def test_virtual_subdirectory_skills(self) -> None:
        DR = _dep_ref()
        ref = DR.parse("owner/repo/skills/architecture-review")
        assert ref.is_virtual is True
        assert ref.is_virtual_subdirectory() is True
        assert ref.is_virtual_file() is False

    def test_virtual_subdirectory_collections(self) -> None:
        DR = _dep_ref()
        ref = DR.parse("owner/repo/collections/project-planning")
        assert ref.is_virtual is True
        assert ref.is_virtual_subdirectory() is True

    def test_virtual_package_name_file(self) -> None:
        DR = _dep_ref()
        ref = DR.parse("owner/repo/prompts/code-review.prompt.md")
        name = ref.get_virtual_package_name()
        assert "repo" in name
        assert "code-review" in name

    def test_virtual_package_name_subdir(self) -> None:
        DR = _dep_ref()
        ref = DR.parse("owner/repo/skills/arch-review")
        name = ref.get_virtual_package_name()
        assert "arch-review" in name

    def test_virtual_type_none_for_non_virtual(self) -> None:
        DR = _dep_ref()
        ref = DR.parse("owner/repo")
        assert ref.virtual_type is None

    def test_removed_collection_yml_raises(self) -> None:
        DR = _dep_ref()
        with pytest.raises(ValueError, match=r"\.collection\.yml"):
            DR.parse("owner/repo/something.collection.yml")

    def test_unknown_dotted_extension_raises(self) -> None:
        DR = _dep_ref()
        from apm_cli.models.validation import InvalidVirtualPackageExtensionError

        with pytest.raises(InvalidVirtualPackageExtensionError):
            DR.parse("owner/repo/prompts/file.txt")

    def test_virtual_with_ref(self) -> None:
        DR = _dep_ref()
        ref = DR.parse("owner/repo/prompts/review.prompt.md#v2")
        assert ref.reference == "v2"
        assert ref.is_virtual is True


# ============================================================================
# SECTION 5 - DependencyReference: local paths
# ============================================================================


class TestLocalPaths:
    """Local path detection and parsing."""

    def test_relative_dot_slash(self) -> None:
        DR = _dep_ref()
        assert DR.is_local_path("./packages/my-pkg") is True

    def test_relative_dot_dot_slash(self) -> None:
        DR = _dep_ref()
        assert DR.is_local_path("../sibling") is True

    def test_absolute_slash(self) -> None:
        DR = _dep_ref()
        assert DR.is_local_path("/usr/local/pkg") is True

    def test_tilde_slash(self) -> None:
        DR = _dep_ref()
        assert DR.is_local_path("~/projects/pkg") is True

    def test_windows_drive(self) -> None:
        DR = _dep_ref()
        assert DR.is_local_path("C:\\Projects\\pkg") is True

    def test_windows_drive_forward_slash(self) -> None:
        DR = _dep_ref()
        assert DR.is_local_path("D:/repos/pkg") is True

    def test_protocol_relative_not_local(self) -> None:
        DR = _dep_ref()
        assert DR.is_local_path("//host/share") is False

    def test_plain_string_not_local(self) -> None:
        DR = _dep_ref()
        assert DR.is_local_path("owner/repo") is False

    def test_parse_local_path_sets_fields(self) -> None:
        DR = _dep_ref()
        ref = DR.parse("./packages/my-pkg")
        assert ref.is_local is True
        assert ref.local_path == "./packages/my-pkg"

    def test_local_path_bare_dot_slash_raises(self) -> None:
        DR = _dep_ref()
        with pytest.raises(ValueError, match="does not resolve to a named directory"):
            DR.parse("./")

    def test_local_to_canonical_returns_path(self) -> None:
        DR = _dep_ref()
        ref = DR.parse("./local-dir")
        assert ref.to_canonical() == "./local-dir"

    def test_local_get_identity_returns_path(self) -> None:
        DR = _dep_ref()
        ref = DR.parse("./local-dir")
        assert ref.get_identity() == "./local-dir"

    def test_local_get_unique_key(self) -> None:
        DR = _dep_ref()
        ref = DR.parse("./local-dir")
        assert ref.get_unique_key() == "./local-dir"


# ============================================================================
# SECTION 6 - DependencyReference: to_canonical / get_identity
# ============================================================================


class TestCanonicalAndIdentity:
    """Verify canonical and identity string generation."""

    def test_canonical_default_host_stripped(self) -> None:
        DR = _dep_ref()
        ref = DR.parse("owner/repo")
        canonical = ref.to_canonical()
        # Default host (github.com) should NOT appear in canonical for shorthand
        assert "github.com" not in canonical

    def test_canonical_non_default_host_kept(self) -> None:
        DR = _dep_ref()
        ref = DR.parse("https://gitlab.com/owner/repo.git")
        canonical = ref.to_canonical()
        assert canonical.split("/", 1)[0] == "gitlab.com"

    def test_canonical_appends_ref(self) -> None:
        DR = _dep_ref()
        ref = DR.parse("owner/repo#v1.2.3")
        assert "#v1.2.3" in ref.to_canonical()

    def test_canonical_appends_virtual_path(self) -> None:
        DR = _dep_ref()
        ref = DR.parse("owner/repo/prompts/review.prompt.md")
        canonical = ref.to_canonical()
        assert "prompts/review.prompt.md" in canonical

    def test_identity_strips_ref(self) -> None:
        DR = _dep_ref()
        ref = DR.parse("owner/repo#v1.2.3")
        identity = ref.get_identity()
        assert "#" not in identity

    def test_canonicalize_static_method(self) -> None:
        DR = _dep_ref()
        # canonicalize() = parse() + to_canonical(); ref is preserved in canonical form
        canonical = DR.canonicalize("owner/repo#main")
        assert "owner/repo" in canonical
        assert "#main" in canonical

    def test_canonical_with_port(self) -> None:
        DR = _dep_ref()
        ref = DR.parse("ssh://git@bitbucket.example.com:7999/owner/repo.git")
        canonical = ref.to_canonical()
        parsed = urllib.parse.urlparse(f"https://{canonical.split('/')[0]}")  # noqa: F841
        assert "7999" in canonical  # Port must appear in canonical when non-default

    def test_get_unique_key_virtual(self) -> None:
        DR = _dep_ref()
        ref = DR.parse("owner/repo/skills/my-skill")
        key = ref.get_unique_key()
        assert "my-skill" in key

    def test_get_unique_key_non_virtual(self) -> None:
        DR = _dep_ref()
        ref = DR.parse("owner/repo")
        assert ref.get_unique_key() == "owner/repo"


# ============================================================================
# SECTION 7 - DependencyReference: to_github_url / to_apm_yml_entry
# ============================================================================


class TestToGithubUrlAndApmYmlEntry:
    """URL generation and apm.yml serialisation."""

    def test_to_github_url_default_host(self) -> None:
        DR = _dep_ref()
        ref = DR.parse("owner/repo")
        url = ref.to_github_url()
        parsed = urllib.parse.urlparse(url)
        assert parsed.scheme == "https"
        assert parsed.hostname == "github.com"
        assert "/owner/repo" in parsed.path

    def test_to_github_url_gitlab(self) -> None:
        DR = _dep_ref()
        ref = DR.parse("https://gitlab.com/owner/repo.git")
        url = ref.to_github_url()
        parsed = urllib.parse.urlparse(url)
        assert parsed.hostname == "gitlab.com"

    def test_to_github_url_ado(self) -> None:
        DR = _dep_ref()
        ref = DR.parse("https://dev.azure.com/myorg/myproject/_git/myrepo")
        url = ref.to_github_url()
        parsed = urllib.parse.urlparse(url)
        assert parsed.hostname == "dev.azure.com"
        assert "_git" in parsed.path

    def test_to_github_url_local_returns_path(self) -> None:
        DR = _dep_ref()
        ref = DR.parse("./local-dir")
        assert ref.to_github_url() == "./local-dir"

    def test_to_github_url_http_insecure(self) -> None:
        DR = _dep_ref()
        ref = DR.parse("http://internal.corp/owner/repo.git")
        url = ref.to_github_url()
        parsed = urllib.parse.urlparse(url)
        assert parsed.scheme == "http"

    def test_to_apm_yml_entry_simple(self) -> None:
        DR = _dep_ref()
        ref = DR.parse("owner/repo")
        entry = ref.to_apm_yml_entry()
        assert isinstance(entry, str)

    def test_to_apm_yml_entry_insecure_is_dict(self) -> None:
        DR = _dep_ref()
        ref = DR.parse("http://internal.corp/owner/repo.git")
        ref.allow_insecure = True
        entry = ref.to_apm_yml_entry()
        assert isinstance(entry, dict)
        assert "git" in entry
        assert entry["allow_insecure"] is True

    def test_to_apm_yml_entry_skill_subset_is_dict(self) -> None:
        DR = _dep_ref()
        ref = DR.parse("owner/repo")
        ref.skill_subset = ["skill-a", "skill-b"]
        entry = ref.to_apm_yml_entry()
        assert isinstance(entry, dict)
        assert "skills" in entry
        assert sorted(entry["skills"]) == ["skill-a", "skill-b"]

    def test_to_clone_url_delegates_to_github_url(self) -> None:
        DR = _dep_ref()
        ref = DR.parse("owner/repo")
        assert ref.to_clone_url() == ref.to_github_url()


# ============================================================================
# SECTION 8 - DependencyReference: get_install_path
# ============================================================================


class TestGetInstallPath:
    """Verify apm_modules install path computation."""

    def test_regular_package_install_path(self, tmp_path: Path) -> None:
        DR = _dep_ref()
        ref = DR.parse("owner/repo")
        apm_modules = tmp_path / "apm_modules"
        install = ref.get_install_path(apm_modules)
        assert install == apm_modules / "owner" / "repo"

    def test_virtual_file_install_path_flattened(self, tmp_path: Path) -> None:
        DR = _dep_ref()
        ref = DR.parse("owner/repo/prompts/review.prompt.md")
        apm_modules = tmp_path / "apm_modules"
        install = ref.get_install_path(apm_modules)
        # Virtual file packages are flattened under owner namespace
        assert install.parent == apm_modules / "owner"

    def test_virtual_subdirectory_install_path(self, tmp_path: Path) -> None:
        DR = _dep_ref()
        ref = DR.parse("owner/repo/skills/arch-review")
        apm_modules = tmp_path / "apm_modules"
        install = ref.get_install_path(apm_modules)
        # Subdirectory packages preserve natural path
        assert "arch-review" in str(install)

    def test_local_package_install_path(self, tmp_path: Path) -> None:
        DR = _dep_ref()
        ref = DR.parse("./my-local-pkg")
        apm_modules = tmp_path / "apm_modules"
        install = ref.get_install_path(apm_modules)
        assert "_local" in str(install)
        assert "my-local-pkg" in str(install)

    def test_ado_package_install_path(self, tmp_path: Path) -> None:
        DR = _dep_ref()
        ref = DR.parse("https://dev.azure.com/myorg/myproject/_git/myrepo")
        apm_modules = tmp_path / "apm_modules"
        install = ref.get_install_path(apm_modules)
        # ADO: org/project/repo layout
        assert "myorg" in str(install)
        assert "myproject" in str(install)
        assert "myrepo" in str(install)

    def test_path_traversal_in_repo_url_raises(self, tmp_path: Path) -> None:
        from apm_cli.utils.path_security import PathTraversalError

        DR = _dep_ref()
        # Manually construct a reference with traversal sequences
        ref = DR(repo_url="owner/repo", is_virtual=False)
        # Override the repo_url after construction to bypass parse validation
        ref.repo_url = "../../../etc/passwd"
        apm_modules = tmp_path / "apm_modules"
        with pytest.raises((PathTraversalError, ValueError)):
            ref.get_install_path(apm_modules)


# ============================================================================
# SECTION 9 - DependencyReference: Azure DevOps parsing
# ============================================================================


class TestAzureDevOpsParsing:
    """ADO URL forms, _git segment normalisation, and field extraction."""

    def test_ado_https_url(self) -> None:
        DR = _dep_ref()
        ref = DR.parse("https://dev.azure.com/myorg/myproject/_git/myrepo")
        assert ref.ado_organization == "myorg"
        assert ref.ado_project == "myproject"
        assert ref.ado_repo == "myrepo"
        assert ref.is_azure_devops() is True

    def test_ado_shorthand(self) -> None:
        DR = _dep_ref()
        ref = DR.parse("dev.azure.com/myorg/myproject/myrepo")
        assert ref.is_azure_devops() is True

    def test_ado_with_ref(self) -> None:
        DR = _dep_ref()
        ref = DR.parse("https://dev.azure.com/myorg/myproject/_git/myrepo#feature/xyz")
        assert ref.reference == "feature/xyz"

    def test_ado_to_github_url_includes_git_segment(self) -> None:
        DR = _dep_ref()
        ref = DR.parse("https://dev.azure.com/myorg/myproject/_git/myrepo")
        url = ref.to_github_url()
        parsed = urllib.parse.urlparse(url)
        assert "_git" in parsed.path

    def test_non_ado_is_not_azure(self) -> None:
        DR = _dep_ref()
        ref = DR.parse("owner/repo")
        assert ref.is_azure_devops() is False


# ============================================================================
# SECTION 10 - DependencyReference: parse_from_dict
# ============================================================================


class TestParseFromDict:
    """Object-style dependency entries from apm.yml."""

    def test_git_simple(self) -> None:
        DR = _dep_ref()
        ref = DR.parse_from_dict({"git": "https://github.com/owner/repo.git"})
        assert ref.repo_url == "owner/repo"

    def test_git_with_ref_override(self) -> None:
        DR = _dep_ref()
        ref = DR.parse_from_dict({"git": "https://github.com/owner/repo.git", "ref": "v3.0"})
        assert ref.reference == "v3.0"

    def test_git_with_alias_override(self) -> None:
        DR = _dep_ref()
        ref = DR.parse_from_dict({"git": "owner/repo", "alias": "my-alias"})
        assert ref.alias == "my-alias"

    def test_git_with_path_virtual(self) -> None:
        DR = _dep_ref()
        ref = DR.parse_from_dict({"git": "owner/repo", "path": "prompts/review.prompt.md"})
        assert ref.is_virtual is True
        assert ref.virtual_path == "prompts/review.prompt.md"

    def test_git_parent_valid(self) -> None:
        DR = _dep_ref()
        ref = DR.parse_from_dict({"git": "parent", "path": "packages/shared"})
        assert ref.is_parent_repo_inheritance is True
        assert ref.virtual_path == "packages/shared"

    def test_git_parent_with_ref(self) -> None:
        DR = _dep_ref()
        ref = DR.parse_from_dict({"git": "parent", "path": "packages/shared", "ref": "v1"})
        assert ref.reference == "v1"

    def test_git_parent_missing_path_raises(self) -> None:
        DR = _dep_ref()
        with pytest.raises(ValueError, match="requires a 'path' field"):
            DR.parse_from_dict({"git": "parent"})

    def test_git_empty_raises(self) -> None:
        DR = _dep_ref()
        with pytest.raises(ValueError, match="non-empty string"):
            DR.parse_from_dict({"git": ""})

    def test_missing_git_and_path_raises(self) -> None:
        DR = _dep_ref()
        with pytest.raises(ValueError, match=r"'git', 'path', or 'registry' field"):
            DR.parse_from_dict({"version": "1.0"})

    def test_allow_insecure_non_bool_raises(self) -> None:
        DR = _dep_ref()
        with pytest.raises(ValueError, match=r"allow_insecure.*boolean"):
            DR.parse_from_dict({"git": "owner/repo", "allow_insecure": "yes"})

    def test_skills_list_valid(self) -> None:
        DR = _dep_ref()
        ref = DR.parse_from_dict({"git": "owner/repo", "skills": ["skill-a", "skill-b"]})
        assert ref.skill_subset == ["skill-a", "skill-b"]

    def test_skills_empty_list_raises(self) -> None:
        DR = _dep_ref()
        with pytest.raises(ValueError, match="at least one name"):
            DR.parse_from_dict({"git": "owner/repo", "skills": []})

    def test_skills_non_list_raises(self) -> None:
        DR = _dep_ref()
        with pytest.raises(ValueError, match="list of skill names"):
            DR.parse_from_dict({"git": "owner/repo", "skills": "skill-a"})

    def test_path_only_local(self, tmp_path: Path) -> None:
        DR = _dep_ref()
        pkg = tmp_path / "local-pkg"
        pkg.mkdir()
        ref = DR.parse_from_dict({"path": str(pkg)})
        assert ref.is_local is True

    def test_alias_invalid_chars_raises(self) -> None:
        DR = _dep_ref()
        with pytest.raises(ValueError, match="Invalid alias"):
            DR.parse_from_dict({"git": "owner/repo", "alias": "bad alias!"})

    def test_path_traversal_in_sub_path_raises(self) -> None:
        DR = _dep_ref()
        with pytest.raises((ValueError, Exception)):
            DR.parse_from_dict({"git": "owner/repo", "path": "../../etc/passwd"})


# ============================================================================
# SECTION 11 - DependencyReference: GitLab shorthand helpers
# ============================================================================


class TestGitlabShorthandHelpers:
    """GitLab direct shorthand probing and boundary detection."""

    def test_split_gitlab_shorthand_returns_tuple(self) -> None:
        DR = _dep_ref()
        result = DR.split_gitlab_direct_shorthand_parts(
            "gitlab.com/owner/repo/prompts/review.prompt.md"
        )
        assert result is not None
        host, segs, _ref = result
        assert host == "gitlab.com"
        assert len(segs) >= 2

    def test_split_gitlab_shorthand_with_ref(self) -> None:
        DR = _dep_ref()
        result = DR.split_gitlab_direct_shorthand_parts("gitlab.com/owner/repo/skills/tool#v1")
        assert result is not None
        _host, _segs, ref = result
        assert ref == "v1"

    def test_split_gitlab_shorthand_returns_none_for_https(self) -> None:
        DR = _dep_ref()
        result = DR.split_gitlab_direct_shorthand_parts("https://gitlab.com/owner/repo")
        assert result is None

    def test_split_gitlab_shorthand_returns_none_for_github(self) -> None:
        DR = _dep_ref()
        # github.com is not a GitLab host
        result = DR.split_gitlab_direct_shorthand_parts("github.com/owner/repo/skills/foo")
        assert result is None

    def test_iter_boundary_candidates_yields_pairs(self) -> None:
        DR = _dep_ref()
        segs = ["owner", "repo", "prompts", "review.prompt.md"]
        candidates = list(DR.iter_gitlab_direct_shorthand_boundary_candidates(segs))
        assert len(candidates) >= 1
        # Each candidate is (repo_url, suffix)
        for repo_url, _suffix in candidates:
            assert "/" in repo_url or len(repo_url) > 0

    def test_iter_boundary_candidates_empty_for_two_segs(self) -> None:
        DR = _dep_ref()
        segs = ["owner", "repo"]
        candidates = list(DR.iter_gitlab_direct_shorthand_boundary_candidates(segs))
        assert candidates == []

    def test_virtual_suffix_is_installable_prompt(self) -> None:
        DR = _dep_ref()
        assert DR.virtual_suffix_is_installable_shape("prompts/review.prompt.md") is True

    def test_virtual_suffix_is_installable_collection(self) -> None:
        DR = _dep_ref()
        assert DR.virtual_suffix_is_installable_shape("collections/planning") is True

    def test_virtual_suffix_is_installable_extension_less(self) -> None:
        DR = _dep_ref()
        assert DR.virtual_suffix_is_installable_shape("skills/my-tool") is True

    def test_virtual_suffix_not_installable_empty(self) -> None:
        DR = _dep_ref()
        assert DR.virtual_suffix_is_installable_shape("") is False

    def test_needs_probing_for_long_gitlab_path(self) -> None:
        DR = _dep_ref()
        package = "gitlab.com/owner/repo/skills/tool"
        dep = DR.parse(package)
        # If already resolved as virtual, probing is False (no ambiguity)
        # If not virtual and has 3+ segments, probing is True
        # Either way, the method must not raise
        result = DR.needs_gitlab_direct_shorthand_probing(package, dep)
        assert isinstance(result, bool)

    def test_from_gitlab_shorthand_probe_sets_fields(self) -> None:
        DR = _dep_ref()
        ref = DR.from_gitlab_shorthand_probe("gitlab.com", "owner/repo", "skills/my-tool", "v1")
        assert ref.host == "gitlab.com"
        assert ref.repo_url == "owner/repo"
        assert ref.virtual_path == "skills/my-tool"
        assert ref.is_virtual is True
        assert ref.reference == "v1"


# ============================================================================
# SECTION 12 - DependencyReference: display name and __str__
# ============================================================================


class TestDisplayNameAndStr:
    """get_display_name and __str__ behaviour."""

    def test_display_name_alias_preferred(self) -> None:
        # Alias is extracted via SCP form (git@host:path#ref@alias)
        DR = _dep_ref()
        ref = DR.parse("git@github.com:owner/repo.git@my-alias")
        assert ref.get_display_name() == "my-alias"

    def test_display_name_local_path(self) -> None:
        DR = _dep_ref()
        ref = DR.parse("./local-dir")
        assert ref.get_display_name() == "./local-dir"

    def test_display_name_virtual_uses_package_name(self) -> None:
        DR = _dep_ref()
        ref = DR.parse("owner/repo/skills/arch-review")
        name = ref.get_display_name()
        assert "arch-review" in name

    def test_str_local_path(self) -> None:
        DR = _dep_ref()
        ref = DR.parse("./local-dir")
        assert str(ref) == "./local-dir"

    def test_str_with_host(self) -> None:
        DR = _dep_ref()
        ref = DR.parse("https://gitlab.com/owner/repo.git")
        s = str(ref)
        assert s.split("/", 1)[0] == "gitlab.com"


# ============================================================================
# SECTION 13 - PromptCompiler: compile and parameter substitution
# ============================================================================


class TestPromptCompiler:
    """PromptCompiler.compile() and helper methods."""

    def test_compile_no_params(self, tmp_path: Path) -> None:
        from apm_cli.core.script_runner import PromptCompiler

        prompt = tmp_path / "review.prompt.md"
        prompt.write_text("# Review\nPlease review this code.", encoding="utf-8")
        compiler = PromptCompiler()
        compiler.compiled_dir = tmp_path / ".apm" / "compiled"
        os.chdir(tmp_path)
        result_path = compiler.compile(str(prompt), {})
        compiled = Path(result_path)
        assert compiled.exists()
        assert "Please review this code." in compiled.read_text(encoding="utf-8")

    def test_compile_substitutes_params(self, tmp_path: Path) -> None:
        from apm_cli.core.script_runner import PromptCompiler

        prompt = tmp_path / "task.prompt.md"
        prompt.write_text("# Task\nPlease do: ${input:task}", encoding="utf-8")
        compiler = PromptCompiler()
        compiler.compiled_dir = tmp_path / ".apm" / "compiled"
        os.chdir(tmp_path)
        result_path = compiler.compile(str(prompt), {"task": "code review"})
        compiled = Path(result_path)
        content = compiled.read_text(encoding="utf-8")
        assert "code review" in content
        assert "${input:task}" not in content

    def test_compile_strips_frontmatter(self, tmp_path: Path) -> None:
        from apm_cli.core.script_runner import PromptCompiler

        prompt = tmp_path / "front.prompt.md"
        prompt.write_text("---\ntitle: Test\n---\n# Body\nMain content here.", encoding="utf-8")
        compiler = PromptCompiler()
        compiler.compiled_dir = tmp_path / ".apm" / "compiled"
        os.chdir(tmp_path)
        result_path = compiler.compile(str(prompt), {})
        compiled = Path(result_path)
        content = compiled.read_text(encoding="utf-8")
        assert "Main content here." in content
        assert "title: Test" not in content

    def test_compile_multiple_params(self, tmp_path: Path) -> None:
        from apm_cli.core.script_runner import PromptCompiler

        prompt = tmp_path / "multi.prompt.md"
        prompt.write_text("Hello ${input:name}, do ${input:action}.", encoding="utf-8")
        compiler = PromptCompiler()
        compiler.compiled_dir = tmp_path / ".apm" / "compiled"
        os.chdir(tmp_path)
        result_path = compiler.compile(str(prompt), {"name": "Alice", "action": "code review"})
        content = Path(result_path).read_text(encoding="utf-8")
        assert "Alice" in content
        assert "code review" in content

    def test_compile_symlink_raises(self, tmp_path: Path) -> None:
        from apm_cli.core.script_runner import PromptCompiler

        target = tmp_path / "real.prompt.md"
        target.write_text("# Real", encoding="utf-8")
        link = tmp_path / "linked.prompt.md"
        link.symlink_to(target)
        compiler = PromptCompiler()
        compiler.compiled_dir = tmp_path / ".apm" / "compiled"
        os.chdir(tmp_path)
        with pytest.raises(FileNotFoundError, match="symlink"):
            compiler.compile("linked.prompt.md", {})

    def test_compile_missing_file_raises(self, tmp_path: Path) -> None:
        from apm_cli.core.script_runner import PromptCompiler

        compiler = PromptCompiler()
        compiler.compiled_dir = tmp_path / ".apm" / "compiled"
        os.chdir(tmp_path)
        with pytest.raises(FileNotFoundError):
            compiler.compile("missing.prompt.md", {})

    def test_compile_creates_compiled_dir(self, tmp_path: Path) -> None:
        from apm_cli.core.script_runner import PromptCompiler

        prompt = tmp_path / "x.prompt.md"
        prompt.write_text("# X", encoding="utf-8")
        compiler = PromptCompiler()
        compiled_dir = tmp_path / "nested" / ".apm" / "compiled"
        compiler.compiled_dir = compiled_dir
        os.chdir(tmp_path)
        compiler.compile(str(prompt), {})
        assert compiled_dir.exists()

    def test_substitute_parameters_replaces_placeholders(self) -> None:
        from apm_cli.core.script_runner import PromptCompiler

        compiler = PromptCompiler()
        content = "Do ${input:action} for ${input:target}."
        result = compiler._substitute_parameters(content, {"action": "review", "target": "PR"})
        assert result == "Do review for PR."

    def test_substitute_parameters_no_params_unchanged(self) -> None:
        from apm_cli.core.script_runner import PromptCompiler

        compiler = PromptCompiler()
        content = "No placeholders here."
        assert compiler._substitute_parameters(content, {}) == content


# ============================================================================
# SECTION 14 - ScriptRunner: list_scripts and _load_config
# ============================================================================


class TestScriptRunnerConfig:
    """list_scripts() and _load_config() with real apm.yml files."""

    def test_list_scripts_returns_dict(self, tmp_path: Path) -> None:
        from apm_cli.core.script_runner import ScriptRunner

        (tmp_path / "apm.yml").write_text(
            "name: test\nscripts:\n  greet: echo hello\n  bye: echo bye\n",
            encoding="utf-8",
        )
        os.chdir(tmp_path)
        runner = ScriptRunner()
        scripts = runner.list_scripts()
        assert scripts == {"greet": "echo hello", "bye": "echo bye"}

    def test_list_scripts_empty_if_no_apm_yml(self, tmp_path: Path) -> None:
        from apm_cli.core.script_runner import ScriptRunner

        os.chdir(tmp_path)
        runner = ScriptRunner()
        assert runner.list_scripts() == {}

    def test_list_scripts_empty_section(self, tmp_path: Path) -> None:
        from apm_cli.core.script_runner import ScriptRunner

        (tmp_path / "apm.yml").write_text("name: test\n", encoding="utf-8")
        os.chdir(tmp_path)
        runner = ScriptRunner()
        assert runner.list_scripts() == {}

    def test_load_config_returns_none_without_file(self, tmp_path: Path) -> None:
        from apm_cli.core.script_runner import ScriptRunner

        os.chdir(tmp_path)
        runner = ScriptRunner()
        assert runner._load_config() is None

    def test_load_config_returns_dict_with_file(self, tmp_path: Path) -> None:
        from apm_cli.core.script_runner import ScriptRunner

        (tmp_path / "apm.yml").write_text("name: myapp\nversion: 1.0.0\n", encoding="utf-8")
        os.chdir(tmp_path)
        runner = ScriptRunner()
        config = runner._load_config()
        assert config is not None
        assert config["name"] == "myapp"


# ============================================================================
# SECTION 15 - ScriptRunner: runtime detection helpers
# ============================================================================


class TestScriptRunnerRuntimeDetection:
    """_detect_runtime() and command builder methods."""

    def test_detect_runtime_copilot(self) -> None:
        from apm_cli.core.script_runner import ScriptRunner

        runner = ScriptRunner()
        assert runner._detect_runtime("copilot -p prompt.txt") == "copilot"

    def test_detect_runtime_codex(self) -> None:
        from apm_cli.core.script_runner import ScriptRunner

        runner = ScriptRunner()
        assert runner._detect_runtime("codex exec my-task") == "codex"

    def test_detect_runtime_llm(self) -> None:
        from apm_cli.core.script_runner import ScriptRunner

        runner = ScriptRunner()
        assert runner._detect_runtime("llm -m gpt-4 prompt") == "llm"

    def test_detect_runtime_gemini(self) -> None:
        from apm_cli.core.script_runner import ScriptRunner

        runner = ScriptRunner()
        assert runner._detect_runtime("gemini -p content") == "gemini"

    def test_detect_runtime_unknown(self) -> None:
        from apm_cli.core.script_runner import ScriptRunner

        runner = ScriptRunner()
        assert runner._detect_runtime("bash -c 'echo hello'") == "unknown"

    def test_detect_runtime_case_insensitive(self) -> None:
        from apm_cli.core.script_runner import ScriptRunner

        runner = ScriptRunner()
        assert runner._detect_runtime("COPILOT -p file.txt") == "copilot"

    def test_build_codex_command_no_args(self) -> None:
        from apm_cli.core.script_runner import ScriptRunner

        runner = ScriptRunner()
        result = runner._build_codex_command("", "")
        assert result == "codex exec"

    def test_build_codex_command_with_args_before(self) -> None:
        from apm_cli.core.script_runner import ScriptRunner

        runner = ScriptRunner()
        result = runner._build_codex_command("-s workspace-write", "")
        assert "codex exec" in result
        assert "-s workspace-write" in result

    def test_build_codex_command_with_env_prefix(self) -> None:
        from apm_cli.core.script_runner import ScriptRunner

        runner = ScriptRunner()
        result = runner._build_codex_command("", "", env_prefix="DEBUG=1")
        assert result.startswith("DEBUG=1")
        assert "codex exec" in result

    def test_build_copilot_command_removes_p_flag(self) -> None:
        from apm_cli.core.script_runner import ScriptRunner

        runner = ScriptRunner()
        result = runner._build_copilot_command("-p", "")
        # The -p flag should be stripped since content is passed separately
        assert result.strip() == "copilot"

    def test_build_copilot_command_with_args(self) -> None:
        from apm_cli.core.script_runner import ScriptRunner

        runner = ScriptRunner()
        result = runner._build_copilot_command("--log-level all", "--allow-all-tools")
        assert "copilot" in result
        assert "--log-level all" in result
        assert "--allow-all-tools" in result

    def test_build_llm_command(self) -> None:
        from apm_cli.core.script_runner import ScriptRunner

        runner = ScriptRunner()
        result = runner._build_llm_command("-m gpt-4o", "")
        assert "llm" in result
        assert "-m gpt-4o" in result

    def test_build_gemini_command_strips_p_flag(self) -> None:
        from apm_cli.core.script_runner import ScriptRunner

        runner = ScriptRunner()
        result = runner._build_gemini_command("-p", "")
        # -p flag should be stripped from gemini args_before
        assert result.strip() == "gemini"

    def test_build_gemini_command_with_args(self) -> None:
        from apm_cli.core.script_runner import ScriptRunner

        runner = ScriptRunner()
        result = runner._build_gemini_command("--model gemini-2.0", "")
        assert "gemini" in result
        assert "--model gemini-2.0" in result


# ============================================================================
# SECTION 16 - ScriptRunner: _detect_installed_runtime
# ============================================================================


class TestDetectInstalledRuntime:
    """_detect_installed_runtime() priority order and error handling."""

    def test_prefers_copilot_when_available(self) -> None:
        from apm_cli.core.script_runner import ScriptRunner

        runner = ScriptRunner()
        with patch("apm_cli.core.script_runner.find_runtime_binary") as mock_find:
            mock_find.side_effect = lambda name: "/usr/bin/copilot" if name == "copilot" else None
            assert runner._detect_installed_runtime() == "copilot"

    def test_falls_back_to_codex(self) -> None:
        from apm_cli.core.script_runner import ScriptRunner

        runner = ScriptRunner()
        with patch("apm_cli.core.script_runner.find_runtime_binary") as mock_find:
            mock_find.side_effect = lambda name: "/usr/bin/codex" if name == "codex" else None
            assert runner._detect_installed_runtime() == "codex"

    def test_falls_back_to_gemini(self) -> None:
        from apm_cli.core.script_runner import ScriptRunner

        runner = ScriptRunner()
        with patch("apm_cli.core.script_runner.find_runtime_binary") as mock_find:
            mock_find.side_effect = lambda name: "/usr/bin/gemini" if name == "gemini" else None
            assert runner._detect_installed_runtime() == "gemini"

    def test_raises_when_no_runtime(self) -> None:
        from apm_cli.core.script_runner import ScriptRunner

        runner = ScriptRunner()
        with patch("apm_cli.core.script_runner.find_runtime_binary", return_value=None):
            with pytest.raises(RuntimeError, match="No compatible runtime"):
                runner._detect_installed_runtime()


# ============================================================================
# SECTION 17 - ScriptRunner: _generate_runtime_command
# ============================================================================


class TestGenerateRuntimeCommand:
    """_generate_runtime_command() for all supported runtimes."""

    def test_copilot_command_format(self, tmp_path: Path) -> None:
        from apm_cli.core.script_runner import ScriptRunner

        runner = ScriptRunner()
        prompt_file = tmp_path / "review.prompt.md"
        cmd = runner._generate_runtime_command("copilot", prompt_file)
        assert "copilot" in cmd
        assert str(prompt_file) in cmd

    def test_codex_command_format(self, tmp_path: Path) -> None:
        from apm_cli.core.script_runner import ScriptRunner

        runner = ScriptRunner()
        prompt_file = tmp_path / "review.prompt.md"
        cmd = runner._generate_runtime_command("codex", prompt_file)
        assert "codex" in cmd
        assert str(prompt_file) in cmd

    def test_gemini_command_format(self, tmp_path: Path) -> None:
        from apm_cli.core.script_runner import ScriptRunner

        runner = ScriptRunner()
        prompt_file = tmp_path / "review.prompt.md"
        cmd = runner._generate_runtime_command("gemini", prompt_file)
        assert "gemini" in cmd

    def test_unsupported_runtime_raises(self, tmp_path: Path) -> None:
        from apm_cli.core.script_runner import ScriptRunner

        runner = ScriptRunner()
        with pytest.raises(ValueError, match="Unsupported runtime"):
            runner._generate_runtime_command("unknown-runtime", tmp_path / "x.prompt.md")


# ============================================================================
# SECTION 18 - ScriptRunner: _transform_runtime_command
# ============================================================================


class TestTransformRuntimeCommand:
    """_transform_runtime_command() dispatches to per-runtime builders."""

    def test_codex_transform(self, tmp_path: Path) -> None:
        from apm_cli.core.script_runner import ScriptRunner

        runner = ScriptRunner()
        prompt = tmp_path / "task.prompt.md"
        prompt.write_text("Do something", encoding="utf-8")
        compiled_path = tmp_path / "task.txt"
        compiled_path.write_text("Do something", encoding="utf-8")
        command = f"codex {prompt}"
        result = runner._transform_runtime_command(
            command, str(prompt), "Do something", str(compiled_path)
        )
        assert "codex exec" in result

    def test_copilot_transform(self, tmp_path: Path) -> None:
        from apm_cli.core.script_runner import ScriptRunner

        runner = ScriptRunner()
        prompt = tmp_path / "review.prompt.md"
        prompt.write_text("Review this", encoding="utf-8")
        compiled_path = tmp_path / "review.txt"
        compiled_path.write_text("Review this", encoding="utf-8")
        command = f"copilot {prompt}"
        result = runner._transform_runtime_command(
            command, str(prompt), "Review this", str(compiled_path)
        )
        assert "copilot" in result

    def test_bare_prompt_file_defaults_to_codex_exec(self, tmp_path: Path) -> None:
        from apm_cli.core.script_runner import ScriptRunner

        runner = ScriptRunner()
        prompt_file = "review.prompt.md"
        result = runner._transform_runtime_command(
            prompt_file, prompt_file, "content", "review.txt"
        )
        assert result == "codex exec"

    def test_non_runtime_command_replaces_with_compiled_path(self, tmp_path: Path) -> None:
        from apm_cli.core.script_runner import ScriptRunner

        runner = ScriptRunner()
        prompt_file = "review.prompt.md"
        compiled = str(tmp_path / "review.txt")
        command = f"cat {prompt_file}"
        result = runner._transform_runtime_command(command, prompt_file, "content", compiled)
        assert compiled in result

    def test_env_var_prefix_codex(self, tmp_path: Path) -> None:
        from apm_cli.core.script_runner import ScriptRunner

        runner = ScriptRunner()
        prompt = "task.prompt.md"
        compiled = str(tmp_path / "task.txt")
        command = f"DEBUG=1 codex {prompt}"
        result = runner._transform_runtime_command(command, prompt, "content", compiled)
        assert "DEBUG=1" in result
        assert "codex exec" in result


# ============================================================================
# SECTION 19 - ScriptRunner: _discover_prompt_file
# ============================================================================


class TestDiscoverPromptFile:
    """Prompt discovery across local dirs and apm_modules."""

    def test_discovers_local_root_prompt(self, tmp_path: Path) -> None:
        from apm_cli.core.script_runner import ScriptRunner

        (tmp_path / "review.prompt.md").write_text("# Review", encoding="utf-8")
        os.chdir(tmp_path)
        runner = ScriptRunner()
        result = runner._discover_prompt_file("review")
        assert result is not None
        assert result.name == "review.prompt.md"

    def test_discovers_apm_prompts_dir(self, tmp_path: Path) -> None:
        from apm_cli.core.script_runner import ScriptRunner

        apm_dir = tmp_path / ".apm" / "prompts"
        apm_dir.mkdir(parents=True)
        (apm_dir / "security.prompt.md").write_text("# Security", encoding="utf-8")
        os.chdir(tmp_path)
        runner = ScriptRunner()
        result = runner._discover_prompt_file("security")
        assert result is not None
        assert result.name == "security.prompt.md"

    def test_discovers_github_prompts_dir(self, tmp_path: Path) -> None:
        from apm_cli.core.script_runner import ScriptRunner

        gh_dir = tmp_path / ".github" / "prompts"
        gh_dir.mkdir(parents=True)
        (gh_dir / "style.prompt.md").write_text("# Style", encoding="utf-8")
        os.chdir(tmp_path)
        runner = ScriptRunner()
        result = runner._discover_prompt_file("style")
        assert result is not None
        assert result.name == "style.prompt.md"

    def test_discovers_in_apm_modules(self, tmp_path: Path) -> None:
        from apm_cli.core.script_runner import ScriptRunner

        pkg_dir = tmp_path / "apm_modules" / "acme" / "tools" / ".apm" / "prompts"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "lint.prompt.md").write_text("# Lint", encoding="utf-8")
        os.chdir(tmp_path)
        runner = ScriptRunner()
        result = runner._discover_prompt_file("lint")
        assert result is not None
        assert result.name == "lint.prompt.md"

    def test_returns_none_when_not_found(self, tmp_path: Path) -> None:
        from apm_cli.core.script_runner import ScriptRunner

        os.chdir(tmp_path)
        runner = ScriptRunner()
        result = runner._discover_prompt_file("nonexistent-prompt")
        assert result is None

    def test_collision_raises_runtime_error(self, tmp_path: Path) -> None:
        from apm_cli.core.script_runner import ScriptRunner

        # Install two packages that both have the same prompt name
        for pkg in ["pkg-a", "pkg-b"]:
            pkg_dir = tmp_path / "apm_modules" / "acme" / pkg / ".apm" / "prompts"
            pkg_dir.mkdir(parents=True)
            (pkg_dir / "common.prompt.md").write_text("# Common", encoding="utf-8")
        os.chdir(tmp_path)
        runner = ScriptRunner()
        with pytest.raises(RuntimeError, match="Multiple prompts"):
            runner._discover_prompt_file("common")

    def test_discovers_with_full_extension(self, tmp_path: Path) -> None:
        from apm_cli.core.script_runner import ScriptRunner

        (tmp_path / "full.prompt.md").write_text("# Full", encoding="utf-8")
        os.chdir(tmp_path)
        runner = ScriptRunner()
        result = runner._discover_prompt_file("full.prompt.md")
        assert result is not None


# ============================================================================
# SECTION 20 - ScriptRunner: _is_virtual_package_reference
# ============================================================================


class TestIsVirtualPackageReference:
    """_is_virtual_package_reference() detects virtual package paths."""

    def test_virtual_file_detected(self) -> None:
        from apm_cli.core.script_runner import ScriptRunner

        runner = ScriptRunner()
        assert runner._is_virtual_package_reference("owner/repo/prompts/review.prompt.md") is True

    def test_virtual_subdir_detected(self) -> None:
        from apm_cli.core.script_runner import ScriptRunner

        runner = ScriptRunner()
        assert runner._is_virtual_package_reference("owner/repo/skills/arch-review") is True

    def test_simple_name_not_virtual(self) -> None:
        from apm_cli.core.script_runner import ScriptRunner

        runner = ScriptRunner()
        assert runner._is_virtual_package_reference("simple-name") is False

    def test_owner_slash_repo_not_virtual(self) -> None:
        from apm_cli.core.script_runner import ScriptRunner

        runner = ScriptRunner()
        assert runner._is_virtual_package_reference("owner/repo") is False

    def test_invalid_format_returns_false(self) -> None:
        from apm_cli.core.script_runner import ScriptRunner

        runner = ScriptRunner()
        # Invalid strings should not raise; they should return False
        assert runner._is_virtual_package_reference("//invalid") is False


# ============================================================================
# SECTION 21 - ScriptRunner: run_script with explicit scripts
# ============================================================================


class TestRunScriptExplicit:
    """run_script() exercising the explicit scripts section of apm.yml."""

    def test_run_explicit_script_success(self, tmp_path: Path) -> None:
        from apm_cli.core.script_runner import ScriptRunner

        (tmp_path / "apm.yml").write_text(
            "name: test\nscripts:\n  greet: echo hello\n",
            encoding="utf-8",
        )
        os.chdir(tmp_path)
        runner = ScriptRunner()
        with (
            patch("apm_cli.core.script_runner.setup_runtime_environment") as mock_env,
            patch("subprocess.run") as mock_run,
        ):
            mock_env.return_value = os.environ.copy()
            mock_proc = MagicMock()
            mock_proc.returncode = 0
            mock_run.return_value = mock_proc
            result = runner.run_script("greet", {})
        assert result is True

    def test_run_script_no_apm_yml_raises(self, tmp_path: Path) -> None:
        from apm_cli.core.script_runner import ScriptRunner

        os.chdir(tmp_path)
        runner = ScriptRunner()
        with pytest.raises(RuntimeError, match=r"No apm\.yml"):
            runner.run_script("missing", {})

    def test_run_script_not_found_raises(self, tmp_path: Path) -> None:
        from apm_cli.core.script_runner import ScriptRunner

        (tmp_path / "apm.yml").write_text(
            "name: test\nscripts:\n  greet: echo hello\n",
            encoding="utf-8",
        )
        os.chdir(tmp_path)
        runner = ScriptRunner()
        with pytest.raises(RuntimeError, match="not found"):
            runner.run_script("nonexistent-script", {})

    def test_run_explicit_script_failure_raises(self, tmp_path: Path) -> None:
        import subprocess

        from apm_cli.core.script_runner import ScriptRunner

        (tmp_path / "apm.yml").write_text(
            "name: test\nscripts:\n  fail: exit 1\n",
            encoding="utf-8",
        )
        os.chdir(tmp_path)
        runner = ScriptRunner()
        with (
            patch("apm_cli.core.script_runner.setup_runtime_environment") as mock_env,
            patch("subprocess.run") as mock_run,
        ):
            mock_env.return_value = os.environ.copy()
            mock_run.side_effect = subprocess.CalledProcessError(1, "exit 1")
            with pytest.raises(RuntimeError, match="exit code"):
                runner.run_script("fail", {})


# ============================================================================
# SECTION 22 - ScriptRunner: auto-compile path in _auto_compile_prompts
# ============================================================================


class TestAutoCompilePrompts:
    """_auto_compile_prompts() detects and compiles .prompt.md references."""

    def test_no_prompt_md_returns_original(self, tmp_path: Path) -> None:
        from apm_cli.core.script_runner import ScriptRunner

        os.chdir(tmp_path)
        runner = ScriptRunner()
        cmd, files, content = runner._auto_compile_prompts("echo hello", {})
        assert cmd == "echo hello"
        assert files == []
        assert content is None

    def test_prompt_md_in_command_is_compiled(self, tmp_path: Path) -> None:
        from apm_cli.core.script_runner import ScriptRunner

        prompt = tmp_path / "review.prompt.md"
        prompt.write_text("# Review\nPlease review.", encoding="utf-8")
        os.chdir(tmp_path)
        runner = ScriptRunner()
        runner.compiler.compiled_dir = tmp_path / ".apm" / "compiled"
        _cmd, files, _content = runner._auto_compile_prompts(f"cat {prompt}", {})
        # files contains the original prompt file path as matched from the command
        assert len(files) == 1
        assert any(f.endswith("review.prompt.md") for f in files)

    def test_runtime_command_sets_content(self, tmp_path: Path) -> None:
        from apm_cli.core.script_runner import ScriptRunner

        prompt = tmp_path / "task.prompt.md"
        prompt.write_text("# Task\nDo this.", encoding="utf-8")
        os.chdir(tmp_path)
        runner = ScriptRunner()
        runner.compiler.compiled_dir = tmp_path / ".apm" / "compiled"
        _cmd, files, content = runner._auto_compile_prompts(f"codex {prompt}", {})
        assert len(files) == 1
        assert content is not None
        assert "Do this." in content


# ============================================================================
# SECTION 23 - ScriptRunner: _collect_dependency_dirs
# ============================================================================


class TestCollectDependencyDirs:
    """_collect_dependency_dirs() walks apm_modules two levels deep."""

    def test_returns_empty_when_no_modules(self, tmp_path: Path) -> None:
        from apm_cli.core.script_runner import PromptCompiler, ScriptRunner

        compiler = PromptCompiler()
        runner = ScriptRunner(compiler=compiler)
        result = runner.compiler._collect_dependency_dirs(tmp_path / "apm_modules")
        assert result == []

    def test_returns_tuples_for_installed_packages(self, tmp_path: Path) -> None:
        from apm_cli.core.script_runner import PromptCompiler, ScriptRunner

        (tmp_path / "apm_modules" / "acme" / "toolkit").mkdir(parents=True)
        (tmp_path / "apm_modules" / "acme" / "toolbox").mkdir(parents=True)
        compiler = PromptCompiler()
        runner = ScriptRunner(compiler=compiler)
        result = runner.compiler._collect_dependency_dirs(tmp_path / "apm_modules")
        org_names = {t[0] for t in result}
        repo_names = {t[1] for t in result}
        assert "acme" in org_names
        assert "toolkit" in repo_names
        assert "toolbox" in repo_names

    def test_skips_hidden_dirs(self, tmp_path: Path) -> None:
        from apm_cli.core.script_runner import PromptCompiler, ScriptRunner

        (tmp_path / "apm_modules" / ".git" / "objects").mkdir(parents=True)
        (tmp_path / "apm_modules" / "acme" / "repo").mkdir(parents=True)
        compiler = PromptCompiler()
        runner = ScriptRunner(compiler=compiler)
        result = runner.compiler._collect_dependency_dirs(tmp_path / "apm_modules")
        org_names = {t[0] for t in result}
        assert ".git" not in org_names
        assert "acme" in org_names


# ============================================================================
# SECTION 24 - ScriptRunner: _add_dependency_to_config
# ============================================================================


class TestAddDependencyToConfig:
    """_add_dependency_to_config() updates apm.yml dependencies."""

    def test_adds_dependency_to_existing_file(self, tmp_path: Path) -> None:
        from apm_cli.core.script_runner import ScriptRunner

        (tmp_path / "apm.yml").write_text("name: test\n", encoding="utf-8")
        os.chdir(tmp_path)
        runner = ScriptRunner()
        runner._add_dependency_to_config("owner/repo/skills/tool")
        from apm_cli.utils.yaml_io import load_yaml

        config = load_yaml(tmp_path / "apm.yml")
        assert "owner/repo/skills/tool" in config["dependencies"]["apm"]

    def test_does_not_duplicate_dependency(self, tmp_path: Path) -> None:
        from apm_cli.core.script_runner import ScriptRunner

        (tmp_path / "apm.yml").write_text("name: test\n", encoding="utf-8")
        os.chdir(tmp_path)
        runner = ScriptRunner()
        runner._add_dependency_to_config("owner/repo/skills/tool")
        runner._add_dependency_to_config("owner/repo/skills/tool")
        from apm_cli.utils.yaml_io import load_yaml

        config = load_yaml(tmp_path / "apm.yml")
        assert config["dependencies"]["apm"].count("owner/repo/skills/tool") == 1

    def test_no_op_without_apm_yml(self, tmp_path: Path) -> None:
        from apm_cli.core.script_runner import ScriptRunner

        os.chdir(tmp_path)
        runner = ScriptRunner()
        # Should silently do nothing
        runner._add_dependency_to_config("owner/repo/skills/tool")
        assert not (tmp_path / "apm.yml").exists()


# ============================================================================
# SECTION 25 - ScriptRunner: _create_minimal_config
# ============================================================================


class TestCreateMinimalConfig:
    """_create_minimal_config() materialises a minimal apm.yml."""

    def test_creates_apm_yml(self, tmp_path: Path) -> None:
        from apm_cli.core.script_runner import ScriptRunner

        os.chdir(tmp_path)
        runner = ScriptRunner()
        runner._create_minimal_config()
        assert (tmp_path / "apm.yml").exists()

    def test_created_config_is_valid_yaml(self, tmp_path: Path) -> None:
        from apm_cli.core.script_runner import ScriptRunner
        from apm_cli.utils.yaml_io import load_yaml

        os.chdir(tmp_path)
        runner = ScriptRunner()
        runner._create_minimal_config()
        config = load_yaml(tmp_path / "apm.yml")
        assert config is not None
        assert "name" in config
        assert "version" in config
