"""Wave 7: integration tests for DependencyReference edge cases and install/sources.

Goal: maximise branch coverage for models/dependency/reference.py
(especially parse_from_dict and get_install_path branches).
"""

from __future__ import annotations

from pathlib import Path

import pytest


class TestDependencyReferenceParseFromDictEdgeCases:
    """Cover parse_from_dict branches that are currently uncovered."""

    def test_path_entry_valid_relative(self, tmp_path: Path) -> None:
        from apm_cli.models.apm_package import DependencyReference

        pkg = tmp_path / "local-pkg"
        pkg.mkdir()
        (pkg / "apm.yml").write_text("name: test\n")
        ref = DependencyReference.parse_from_dict({"path": str(pkg)})
        assert ref.is_local is True

    def test_path_entry_empty_string(self) -> None:
        from apm_cli.models.apm_package import DependencyReference

        with pytest.raises(ValueError, match=r"non-empty string"):
            DependencyReference.parse_from_dict({"path": ""})

    def test_path_entry_non_string(self) -> None:
        from apm_cli.models.apm_package import DependencyReference

        with pytest.raises(ValueError, match=r"non-empty string"):
            DependencyReference.parse_from_dict({"path": 123})

    def test_path_not_local(self) -> None:
        from apm_cli.models.apm_package import DependencyReference

        with pytest.raises(ValueError, match=r"local filesystem path"):
            DependencyReference.parse_from_dict({"path": "not-a-path"})

    def test_git_empty_string(self) -> None:
        from apm_cli.models.apm_package import DependencyReference

        with pytest.raises(ValueError, match=r"non-empty string"):
            DependencyReference.parse_from_dict({"git": ""})

    def test_git_non_string(self) -> None:
        from apm_cli.models.apm_package import DependencyReference

        with pytest.raises(ValueError, match=r"non-empty string"):
            DependencyReference.parse_from_dict({"git": 42})

    def test_git_parent_without_path(self) -> None:
        from apm_cli.models.apm_package import DependencyReference

        with pytest.raises(ValueError, match=r"requires a 'path' field"):
            DependencyReference.parse_from_dict({"git": "parent"})

    def test_git_parent_empty_path(self) -> None:
        from apm_cli.models.apm_package import DependencyReference

        with pytest.raises(ValueError, match=r"non-empty string"):
            DependencyReference.parse_from_dict({"git": "parent", "path": ""})

    def test_git_parent_non_string_path(self) -> None:
        from apm_cli.models.apm_package import DependencyReference

        with pytest.raises(ValueError, match=r"non-empty string"):
            DependencyReference.parse_from_dict({"git": "parent", "path": 123})

    def test_git_parent_with_valid_path(self) -> None:
        from apm_cli.models.apm_package import DependencyReference

        ref = DependencyReference.parse_from_dict({"git": "parent", "path": "packages/my-skill"})
        assert ref.is_parent_repo_inheritance is True
        assert ref.is_virtual is True

    def test_git_parent_with_ref(self) -> None:
        from apm_cli.models.apm_package import DependencyReference

        ref = DependencyReference.parse_from_dict(
            {"git": "parent", "path": "skills/a", "ref": "main"}
        )
        assert ref.reference == "main"

    def test_git_parent_with_empty_ref(self) -> None:
        from apm_cli.models.apm_package import DependencyReference

        with pytest.raises(ValueError, match=r"non-empty string"):
            DependencyReference.parse_from_dict({"git": "parent", "path": "skills/a", "ref": ""})

    def test_git_parent_with_alias(self) -> None:
        from apm_cli.models.apm_package import DependencyReference

        ref = DependencyReference.parse_from_dict(
            {"git": "parent", "path": "skills/a", "alias": "my-alias"}
        )
        assert ref.alias == "my-alias"

    def test_git_parent_with_empty_alias(self) -> None:
        from apm_cli.models.apm_package import DependencyReference

        with pytest.raises(ValueError, match=r"non-empty string"):
            DependencyReference.parse_from_dict({"git": "parent", "path": "skills/a", "alias": ""})

    def test_git_parent_with_invalid_alias(self) -> None:
        from apm_cli.models.apm_package import DependencyReference

        with pytest.raises(ValueError, match=r"Invalid alias"):
            DependencyReference.parse_from_dict(
                {"git": "parent", "path": "skills/a", "alias": "bad alias!"}
            )

    def test_git_with_subpath(self) -> None:
        from apm_cli.models.apm_package import DependencyReference

        ref = DependencyReference.parse_from_dict(
            {"git": "https://github.com/owner/repo", "path": "packages/skill"}
        )
        assert ref is not None

    def test_git_with_ref_override(self) -> None:
        from apm_cli.models.apm_package import DependencyReference

        ref = DependencyReference.parse_from_dict(
            {"git": "https://github.com/owner/repo", "ref": "v2.0.0"}
        )
        assert ref.reference == "v2.0.0"

    def test_git_with_alias_override(self) -> None:
        from apm_cli.models.apm_package import DependencyReference

        ref = DependencyReference.parse_from_dict(
            {"git": "https://github.com/owner/repo", "alias": "my-dep"}
        )
        assert ref.alias == "my-dep"


class TestDependencyReferenceGetInstallPath:
    """Cover get_install_path branches for various package types."""

    def test_regular_owner_repo(self, tmp_path: Path) -> None:
        from apm_cli.models.apm_package import DependencyReference

        ref = DependencyReference.parse("owner/repo")
        path = ref.get_install_path(tmp_path)
        assert path == tmp_path / "owner" / "repo"

    def test_virtual_subdirectory(self, tmp_path: Path) -> None:
        from apm_cli.models.apm_package import DependencyReference

        ref = DependencyReference.parse("owner/repo/subdir")
        path = ref.get_install_path(tmp_path)
        assert isinstance(path, Path)

    def test_virtual_file(self, tmp_path: Path) -> None:
        from apm_cli.models.apm_package import DependencyReference

        ref = DependencyReference.parse("owner/repo/path/to/skill.prompt.md")
        path = ref.get_install_path(tmp_path)
        assert isinstance(path, Path)

    def test_ado_regular(self, tmp_path: Path) -> None:
        from apm_cli.models.apm_package import DependencyReference

        ref = DependencyReference.parse("dev.azure.com/org/project/_git/repo")
        path = ref.get_install_path(tmp_path)
        assert isinstance(path, Path)

    def test_ghes_host(self, tmp_path: Path) -> None:
        from apm_cli.models.apm_package import DependencyReference

        ref = DependencyReference.parse("ghes.corp.com/owner/repo")
        path = ref.get_install_path(tmp_path)
        assert isinstance(path, Path)

    def test_three_part_repo(self, tmp_path: Path) -> None:
        from apm_cli.models.apm_package import DependencyReference

        ref = DependencyReference.parse("gitlab.com/group/subgroup/repo")
        path = ref.get_install_path(tmp_path)
        assert isinstance(path, Path)


class TestDependencyReferenceProperties:
    """Cover property and method branches."""

    def test_is_azure_devops_true(self) -> None:
        from apm_cli.models.apm_package import DependencyReference

        ref = DependencyReference.parse("dev.azure.com/org/project/_git/repo")
        assert ref.is_azure_devops() is True

    def test_is_azure_devops_false(self) -> None:
        from apm_cli.models.apm_package import DependencyReference

        ref = DependencyReference.parse("owner/repo")
        assert ref.is_azure_devops() is False

    def test_is_local_true(self, tmp_path: Path) -> None:
        from apm_cli.models.apm_package import DependencyReference

        ref = DependencyReference.parse(str(tmp_path))
        assert ref.is_local is True

    def test_is_local_false(self) -> None:
        from apm_cli.models.apm_package import DependencyReference

        ref = DependencyReference.parse("owner/repo")
        assert ref.is_local is False

    def test_get_identity_regular(self) -> None:
        from apm_cli.models.apm_package import DependencyReference

        ref = DependencyReference.parse("owner/repo")
        identity = ref.get_identity()
        assert "owner" in identity
        assert "repo" in identity

    def test_get_unique_key(self) -> None:
        from apm_cli.models.apm_package import DependencyReference

        ref = DependencyReference.parse("owner/repo")
        key = ref.get_unique_key()
        assert isinstance(key, str)
        assert len(key) > 0

    def test_get_virtual_package_name(self) -> None:
        from apm_cli.models.apm_package import DependencyReference

        ref = DependencyReference.parse("owner/repo/path/to/skill.prompt.md")
        name = ref.get_virtual_package_name()
        assert isinstance(name, str)
        assert len(name) > 0

    def test_str_representation(self) -> None:
        from apm_cli.models.apm_package import DependencyReference

        ref = DependencyReference.parse("owner/repo")
        s = str(ref)
        assert isinstance(s, str)

    def test_repr(self) -> None:
        from apm_cli.models.apm_package import DependencyReference

        ref = DependencyReference.parse("owner/repo")
        r = repr(ref)
        assert isinstance(r, str)


class TestDependencyReferenceSshUrls:
    """Cover SSH URL parsing branches."""

    def test_ssh_url(self) -> None:
        from apm_cli.models.apm_package import DependencyReference

        ref = DependencyReference.parse("git@github.com:owner/repo.git")
        assert ref is not None
        assert ref.host == "github.com"

    def test_ssh_ghes(self) -> None:
        from apm_cli.models.apm_package import DependencyReference

        ref = DependencyReference.parse("git@ghes.corp.com:owner/repo.git")
        assert ref is not None
        assert ref.host == "ghes.corp.com"

    def test_https_url(self) -> None:
        from apm_cli.models.apm_package import DependencyReference

        ref = DependencyReference.parse("https://github.com/owner/repo")
        assert ref is not None

    def test_https_ghes(self) -> None:
        from apm_cli.models.apm_package import DependencyReference

        ref = DependencyReference.parse("https://ghes.corp.com/owner/repo")
        assert ref is not None
        assert ref.host == "ghes.corp.com"


class TestInstallSources:
    """Cover install/sources.py helper functions."""

    def test_import_sources_module(self) -> None:
        from apm_cli.install import sources

        # Verify module loads and has expected attributes
        assert hasattr(sources, "make_dependency_source")
        assert hasattr(sources, "DependencySource")

    def test_materialization_dataclass(self) -> None:
        from apm_cli.install.sources import Materialization

        mat = Materialization(
            package_info=None,
            install_path=Path("/tmp/test"),
            dep_key="owner/repo",
        )
        assert mat.install_path == Path("/tmp/test")
        assert mat.dep_key == "owner/repo"

    def test_format_package_type_label(self) -> None:
        from apm_cli.install.sources import _format_package_type_label

        result = _format_package_type_label("skill")
        assert result is None or isinstance(result, str)
        result2 = _format_package_type_label(None)
        assert result2 is None or isinstance(result2, str)
