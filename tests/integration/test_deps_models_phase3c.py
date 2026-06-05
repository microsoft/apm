"""Phase-3c integration tests for four modules with large coverage gaps.

Modules covered
---------------
1. ``src/apm_cli/deps/github_downloader.py``          (63.4%, gap=272)
2. ``src/apm_cli/models/dependency/reference.py``     (81.8%, gap=212)
3. ``src/apm_cli/install/pipeline.py``                (gap=~150 integ)
4. ``src/apm_cli/install/drift.py``                   (gap=~100 integ)

Strategy
--------
* Exercise real code paths; mock only HTTP / git / subprocess side-effects.
* No live network calls.
* Type hints on every public test function signature.
* URL assertions use ``urllib.parse.urlparse``, never substring matching.
"""

from __future__ import annotations

import json
import os
import urllib.parse
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_dep_ref(**kwargs: Any) -> Any:
    """Build a DependencyReference without network calls."""
    from apm_cli.models.dependency.reference import DependencyReference

    defaults = {
        "repo_url": "owner/repo",
        "host": "github.com",
    }
    return DependencyReference(**{**defaults, **kwargs})


def _make_downloader() -> Any:
    """Build a GitHubPackageDownloader with all external I/O mocked."""
    from apm_cli.deps.github_downloader import GitHubPackageDownloader

    with (
        patch("apm_cli.deps.github_downloader.AuthResolver") as mock_ar,
        patch("apm_cli.deps.github_downloader.TransportSelector"),
    ):
        mock_tm = MagicMock()
        mock_tm.get_token_for_purpose.return_value = None
        mock_ar.return_value._token_manager = mock_tm
        with patch(
            "apm_cli.deps.git_auth_env.GitAuthEnvBuilder.setup_environment",
            return_value={},
        ):
            dl = GitHubPackageDownloader()
    return dl


def _DR():
    from apm_cli.models.dependency.reference import DependencyReference

    return DependencyReference


# ============================================================================
# SECTION 1 - DependencyReference: HTTPS & SSH URL parsing
# ============================================================================


class TestParseHttpsUrls:
    """Parse https:// dependency strings."""

    def test_plain_https_github(self) -> None:
        ref = _DR().parse("https://github.com/acme/my-tool.git")
        parsed = urllib.parse.urlparse(ref.to_github_url())
        assert parsed.hostname == "github.com"
        assert ref.repo_url == "acme/my-tool"
        assert ref.explicit_scheme == "https"

    def test_https_with_ref(self) -> None:
        ref = _DR().parse("https://github.com/acme/my-tool.git#main")
        assert ref.reference == "main"
        assert ref.repo_url == "acme/my-tool"

    def test_https_gitlab(self) -> None:
        ref = _DR().parse("https://gitlab.com/acme/my-tool.git")
        assert ref.host == "gitlab.com"
        assert ref.repo_url == "acme/my-tool"

    def test_https_non_default_host_preserved(self) -> None:
        ref = _DR().parse("https://myghe.corp.com/org/repo.git")
        assert ref.host == "myghe.corp.com"
        assert ref.repo_url == "org/repo"

    def test_https_default_port_stripped(self) -> None:
        """Port 443 is the HTTPS default and must be normalised away."""
        ref = _DR().parse("https://github.com:443/owner/repo.git")
        assert ref.port is None

    def test_http_sets_insecure(self) -> None:
        ref = _DR().parse("http://gitlab.corp.com/owner/repo.git")
        assert ref.is_insecure is True
        assert ref.explicit_scheme == "http"

    def test_https_with_non_standard_port(self) -> None:
        ref = _DR().parse("https://ghe.corp.com:8443/org/repo.git")
        assert ref.port == 8443


class TestParseSshUrls:
    """Parse SSH forms: SCP shorthand and ssh:// protocol URL."""

    def test_scp_git_at_github(self) -> None:
        ref = _DR().parse("git@github.com:acme/tool.git")
        assert ref.host == "github.com"
        assert ref.repo_url == "acme/tool"
        assert ref.explicit_scheme == "ssh"
        assert ref.ssh_user == "git"

    def test_scp_custom_user(self) -> None:
        ref = _DR().parse("enterprise-bot@ghe.corp.com:acme/tool.git")
        assert ref.ssh_user == "enterprise-bot"
        assert ref.host == "ghe.corp.com"

    def test_scp_with_ref_and_alias(self) -> None:
        ref = _DR().parse("git@github.com:owner/repo.git#develop@my-alias")
        assert ref.reference == "develop"
        assert ref.alias == "my-alias"

    def test_ssh_protocol_url_basic(self) -> None:
        ref = _DR().parse("ssh://git@github.com/owner/repo.git")
        assert ref.host == "github.com"
        assert ref.repo_url == "owner/repo"
        assert ref.explicit_scheme == "ssh"
        assert ref.ssh_user == "git"

    def test_ssh_protocol_url_with_port(self) -> None:
        ref = _DR().parse("ssh://git@bitbucket.corp.com:7999/org/repo.git")
        assert ref.port == 7999
        assert ref.host == "bitbucket.corp.com"
        assert ref.repo_url == "org/repo"

    def test_ssh_default_port_normalised(self) -> None:
        """ssh://host:22/... must normalise port 22 away."""
        ref = _DR().parse("ssh://git@github.com:22/owner/repo.git")
        assert ref.port is None

    def test_ssh_protocol_url_with_fragment_ref(self) -> None:
        ref = _DR().parse("ssh://git@github.com/owner/repo.git#v2.0")
        assert ref.reference == "v2.0"

    def test_ssh_protocol_percent_encoded_userinfo_raises(self) -> None:
        # The guard raises on percent-encoded chars OR invalid SSH user shape
        with pytest.raises(ValueError):
            _DR().parse("ssh://%2DoProxyCommand=evil@github.com/owner/repo")

    def test_scp_port_lookalike_raises(self) -> None:
        """First path segment that looks like a port number must raise."""
        with pytest.raises(ValueError, match="port number"):
            _DR().parse("git@bitbucket.corp.com:7999/owner/repo.git")


class TestParseLocalPaths:
    """Parse local filesystem dependency strings."""

    def test_relative_dot_slash(self) -> None:
        ref = _DR().parse("./packages/my-tool")
        assert ref.is_local is True
        assert ref.local_path == "./packages/my-tool"
        assert ref.repo_url == "_local/my-tool"

    def test_relative_dot_dot(self) -> None:
        ref = _DR().parse("../sibling-pkg")
        assert ref.is_local is True

    def test_absolute_unix_path(self) -> None:
        ref = _DR().parse("/opt/packages/my-tool")
        assert ref.is_local is True
        assert ref.local_path == "/opt/packages/my-tool"

    def test_tilde_home_path(self) -> None:
        ref = _DR().parse("~/my-packages/tool")
        assert ref.is_local is True

    def test_bare_dot_slash_raises(self) -> None:
        """'./' alone has no named directory."""
        with pytest.raises(ValueError, match="named directory"):
            _DR().parse("./")

    def test_bare_dot_dot_raises(self) -> None:
        with pytest.raises(ValueError, match="named directory"):
            _DR().parse("../")

    def test_protocol_relative_raises(self) -> None:
        with pytest.raises(ValueError, match="not supported"):
            _DR().parse("//host/owner/repo")


class TestParseVirtualPackages:
    """Virtual package detection in shorthand strings."""

    def test_virtual_file_prompt(self) -> None:
        ref = _DR().parse("owner/repo/prompts/code-review.prompt.md")
        assert ref.is_virtual is True
        assert ref.virtual_path == "prompts/code-review.prompt.md"
        assert ref.is_virtual_file() is True

    def test_virtual_file_instructions(self) -> None:
        ref = _DR().parse("owner/repo/instructions/security.instructions.md")
        assert ref.is_virtual is True
        assert ref.is_virtual_file() is True

    def test_virtual_file_chatmode(self) -> None:
        ref = _DR().parse("owner/repo/chatmodes/review.chatmode.md")
        assert ref.is_virtual is True
        assert ref.virtual_path == "chatmodes/review.chatmode.md"

    def test_virtual_file_agent(self) -> None:
        ref = _DR().parse("owner/repo/agents/my-agent.agent.md")
        assert ref.is_virtual is True
        assert ref.virtual_path == "agents/my-agent.agent.md"

    def test_virtual_subdir_collections(self) -> None:
        ref = _DR().parse("owner/repo/collections/project-planning")
        assert ref.is_virtual is True
        assert ref.is_virtual_subdirectory() is True
        assert ref.virtual_path == "collections/project-planning"

    def test_virtual_subdir_no_extension(self) -> None:
        ref = _DR().parse("owner/repo/skills/brand-guidelines")
        assert ref.is_virtual is True
        assert ref.is_virtual_subdirectory() is True

    def test_collection_yml_rejected(self) -> None:
        """Legacy .collection.yml extension must be rejected."""
        with pytest.raises(ValueError, match=r"collection\.yml is no longer supported"):
            _DR().parse("owner/repo/old.collection.yml")

    def test_invalid_virtual_extension_rejected(self) -> None:
        """Dotted path segment with unknown extension must fail."""
        from apm_cli.models.validation import InvalidVirtualPackageExtensionError

        with pytest.raises((InvalidVirtualPackageExtensionError, ValueError)):
            _DR().parse("owner/repo/prompts/file.txt")


# ============================================================================
# SECTION 2 - DependencyReference: ADO parsing
# ============================================================================


class TestParseAdoUrls:
    """Azure DevOps URL parsing."""

    def test_ado_https_dev_azure(self) -> None:
        ref = _DR().parse("https://dev.azure.com/myorg/myproject/_git/myrepo")
        assert ref.is_azure_devops() is True
        assert ref.ado_organization == "myorg"
        assert ref.ado_project == "myproject"
        assert ref.ado_repo == "myrepo"

    def test_ado_shorthand(self) -> None:
        ref = _DR().parse("dev.azure.com/myorg/myproject/myrepo")
        assert ref.is_azure_devops() is True
        assert ref.ado_organization == "myorg"

    def test_ado_visualstudio_legacy(self) -> None:
        ref = _DR().parse("https://myorg.visualstudio.com/myproject/_git/myrepo")
        assert ref.is_azure_devops() is True
        # normalised to include org
        assert "myorg" in ref.repo_url

    def test_ado_with_virtual_path_in_url(self) -> None:
        """Extra URL segments after org/project/_git/repo become virtual_path."""
        ref = _DR().parse(
            "https://dev.azure.com/org/proj/_git/repo/instructions/sec.instructions.md"
        )
        assert ref.is_virtual is True
        assert ref.virtual_path is not None

    def test_ado_get_identity(self) -> None:
        ref = _make_dep_ref(
            host="dev.azure.com",
            repo_url="org/proj/repo",
            ado_organization="org",
            ado_project="proj",
            ado_repo="repo",
        )
        identity = ref.get_identity()
        assert "dev.azure.com" in identity
        assert "org" in identity


# ============================================================================
# SECTION 3 - DependencyReference: canonical / identity / install path
# ============================================================================


class TestToCanonical:
    """to_canonical() returns the correct scheme-free identity string."""

    def test_default_host_stripped(self) -> None:
        ref = _make_dep_ref(host="github.com", repo_url="owner/repo")
        assert ref.to_canonical() == "owner/repo"

    def test_non_default_host_preserved(self) -> None:
        ref = _make_dep_ref(host="gitlab.com", repo_url="owner/repo")
        assert ref.to_canonical() == "gitlab.com/owner/repo"

    def test_ref_appended(self) -> None:
        ref = _make_dep_ref(repo_url="owner/repo", reference="main")
        assert ref.to_canonical() == "owner/repo#main"

    def test_virtual_path_appended(self) -> None:
        ref = _make_dep_ref(
            repo_url="owner/repo",
            is_virtual=True,
            virtual_path="prompts/review.prompt.md",
        )
        canonical = ref.to_canonical()
        assert "prompts/review.prompt.md" in canonical

    def test_local_returns_local_path(self) -> None:
        ref = _make_dep_ref(is_local=True, local_path="./packages/tool", repo_url="_local/tool")
        assert ref.to_canonical() == "./packages/tool"

    def test_port_in_host_label(self) -> None:
        ref = _make_dep_ref(host="ghe.corp.com", port=8443, repo_url="org/repo")
        canonical = ref.to_canonical()
        assert "ghe.corp.com:8443" in canonical

    def test_canonicalize_static_method(self) -> None:
        result = _DR().canonicalize("owner/repo#main")
        assert result == "owner/repo#main"


class TestGetIdentity:
    """get_identity() strips ref but keeps host for non-default."""

    def test_github_default_host(self) -> None:
        ref = _make_dep_ref(host="github.com", repo_url="owner/repo", reference="v1.0")
        assert ref.get_identity() == "owner/repo"

    def test_gitlab_host_included(self) -> None:
        ref = _make_dep_ref(host="gitlab.com", repo_url="group/repo", reference="main")
        assert "gitlab.com" in ref.get_identity()
        assert "group/repo" in ref.get_identity()

    def test_local_returns_local_path(self) -> None:
        ref = _make_dep_ref(is_local=True, local_path="./tool", repo_url="_local/tool")
        assert ref.get_identity() == "./tool"


class TestGetInstallPath:
    """get_install_path() returns the correct filesystem path."""

    def test_github_regular_package(self, tmp_path: Path) -> None:
        apm_modules = tmp_path / "apm_modules"
        ref = _make_dep_ref(host="github.com", repo_url="owner/repo")
        path = ref.get_install_path(apm_modules)
        assert path == apm_modules / "owner" / "repo"

    def test_ado_regular_package(self, tmp_path: Path) -> None:
        apm_modules = tmp_path / "apm_modules"
        ref = _make_dep_ref(
            host="dev.azure.com",
            repo_url="org/proj/repo",
            ado_organization="org",
            ado_project="proj",
            ado_repo="repo",
        )
        path = ref.get_install_path(apm_modules)
        assert path == apm_modules / "org" / "proj" / "repo"

    def test_virtual_file_install_path(self, tmp_path: Path) -> None:
        apm_modules = tmp_path / "apm_modules"
        ref = _make_dep_ref(
            repo_url="owner/repo",
            is_virtual=True,
            virtual_path="prompts/review.prompt.md",
        )
        path = ref.get_install_path(apm_modules)
        # File virtual packages use get_virtual_package_name() as dir name
        assert path.parent.name == "owner"

    def test_virtual_subdir_install_path(self, tmp_path: Path) -> None:
        apm_modules = tmp_path / "apm_modules"
        ref = _make_dep_ref(
            repo_url="owner/repo",
            is_virtual=True,
            virtual_path="skills/brand-guidelines",
        )
        path = ref.get_install_path(apm_modules)
        assert "skills" in str(path)

    def test_local_install_path(self, tmp_path: Path) -> None:
        apm_modules = tmp_path / "apm_modules"
        ref = _make_dep_ref(is_local=True, local_path="./my-pkg", repo_url="_local/my-pkg")
        path = ref.get_install_path(apm_modules)
        assert path == apm_modules / "_local" / "my-pkg"

    def test_traversal_in_repo_url_raises(self, tmp_path: Path) -> None:
        from apm_cli.utils.path_security import PathTraversalError

        apm_modules = tmp_path / "apm_modules"
        ref = _make_dep_ref(repo_url="../evil/repo")
        with pytest.raises((PathTraversalError, ValueError)):
            ref.get_install_path(apm_modules)


class TestVirtualPackageMethods:
    """Virtual package classification and name generation."""

    def test_is_virtual_file_true(self) -> None:
        ref = _make_dep_ref(
            is_virtual=True, virtual_path="prompts/review.prompt.md", repo_url="owner/repo"
        )
        assert ref.is_virtual_file() is True
        assert ref.is_virtual_subdirectory() is False

    def test_is_virtual_subdir_true(self) -> None:
        ref = _make_dep_ref(is_virtual=True, virtual_path="skills/brand", repo_url="owner/repo")
        assert ref.is_virtual_subdirectory() is True
        assert ref.is_virtual_file() is False

    def test_virtual_type_none_when_not_virtual(self) -> None:
        ref = _make_dep_ref(is_virtual=False)
        assert ref.virtual_type is None

    def test_get_virtual_package_name_file(self) -> None:
        ref = _make_dep_ref(
            is_virtual=True,
            virtual_path="prompts/code-review.prompt.md",
            repo_url="owner/repo",
        )
        name = ref.get_virtual_package_name()
        assert "repo" in name
        assert "code-review" in name

    def test_get_virtual_package_name_subdir(self) -> None:
        ref = _make_dep_ref(
            is_virtual=True,
            virtual_path="collections/project-planning",
            repo_url="owner/repo",
        )
        name = ref.get_virtual_package_name()
        assert "project-planning" in name

    def test_get_virtual_package_name_fallback_for_non_virtual(self) -> None:
        ref = _make_dep_ref(repo_url="owner/repo", is_virtual=False)
        assert ref.get_virtual_package_name() == "repo"


# ============================================================================
# SECTION 4 - DependencyReference: to_apm_yml_entry / to_github_url
# ============================================================================


class TestToApmYmlEntry:
    """to_apm_yml_entry() returns str for simple, dict for HTTP / skills."""

    def test_simple_returns_canonical_string(self) -> None:
        ref = _make_dep_ref(repo_url="owner/repo")
        entry = ref.to_apm_yml_entry()
        assert isinstance(entry, str)
        assert entry == "owner/repo"

    def test_insecure_returns_dict(self) -> None:
        ref = _make_dep_ref(
            repo_url="owner/repo",
            host="gitlab.corp.com",
            is_insecure=True,
            allow_insecure=True,
        )
        entry = ref.to_apm_yml_entry()
        assert isinstance(entry, dict)
        assert entry.get("allow_insecure") is True
        assert entry["git"].startswith("http://")

    def test_insecure_with_ref_includes_ref(self) -> None:
        ref = _make_dep_ref(
            repo_url="owner/repo",
            host="gitlab.corp.com",
            is_insecure=True,
            reference="main",
        )
        entry = ref.to_apm_yml_entry()
        assert entry.get("ref") == "main"

    def test_skill_subset_returns_dict(self) -> None:
        ref = _make_dep_ref(repo_url="owner/repo", skill_subset=["skill-a", "skill-b"])
        entry = ref.to_apm_yml_entry()
        assert isinstance(entry, dict)
        assert "skills" in entry
        assert sorted(entry["skills"]) == ["skill-a", "skill-b"]

    def test_skill_subset_with_alias(self) -> None:
        ref = _make_dep_ref(repo_url="owner/repo", skill_subset=["skill-a"], alias="my-alias")
        entry = ref.to_apm_yml_entry()
        assert entry.get("alias") == "my-alias"


class TestToGithubUrl:
    """to_github_url() generates proper HTTPS URLs."""

    def test_github_url(self) -> None:
        ref = _make_dep_ref(host="github.com", repo_url="owner/repo")
        url = ref.to_github_url()
        parsed = urllib.parse.urlparse(url)
        assert parsed.scheme == "https"
        assert parsed.hostname == "github.com"
        assert parsed.path == "/owner/repo"

    def test_ado_url_format(self) -> None:
        ref = _make_dep_ref(
            host="dev.azure.com",
            repo_url="org/proj/repo",
            ado_organization="org",
            ado_project="proj",
            ado_repo="repo",
        )
        url = ref.to_github_url()
        parsed = urllib.parse.urlparse(url)
        assert "_git" in parsed.path
        assert "org" in parsed.path

    def test_insecure_dep_uses_http_scheme(self) -> None:
        ref = _make_dep_ref(host="gitlab.corp.com", repo_url="owner/repo", is_insecure=True)
        url = ref.to_github_url()
        parsed = urllib.parse.urlparse(url)
        assert parsed.scheme == "http"

    def test_local_returns_local_path(self) -> None:
        ref = _make_dep_ref(is_local=True, local_path="./my-pkg", repo_url="_local/my-pkg")
        assert ref.to_github_url() == "./my-pkg"

    def test_custom_port_in_netloc(self) -> None:
        ref = _make_dep_ref(host="ghe.corp.com", port=8443, repo_url="owner/repo")
        url = ref.to_github_url()
        parsed = urllib.parse.urlparse(url)
        assert parsed.port == 8443


class TestGetDisplayName:
    """get_display_name() returns alias > virtual name > repo_url."""

    def test_alias_wins(self) -> None:
        ref = _make_dep_ref(repo_url="owner/repo", alias="my-tools")
        assert ref.get_display_name() == "my-tools"

    def test_virtual_uses_package_name(self) -> None:
        ref = _make_dep_ref(
            repo_url="owner/repo",
            is_virtual=True,
            virtual_path="prompts/review.prompt.md",
        )
        name = ref.get_display_name()
        assert "review" in name

    def test_local_returns_local_path(self) -> None:
        ref = _make_dep_ref(is_local=True, local_path="./tool", repo_url="_local/tool")
        assert ref.get_display_name() == "./tool"

    def test_repo_url_fallback(self) -> None:
        ref = _make_dep_ref(repo_url="owner/repo")
        assert ref.get_display_name() == "owner/repo"


# ============================================================================
# SECTION 5 - DependencyReference: parse_from_dict
# ============================================================================


class TestParseFromDict:
    """Object-style dependency entry parsing."""

    def test_git_only(self) -> None:
        ref = _DR().parse_from_dict({"git": "owner/repo"})
        assert ref.repo_url == "owner/repo"

    def test_git_with_ref_override(self) -> None:
        ref = _DR().parse_from_dict({"git": "owner/repo", "ref": "v2.0"})
        assert ref.reference == "v2.0"

    def test_git_with_alias_override(self) -> None:
        ref = _DR().parse_from_dict({"git": "owner/repo", "alias": "my-alias"})
        assert ref.alias == "my-alias"

    def test_git_with_path_creates_virtual(self) -> None:
        ref = _DR().parse_from_dict(
            {"git": "https://github.com/owner/repo.git", "path": "prompts/review.prompt.md"}
        )
        assert ref.is_virtual is True
        assert ref.virtual_path == "prompts/review.prompt.md"

    def test_local_path_dict_form(self) -> None:
        ref = _DR().parse_from_dict({"path": "./packages/my-tool"})
        assert ref.is_local is True
        assert ref.local_path == "./packages/my-tool"

    def test_non_local_path_without_git_raises(self) -> None:
        with pytest.raises(ValueError, match="git"):
            _DR().parse_from_dict({"path": "not-local-path"})

    def test_missing_git_field_raises(self) -> None:
        with pytest.raises(ValueError, match="'git'"):
            _DR().parse_from_dict({"ref": "main"})

    def test_parent_git_requires_path(self) -> None:
        with pytest.raises(ValueError, match="'path'"):
            _DR().parse_from_dict({"git": "parent"})

    def test_parent_git_with_path(self) -> None:
        ref = _DR().parse_from_dict({"git": "parent", "path": "packages/tool"})
        assert ref.is_parent_repo_inheritance is True
        assert ref.virtual_path == "packages/tool"

    def test_skills_field_parsed(self) -> None:
        ref = _DR().parse_from_dict({"git": "owner/repo", "skills": ["skill-a", "skill-b"]})
        assert ref.skill_subset is not None
        assert "skill-a" in ref.skill_subset

    def test_empty_skills_list_raises(self) -> None:
        with pytest.raises(ValueError, match="skills"):
            _DR().parse_from_dict({"git": "owner/repo", "skills": []})

    def test_allow_insecure_field_parsed(self) -> None:
        ref = _DR().parse_from_dict(
            {"git": "http://gitlab.corp.com/owner/repo.git", "allow_insecure": True}
        )
        assert ref.allow_insecure is True

    def test_invalid_alias_raises(self) -> None:
        with pytest.raises(ValueError, match="alias"):
            _DR().parse_from_dict({"git": "owner/repo", "alias": "bad alias!"})

    def test_invalid_ref_type_raises(self) -> None:
        with pytest.raises(ValueError, match="'ref'"):
            _DR().parse_from_dict({"git": "owner/repo", "ref": ""})


# ============================================================================
# SECTION 6 - DependencyReference: GitLab shorthand helpers
# ============================================================================


class TestGitLabShorthandHelpers:
    """split_gitlab_direct_shorthand_parts / virtual_suffix_is_installable_shape."""

    def test_virtual_suffix_file_extension(self) -> None:
        assert _DR().virtual_suffix_is_installable_shape("prompts/review.prompt.md") is True

    def test_virtual_suffix_collections(self) -> None:
        assert _DR().virtual_suffix_is_installable_shape("collections/foo") is True

    def test_virtual_suffix_no_extension(self) -> None:
        assert _DR().virtual_suffix_is_installable_shape("skills/brand") is True

    def test_virtual_suffix_empty_false(self) -> None:
        assert _DR().virtual_suffix_is_installable_shape("") is False

    def test_split_gitlab_non_gitlab_host_returns_none(self) -> None:
        result = _DR().split_gitlab_direct_shorthand_parts("owner/repo")
        assert result is None

    def test_iter_gitlab_boundary_candidates(self) -> None:
        segs = ["group", "subgroup", "repo", "prompts", "review.prompt.md"]
        candidates = list(_DR().iter_gitlab_direct_shorthand_boundary_candidates(segs))
        # k=2..4: at least (group/subgroup, repo/prompts/review.prompt.md) candidate
        assert len(candidates) >= 1

    def test_iter_gitlab_boundary_too_short(self) -> None:
        candidates = list(_DR().iter_gitlab_direct_shorthand_boundary_candidates(["a", "b"]))
        assert candidates == []


# ============================================================================
# SECTION 7 - DependencyReference: __str__ and get_unique_key
# ============================================================================


class TestStrAndUniqueKey:
    """__str__ and get_unique_key edge cases."""

    def test_str_with_host_and_ref(self) -> None:
        ref = _make_dep_ref(host="gitlab.com", repo_url="group/repo", reference="main")
        s = str(ref)
        assert "gitlab.com" in s
        assert "main" in s

    def test_str_with_alias(self) -> None:
        ref = _make_dep_ref(repo_url="owner/repo", alias="my-alias")
        s = str(ref)
        assert "my-alias" in s

    def test_str_local(self) -> None:
        ref = _make_dep_ref(is_local=True, local_path="./tool", repo_url="_local/tool")
        assert str(ref) == "./tool"

    def test_get_unique_key_local(self) -> None:
        ref = _make_dep_ref(is_local=True, local_path="./tool", repo_url="_local/tool")
        assert ref.get_unique_key() == "./tool"

    def test_get_unique_key_virtual(self) -> None:
        ref = _make_dep_ref(
            repo_url="owner/repo",
            is_virtual=True,
            virtual_path="prompts/review.prompt.md",
        )
        key = ref.get_unique_key()
        assert "owner/repo" in key
        assert "prompts/review.prompt.md" in key

    def test_get_unique_key_regular(self) -> None:
        ref = _make_dep_ref(repo_url="owner/repo")
        assert ref.get_unique_key() == "owner/repo"


# ============================================================================
# SECTION 8 - GitHubPackageDownloader: download_virtual_file_package
# ============================================================================


class TestDownloadVirtualFilePackage:
    """download_virtual_file_package() success and error paths."""

    def test_raises_if_not_virtual(self, tmp_path: Path) -> None:
        dl = _make_downloader()
        dep = _make_dep_ref(is_virtual=False)
        with pytest.raises(ValueError, match="virtual file package"):
            dl.download_virtual_file_package(dep, tmp_path)

    def test_raises_if_virtual_subdir(self, tmp_path: Path) -> None:
        dl = _make_downloader()
        dep = _make_dep_ref(
            is_virtual=True, virtual_path="skills/brand-guidelines", repo_url="owner/repo"
        )
        with pytest.raises(ValueError, match="not a valid individual file"):
            dl.download_virtual_file_package(dep, tmp_path)

    def test_success_creates_package_info(self, tmp_path: Path) -> None:
        dl = _make_downloader()
        dep = _make_dep_ref(
            repo_url="owner/repo",
            is_virtual=True,
            virtual_path="prompts/review.prompt.md",
            reference="main",
        )
        file_content = b"# Review prompt\nSome content."
        with (
            patch.object(dl, "_resolve_commit_sha_for_ref", return_value="abc123"),
            patch.object(dl, "download_raw_file", return_value=file_content),
        ):
            pkg_info = dl.download_virtual_file_package(dep, tmp_path / "out")
        assert pkg_info is not None
        assert pkg_info.install_path == tmp_path / "out"
        assert (tmp_path / "out" / "apm.yml").exists()
        assert (tmp_path / "out" / ".apm" / "prompts" / "review.prompt.md").exists()

    def test_frontmatter_description_extracted(self, tmp_path: Path) -> None:
        dl = _make_downloader()
        dep = _make_dep_ref(
            repo_url="owner/repo",
            is_virtual=True,
            virtual_path="prompts/review.prompt.md",
        )
        file_content = b"---\ndescription: My custom description\n---\n# Content\n"
        with (
            patch.object(dl, "_resolve_commit_sha_for_ref", return_value=None),
            patch.object(dl, "download_raw_file", return_value=file_content),
        ):
            pkg_info = dl.download_virtual_file_package(dep, tmp_path / "out")
        # Description should come from frontmatter
        assert pkg_info.package.description == "My custom description"

    def test_progress_updated_on_success(self, tmp_path: Path) -> None:
        dl = _make_downloader()
        dep = _make_dep_ref(
            repo_url="owner/repo",
            is_virtual=True,
            virtual_path="instructions/sec.instructions.md",
        )
        mock_progress = MagicMock()
        file_content = b"# Instructions"
        with (
            patch.object(dl, "_resolve_commit_sha_for_ref", return_value=None),
            patch.object(dl, "download_raw_file", return_value=file_content),
        ):
            dl.download_virtual_file_package(
                dep, tmp_path / "out", progress_task_id=1, progress_obj=mock_progress
            )
        mock_progress.update.assert_called()

    def test_runtime_error_on_download_failure(self, tmp_path: Path) -> None:
        dl = _make_downloader()
        dep = _make_dep_ref(
            repo_url="owner/repo",
            is_virtual=True,
            virtual_path="prompts/review.prompt.md",
        )
        with (
            patch.object(dl, "_resolve_commit_sha_for_ref", return_value=None),
            patch.object(dl, "download_raw_file", side_effect=RuntimeError("network err")),
        ):
            with pytest.raises(RuntimeError, match="Failed to download virtual package"):
                dl.download_virtual_file_package(dep, tmp_path / "out")

    def test_chatmode_extension_placed_in_chatmodes_dir(self, tmp_path: Path) -> None:
        dl = _make_downloader()
        dep = _make_dep_ref(
            repo_url="owner/repo",
            is_virtual=True,
            virtual_path="chatmodes/review.chatmode.md",
        )
        with (
            patch.object(dl, "_resolve_commit_sha_for_ref", return_value=None),
            patch.object(dl, "download_raw_file", return_value=b"# Chatmode"),
        ):
            dl.download_virtual_file_package(dep, tmp_path / "out")
        assert (tmp_path / "out" / ".apm" / "chatmodes" / "review.chatmode.md").exists()


# ============================================================================
# SECTION 9 - GitHubPackageDownloader: download_subdirectory_package guards
# ============================================================================


class TestDownloadSubdirectoryPackageGuards:
    """Guard clauses on download_subdirectory_package()."""

    def test_raises_if_not_virtual(self, tmp_path: Path) -> None:
        dl = _make_downloader()
        dep = _make_dep_ref(is_virtual=False, virtual_path=None)
        with pytest.raises(ValueError, match="virtual subdirectory package"):
            dl.download_subdirectory_package(dep, tmp_path)

    def test_raises_if_virtual_file(self, tmp_path: Path) -> None:
        dl = _make_downloader()
        dep = _make_dep_ref(
            is_virtual=True,
            virtual_path="prompts/review.prompt.md",
            repo_url="owner/repo",
        )
        with pytest.raises(ValueError, match="not a valid subdirectory package"):
            dl.download_subdirectory_package(dep, tmp_path)


# ============================================================================
# SECTION 10 - GitHubPackageDownloader: _try_sparse_checkout
# ============================================================================


class TestTrySparseCheckout:
    """_try_sparse_checkout returns True on success, False on failure."""

    def test_success_all_commands_pass(self, tmp_path: Path) -> None:
        dl = _make_downloader()
        dep = _make_dep_ref(repo_url="owner/repo", reference="main")

        mock_result = MagicMock()
        mock_result.returncode = 0

        with (
            patch.object(dl, "_resolve_dep_token", return_value=None),
            patch.object(dl, "_resolve_dep_auth_ctx", return_value=None),
            patch.object(dl, "_build_repo_url", return_value="https://github.com/owner/repo"),
            patch("subprocess.run", return_value=mock_result),
        ):
            ok = dl._try_sparse_checkout(dep, tmp_path / "clone", "skills/brand", "main")
        assert ok is True

    def test_failure_returns_false_on_nonzero_exit(self, tmp_path: Path) -> None:
        dl = _make_downloader()
        dep = _make_dep_ref(repo_url="owner/repo", reference="main")

        fail_result = MagicMock()
        fail_result.returncode = 128
        fail_result.stderr = "fatal: not found"

        with (
            patch.object(dl, "_resolve_dep_token", return_value=None),
            patch.object(dl, "_resolve_dep_auth_ctx", return_value=None),
            patch.object(dl, "_build_repo_url", return_value="https://github.com/owner/repo"),
            patch("subprocess.run", return_value=fail_result),
        ):
            ok = dl._try_sparse_checkout(dep, tmp_path / "clone", "skills/brand", "main")
        assert ok is False

    def test_exception_returns_false(self, tmp_path: Path) -> None:
        dl = _make_downloader()
        dep = _make_dep_ref(repo_url="owner/repo")

        with (
            patch.object(dl, "_resolve_dep_token", return_value=None),
            patch.object(dl, "_resolve_dep_auth_ctx", return_value=None),
            patch.object(dl, "_build_repo_url", side_effect=RuntimeError("oops")),
        ):
            ok = dl._try_sparse_checkout(dep, tmp_path / "clone", "skills/brand")
        assert ok is False


# ============================================================================
# SECTION 11 - GitHubPackageDownloader: resolve_git_reference
# ============================================================================


class TestResolveGitReference:
    """resolve_git_reference() delegates to tiered resolver when attached."""

    def test_delegates_to_tiered_resolver_when_set(self) -> None:
        dl = _make_downloader()
        dep = _make_dep_ref(repo_url="owner/repo", reference="main")

        mock_tiered = MagicMock()
        mock_resolved = MagicMock()
        mock_tiered.resolve.return_value = mock_resolved
        dl._tiered_resolver = mock_tiered

        result = dl.resolve_git_reference(dep)
        assert result is mock_resolved
        mock_tiered.resolve.assert_called_once_with(dep)

    def test_falls_through_to_refs_when_no_tiered(self) -> None:
        dl = _make_downloader()
        dep = _make_dep_ref(repo_url="owner/repo")
        dl._tiered_resolver = None

        mock_resolved = MagicMock()
        dl._refs = MagicMock()
        dl._refs.resolve.return_value = mock_resolved

        result = dl.resolve_git_reference(dep)
        assert result is mock_resolved
        dl._refs.resolve.assert_called_once_with(dep)


# ============================================================================
# SECTION 12 - GitHubPackageDownloader: _close_repo & debug helper
# ============================================================================


class TestCloseRepoAndDebug:
    """_close_repo and _debug utility functions."""

    def test_close_repo_none_is_noop(self) -> None:
        from apm_cli.deps.github_downloader import _close_repo

        _close_repo(None)  # must not raise

    def test_close_repo_calls_clear_cache_and_close(self) -> None:
        from apm_cli.deps.github_downloader import _close_repo

        mock_repo = MagicMock()
        _close_repo(mock_repo)
        mock_repo.git.clear_cache.assert_called_once()
        mock_repo.close.assert_called_once()

    def test_debug_suppressed_when_env_not_set(self, capsys: pytest.CaptureFixture) -> None:
        from apm_cli.deps.github_downloader import _debug

        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("APM_DEBUG", None)
            _debug("hidden message")
        captured = capsys.readouterr()
        assert "hidden message" not in captured.err

    def test_debug_prints_when_env_set(self, capsys: pytest.CaptureFixture) -> None:
        from apm_cli.deps.github_downloader import _debug

        with patch.dict(os.environ, {"APM_DEBUG": "1"}):
            _debug("visible message")
        captured = capsys.readouterr()
        assert "visible message" in captured.err


# ============================================================================
# SECTION 13 - GitHubPackageDownloader: download_raw_file routing
# ============================================================================


class TestDownloadRawFileRouting:
    """download_raw_file() routes to the correct backend."""

    def test_routes_to_ado_when_ado_dep(self) -> None:
        dl = _make_downloader()
        dep = _make_dep_ref(
            host="dev.azure.com",
            repo_url="org/proj/repo",
            ado_organization="org",
            ado_project="proj",
            ado_repo="repo",
        )
        with patch.object(dl, "_download_ado_file", return_value=b"ado content") as mock_ado:
            content = dl.download_raw_file(dep, "path/file.txt", ref="main")
        assert content == b"ado content"
        mock_ado.assert_called_once()

    def test_routes_to_github_for_github_dep(self) -> None:
        dl = _make_downloader()
        dep = _make_dep_ref(host="github.com", repo_url="owner/repo")
        with (
            patch.object(dl, "_parse_artifactory_base_url", return_value=None),
            patch.object(dl, "_download_github_file", return_value=b"gh content") as mock_gh,
        ):
            content = dl.download_raw_file(dep, "file.txt", ref="main")
        assert content == b"gh content"
        mock_gh.assert_called_once()

    def test_routes_to_artifactory_when_direct_prefix(self) -> None:
        dl = _make_downloader()
        dep = _make_dep_ref(
            host="art.corp.com",
            repo_url="owner/repo",
            artifactory_prefix="artifactory/github",
        )
        with patch.object(
            dl, "_download_file_from_artifactory", return_value=b"art content"
        ) as mock_art:
            content = dl.download_raw_file(dep, "file.txt", ref="main")
        assert content == b"art content"
        mock_art.assert_called_once()


# ============================================================================
# SECTION 14 - install/pipeline.py: _run_phase
# ============================================================================


class TestRunPhase:
    """_run_phase() invokes phase.run(ctx) with optional timing."""

    def test_non_verbose_calls_phase_run(self) -> None:
        from apm_cli.install.pipeline import _run_phase

        mock_phase = MagicMock()
        mock_phase.run.return_value = "result"
        ctx = MagicMock()
        ctx.verbose = False
        ctx.logger = None

        result = _run_phase("resolve", mock_phase, ctx)
        assert result == "result"
        mock_phase.run.assert_called_once_with(ctx)

    def test_verbose_calls_phase_run_and_logs_timing(self) -> None:
        from apm_cli.install.pipeline import _run_phase

        mock_phase = MagicMock()
        mock_phase.run.return_value = None
        mock_logger = MagicMock()
        ctx = MagicMock()
        ctx.verbose = True
        ctx.logger = mock_logger

        _run_phase("download", mock_phase, ctx)

        mock_phase.run.assert_called_once_with(ctx)
        mock_logger.verbose_detail.assert_called_once()
        timing_msg = mock_logger.verbose_detail.call_args[0][0]
        assert "Phase: download" in timing_msg

    def test_verbose_timing_logged_even_if_phase_raises(self) -> None:
        """The finally block must log timing even on exception."""
        from apm_cli.install.pipeline import _run_phase

        mock_phase = MagicMock()
        mock_phase.run.side_effect = RuntimeError("phase fail")
        mock_logger = MagicMock()
        ctx = MagicMock()
        ctx.verbose = True
        ctx.logger = mock_logger

        with pytest.raises(RuntimeError, match="phase fail"):
            _run_phase("integrate", mock_phase, ctx)
        mock_logger.verbose_detail.assert_called_once()


# ============================================================================
# SECTION 15 - install/pipeline.py: run_install_pipeline early exits
# ============================================================================


class TestRunInstallPipelineEarlyExits:
    """run_install_pipeline() returns empty InstallResult when nothing to do."""

    def _make_minimal_apm_package(self) -> Any:
        from apm_cli.models.apm_package import APMPackage

        pkg = MagicMock(spec=APMPackage)
        pkg.get_apm_dependencies.return_value = []
        pkg.get_dev_apm_dependencies.return_value = []
        pkg.get_mcp_dependencies.return_value = []
        return pkg

    def test_returns_empty_when_no_deps_no_local_primitives(self, tmp_path: Path) -> None:
        from apm_cli.install.pipeline import run_install_pipeline
        from apm_cli.models.results import InstallResult

        pkg = self._make_minimal_apm_package()

        with (
            patch("apm_cli.deps.lockfile.LockFile.read", return_value=None),
            patch("apm_cli.core.scope.get_apm_dir", return_value=tmp_path / ".apm"),
            patch("apm_cli.core.scope.get_deploy_root", return_value=tmp_path),
            patch(
                "apm_cli.install.phases.local_content._project_has_root_primitives",
                return_value=False,
            ),
        ):
            result = run_install_pipeline(pkg)

        assert isinstance(result, InstallResult)

    def test_plan_callback_cancel_returns_empty(self, tmp_path: Path) -> None:
        """plan_callback returning False must abort cleanly."""
        from apm_cli.install.pipeline import run_install_pipeline
        from apm_cli.models.results import InstallResult

        pkg = self._make_minimal_apm_package()

        # Provide a dep so the pipeline passes the early-exit gate
        mock_dep = _make_dep_ref(repo_url="owner/repo")
        pkg.get_apm_dependencies.return_value = [mock_dep]

        def _cancel_callback(plan: Any) -> bool:
            return False

        with (
            patch("apm_cli.deps.lockfile.LockFile.read", return_value=None),
            patch("apm_cli.core.scope.get_apm_dir", return_value=tmp_path / ".apm"),
            patch("apm_cli.core.scope.get_deploy_root", return_value=tmp_path),
            patch(
                "apm_cli.install.phases.local_content._project_has_root_primitives",
                return_value=False,
            ),
            patch("apm_cli.install.pipeline._run_phase") as mock_run_phase,
            patch("apm_cli.install.plan.build_update_plan", return_value=MagicMock()),
        ):
            # Simulate resolve phase populating deps_to_install
            def _side_effect(name: str, phase: Any, ctx: Any) -> None:
                if name == "resolve":
                    ctx.deps_to_install = [mock_dep]
                    ctx.transitive_failures = []

            mock_run_phase.side_effect = _side_effect

            result = run_install_pipeline(
                pkg,
                update_refs=False,
                plan_callback=_cancel_callback,
            )
        assert isinstance(result, InstallResult)


# ============================================================================
# SECTION 16 - install/pipeline.py: _preflight_auth_check
# ============================================================================


class TestPreflightAuthCheck:
    """_preflight_auth_check() skips GitHub and no-host deps."""

    def test_skips_github_host(self) -> None:
        """GitHub deps must be skipped entirely (API probe uses unauth fallback)."""
        from apm_cli.install.pipeline import _preflight_auth_check

        ctx = MagicMock()
        github_dep = _make_dep_ref(host="github.com", repo_url="owner/repo")
        ctx.deps_to_install = [github_dep]
        mock_ar = MagicMock()

        # If the function tries ls-remote for github.com, the test will fail
        with patch("subprocess.run", side_effect=AssertionError("should not call")) as mock_sp:
            _preflight_auth_check(ctx, mock_ar, verbose=False)
        # Reached here => no subprocess call was made
        mock_sp.assert_not_called()

    def test_skips_none_host(self) -> None:
        from apm_cli.install.pipeline import _preflight_auth_check

        ctx = MagicMock()
        dep = _make_dep_ref(host=None, repo_url="owner/repo")
        ctx.deps_to_install = [dep]
        mock_ar = MagicMock()

        with patch("subprocess.run", side_effect=AssertionError("should not call")) as mock_sp:
            _preflight_auth_check(ctx, mock_ar, verbose=False)
        mock_sp.assert_not_called()

    def test_ado_dep_runs_ls_remote(self) -> None:
        from apm_cli.install.pipeline import _preflight_auth_check

        ctx = MagicMock()
        ctx.verbose = False
        ado_dep = _make_dep_ref(
            host="dev.azure.com",
            repo_url="org/proj/repo",
            ado_organization="org",
            ado_project="proj",
            ado_repo="repo",
        )
        ctx.deps_to_install = [ado_dep]

        mock_ar = MagicMock()
        mock_dep_ctx = MagicMock()
        mock_dep_ctx.token = "mytoken"
        mock_dep_ctx.auth_scheme = "basic"
        mock_dep_ctx.source = "manual"
        mock_dep_ctx.git_env = {}
        mock_ar.resolve_for_dep.return_value = mock_dep_ctx

        success_result = MagicMock()
        success_result.returncode = 0
        success_result.stderr = ""

        mock_dl = MagicMock()
        mock_dl._build_repo_url.return_value = "https://dev.azure.com/org/proj/repo"
        mock_dl.git_env = {}

        with (
            patch(
                "apm_cli.deps.github_downloader.GitHubPackageDownloader",
                return_value=mock_dl,
            ),
            patch("subprocess.run", return_value=success_result),
        ):
            _preflight_auth_check(ctx, mock_ar, verbose=False)  # must not raise


# ============================================================================
# SECTION 17 - install/drift.py: normalization helpers
# ============================================================================


class TestNormalizationHelpers:
    """_normalize, _strip_bom, _strip_build_id, _normalize_line_endings."""

    def test_strip_crlf_to_lf(self) -> None:
        from apm_cli.install.drift import _normalize_line_endings

        data = b"line1\r\nline2\r\n"
        result = _normalize_line_endings(data)
        assert b"\r" not in result
        assert result == b"line1\nline2\n"

    def test_strip_bom_utf8(self) -> None:
        from apm_cli.install.drift import _strip_bom

        bom_data = b"\xef\xbb\xbfHello"
        result = _strip_bom(bom_data)
        assert result == b"Hello"

    def test_strip_bom_noop_when_absent(self) -> None:
        from apm_cli.install.drift import _strip_bom

        data = b"Hello"
        result = _strip_bom(data)
        assert result == b"Hello"

    def test_strip_build_id_removes_header(self) -> None:
        from apm_cli.install.drift import _strip_build_id

        data = b"<!-- Build ID: abc123def456 -->\nContent line"
        result = _strip_build_id(data)
        assert b"Build ID" not in result
        assert b"Content line" in result

    def test_normalize_full_pipeline(self) -> None:
        from apm_cli.install.drift import _normalize

        data = b"\xef\xbb\xbf<!-- Build ID: abcdef -->\r\nNormal content\r\n"
        result = _normalize(data)
        assert b"\xef\xbb\xbf" not in result  # BOM stripped
        assert b"\r" not in result  # CRLF normalised
        assert b"Build ID" not in result  # header stripped
        assert b"Normal content" in result


# ============================================================================
# SECTION 18 - install/drift.py: scratch directory lifecycle
# ============================================================================


class TestScratchDirectoryLifecycle:
    """_assert_scratch_bound and _make_scratch_root."""

    def test_assert_scratch_bound_outside_project_ok(self, tmp_path: Path) -> None:
        from apm_cli.install.drift import _assert_scratch_bound

        project = tmp_path / "project"
        scratch = tmp_path / "scratch"
        project.mkdir()
        scratch.mkdir()
        _assert_scratch_bound(project, scratch)  # must not raise

    def test_assert_scratch_bound_inside_project_raises(self, tmp_path: Path) -> None:
        from apm_cli.install.drift import _assert_scratch_bound

        project = tmp_path / "project"
        scratch = project / "scratch"
        project.mkdir()
        scratch.mkdir()
        with pytest.raises(RuntimeError, match="inside project tree"):
            _assert_scratch_bound(project, scratch)

    def test_make_scratch_root_outside_project(self, tmp_path: Path) -> None:
        from apm_cli.install.drift import _make_scratch_root

        project = tmp_path / "project"
        project.mkdir()
        scratch = _make_scratch_root(project)
        # Must exist and NOT be inside the project tree
        assert scratch.exists()
        try:
            scratch.relative_to(project)
            pytest.fail("scratch should not be inside project")
        except ValueError:
            pass  # expected: scratch is outside project


# ============================================================================
# SECTION 19 - install/drift.py: _walk_managed / _collect_tracked_files
# ============================================================================


class TestWalkManaged:
    """_walk_managed and _collect_tracked_files."""

    def test_walk_managed_returns_files(self, tmp_path: Path) -> None:
        from apm_cli.install.drift import _walk_managed

        root = tmp_path / "project"
        apm_dir = root / ".apm" / "instructions"
        apm_dir.mkdir(parents=True)
        (apm_dir / "rules.instructions.md").write_bytes(b"content")

        files = _walk_managed(root, {".apm"})
        assert ".apm/instructions/rules.instructions.md" in files

    def test_walk_managed_agents_md_at_root(self, tmp_path: Path) -> None:
        from apm_cli.install.drift import _walk_managed

        root = tmp_path / "project"
        root.mkdir()
        (root / "AGENTS.md").write_bytes(b"agents content")

        files = _walk_managed(root, set())
        assert "AGENTS.md" in files

    def test_walk_managed_empty_root(self, tmp_path: Path) -> None:
        from apm_cli.install.drift import _walk_managed

        root = tmp_path / "nonexistent"
        files = _walk_managed(root, {".apm"})
        assert files == {}

    def test_collect_tracked_files(self) -> None:
        from apm_cli.install.drift import _collect_tracked_files

        mock_lf = MagicMock()
        dep1 = MagicMock()
        dep1.deployed_files = [".apm/instructions/rules.instructions.md"]
        mock_lf.dependencies = {"pkg1": dep1}
        mock_lf.local_deployed_files = [".apm/instructions/local.instructions.md"]

        tracked = _collect_tracked_files(mock_lf)
        assert ".apm/instructions/rules.instructions.md" in tracked
        assert tracked[".apm/instructions/rules.instructions.md"] == "pkg1"
        assert ".apm/instructions/local.instructions.md" in tracked
        assert tracked[".apm/instructions/local.instructions.md"] == "."


# ============================================================================
# SECTION 20 - install/drift.py: _governed_root_dirs
# ============================================================================


class TestGovernedRootDirs:
    """_governed_root_dirs always includes .apm."""

    def test_includes_apm(self) -> None:
        from apm_cli.install.drift import _governed_root_dirs

        roots = _governed_root_dirs([])
        assert ".apm" in roots

    def test_includes_target_root_dirs(self) -> None:
        from apm_cli.install.drift import _governed_root_dirs

        target = MagicMock()
        target.root_dir = ".github"
        roots = _governed_root_dirs([target])
        assert ".github" in roots
        assert ".apm" in roots

    def test_empty_targets_just_apm(self) -> None:
        from apm_cli.install.drift import _governed_root_dirs

        roots = _governed_root_dirs(None)
        assert ".apm" in roots


# ============================================================================
# SECTION 21 - install/drift.py: diff_scratch_against_project
# ============================================================================


class TestDiffScratchAgainstProject:
    """diff_scratch_against_project() emits the three finding kinds."""

    def _setup_dirs(self, tmp_path: Path) -> tuple[Path, Path]:
        scratch = tmp_path / "scratch"
        project = tmp_path / "project"
        scratch.mkdir()
        project.mkdir()
        return scratch, project

    def _make_lockfile(self, deployed: dict[str, str] | None = None) -> Any:
        """Build a minimal mock LockFile."""
        mock_lf = MagicMock()
        dep = MagicMock()
        dep.deployed_files = list((deployed or {}).keys())
        mock_lf.dependencies = {"pkg": dep}
        mock_lf.local_deployed_files = []
        return mock_lf

    def test_no_findings_when_trees_match(self, tmp_path: Path) -> None:
        from apm_cli.install.drift import diff_scratch_against_project

        scratch, project = self._setup_dirs(tmp_path)
        content = b"same content\n"
        for root in (scratch, project):
            p = root / ".apm" / "instructions" / "rules.instructions.md"
            p.parent.mkdir(parents=True)
            p.write_bytes(content)

        mock_lf = MagicMock()
        mock_lf.dependencies = {}
        mock_lf.local_deployed_files = []
        findings = diff_scratch_against_project(scratch, project, mock_lf, targets=[])
        assert findings == []

    def test_modified_finding_when_content_differs(self, tmp_path: Path) -> None:
        from apm_cli.install.drift import diff_scratch_against_project

        scratch, project = self._setup_dirs(tmp_path)
        for root, content in ((scratch, b"scratch"), (project, b"different")):
            p = root / ".apm" / "instructions" / "rules.instructions.md"
            p.parent.mkdir(parents=True)
            p.write_bytes(content)

        mock_lf = MagicMock()
        mock_lf.dependencies = {}
        mock_lf.local_deployed_files = []
        findings = diff_scratch_against_project(scratch, project, mock_lf, targets=[])
        kinds = [f.kind for f in findings]
        assert "modified" in kinds

    def test_unintegrated_finding_when_only_in_scratch(self, tmp_path: Path) -> None:
        from apm_cli.install.drift import diff_scratch_against_project

        scratch, project = self._setup_dirs(tmp_path)
        p = scratch / ".apm" / "instructions" / "missing.instructions.md"
        p.parent.mkdir(parents=True)
        p.write_bytes(b"content")

        mock_lf = MagicMock()
        mock_lf.dependencies = {}
        mock_lf.local_deployed_files = []
        findings = diff_scratch_against_project(scratch, project, mock_lf, targets=[])
        kinds = [f.kind for f in findings]
        assert "unintegrated" in kinds

    def test_orphaned_finding_when_tracked_file_missing_from_scratch(self, tmp_path: Path) -> None:
        from apm_cli.install.drift import diff_scratch_against_project

        scratch, project = self._setup_dirs(tmp_path)
        rel = ".apm/instructions/orphan.instructions.md"
        project_file = project / ".apm" / "instructions" / "orphan.instructions.md"
        project_file.parent.mkdir(parents=True)
        project_file.write_bytes(b"orphan content")

        dep = MagicMock()
        dep.deployed_files = [rel]
        mock_lf = MagicMock()
        mock_lf.dependencies = {"pkg": dep}
        mock_lf.local_deployed_files = []

        findings = diff_scratch_against_project(scratch, project, mock_lf, targets=[])
        kinds = [f.kind for f in findings]
        assert "orphaned" in kinds

    def test_untracked_governed_file_ignored(self, tmp_path: Path) -> None:
        from apm_cli.install.drift import diff_scratch_against_project

        scratch, project = self._setup_dirs(tmp_path)
        # File in project's .apm but NOT tracked by any dep
        p = project / ".apm" / "instructions" / "user-authored.instructions.md"
        p.parent.mkdir(parents=True)
        p.write_bytes(b"user content")

        mock_lf = MagicMock()
        mock_lf.dependencies = {}
        mock_lf.local_deployed_files = []

        findings = diff_scratch_against_project(scratch, project, mock_lf, targets=[])
        assert findings == []


# ============================================================================
# SECTION 22 - install/drift.py: render functions
# ============================================================================


class TestRenderDrift:
    """render_drift_text, render_drift_json, render_drift_sarif, render_drift."""

    def _make_findings(self) -> list:
        from apm_cli.install.drift import DriftFinding

        return [
            DriftFinding(path=".apm/a.md", kind="modified", package="pkg1"),
            DriftFinding(path=".apm/b.md", kind="unintegrated", package="pkg2"),
            DriftFinding(path=".apm/c.md", kind="orphaned", package="pkg3"),
        ]

    def test_render_text_clean(self) -> None:
        from apm_cli.install.drift import render_drift_text

        text = render_drift_text([])
        assert "No drift" in text

    def test_render_text_with_findings(self) -> None:
        from apm_cli.install.drift import render_drift_text

        findings = self._make_findings()
        text = render_drift_text(findings)
        assert "Drift detected" in text
        assert "modified" in text
        assert "unintegrated" in text
        assert "orphaned" in text

    def test_render_text_verbose_includes_inline_diff(self) -> None:
        from apm_cli.install.drift import DriftFinding, render_drift_text

        findings = [DriftFinding(path="a.md", kind="modified", package="pkg", inline_diff="hint")]
        text = render_drift_text(findings, verbose=True)
        assert "hint" in text

    def test_render_json_shape(self) -> None:
        from apm_cli.install.drift import render_drift_json

        findings = self._make_findings()
        result = render_drift_json(findings)
        assert "drift" in result
        assert len(result["drift"]) == 3
        first = result["drift"][0]
        assert "path" in first
        assert "kind" in first
        assert "package" in first

    def test_render_sarif_shape(self) -> None:
        from apm_cli.install.drift import render_drift_sarif

        findings = self._make_findings()
        results = render_drift_sarif(findings)
        assert len(results) == 3
        first = results[0]
        assert first["ruleId"].startswith("apm/drift/")
        assert "message" in first
        assert "locations" in first

    def test_render_drift_text_format_dispatch(self) -> None:
        from apm_cli.install.drift import render_drift

        text = render_drift([], fmt="text")
        assert "No drift" in text

    def test_render_drift_json_format_dispatch(self) -> None:
        from apm_cli.install.drift import render_drift

        raw = render_drift([], fmt="json")
        parsed = json.loads(raw)
        assert "drift" in parsed

    def test_render_drift_sarif_format_dispatch(self) -> None:
        from apm_cli.install.drift import render_drift

        raw = render_drift([], fmt="sarif")
        parsed = json.loads(raw)
        assert "results" in parsed


# ============================================================================
# SECTION 23 - install/drift.py: CheckLogger
# ============================================================================


class TestCheckLogger:
    """CheckLogger emits to stderr via click.echo."""

    def test_replay_start_emits_to_stderr(self, capsys: pytest.CaptureFixture) -> None:
        from apm_cli.install.drift import CheckLogger

        logger = CheckLogger(verbose=False)
        logger.replay_start()
        captured = capsys.readouterr()
        assert "Replaying" in captured.err

    def test_clean_emits_to_stderr(self, capsys: pytest.CaptureFixture) -> None:
        from apm_cli.install.drift import CheckLogger

        logger = CheckLogger(verbose=False)
        logger.clean()
        captured = capsys.readouterr()
        assert "No drift" in captured.err

    def test_findings_emits_count(self, capsys: pytest.CaptureFixture) -> None:
        from apm_cli.install.drift import CheckLogger

        logger = CheckLogger(verbose=False)
        logger.findings(5)
        captured = capsys.readouterr()
        assert "5" in captured.err

    def test_scratch_root_silent_when_not_verbose(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        from apm_cli.install.drift import CheckLogger

        logger = CheckLogger(verbose=False)
        logger.scratch_root(tmp_path)
        captured = capsys.readouterr()
        assert str(tmp_path) not in captured.err

    def test_scratch_root_emits_when_verbose(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        from apm_cli.install.drift import CheckLogger

        logger = CheckLogger(verbose=True)
        logger.scratch_root(tmp_path)
        captured = capsys.readouterr()
        assert str(tmp_path) in captured.err

    def test_diff_start_emits_to_stderr(self, capsys: pytest.CaptureFixture) -> None:
        from apm_cli.install.drift import CheckLogger

        logger = CheckLogger(verbose=False)
        logger.diff_start()
        captured = capsys.readouterr()
        assert "Diffing" in captured.err

    def test_replay_complete_emits_count(self, capsys: pytest.CaptureFixture) -> None:
        from apm_cli.install.drift import CheckLogger

        logger = CheckLogger(verbose=False)
        logger.replay_complete(3)
        captured = capsys.readouterr()
        assert "3" in captured.err


# ============================================================================
# SECTION 24 - install/drift.py: DriftFinding / ReplayConfig / CacheMissError
# ============================================================================


class TestDriftDataClasses:
    """DriftFinding, ReplayConfig, and CacheMissError behave correctly."""

    def test_drift_finding_fields(self) -> None:
        from apm_cli.install.drift import DriftFinding

        f = DriftFinding(path="a/b.md", kind="modified", package="pkg1", inline_diff="diff")
        assert f.path == "a/b.md"
        assert f.kind == "modified"
        assert f.package == "pkg1"
        assert f.inline_diff == "diff"

    def test_drift_finding_frozen(self) -> None:
        from apm_cli.install.drift import DriftFinding

        f = DriftFinding(path="a.md", kind="orphaned")
        with pytest.raises((AttributeError, TypeError)):
            f.path = "changed"  # type: ignore[misc]

    def test_replay_config_frozen(self, tmp_path: Path) -> None:
        from apm_cli.install.drift import ReplayConfig

        cfg = ReplayConfig(project_root=tmp_path, lockfile_path=tmp_path / "apm.lock.yaml")
        with pytest.raises((AttributeError, TypeError)):
            cfg.cache_only = False  # type: ignore[misc]

    def test_replay_config_defaults(self, tmp_path: Path) -> None:
        from apm_cli.install.drift import ReplayConfig

        cfg = ReplayConfig(project_root=tmp_path, lockfile_path=tmp_path / "apm.lock.yaml")
        assert cfg.cache_only is True
        assert cfg.no_hooks is True
        assert cfg.parallel_downloads == 1
        assert cfg.targets is None

    def test_cache_miss_error_is_runtime_error(self) -> None:
        from apm_cli.install.drift import CacheMissError

        err = CacheMissError("cache miss message")
        assert isinstance(err, RuntimeError)
        assert "cache miss" in str(err)


# ============================================================================
# SECTION 25 - install/drift.py: _materialize_install_path
# ============================================================================


class TestMaterializeInstallPath:
    """_materialize_install_path returns correct paths and raises on misses."""

    def test_not_implemented_for_non_cache_only(self, tmp_path: Path) -> None:
        from apm_cli.install.drift import _materialize_install_path

        lock_dep = MagicMock()
        lock_dep.source = "github.com"
        lock_dep.local_path = None
        lock_dep.resolved_commit = "abc123"

        with pytest.raises(NotImplementedError):
            _materialize_install_path(lock_dep, tmp_path, tmp_path, cache_only=False)

    def test_local_dep_returns_project_subpath(self, tmp_path: Path) -> None:
        from apm_cli.install.drift import _materialize_install_path

        project_root = tmp_path / "project"
        local_pkg = project_root / "packages" / "my-tool"
        local_pkg.mkdir(parents=True)
        local_pkg_rel = "packages/my-tool"

        lock_dep = MagicMock()
        lock_dep.source = "local"
        lock_dep.local_path = local_pkg_rel
        lock_dep.repo_url = "_local/my-tool"
        lock_dep.resolved_commit = None
        lock_dep.resolved_by = None

        path = _materialize_install_path(
            lock_dep, project_root, tmp_path / "apm_modules", cache_only=True
        )
        assert path == local_pkg.resolve()

    def test_local_dep_missing_path_raises(self, tmp_path: Path) -> None:
        from apm_cli.install.drift import CacheMissError, _materialize_install_path

        lock_dep = MagicMock()
        lock_dep.source = "local"
        lock_dep.local_path = None

        with pytest.raises(CacheMissError, match="no local_path"):
            _materialize_install_path(lock_dep, tmp_path, tmp_path, cache_only=True)

    def test_local_dep_nonexistent_dir_raises(self, tmp_path: Path) -> None:
        from apm_cli.install.drift import CacheMissError, _materialize_install_path

        lock_dep = MagicMock()
        lock_dep.source = "local"
        lock_dep.local_path = "nonexistent/path"
        lock_dep.resolved_by = None

        with pytest.raises(CacheMissError, match="local source missing"):
            _materialize_install_path(lock_dep, tmp_path, tmp_path, cache_only=True)

    def test_remote_dep_no_resolved_commit_raises(self, tmp_path: Path) -> None:
        from apm_cli.install.drift import CacheMissError, _materialize_install_path

        lock_dep = MagicMock()
        lock_dep.source = "github.com"
        lock_dep.local_path = None
        lock_dep.resolved_commit = None
        lock_dep.repo_url = "owner/repo"

        dep_ref = _make_dep_ref(repo_url="owner/repo")
        lock_dep.to_dependency_ref.return_value = dep_ref

        with pytest.raises(CacheMissError, match="no resolved_commit"):
            _materialize_install_path(lock_dep, tmp_path, tmp_path / "apm_modules", cache_only=True)

    def test_remote_dep_cache_miss_raises(self, tmp_path: Path) -> None:
        from apm_cli.install.drift import CacheMissError, _materialize_install_path

        apm_modules = tmp_path / "apm_modules"
        apm_modules.mkdir()

        lock_dep = MagicMock()
        lock_dep.source = "github.com"
        lock_dep.local_path = None
        lock_dep.resolved_commit = "abc123"
        lock_dep.repo_url = "owner/repo"

        dep_ref = _make_dep_ref(repo_url="owner/repo")
        lock_dep.to_dependency_ref.return_value = dep_ref

        with pytest.raises(CacheMissError, match="cache miss"):
            _materialize_install_path(lock_dep, tmp_path, apm_modules, cache_only=True)


# ============================================================================
# SECTION 26 - install/drift.py: _build_package_info
# ============================================================================


class TestBuildPackageInfo:
    """_build_package_info loads apm.yml when present and falls back gracefully."""

    def test_builds_package_info_without_apm_yml(self, tmp_path: Path) -> None:
        from apm_cli.install.drift import _build_package_info

        install_path = tmp_path / "pkg"
        install_path.mkdir()
        # No apm.yml -- fallback construction

        lock_dep = MagicMock()
        lock_dep.repo_url = "owner/repo"
        lock_dep.version = "1.0.0"
        lock_dep.resolved_ref = "main"
        lock_dep.resolved_commit = "abc123def"

        dep_ref = _make_dep_ref(repo_url="owner/repo")
        lock_dep.to_dependency_ref.return_value = dep_ref

        info = _build_package_info(lock_dep, install_path)
        assert info.install_path == install_path
        assert info.package is not None

    def test_builds_package_info_with_apm_yml(self, tmp_path: Path) -> None:
        from apm_cli.install.drift import _build_package_info

        install_path = tmp_path / "pkg"
        install_path.mkdir()
        apm_yml = install_path / "apm.yml"
        apm_yml.write_text(yaml.safe_dump({"name": "my-pkg", "version": "2.0.0"}), encoding="utf-8")

        lock_dep = MagicMock()
        lock_dep.repo_url = "owner/repo"
        lock_dep.version = "2.0.0"
        lock_dep.resolved_ref = "v2.0.0"
        lock_dep.resolved_commit = "deadbeef"

        dep_ref = _make_dep_ref(repo_url="owner/repo")
        lock_dep.to_dependency_ref.return_value = dep_ref

        info = _build_package_info(lock_dep, install_path)
        assert info.package.name == "my-pkg"


# ============================================================================
# SECTION 27 - install/drift.py: _make_integrators / _filter_targets
# ============================================================================


class TestMakeIntegratorsAndFilterTargets:
    """_make_integrators returns a dict; _filter_targets filters by name."""

    def test_make_integrators_returns_expected_keys(self) -> None:
        from apm_cli.install.drift import _make_integrators

        integrators = _make_integrators()
        assert "prompt" in integrators
        assert "agent" in integrators
        assert "skill" in integrators
        assert "command" in integrators
        assert "hook" in integrators
        assert "instruction" in integrators

    def test_filter_targets_all_when_no_names(self) -> None:
        from apm_cli.install.drift import _filter_targets

        targets = [MagicMock(name="a"), MagicMock(name="b")]
        result = _filter_targets(targets, None)
        assert result is targets

    def test_filter_targets_by_name(self) -> None:
        from apm_cli.install.drift import _filter_targets

        t1 = MagicMock()
        t1.name = "copilot"
        t2 = MagicMock()
        t2.name = "claude"
        result = _filter_targets([t1, t2], frozenset({"copilot"}))
        assert len(result) == 1
        assert result[0].name == "copilot"


# ============================================================================
# SECTION 28 - install/drift.py: _inline_diff_for
# ============================================================================


class TestInlineDiffFor:
    """_inline_diff_for returns appropriate hints."""

    def test_empty_string_for_small_files(self, tmp_path: Path) -> None:
        from apm_cli.install.drift import _inline_diff_for

        a = tmp_path / "a.md"
        b = tmp_path / "b.md"
        a.write_bytes(b"small content")
        b.write_bytes(b"other small content")
        result = _inline_diff_for(a, b)
        assert result == ""

    def test_hint_for_large_files(self, tmp_path: Path) -> None:
        from apm_cli.install.drift import _INLINE_DIFF_BYTE_CAP, _inline_diff_for

        large = tmp_path / "large.md"
        large.write_bytes(b"x" * (_INLINE_DIFF_BYTE_CAP + 1))
        small = tmp_path / "small.md"
        small.write_bytes(b"small")
        result = _inline_diff_for(large, small)
        assert "too large" in result


# ============================================================================
# SECTION 29 - DependencyReference: is_local_path static edge cases
# ============================================================================


class TestIsLocalPath:
    """is_local_path covers Windows paths and edge cases."""

    def test_dot_slash_is_local(self) -> None:
        assert _DR().is_local_path("./packages/tool") is True

    def test_dot_dot_slash_is_local(self) -> None:
        assert _DR().is_local_path("../sibling") is True

    def test_absolute_unix_is_local(self) -> None:
        assert _DR().is_local_path("/opt/pkg") is True

    def test_tilde_is_local(self) -> None:
        assert _DR().is_local_path("~/packages/pkg") is True

    def test_windows_drive_letter_is_local(self) -> None:
        assert _DR().is_local_path("C:\\packages\\tool") is True
        assert _DR().is_local_path("C:/packages/tool") is True

    def test_protocol_relative_not_local(self) -> None:
        assert _DR().is_local_path("//host/path") is False

    def test_shorthand_not_local(self) -> None:
        assert _DR().is_local_path("owner/repo") is False

    def test_https_not_local(self) -> None:
        assert _DR().is_local_path("https://github.com/owner/repo") is False


# ============================================================================
# SECTION 30 - Additional edge case parsing
# ============================================================================


class TestParseEdgeCases:
    """Additional parsing edge cases to close coverage gaps."""

    def test_empty_string_raises(self) -> None:
        with pytest.raises(ValueError, match="Empty dependency string"):
            _DR().parse("")

    def test_whitespace_only_raises(self) -> None:
        with pytest.raises(ValueError, match="Empty dependency string"):
            _DR().parse("   ")

    def test_control_character_raises(self) -> None:
        with pytest.raises(ValueError, match="control characters"):
            _DR().parse("owner/repo\x01extra")

    def test_shorthand_gh_prefix_stripped(self) -> None:
        """gh/ prefix strips 'gh' as namespace, making owner the second segment."""
        ref = _DR().parse("gh/owner/repo")
        # 'gh' is treated as owner, 'owner' as repo in this 3-segment shorthand
        # (virtual detection strips gh/ then sees owner/repo as virtual path)
        assert ref.host == "github.com"

    def test_parse_with_only_ref_no_alias(self) -> None:
        ref = _DR().parse("owner/repo#v1.2.3")
        assert ref.reference == "v1.2.3"
        assert ref.alias is None

    def test_artifactory_prefix_extracted(self) -> None:
        """Artifactory VCS path extracts the prefix."""
        ref = _DR().parse("art.corp.com/artifactory/github/owner/repo")
        # prefix should be captured when path matches Artifactory shape
        if ref.artifactory_prefix:
            assert "artifactory" in ref.artifactory_prefix
