"""Tests for DependencyTree node lookup with pinned references (#1846)."""

from __future__ import annotations

from apm_cli.deps.dependency_graph import DependencyNode, DependencyTree
from apm_cli.models.apm_package import APMPackage, DependencyReference


def _make_node(repo_url: str, reference: str | None = None, depth: int = 1, parent=None):
    dep_ref = DependencyReference(repo_url=repo_url, reference=reference)
    pkg = APMPackage(name=repo_url.split("/")[-1], version="0.0.0")
    return DependencyNode(package=pkg, dependency_ref=dep_ref, depth=depth, parent=parent)


class TestDependencyTreeGetNode:
    """get_node() must find nodes regardless of whether they have a pinned ref."""

    def test_get_node_without_reference(self):
        root = APMPackage(name="root", version="1.0.0")
        tree = DependencyTree(root_package=root)

        node = _make_node("owner/repo")
        tree.add_node(node)

        assert tree.get_node("owner/repo") is node

    def test_get_node_with_pinned_reference(self):
        """Nodes stored with get_id() = 'repo#sha' must be found by unique_key = 'repo'."""
        root = APMPackage(name="root", version="1.0.0")
        tree = DependencyTree(root_package=root)

        node = _make_node("prisma/skills", reference="0b8e83cddde30b3e028fb4e0f6770948c6160e08")
        tree.add_node(node)

        # get_id() includes the reference
        assert node.get_id() == "prisma/skills#0b8e83cddde30b3e028fb4e0f6770948c6160e08"
        # get_node() should still find it by unique_key (without reference)
        assert tree.get_node("prisma/skills") is node

    def test_get_node_transitive_with_parent(self):
        """Transitive dep with ref: parent is accessible via get_node() lookup."""
        root = APMPackage(name="root", version="1.0.0")
        tree = DependencyTree(root_package=root)

        parent_node = _make_node("_local/agent-config", depth=1)
        tree.add_node(parent_node)

        child_node = _make_node(
            "prisma/skills",
            reference="0b8e83cddde30b3e028fb4e0f6770948c6160e08",
            depth=2,
            parent=parent_node,
        )
        tree.add_node(child_node)

        found = tree.get_node("prisma/skills")
        assert found is not None
        assert found.parent is parent_node
        assert found.parent.dependency_ref.repo_url == "_local/agent-config"
