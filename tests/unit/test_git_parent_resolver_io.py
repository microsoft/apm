"""Unit tests for ``git: parent`` expansion in ``APMDependencyResolver`` (filesystem, process and CLI boundary cases)."""

import pytest
import yaml

from apm_cli.deps.apm_resolver import APMDependencyResolver
from apm_cli.models.dependency import DependencyReference

pytestmark = [
    pytest.mark.component,
    pytest.mark.promise("P-OWN-dependency-identity-materialization"),
]


@pytest.fixture
def resolver(tmp_path):
    return APMDependencyResolver(apm_modules_dir=tmp_path / "apm_modules")


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
