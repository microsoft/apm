"""Tests for pure helper functions in ``apm_cli.commands.deps.cli``.

Covers:
- ``_format_primitive_counts``
- ``_dep_display_name``
- ``_deps_list_source_label``
- ``_add_tree_children`` / ``_echo_tree_children``
- ``_show_scope_deps``
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.commands.deps.cli import (
    _add_tree_children,
    _dep_display_name,
    _deps_list_source_label,
    _echo_tree_children,
    _format_primitive_counts,
    _show_scope_deps,
)
from apm_cli.deps.lockfile import LockedDependency
from apm_cli.models.dependency.reference import DependencyReference

pytestmark = pytest.mark.unit


class _RecordingBranch:
    def __init__(self, labels: list[str]) -> None:
        self.labels = labels

    def add(self, label: str) -> _RecordingBranch:
        self.labels.append(label)
        return _RecordingBranch(self.labels)


def _make_pkg_dict(
    name: str = "owner/repo",
    version: str = "1.0.0",
    source: str = "github",
    is_orphaned: bool = False,
    is_insecure: bool = False,
) -> dict:
    return {
        "name": name,
        "version": version,
        "source": source,
        "primitives": {"prompts": 0, "instructions": 0, "agents": 0, "skills": 0, "hooks": 0},
        "path": f"/fake/{name}",
        "is_orphaned": is_orphaned,
        "is_insecure": is_insecure,
        "insecure_via": "direct",
    }


# ---------------------------------------------------------------------------
# _format_primitive_counts
# ---------------------------------------------------------------------------


class TestFormatPrimitiveCounts:
    def test_empty_dict_returns_empty_string(self):
        assert _format_primitive_counts({}) == ""

    def test_single_nonzero_entry(self):
        assert _format_primitive_counts({"skills": 3}) == "3 skills"

    def test_zero_entries_excluded(self):
        assert _format_primitive_counts({"skills": 0, "agents": 0}) == ""

    def test_mixed_zero_and_nonzero(self):
        result = _format_primitive_counts({"skills": 2, "agents": 0, "instructions": 1})
        assert "2 skills" in result
        assert "1 instructions" in result
        assert "agents" not in result

    def test_multiple_nonzero_comma_separated(self):
        result = _format_primitive_counts({"skills": 1, "agents": 2})
        assert "1 skills" in result
        assert "2 agents" in result
        assert "," in result


# ---------------------------------------------------------------------------
# _dep_display_name
# ---------------------------------------------------------------------------


def _make_dep(
    key: str = "owner/repo",
    version: str | None = None,
    resolved_commit: str | None = None,
    resolved_ref: str | None = None,
    repo_url: str | None = None,
) -> LockedDependency:
    return LockedDependency(
        repo_url=repo_url or key,
        version=version,
        resolved_commit=resolved_commit,
        resolved_ref=resolved_ref,
    )


class TestDepDisplayName:
    def test_uses_version_when_present(self):
        dep = _make_dep(version="1.2.3")
        assert _dep_display_name(dep) == "owner/repo@1.2.3"

    def test_falls_back_to_short_commit(self):
        dep = _make_dep(resolved_commit="abcdef1234567")
        assert _dep_display_name(dep) == "owner/repo@abcdef1"

    def test_falls_back_to_resolved_ref(self):
        dep = _make_dep(resolved_ref="main")
        assert _dep_display_name(dep) == "owner/repo@main"

    def test_falls_back_to_latest(self):
        dep = _make_dep()
        assert _dep_display_name(dep) == "owner/repo@latest"

    def test_version_takes_priority_over_commit(self):
        dep = _make_dep(version="2.0.0", resolved_commit="abc1234")
        assert _dep_display_name(dep) == "owner/repo@2.0.0"


def _make_local_dep(local_path=None, repo_url="_local/pkg", version=None):
    """Local dep whose unique key would leak an absolute host slot."""
    dep = LockedDependency(
        repo_url=repo_url,
        source="local",
        local_path=local_path,
        anchored_local_path="/abs/host/path/pkg",
        version=version,
    )
    # The anchored unique key is an absolute ``local:/...`` slot the display
    # must never surface for a local dep.
    dep.get_unique_key = MagicMock(wraps=dep.get_unique_key)
    return dep


class TestDepDisplayNameLocal:
    def test_local_dep_uses_local_path_when_present(self):
        dep = _make_local_dep(local_path="../pkg-depth-2")
        result = _dep_display_name(dep)
        assert result == "../pkg-depth-2@latest"
        assert "local:/" not in result
        dep.get_unique_key.assert_not_called()

    def test_local_dep_falls_back_to_logical_repo_url_when_path_missing(self):
        dep = _make_local_dep(local_path=None, repo_url="_local/pkg-depth-2")
        result = _dep_display_name(dep)
        assert result == "_local/pkg-depth-2@latest"
        assert "local:/" not in result
        dep.get_unique_key.assert_not_called()

    def test_local_dep_with_posix_absolute_path_renders_logical_repo_url(self):
        """A POSIX-absolute declared path must never leak the host filesystem."""
        dep = _make_local_dep(local_path="/Users/alice/pkg", repo_url="_local/pkg")
        result = _dep_display_name(dep)
        assert result == "_local/pkg@latest"
        assert "/Users/" not in result
        assert result.startswith("_local/")

    def test_local_dep_with_windows_absolute_path_renders_logical_repo_url(self):
        """A Windows-absolute declared path must never leak the host filesystem."""
        dep = _make_local_dep(local_path=r"C:\Users\alice\pkg", repo_url="_local/pkg")
        result = _dep_display_name(dep)
        assert result == "_local/pkg@latest"
        assert "C:" not in result
        assert "\\" not in result

    def test_parsed_windows_absolute_path_renders_cross_platform_logical_name(self):
        """Display derives a safe name even when parsed on a non-Windows host."""
        dep_ref = DependencyReference.parse(r"C:\Users\alice\pkg")
        dep = LockedDependency.from_dependency_ref(
            dep_ref,
            resolved_commit=None,
            depth=0,
            resolved_by=None,
        )

        result = _dep_display_name(dep)

        assert result == "_local/pkg@latest"
        assert "C:" not in result
        assert "\\" not in result

    def test_local_dep_with_home_prefixed_path_renders_logical_repo_url(self):
        """A ``~``-prefixed declared path embeds home structure; render logical."""
        dep = _make_local_dep(local_path="~/pkg", repo_url="_local/pkg")
        result = _dep_display_name(dep)
        assert result == "_local/pkg@latest"
        assert "~" not in result


# ---------------------------------------------------------------------------
# _resolve_scope_deps - filesystem paths
# ---------------------------------------------------------------------------


def _make_logger() -> MagicMock:
    logger = MagicMock()
    logger.verbose = False
    logger.verbose_detail = MagicMock()
    logger.progress = MagicMock()
    logger.warning = MagicMock()
    logger.error = MagicMock()
    logger.success = MagicMock()
    logger.info = MagicMock()
    return logger


# ---------------------------------------------------------------------------
# _resolve_scope_deps - hash-slot / read-failure leak guards
# ---------------------------------------------------------------------------


class TestDepsListSourceLabel:
    @pytest.mark.parametrize("kwargs", [{"is_local": True}, {"lockfile_source": "local"}])
    def test_local_source_returns_local(self, kwargs: dict) -> None:
        assert _deps_list_source_label(None, **kwargs) == "local"

    def test_azure_devops_hostname_returns_azure_devops(self) -> None:
        with patch("apm_cli.utils.github_host.is_azure_devops_hostname", return_value=True):
            result = _deps_list_source_label("dev.azure.com")
        assert result == "azure-devops"

    def test_gitlab_hostname_returns_gitlab(self) -> None:
        with patch("apm_cli.utils.github_host.is_azure_devops_hostname", return_value=False):
            with patch("apm_cli.utils.github_host.is_gitlab_hostname", return_value=True):
                result = _deps_list_source_label("gitlab.com")
        assert result == "gitlab"

    def test_unknown_host_returns_github(self) -> None:
        with patch("apm_cli.utils.github_host.is_azure_devops_hostname", return_value=False):
            with patch("apm_cli.utils.github_host.is_gitlab_hostname", return_value=False):
                result = _deps_list_source_label("github.com")
        assert result == "github"

    def test_none_host_returns_github(self) -> None:
        with patch("apm_cli.utils.github_host.is_azure_devops_hostname", return_value=False):
            with patch("apm_cli.utils.github_host.is_gitlab_hostname", return_value=False):
                result = _deps_list_source_label(None)
        assert result == "github"


class TestAddTreeChildren:
    def test_no_children_does_nothing(self) -> None:
        parent = MagicMock()
        _add_tree_children(parent, "owner/repo", {})
        parent.add.assert_not_called()

    def test_adds_child_dep_names(self) -> None:
        """The Rich parent branch receives each child."""
        child = _make_dep("child/repo", version="1.0.0", repo_url="child/repo")
        parent = MagicMock()
        child_branch = MagicMock()
        parent.add.return_value = child_branch
        children_map = {"owner/repo": [child]}
        _add_tree_children(parent, "owner/repo", children_map)
        parent.add.assert_called_once()

    def test_cycle_guard_marks_repeated_ancestor(self) -> None:
        """A dependency cycle is marked at the first repeated ancestor."""
        child = _make_dep("child/repo", version="1.0.0", repo_url="child/repo")
        repeated_parent = _make_dep("owner/repo", version="1.0.0", repo_url="owner/repo")
        children_map = {
            "owner/repo": [child],
            "child/repo": [repeated_parent],
        }
        parent = MagicMock()
        child_branch = MagicMock()
        parent.add.return_value = child_branch

        _add_tree_children(parent, "owner/repo", children_map)

        parent.add.assert_called_once()
        child_branch.add.assert_called_once()
        assert "(circular)" in child_branch.add.call_args.args[0]

    def test_renders_chain_beyond_python_recursion_limit(self) -> None:
        """A valid lock graph is rendered without a call-stack depth limit."""
        chain_length = 1100
        children_map = {
            f"pkg-{index}": [_make_dep(f"pkg-{index + 1}", version="1.0.0")]
            for index in range(chain_length)
        }
        labels: list[str] = []

        _add_tree_children(
            _RecordingBranch(labels),
            "pkg-0",
            children_map,
        )

        assert len(labels) == chain_length
        assert "pkg-1100@1.0.0" in labels[-1]

    def test_has_rich_uses_rich_markup(self) -> None:
        child = _make_dep("child/repo", version="1.0.0", repo_url="child/repo")
        parent = MagicMock()
        child_branch = MagicMock()
        parent.add.return_value = child_branch
        children_map = {"owner/repo": [child]}
        _add_tree_children(parent, "owner/repo", children_map)
        call_args = parent.add.call_args[0][0]
        assert "[dim]" in call_args


class TestEchoTreeChildren:
    def test_cycle_is_visible_and_stops_traversal(self) -> None:
        """The text fallback marks a repeated ancestor without looping."""
        child = _make_dep("child/repo", version="1.0.0")
        repeated_parent = _make_dep("owner/repo", version="1.0.0")
        children_map = {
            "owner/repo": [child],
            "child/repo": [repeated_parent],
        }

        with patch("apm_cli.commands.deps.cli.click.echo") as echo:
            _echo_tree_children("owner/repo", children_map)

        assert echo.call_count == 2
        assert "(circular)" in echo.call_args_list[-1].args[0]

    def test_nested_children_use_ascii_tree_prefixes(self) -> None:
        """The text fallback preserves nesting with portable ASCII prefixes."""
        child = _make_dep("child/repo", version="1.0.0")
        grandchild = _make_dep("grandchild/repo", version="1.0.0")
        children_map = {
            "owner/repo": [child],
            "child/repo": [grandchild],
        }

        with patch("apm_cli.commands.deps.cli.click.echo") as echo:
            _echo_tree_children("owner/repo", children_map)

        assert [call.args[0] for call in echo.call_args_list] == [
            "+-- child/repo@1.0.0",
            "    +-- grandchild/repo@1.0.0",
        ]


class TestShowScopeDeps:
    def test_no_modules_logs_no_deps(self) -> None:
        logger = _make_logger()
        with patch("apm_cli.commands.deps.cli._resolve_scope_deps", return_value=(None, None)):
            _show_scope_deps("Project", Path("/fake"), logger, None, False)
        logger.progress.assert_called_once()
        assert "No APM dependencies" in logger.progress.call_args[0][0]

    def test_empty_packages_no_insecure_logs_empty_message(self) -> None:
        logger = _make_logger()
        with patch("apm_cli.commands.deps.cli._resolve_scope_deps", return_value=([], [])):
            _show_scope_deps("Project", Path("/fake"), logger, None, False)
        logger.progress.assert_called_once()
        assert "no valid packages" in logger.progress.call_args[0][0]

    def test_empty_insecure_packages_logs_insecure_message(self) -> None:
        logger = _make_logger()
        with patch("apm_cli.commands.deps.cli._resolve_scope_deps", return_value=([], [])):
            _show_scope_deps("Project", Path("/fake"), logger, None, False, insecure_only=True)
        logger.progress.assert_called_once()
        assert "No insecure" in logger.progress.call_args[0][0]

    def test_packages_displayed_without_rich(self) -> None:
        logger = _make_logger()
        pkgs = [_make_pkg_dict()]
        with patch("apm_cli.commands.deps.cli._resolve_scope_deps", return_value=(pkgs, [])):
            with patch("click.echo") as mock_echo:
                _show_scope_deps("Project", Path("/fake"), logger, None, False)
        assert mock_echo.called

    def test_orphaned_packages_trigger_warning(self) -> None:
        logger = _make_logger()
        pkgs = [_make_pkg_dict(name="owner/orphaned", is_orphaned=True)]
        with patch(
            "apm_cli.commands.deps.cli._resolve_scope_deps", return_value=(pkgs, ["owner/orphaned"])
        ):
            with patch("click.echo"):
                _show_scope_deps("Project", Path("/fake"), logger, None, False)
        assert logger.warning.called

    def test_insecure_packages_displayed_without_rich(self) -> None:
        logger = _make_logger()
        pkgs = [_make_pkg_dict(is_insecure=True)]
        with patch("apm_cli.commands.deps.cli._resolve_scope_deps", return_value=(pkgs, [])):
            with patch("click.echo") as mock_echo:
                _show_scope_deps("Project", Path("/fake"), logger, None, False, insecure_only=True)
        assert mock_echo.called
