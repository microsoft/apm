"""Integration tests for apm deps command CLI coverage."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from apm_cli.commands.deps.cli import (
    _add_tree_children,
    _dep_display_name,
    _deps_list_source_label,
    _format_primitive_counts,
)


class TestFormatPrimitiveCounts:
    """Tests for _format_primitive_counts helper."""

    def test_format_single_primitive(self):
        """Format single primitive type count."""
        primitives = {"skills": 1}

        result = _format_primitive_counts(primitives)

        assert result == "1 skills"

    def test_format_multiple_primitives(self):
        """Format multiple primitive type counts."""
        primitives = {"skills": 2, "agents": 1, "workflows": 3}

        result = _format_primitive_counts(primitives)

        # Result should contain all non-zero counts
        assert "2 skills" in result
        assert "1 agents" in result
        assert "3 workflows" in result

    def test_format_skips_zero_counts(self):
        """Zero counts are not included in output."""
        primitives = {"skills": 0, "agents": 1}

        result = _format_primitive_counts(primitives)

        assert "skills" not in result
        assert "1 agents" in result

    def test_format_all_zero_counts(self):
        """All zero counts returns empty string."""
        primitives = {"skills": 0, "agents": 0}

        result = _format_primitive_counts(primitives)

        assert result == ""

    def test_format_empty_dict(self):
        """Empty dict returns empty string."""
        result = _format_primitive_counts({})

        assert result == ""

    def test_format_comma_separated(self):
        """Multiple counts are comma-separated."""
        primitives = {"skills": 1, "prompts": 2, "workflows": 1}

        result = _format_primitive_counts(primitives)

        # Should have commas between items
        parts = result.split(", ")
        assert len(parts) == 3


class TestDepsListSourceLabel:
    """Tests for _deps_list_source_label helper."""

    def test_label_for_local_flag(self):
        """is_local=True returns 'local'."""
        result = _deps_list_source_label(None, is_local=True)

        assert result == "local"

    def test_label_for_lockfile_source_local(self):
        """lockfile_source='local' returns 'local'."""
        result = _deps_list_source_label(None, lockfile_source="local")

        assert result == "local"

    def test_label_for_github_host(self):
        """GitHub hostname returns 'github'."""
        result = _deps_list_source_label("github.com")

        assert result == "github"

    def test_label_for_azure_devops_host(self):
        """Azure DevOps hostname returns 'azure-devops'."""
        with patch("apm_cli.utils.github_host.is_azure_devops_hostname") as mock_check:
            mock_check.return_value = True

            result = _deps_list_source_label("dev.azure.com")

            assert result == "azure-devops"

    def test_label_for_gitlab_host(self):
        """GitLab hostname returns 'gitlab'."""
        with patch("apm_cli.utils.github_host.is_gitlab_hostname") as mock_check:
            with patch("apm_cli.utils.github_host.is_azure_devops_hostname") as mock_ado:
                mock_ado.return_value = False
                mock_check.return_value = True

                result = _deps_list_source_label("gitlab.com")

                assert result == "gitlab"

    def test_label_default_github(self):
        """Unknown host defaults to 'github'."""
        result = _deps_list_source_label(None)

        assert result == "github"

    def test_label_is_local_takes_precedence(self):
        """is_local=True takes precedence over host."""
        result = _deps_list_source_label("dev.azure.com", is_local=True)

        assert result == "local"


class TestDepDisplayName:
    """Tests for _dep_display_name helper."""

    def test_display_name_with_version(self):
        """Display with explicit version."""
        mock_dep = MagicMock()
        mock_dep.get_unique_key.return_value = "owner/repo"
        mock_dep.version = "1.0.0"
        mock_dep.resolved_commit = None
        mock_dep.resolved_ref = None

        result = _dep_display_name(mock_dep)

        assert result == "owner/repo@1.0.0"

    def test_display_name_with_commit_short_hash(self):
        """Display with commit SHA (short 7-char hash)."""
        mock_dep = MagicMock()
        mock_dep.get_unique_key.return_value = "owner/repo"
        mock_dep.version = None
        mock_dep.resolved_commit = "abc123def456789"
        mock_dep.resolved_ref = None

        result = _dep_display_name(mock_dep)

        assert result == "owner/repo@abc123d"

    def test_display_name_with_resolved_ref(self):
        """Display with resolved_ref when no version/commit."""
        mock_dep = MagicMock()
        mock_dep.get_unique_key.return_value = "owner/repo"
        mock_dep.version = None
        mock_dep.resolved_commit = None
        mock_dep.resolved_ref = "main"

        result = _dep_display_name(mock_dep)

        assert result == "owner/repo@main"

    def test_display_name_fallback_latest(self):
        """Display falls back to 'latest' when no info available."""
        mock_dep = MagicMock()
        mock_dep.get_unique_key.return_value = "owner/repo"
        mock_dep.version = None
        mock_dep.resolved_commit = None
        mock_dep.resolved_ref = None

        result = _dep_display_name(mock_dep)

        assert result == "owner/repo@latest"

    def test_display_name_version_takes_precedence(self):
        """Version takes precedence over commit."""
        mock_dep = MagicMock()
        mock_dep.get_unique_key.return_value = "owner/repo"
        mock_dep.version = "1.0.0"
        mock_dep.resolved_commit = "abc123def456789"
        mock_dep.resolved_ref = "main"

        result = _dep_display_name(mock_dep)

        assert result == "owner/repo@1.0.0"


class TestAddTreeChildren:
    """Tests for _add_tree_children helper."""

    def test_add_children_no_rich(self):
        """Add children without Rich console."""
        parent_branch = MagicMock()
        parent_repo_url = "https://github.com/owner/parent"
        children_map = {}

        _add_tree_children(parent_branch, parent_repo_url, children_map, False)

        # No children, so parent_branch.add should not be called
        parent_branch.add.assert_not_called()

    def test_add_single_child(self):
        """Add single child to parent."""
        parent_branch = MagicMock()
        child_branch = MagicMock()
        parent_branch.add.return_value = child_branch

        parent_repo_url = "https://github.com/owner/parent"

        mock_child = MagicMock()
        mock_child.get_unique_key.return_value = "owner/child"
        mock_child.version = "1.0.0"
        mock_child.resolved_commit = None
        mock_child.resolved_ref = None
        mock_child.repo_url = "https://github.com/owner/child"

        children_map = {parent_repo_url: [mock_child]}

        _add_tree_children(parent_branch, parent_repo_url, children_map, True)

        # Parent should have added child
        parent_branch.add.assert_called_once()

    def test_add_multiple_children(self):
        """Add multiple children to parent."""
        parent_branch = MagicMock()
        child_branch1 = MagicMock()
        child_branch2 = MagicMock()
        parent_branch.add.side_effect = [child_branch1, child_branch2]

        parent_repo_url = "https://github.com/owner/parent"

        mock_child1 = MagicMock()
        mock_child1.get_unique_key.return_value = "owner/child1"
        mock_child1.version = "1.0.0"
        mock_child1.resolved_commit = None
        mock_child1.resolved_ref = None
        mock_child1.repo_url = "https://github.com/owner/child1"

        mock_child2 = MagicMock()
        mock_child2.get_unique_key.return_value = "owner/child2"
        mock_child2.version = "1.1.0"
        mock_child2.resolved_commit = None
        mock_child2.resolved_ref = None
        mock_child2.repo_url = "https://github.com/owner/child2"

        children_map = {parent_repo_url: [mock_child1, mock_child2]}

        _add_tree_children(parent_branch, parent_repo_url, children_map, True)

        # Parent should have added both children
        assert parent_branch.add.call_count == 2

    def test_add_children_stops_at_cycle(self):
        """Recursive rendering stops when a dependency repeats an ancestor."""
        parent_branch = MagicMock()
        child_branch = MagicMock()
        parent_branch.add.return_value = child_branch
        parent_repo_url = "https://github.com/owner/parent"

        mock_child = MagicMock()
        mock_child.get_unique_key.return_value = "owner/child"
        mock_child.version = "1.0.0"
        mock_child.resolved_commit = None
        mock_child.resolved_ref = None
        mock_child.repo_url = "https://github.com/owner/child"

        repeated_parent = MagicMock()
        repeated_parent.get_unique_key.return_value = parent_repo_url
        repeated_parent.version = "1.0.0"
        repeated_parent.resolved_commit = None
        repeated_parent.resolved_ref = None
        repeated_parent.repo_url = parent_repo_url
        children_map = {
            parent_repo_url: [mock_child],
            "owner/child": [repeated_parent],
        }

        _add_tree_children(parent_branch, parent_repo_url, children_map, True)

        parent_branch.add.assert_called_once()
        child_branch.add.assert_not_called()

    def test_add_children_with_no_children_map_entry(self):
        """No children for parent means nothing is added."""
        parent_branch = MagicMock()
        parent_repo_url = "https://github.com/owner/parent"
        children_map = {}

        _add_tree_children(parent_branch, parent_repo_url, children_map, True)

        parent_branch.add.assert_not_called()


class TestDepsCommand:
    """Tests for deps command structure."""

    def test_deps_list_source_label_logic(self):
        """Test source label detection logic."""
        test_cases = [
            (None, {"is_local": True}, "local"),
            (None, {"lockfile_source": "local"}, "local"),
            ("github.com", {}, "github"),
            ("dev.azure.com", {}, None),  # Depends on mock
            ("gitlab.com", {}, None),  # Depends on mock
        ]

        for host, kwargs, expected in test_cases:
            if expected is not None:
                result = _deps_list_source_label(host, **kwargs)
                assert result == expected

    def test_deps_cli_module_imports(self):
        """Deps CLI module imports required components."""
        from apm_cli.commands.deps import cli

        assert hasattr(cli, "_format_primitive_counts")
        assert hasattr(cli, "_deps_list_source_label")
        assert hasattr(cli, "_dep_display_name")
        assert hasattr(cli, "_add_tree_children")


class TestDepsIntegration:
    """Integration tests for deps command."""

    def test_format_and_display_workflow(self):
        """Integration of formatting and display."""
        primitives = {"skills": 2, "prompts": 1, "agents": 0}

        formatted = _format_primitive_counts(primitives)

        assert "2 skills" in formatted
        assert "1 prompts" in formatted
        assert "agents" not in formatted

    def test_source_label_detection_workflow(self):
        """Integration of source label detection."""
        # Test local
        label = _deps_list_source_label(None, is_local=True)
        assert label == "local"

        # Test lockfile source
        label = _deps_list_source_label(None, lockfile_source="local")
        assert label == "local"

        # Test default
        label = _deps_list_source_label(None)
        assert label == "github"

    def test_dependency_display_with_fallback(self):
        """Integration of dep display with fallback logic."""
        # Create mock dependencies with different info levels
        mock_dep_with_version = MagicMock()
        mock_dep_with_version.get_unique_key.return_value = "owner/repo"
        mock_dep_with_version.version = "1.0.0"
        mock_dep_with_version.resolved_commit = None
        mock_dep_with_version.resolved_ref = None

        result = _dep_display_name(mock_dep_with_version)
        assert "1.0.0" in result

        # Create mock with commit but no version
        mock_dep_with_commit = MagicMock()
        mock_dep_with_commit.get_unique_key.return_value = "owner/repo"
        mock_dep_with_commit.version = None
        mock_dep_with_commit.resolved_commit = "abc123def456"
        mock_dep_with_commit.resolved_ref = None

        result = _dep_display_name(mock_dep_with_commit)
        assert "abc123d" in result
