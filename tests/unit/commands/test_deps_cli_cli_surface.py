"""Comprehensive unit tests for apm_cli.commands.deps.cli.

Targets the uncovered branches in:
- _deps_list_source_label
- _add_tree_children
- _resolve_scope_deps (more branches)
- _show_scope_deps
- _build_dep_tree
- list_packages CLI command
- tree CLI command
- clean CLI command
- update CLI command
- info CLI command
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from apm_cli.commands.deps.cli import (
    _add_tree_children,
    _build_dep_tree,
    _deps_list_source_label,
    _echo_tree_children,
    _resolve_scope_deps,
    _show_scope_deps,
)
from apm_cli.constants import APM_MODULES_DIR, APM_YML_FILENAME, SKILL_MD_FILENAME

# ---------------------------------------------------------------------------
# Helpers
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


def _make_dep(
    key: str = "owner/repo",
    version: str | None = None,
    resolved_commit: str | None = None,
    resolved_ref: str | None = None,
    repo_url: str = "owner/repo",
) -> MagicMock:
    dep = MagicMock()
    dep.get_unique_key.return_value = key
    dep.get_canonical_dependency_string.return_value = key
    dep.version = version
    dep.resolved_commit = resolved_commit
    dep.resolved_ref = resolved_ref
    dep.repo_url = repo_url
    dep.depth = 1
    dep.resolved_by = None
    dep.host = None
    dep.is_local = False
    dep.source = "github"
    dep.is_azure_devops = MagicMock(return_value=False)
    dep.is_insecure = False
    return dep


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
# _deps_list_source_label
# ---------------------------------------------------------------------------


class TestDepsListSourceLabel:
    def test_local_flag_returns_local(self) -> None:
        result = _deps_list_source_label(None, is_local=True)
        assert result == "local"

    def test_lockfile_source_local_returns_local(self) -> None:
        result = _deps_list_source_label(None, lockfile_source="local")
        assert result == "local"

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


# ---------------------------------------------------------------------------
# _add_tree_children
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# _show_scope_deps
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# _build_dep_tree
# ---------------------------------------------------------------------------


class TestBuildDepTree:
    def test_no_modules_dir_returns_has_modules_false(self, tmp_path: Path) -> None:
        result = _build_dep_tree(tmp_path)
        assert result["has_modules"] is False
        assert result["source"] == "directory"

    def test_empty_modules_dir_returns_empty_scanned(self, tmp_path: Path) -> None:
        (tmp_path / APM_MODULES_DIR).mkdir()
        result = _build_dep_tree(tmp_path)
        assert result["has_modules"] is True
        assert result["scanned_packages"] == []

    def test_reads_project_name_from_apm_yml(self, tmp_path: Path) -> None:
        apm_yml = tmp_path / APM_YML_FILENAME
        apm_yml.write_text("name: my-cool-project\nversion: 1.0.0\n")
        result = _build_dep_tree(tmp_path)
        assert result["project_name"] == "my-cool-project"

    def test_defaults_project_name_on_parse_error(self, tmp_path: Path) -> None:
        apm_yml = tmp_path / APM_YML_FILENAME
        apm_yml.write_text(": invalid\n")  # malformed YAML name
        result = _build_dep_tree(tmp_path)
        assert result["project_name"] == "my-project"

    def test_lockfile_source_when_lockfile_exists(self, tmp_path: Path) -> None:
        (tmp_path / APM_MODULES_DIR).mkdir()
        dep = _make_dep(key="owner/repo", repo_url="owner/repo")
        dep.depth = 1

        mock_lockfile = MagicMock()
        mock_lockfile.get_package_dependencies.return_value = [dep]

        with patch("apm_cli.deps.lockfile.LockFile") as mock_lf_cls:
            with patch("apm_cli.deps.lockfile.get_lockfile_path") as mock_glp:
                lockfile_path = tmp_path / "apm.lock.yaml"
                lockfile_path.write_text("dependencies: {}\n")
                mock_glp.return_value = lockfile_path
                mock_lf_cls.read.return_value = mock_lockfile
                result = _build_dep_tree(tmp_path)

        assert result["source"] == "lockfile"
        assert len(result["direct"]) == 1

    def test_transitive_deps_go_into_children_map(self, tmp_path: Path) -> None:
        (tmp_path / APM_MODULES_DIR).mkdir()
        direct = _make_dep(key="owner/direct", repo_url="owner/direct")
        direct.depth = 1
        transitive = _make_dep(key="owner/trans", repo_url="owner/trans")
        transitive.depth = 2
        transitive.resolved_by = "owner/direct"

        mock_lockfile = MagicMock()
        mock_lockfile.get_package_dependencies.return_value = [direct, transitive]

        with patch("apm_cli.deps.lockfile.LockFile") as mock_lf_cls:
            with patch("apm_cli.deps.lockfile.get_lockfile_path") as mock_glp:
                lockfile_path = tmp_path / "apm.lock.yaml"
                lockfile_path.write_text("dependencies: {}\n")
                mock_glp.return_value = lockfile_path
                mock_lf_cls.read.return_value = mock_lockfile
                result = _build_dep_tree(tmp_path)

        assert "owner/direct" in result["children_map"]
        assert len(result["children_map"]["owner/direct"]) == 1

    def test_scanned_packages_in_fallback_mode(self, tmp_path: Path) -> None:
        modules_dir = tmp_path / APM_MODULES_DIR
        pkg_dir = modules_dir / "github" / "owner" / "repo"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / APM_YML_FILENAME).write_text("name: repo\nversion: 1.0.0\n")

        # No lockfile -> should fall back to directory scan
        result = _build_dep_tree(tmp_path)
        assert result["source"] == "directory"
        assert len(result["scanned_packages"]) == 1


# ---------------------------------------------------------------------------
# _resolve_scope_deps -- additional branches
# ---------------------------------------------------------------------------


class TestResolveScopeDepsAdditional:
    def test_skill_md_only_version_is_unknown(self, tmp_path: Path) -> None:
        """Package with only SKILL.md (no apm.yml) has 'unknown' version."""
        modules_dir = tmp_path / APM_MODULES_DIR
        pkg_dir = modules_dir / "github" / "owner" / "skill-pkg"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / SKILL_MD_FILENAME).write_text("# My Skill\n")

        installed, _ = _resolve_scope_deps(tmp_path, _make_logger())
        assert installed is not None
        pkg = next((p for p in installed if p["name"].endswith("skill-pkg")), None)
        assert pkg is not None
        assert pkg["version"] == "unknown"

    def test_insecure_dep_from_lockfile_flagged(self, tmp_path: Path) -> None:
        """Packages in the lockfile with is_insecure=True are flagged."""
        modules_dir = tmp_path / APM_MODULES_DIR
        # 2-level path: owner/repo — matches dep.get_unique_key() = "owner/repo"
        pkg_dir = modules_dir / "owner" / "repo"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / APM_YML_FILENAME).write_text("name: repo\nversion: 0.1.0\n")

        insecure_dep = MagicMock()
        insecure_dep.get_unique_key.return_value = "owner/repo"
        insecure_dep.get_canonical_dependency_string.return_value = "owner/repo"
        insecure_dep.host = None
        insecure_dep.source = "github"
        insecure_dep.is_insecure = True
        insecure_dep.resolved_by = None

        mock_lockfile = MagicMock()
        mock_lockfile.dependencies = {"owner/repo": insecure_dep}

        with patch("apm_cli.deps.lockfile.LockFile") as mock_lf_cls:
            with patch("apm_cli.deps.lockfile.get_lockfile_path") as mock_glp:
                lockfile_path = tmp_path / "apm.lock.yaml"
                lockfile_path.write_text("dependencies: {}\n")
                mock_glp.return_value = lockfile_path
                mock_lf_cls.read.return_value = mock_lockfile
                installed, _ = _resolve_scope_deps(tmp_path, _make_logger())

        insecure_pkgs = [p for p in (installed or []) if p["is_insecure"]]
        assert len(insecure_pkgs) >= 1

    def test_apm_yml_parse_exception_silently_continues(self, tmp_path: Path) -> None:
        """Malformed apm.yml for declared_sources parsing is swallowed."""
        modules_dir = tmp_path / APM_MODULES_DIR
        pkg_dir = modules_dir / "github" / "owner" / "repo"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / APM_YML_FILENAME).write_text("name: repo\nversion: 1.0.0\n")

        # Write a malformed project apm.yml
        (tmp_path / APM_YML_FILENAME).write_text("{invalid yaml]")

        # Should not raise; packages should still be discovered
        installed, _ = _resolve_scope_deps(tmp_path, _make_logger())
        assert installed is not None

    def test_insecure_only_filter_returns_only_insecure(self, tmp_path: Path) -> None:
        """insecure_only=True filters out secure packages."""
        modules_dir = tmp_path / APM_MODULES_DIR
        # One secure package
        secure_dir = modules_dir / "github" / "owner" / "secure-repo"
        secure_dir.mkdir(parents=True)
        (secure_dir / APM_YML_FILENAME).write_text("name: secure\nversion: 1.0.0\n")

        installed, _ = _resolve_scope_deps(tmp_path, _make_logger(), insecure_only=True)
        assert installed == []


# ---------------------------------------------------------------------------
# clean CLI command
# ---------------------------------------------------------------------------


class TestCleanCommand:
    def _invoke(self, args: list[str], input_data: str | None = None) -> None:
        from click.testing import CliRunner

        from apm_cli.commands.deps.cli import deps

        runner = CliRunner()
        return runner.invoke(deps, ["clean", *args], input=input_data, catch_exceptions=False)

    def test_no_apm_modules_logs_already_clean(self, tmp_path: Path) -> None:
        from click.testing import CliRunner

        from apm_cli.commands.deps.cli import deps

        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            result = runner.invoke(deps, ["clean"], catch_exceptions=False)
        assert "already clean" in result.output

    def test_dry_run_shows_packages_without_removing(self, tmp_path: Path) -> None:
        from click.testing import CliRunner

        from apm_cli.commands.deps.cli import deps

        modules_dir = tmp_path / APM_MODULES_DIR
        pkg_dir = modules_dir / "github" / "owner" / "repo"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / APM_YML_FILENAME).write_text("name: repo\nversion: 1.0.0\n")

        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            # Change to tmp_path so Path(".") resolves correctly
            import os

            orig = os.getcwd()
            os.chdir(tmp_path)
            try:
                result = runner.invoke(deps, ["clean", "--dry-run"], catch_exceptions=False)
            finally:
                os.chdir(orig)
        assert "Dry run" in result.output
        # apm_modules should still exist
        assert modules_dir.exists()

    def test_yes_flag_removes_without_prompt(self, tmp_path: Path) -> None:
        from click.testing import CliRunner

        from apm_cli.commands.deps.cli import deps

        modules_dir = tmp_path / APM_MODULES_DIR
        modules_dir.mkdir(parents=True)

        runner = CliRunner()
        import os

        orig = os.getcwd()
        os.chdir(tmp_path)
        try:
            runner.invoke(deps, ["clean", "--yes"], catch_exceptions=False)
        finally:
            os.chdir(orig)
        # apm_modules should be removed
        assert not modules_dir.exists()


# ---------------------------------------------------------------------------
# list CLI command
# ---------------------------------------------------------------------------


class TestListCommand:
    def _invoke_in_dir(self, args: list[str], cwd: Path) -> None:
        from click.testing import CliRunner

        from apm_cli.commands.deps.cli import deps

        runner = CliRunner()
        import os

        orig = os.getcwd()
        os.chdir(cwd)
        try:
            with patch("apm_cli.core.scope.get_apm_dir") as mock_apm:
                mock_apm.return_value = cwd
                with patch(
                    "apm_cli.commands.deps.cli._resolve_scope_deps", return_value=(None, None)
                ):
                    result = runner.invoke(deps, ["list", *args], catch_exceptions=False)
        finally:
            os.chdir(orig)
        return result

    def test_list_no_packages_exit_zero(self, tmp_path: Path) -> None:
        result = self._invoke_in_dir([], tmp_path)
        assert result.exit_code == 0

    def test_list_global_uses_user_scope(self, tmp_path: Path) -> None:
        from click.testing import CliRunner

        from apm_cli.commands.deps.cli import deps

        runner = CliRunner()
        with patch("apm_cli.core.scope.get_apm_dir") as mock_apm:
            mock_apm.return_value = tmp_path
            with patch("apm_cli.commands.deps.cli._resolve_scope_deps", return_value=(None, None)):
                result = runner.invoke(deps, ["list", "--global"], catch_exceptions=False)
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# tree CLI command
# ---------------------------------------------------------------------------


class TestTreeCommand:
    def test_tree_no_modules_exits_zero(self, tmp_path: Path) -> None:
        from click.testing import CliRunner

        from apm_cli.commands.deps.cli import deps

        runner = CliRunner()
        import os

        orig = os.getcwd()
        os.chdir(tmp_path)
        try:
            with patch("apm_cli.core.scope.get_apm_dir", return_value=tmp_path):
                result = runner.invoke(deps, ["tree"], catch_exceptions=False)
        finally:
            os.chdir(orig)
        assert result.exit_code == 0

    def test_tree_with_lockfile_displays_project_name(self, tmp_path: Path) -> None:
        from click.testing import CliRunner

        from apm_cli.commands.deps.cli import deps

        (tmp_path / APM_YML_FILENAME).write_text("name: my-tree-project\nversion: 1.0.0\n")
        (tmp_path / APM_MODULES_DIR).mkdir()

        runner = CliRunner()
        import os

        orig = os.getcwd()
        os.chdir(tmp_path)
        try:
            with patch("apm_cli.core.scope.get_apm_dir", return_value=tmp_path):
                with patch("apm_cli.commands.deps.cli._build_dep_tree") as mock_tree:
                    mock_tree.return_value = {
                        "project_name": "my-tree-project",
                        "apm_modules_path": tmp_path / APM_MODULES_DIR,
                        "source": "lockfile",
                        "direct": [],
                        "children_map": {},
                        "scanned_packages": [],
                        "has_modules": True,
                    }
                    result = runner.invoke(deps, ["tree"], catch_exceptions=False)
        finally:
            os.chdir(orig)
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# info CLI command
# ---------------------------------------------------------------------------


class TestInfoCommand:
    def test_info_no_modules_exits_with_error(self, tmp_path: Path) -> None:
        from click.testing import CliRunner

        from apm_cli.commands.deps.cli import deps

        runner = CliRunner()
        import os

        orig = os.getcwd()
        os.chdir(tmp_path)
        try:
            result = runner.invoke(deps, ["info", "owner/repo"])
        finally:
            os.chdir(orig)
        assert result.exit_code == 1

    def test_info_package_delegates_to_display(self, tmp_path: Path) -> None:
        from click.testing import CliRunner

        from apm_cli.commands.deps.cli import deps

        modules_dir = tmp_path / APM_MODULES_DIR
        modules_dir.mkdir()

        runner = CliRunner()
        import os

        orig = os.getcwd()
        os.chdir(tmp_path)
        try:
            with patch("apm_cli.commands.view.resolve_package_path") as mock_rpp:
                with patch("apm_cli.commands.view.display_package_info") as mock_dpi:
                    mock_rpp.return_value = tmp_path / "owner" / "repo"
                    result = runner.invoke(deps, ["info", "owner/repo"])
        finally:
            os.chdir(orig)
        assert result.exit_code == 0
        mock_dpi.assert_called_once()
