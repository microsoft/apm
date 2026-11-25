"""Unit tests for ``git: parent`` expansion in ``APMDependencyResolver``."""

import pytest

from apm_cli.deps.apm_resolver import APMDependencyResolver
from apm_cli.models.dependency import DependencyReference

pytestmark = pytest.mark.unit


class TestExpandParentRepoDecl:
    def test_expansion_matches_explicit_gitlab_class_host(self):
        parent = DependencyReference.parse_from_dict(
            {
                "git": "https://git.example.com/org/monorepo.git",
                "path": "agents/pkg-a",
                "ref": "main",
            }
        )
        child = DependencyReference.parse_from_dict(
            {"git": "parent", "path": "skills/shared"},
        )
        r = APMDependencyResolver()
        expanded = r.expand_parent_repo_decl(parent, child)

        explicit = DependencyReference.parse_from_dict(
            {
                "git": "https://git.example.com/org/monorepo.git",
                "path": "skills/shared",
                "ref": "main",
            }
        )
        assert expanded.get_unique_key() == explicit.get_unique_key()
        assert expanded.host == "git.example.com"
        assert expanded.repo_url == "org/monorepo"
        assert expanded.virtual_path == "skills/shared"
        assert expanded.reference == "main"
        assert expanded.is_virtual is True
        assert expanded.is_parent_repo_inheritance is False

    def test_expansion_matches_explicit_github_default_host(self):
        parent = DependencyReference.parse("owner/monorepo#main")
        child = DependencyReference.parse_from_dict(
            {"git": "parent", "path": "skills/shared"},
        )
        expanded = APMDependencyResolver().expand_parent_repo_decl(parent, child)
        explicit = DependencyReference.parse_from_dict(
            {
                "git": "https://github.com/owner/monorepo.git",
                "path": "skills/shared",
                "ref": "main",
            }
        )
        assert expanded.get_unique_key() == explicit.get_unique_key()

    def test_expansion_azure_devops_parent(self):
        parent = DependencyReference.parse_from_dict(
            {
                "git": "https://dev.azure.com/myorg/myproject/_git/myrepo",
                "path": "agents/a",
                "ref": "main",
            }
        )
        child = DependencyReference.parse_from_dict(
            {"git": "parent", "path": "tools/shared"},
        )
        expanded = APMDependencyResolver().expand_parent_repo_decl(parent, child)
        assert expanded.repo_url == "myorg/myproject/myrepo"
        assert expanded.virtual_path == "tools/shared"
        assert expanded.is_azure_devops()
        explicit = DependencyReference.parse_from_dict(
            {
                "git": "https://dev.azure.com/myorg/myproject/_git/myrepo",
                "path": "tools/shared",
                "ref": "main",
            }
        )
        assert expanded.get_unique_key() == explicit.get_unique_key()

    def test_ref_inherited_from_parent_when_child_omits_ref(self):
        parent = DependencyReference.parse("org/repo#release")
        child = DependencyReference.parse_from_dict(
            {"git": "parent", "path": "pkg/sub"},
        )
        expanded = APMDependencyResolver().expand_parent_repo_decl(parent, child)
        assert expanded.reference == "release"

    def test_ref_override_from_child_decl(self):
        parent = DependencyReference.parse("org/repo#main")
        child = DependencyReference.parse_from_dict(
            {
                "git": "parent",
                "path": "skills/shared",
                "ref": "v1.0.0",
            }
        )
        expanded = APMDependencyResolver().expand_parent_repo_decl(parent, child)
        assert expanded.reference == "v1.0.0"

    def test_parent_without_ref_child_without_ref(self):
        parent = DependencyReference.parse("org/repo")
        child = DependencyReference.parse_from_dict(
            {"git": "parent", "path": "x/y"},
        )
        expanded = APMDependencyResolver().expand_parent_repo_decl(parent, child)
        assert expanded.reference is None

    def test_alias_preserved_on_child(self):
        parent = DependencyReference.parse("org/repo#main")
        child = DependencyReference.parse_from_dict(
            {
                "git": "parent",
                "path": "skills/z",
                "alias": "z-alias",
            }
        )
        expanded = APMDependencyResolver().expand_parent_repo_decl(parent, child)
        assert expanded.alias == "z-alias"

    def test_rejects_non_parent_child(self):
        parent = DependencyReference.parse("org/repo")
        child = DependencyReference.parse("other/dep")
        with pytest.raises(ValueError, match="is_parent_repo_inheritance"):
            APMDependencyResolver().expand_parent_repo_decl(parent, child)

    def test_rejects_local_parent(self):
        parent = DependencyReference.parse("./packages/foo")
        child = DependencyReference.parse_from_dict(
            {"git": "parent", "path": "skills/shared"},
        )
        with pytest.raises(ValueError, match="local path"):
            APMDependencyResolver().expand_parent_repo_decl(parent, child)

    def test_rejects_local_repo_url_prefix(self):
        parent = DependencyReference(
            repo_url="_local/foo",
            is_local=False,
            host="github.com",
        )
        child = DependencyReference.parse_from_dict(
            {"git": "parent", "path": "skills/shared"},
        )
        with pytest.raises(ValueError, match="local path"):
            APMDependencyResolver().expand_parent_repo_decl(parent, child)

    def test_rejects_non_git_parent_missing_repo_shape(self):
        parent = DependencyReference(repo_url="onlyone", host="github.com")
        child = DependencyReference.parse_from_dict(
            {"git": "parent", "path": "skills/shared"},
        )
        with pytest.raises(ValueError, match="remote Git parent"):
            APMDependencyResolver().expand_parent_repo_decl(parent, child)

    def test_rejects_ado_parent_incomplete_repo_url(self):
        parent = DependencyReference(
            repo_url="org/project",
            host="dev.azure.com",
            ado_organization="org",
            ado_project="project",
            ado_repo=None,
        )
        child = DependencyReference.parse_from_dict(
            {"git": "parent", "path": "skills/shared"},
        )
        with pytest.raises(ValueError, match="remote Git parent"):
            APMDependencyResolver().expand_parent_repo_decl(parent, child)
