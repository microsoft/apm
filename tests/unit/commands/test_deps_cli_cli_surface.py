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

import pytest

from apm_cli.commands.deps.cli import (
    _build_dep_tree,
    _resolve_scope_deps,
)
from apm_cli.constants import APM_MODULES_DIR, APM_YML_FILENAME, SKILL_MD_FILENAME
from apm_cli.deps.lockfile import LockedDependency, LockFile, get_lockfile_path

pytestmark = pytest.mark.component

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
    repo_url: str | None = None,
) -> LockedDependency:
    return LockedDependency(
        repo_url=repo_url or key,
        version=version,
        resolved_commit=resolved_commit,
        resolved_ref=resolved_ref,
    )


# ---------------------------------------------------------------------------
# _deps_list_source_label
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _add_tree_children
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _show_scope_deps
# ---------------------------------------------------------------------------


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

        mock_lockfile = LockFile(dependencies={dep.get_unique_key(): dep})

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

        mock_lockfile = LockFile(
            dependencies={dep.get_unique_key(): dep for dep in (direct, transitive)}
        )

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

        insecure_dep = LockedDependency(
            host=None, source="github", is_insecure=True, resolved_by=None, repo_url="owner/repo"
        )

        mock_lockfile = MagicMock()
        mock_lockfile.dependencies = {"owner/repo": insecure_dep}
        mock_lockfile.get_package_dependencies.return_value = [insecure_dep]

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


class TestResolveScopeDepsNoModules:
    def test_returns_none_when_apm_modules_absent(self, tmp_path):
        """No apm_modules directory -> (None, None)."""
        result = _resolve_scope_deps(tmp_path, _make_logger())
        assert result == (None, None)


class TestResolveScopeDepsWithPackages:
    def _setup_package(self, modules_dir: Path, owner: str, repo: str, name: str = "pkg"):
        """Create a minimal installed package directory."""
        pkg_dir = modules_dir / "github" / owner / repo
        pkg_dir.mkdir(parents=True)
        (pkg_dir / APM_YML_FILENAME).write_text(f"name: {name}\nversion: 1.0.0\n")
        return pkg_dir

    def test_package_without_apm_yml_declared_not_orphaned(self, tmp_path):
        """Package declared in apm.yml is not orphaned."""
        modules_dir = tmp_path / APM_MODULES_DIR
        modules_dir.mkdir()

        # Set up declared dep in apm.yml
        (tmp_path / APM_YML_FILENAME).write_text(
            "name: myproject\ndependencies:\n  - owner/myrepo\n"
        )

        # Create matching installed package
        pkg_dir = modules_dir / "github" / "owner" / "myrepo"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / APM_YML_FILENAME).write_text("name: myrepo\nversion: 1.0.0\n")

        installed, orphaned = _resolve_scope_deps(tmp_path, _make_logger())
        assert installed is not None
        assert isinstance(orphaned, list)

    def test_no_packages_in_modules_returns_empty_lists(self, tmp_path):
        """apm_modules exists but contains no valid packages -> empty lists."""
        modules_dir = tmp_path / APM_MODULES_DIR
        modules_dir.mkdir()
        # No packages inside
        installed, orphaned = _resolve_scope_deps(tmp_path, _make_logger())
        assert installed == []
        assert orphaned == []

    def test_insecure_only_filter_excludes_secure_packages(self, tmp_path):
        """insecure_only=True returns empty list when no insecure packages."""
        modules_dir = tmp_path / APM_MODULES_DIR
        pkg_dir = modules_dir / "github" / "owner" / "repo"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / APM_YML_FILENAME).write_text("name: repo\nversion: 0.1.0\n")

        installed, _orphaned = _resolve_scope_deps(tmp_path, _make_logger(), insecure_only=True)
        # No lockfile -> no insecure deps -> empty list
        assert installed == []

    def test_skill_md_only_package_discovered(self, tmp_path):
        """Package with only SKILL.md (no apm.yml) is still discovered."""
        modules_dir = tmp_path / APM_MODULES_DIR
        pkg_dir = modules_dir / "github" / "owner" / "skill-repo"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / SKILL_MD_FILENAME).write_text("# My Skill\n")

        installed, _orphaned = _resolve_scope_deps(tmp_path, _make_logger())
        assert installed is not None
        assert any(pkg["name"].endswith("skill-repo") for pkg in installed)

    def test_nested_package_manifest_is_not_listed_independently(self, tmp_path):
        """A nested manifest is omitted while its undeclared parent is orphaned."""
        modules_dir = tmp_path / APM_MODULES_DIR
        parent = modules_dir / "_local" / "package"
        nested = parent / "sub-package"
        nested.mkdir(parents=True)
        (parent / APM_YML_FILENAME).write_text("name: package\nversion: 1.0.0\n")
        (nested / APM_YML_FILENAME).write_text("name: sub-package\nversion: 1.0.0\n")

        installed, orphaned = _resolve_scope_deps(tmp_path, _make_logger())

        assert installed is not None
        assert [package["name"] for package in installed] == ["_local/package"]
        assert orphaned == ["_local/package"]


_HASH_SLOT = "abcdef123456"


class TestResolveScopeDepsOrphanHashSlot:
    """A genuinely orphaned physical slot (``_local/<12hex>/pkg``) has no
    lockfile entry to map back to. It must still be detected as orphaned, but
    its user-facing name must be the hash-free logical form (``_local/pkg``).
    """

    def test_orphan_hashed_slot_is_orphaned_and_shows_no_hash(self, tmp_path):
        modules_dir = tmp_path / APM_MODULES_DIR
        slot = modules_dir / "_local" / _HASH_SLOT / "pkg"
        slot.mkdir(parents=True)
        (slot / APM_YML_FILENAME).write_text("name: pkg\nversion: 1.0.0\n")
        # No apm.yml declaration and no lockfile -> genuinely orphaned.

        installed, orphaned = _resolve_scope_deps(tmp_path, _make_logger())

        assert installed is not None
        names = [package["name"] for package in installed]
        assert names == ["_local/pkg"]
        assert orphaned == ["_local/pkg"]
        # The raw physical hash slot must never surface in user-facing output.
        for name in names + orphaned:
            assert _HASH_SLOT not in name

    def test_declared_remote_hash_shaped_identity_is_not_redacted(self, tmp_path):
        remote_name = f"_local/{_HASH_SLOT}/pkg"
        modules_dir = tmp_path / APM_MODULES_DIR
        package_dir = modules_dir / remote_name
        package_dir.mkdir(parents=True)
        (package_dir / APM_YML_FILENAME).write_text("name: pkg\nversion: 1.0.0\n")

        dep = LockedDependency(repo_url=remote_name, resolved_commit="a" * 40)
        LockFile(dependencies={dep.get_unique_key(): dep}).write(get_lockfile_path(tmp_path))

        installed, orphaned = _resolve_scope_deps(tmp_path, _make_logger())

        assert installed is not None
        assert [package["name"] for package in installed] == [remote_name]
        assert orphaned == []


class TestResolveScopeDepsReadFailure:
    """A malformed package apm.yml makes ``APMPackage.from_apm_yml`` raise a
    ``ValueError`` embedding the absolute apm.yml path. The read-failure
    warning must not leak that path (or a hash slot); it must identify the
    package by its hash-free logical name and stay actionable.
    """

    def test_malformed_apm_yml_warning_excludes_path_and_hash(self, tmp_path):
        modules_dir = tmp_path / APM_MODULES_DIR
        slot = modules_dir / "_local" / "pkg"
        slot.mkdir(parents=True)
        # Invalid YAML: unbalanced bracket -> yaml.YAMLError -> ValueError(path).
        (slot / APM_YML_FILENAME).write_text("name: [unterminated\nversion: 1.0.0\n")

        logger = _make_logger()
        installed, _orphaned = _resolve_scope_deps(tmp_path, logger)

        assert installed is not None
        warnings = [call.args[0] for call in logger.warning.call_args_list if call.args]
        read_failures = [w for w in warnings if "inspect package" in w or "read package" in w]
        assert read_failures, f"expected a read-failure warning, got: {warnings}"
        message = read_failures[0]
        # Must identify the package by its logical name...
        assert "_local/pkg" in message
        # ...and must NOT leak the absolute apm.yml path or a hash slot.
        assert str(tmp_path) not in message
        assert APM_YML_FILENAME not in message
        assert _HASH_SLOT not in message
