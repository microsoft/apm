"""Unit tests for ``git: parent`` expansion in ``APMDependencyResolver``."""

import pytest
import yaml

from apm_cli.deps.apm_resolver import APMDependencyResolver
from apm_cli.models.dependency import DependencyReference


@pytest.fixture
def resolver(tmp_path):
    return APMDependencyResolver(apm_modules_dir=tmp_path / "apm_modules")


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


class TestBuildDependencyTreeGitParent:
    def test_root_manifest_git_parent_raises(self, tmp_path):
        (tmp_path / "apm.yml").write_text(
            yaml.dump(
                {
                    "name": "root",
                    "version": "1.0.0",
                    "dependencies": {
                        "apm": [
                            {"git": "parent", "path": "skills/shared"},
                        ]
                    },
                }
            )
        )
        resolver = APMDependencyResolver(apm_modules_dir=tmp_path / "apm_modules")
        with pytest.raises(ValueError, match="transitive dependencies"):
            resolver.build_dependency_tree(tmp_path / "apm.yml")

    def test_resolve_root_git_parent_propagates_error(self, tmp_path):
        (tmp_path / "apm.yml").write_text(
            yaml.dump(
                {
                    "name": "root",
                    "version": "1.0.0",
                    "dependencies": {
                        "apm": [{"git": "parent", "path": "skills/shared"}],
                    },
                }
            )
        )
        resolver = APMDependencyResolver(apm_modules_dir=tmp_path / "apm_modules")
        with pytest.raises(ValueError, match="transitive dependencies"):
            resolver.resolve_dependencies(tmp_path)

    def test_transitive_expands_before_enqueue_and_tree_node_key(self, tmp_path, resolver):
        (tmp_path / "apm.yml").write_text(
            yaml.dump(
                {
                    "name": "root",
                    "version": "1.0.0",
                    "dependencies": {
                        "apm": [
                            {
                                "git": "https://git.example.com/org/monorepo.git",
                                "path": "agents/pkg-a",
                                "ref": "main",
                            }
                        ]
                    },
                }
            )
        )
        pkg_dir = tmp_path / "apm_modules" / "org" / "monorepo" / "agents" / "pkg-a"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "apm.yml").write_text(
            yaml.dump(
                {
                    "name": "pkg-a",
                    "version": "1.0.0",
                    "dependencies": {
                        "apm": [
                            {"git": "parent", "path": "skills/shared"},
                        ]
                    },
                }
            )
        )

        tree = resolver.build_dependency_tree(tmp_path / "apm.yml")
        explicit = DependencyReference.parse_from_dict(
            {
                "git": "https://git.example.com/org/monorepo.git",
                "path": "skills/shared",
                "ref": "main",
            }
        )
        # Tree indexes nodes by DependencyNode.get_id() = unique_key#ref when ref set
        node_id = (
            f"{explicit.get_unique_key()}#{explicit.reference}"
            if explicit.reference
            else explicit.get_unique_key()
        )
        node = tree.get_node(node_id)
        assert node is not None
        assert node.dependency_ref.is_parent_repo_inheritance is False
        assert node.dependency_ref.virtual_path == "skills/shared"
        assert node.dependency_ref.repo_url == "org/monorepo"
        assert "_parent" not in node.dependency_ref.get_unique_key()

    def test_transitive_local_parent_raises_when_loading_subdeps(self, tmp_path, resolver):
        (tmp_path / "apm.yml").write_text(
            yaml.dump(
                {
                    "name": "root",
                    "version": "1.0.0",
                    "dependencies": {"apm": ["./vendor/pkg"]},
                }
            )
        )
        local_mod = tmp_path / "apm_modules" / "_local" / "pkg"
        local_mod.mkdir(parents=True)
        (local_mod / "apm.yml").write_text(
            yaml.dump(
                {
                    "name": "pkg",
                    "version": "1.0.0",
                    "dependencies": {
                        "apm": [{"git": "parent", "path": "skills/shared"}],
                    },
                }
            )
        )
        with pytest.raises(ValueError, match="local path"):
            resolver.build_dependency_tree(tmp_path / "apm.yml")
