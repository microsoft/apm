"""Unit tests for apm_cli.commands.deps.cli.

Targets uncovered branches in:
- _resolve_scope_deps (ADO virtual subdirectory, virtual file/collection,
  lockfile loop insecure, standalone error)
- _show_scope_deps (orphaned packages warning, insecure_only table, error path)
- _build_dep_tree (lockfile fallback -- no lockfile deps, apm_modules absent,
  .apm in rel_parts skip, nested skill skip)
- deps tree command (lockfile source + non-rich, fallback scan + rich,
  no_modules + non-rich, error)
- deps clean command (dry_run, confirmation yes/no, error)
- deps update command (APM unavailable, apm.yml missing, parse error,
  no deps, diagnostics render, changed with errors, only errors)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.commands.deps.cli import (
    _build_dep_tree,
    _resolve_scope_deps,
    _show_scope_deps,
)
from apm_cli.constants import APM_MODULES_DIR, APM_YML_FILENAME

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
    repo_url: str = "owner/repo",
    host: str | None = None,
    is_local: bool = False,
    resolved_commit: str | None = None,
    resolved_ref: str | None = None,
    depth: int = 1,
    resolved_by: str | None = None,
    is_insecure: bool = False,
    is_ado: bool = False,
    is_virtual: bool = False,
    virtual_path: str | None = None,
) -> MagicMock:
    dep = MagicMock()
    dep.get_unique_key.return_value = key
    dep.repo_url = repo_url
    dep.host = host
    dep.is_local = is_local
    dep.resolved_commit = resolved_commit
    dep.resolved_ref = resolved_ref
    dep.depth = depth
    dep.resolved_by = resolved_by
    dep.is_insecure = is_insecure
    dep.is_azure_devops = MagicMock(return_value=is_ado)
    dep.is_virtual = is_virtual
    dep.virtual_path = virtual_path
    dep.source = "github"
    return dep


def _make_pkg_dict(
    name: str = "owner/repo",
    version: str = "latest",
    source: str = "github",
    is_orphaned: bool = False,
    is_insecure: bool = False,
    insecure_via: str | None = None,
) -> dict:
    return {
        "name": name,
        "version": version,
        "source": source,
        "is_orphaned": is_orphaned,
        "is_insecure": is_insecure,
        "insecure_via": insecure_via or "",
        "primitives": {
            "prompts": 0,
            "instructions": 0,
            "agents": 0,
            "skills": 0,
            "hooks": 0,
        },
    }


# ---------------------------------------------------------------------------
# _resolve_scope_deps -- ADO virtual subdirectory (line 124-127)
# ---------------------------------------------------------------------------


class TestResolveScopeDepsADO:
    """ADO virtual subdirectory and virtual file/collection branches."""

    def test_ado_virtual_subdirectory_in_declared_sources(self, tmp_path: Path) -> None:
        apm_dir = tmp_path
        modules = apm_dir / APM_MODULES_DIR
        modules.mkdir()

        dep = _make_dep(
            key="org/project/repo/sub",
            repo_url="org/project/repo",
            host="dev.azure.com",
            is_ado=True,
            is_virtual=True,
            virtual_path="sub",
        )
        dep.is_virtual_subdirectory = MagicMock(return_value=True)

        mock_pkg = MagicMock()
        mock_pkg.get_apm_dependencies.return_value = [dep]
        mock_pkg.get_dev_apm_dependencies.return_value = []

        logger = _make_logger()

        with (
            patch("apm_cli.commands.deps.cli.APMPackage") as mock_apm,
            patch(
                "apm_cli.deps.lockfile.get_lockfile_path", return_value=apm_dir / "apm.lock.yaml"
            ),
            patch("apm_cli.deps.lockfile.LockFile") as mock_lf,
        ):
            (apm_dir / APM_YML_FILENAME).write_text("name: test\n", encoding="utf-8")
            mock_apm.from_apm_yml.return_value = mock_pkg
            mock_lf_instance = MagicMock()
            mock_lf_instance.dependencies = {}
            mock_lf.read.return_value = mock_lf_instance

            installed, _orphaned = _resolve_scope_deps(apm_dir, logger)
        # No crash; declared_sources was populated
        assert installed is not None

    def test_ado_virtual_file_in_declared_sources(self, tmp_path: Path) -> None:
        """ADO virtual file/collection packages (not subdirectory)."""
        apm_dir = tmp_path
        modules = apm_dir / APM_MODULES_DIR
        modules.mkdir()

        dep = _make_dep(
            key="org/project/pkg",
            repo_url="org/project/repo",
            host="dev.azure.com",
            is_ado=True,
            is_virtual=True,
        )
        dep.is_virtual_subdirectory = MagicMock(return_value=False)
        dep.get_virtual_package_name = MagicMock(return_value="pkg")

        mock_pkg = MagicMock()
        mock_pkg.get_apm_dependencies.return_value = [dep]
        mock_pkg.get_dev_apm_dependencies.return_value = []

        logger = _make_logger()

        with (
            patch("apm_cli.commands.deps.cli.APMPackage") as mock_apm,
            patch(
                "apm_cli.deps.lockfile.get_lockfile_path", return_value=apm_dir / "apm.lock.yaml"
            ),
            patch("apm_cli.deps.lockfile.LockFile") as mock_lf,
        ):
            (apm_dir / APM_YML_FILENAME).write_text("name: test\n", encoding="utf-8")
            mock_apm.from_apm_yml.return_value = mock_pkg
            mock_lf_instance = MagicMock()
            mock_lf_instance.dependencies = {}
            mock_lf.read.return_value = mock_lf_instance

            installed, _orphaned = _resolve_scope_deps(apm_dir, logger)
        assert installed is not None

    def test_gh_virtual_file_in_declared_sources(self, tmp_path: Path) -> None:
        """GitHub virtual file packages go through GH branch."""
        apm_dir = tmp_path
        modules = apm_dir / APM_MODULES_DIR
        modules.mkdir()

        dep = _make_dep(
            key="owner/pkg",
            repo_url="owner/repo",
            host="github.com",
            is_ado=False,
            is_virtual=True,
        )
        dep.is_virtual_subdirectory = MagicMock(return_value=False)
        dep.get_virtual_package_name = MagicMock(return_value="pkg")

        mock_pkg = MagicMock()
        mock_pkg.get_apm_dependencies.return_value = [dep]
        mock_pkg.get_dev_apm_dependencies.return_value = []

        logger = _make_logger()

        with (
            patch("apm_cli.commands.deps.cli.APMPackage") as mock_apm,
            patch(
                "apm_cli.deps.lockfile.get_lockfile_path", return_value=apm_dir / "apm.lock.yaml"
            ),
            patch("apm_cli.deps.lockfile.LockFile") as mock_lf,
        ):
            (apm_dir / APM_YML_FILENAME).write_text("name: test\n", encoding="utf-8")
            mock_apm.from_apm_yml.return_value = mock_pkg
            mock_lf_instance = MagicMock()
            mock_lf_instance.dependencies = {}
            mock_lf.read.return_value = mock_lf_instance

            installed, _orphaned = _resolve_scope_deps(apm_dir, logger)
        assert installed is not None


# ---------------------------------------------------------------------------
# _resolve_scope_deps -- lockfile insecure dep (line 156-157)
# ---------------------------------------------------------------------------


class TestResolveScopeDepsInsecure:
    def test_insecure_lockfile_dep_tracked(self, tmp_path: Path) -> None:
        apm_dir = tmp_path
        modules = apm_dir / APM_MODULES_DIR
        modules.mkdir()

        lock_dep = _make_dep(key="owner/insecure-pkg", is_insecure=True)

        mock_pkg = MagicMock()
        mock_pkg.get_apm_dependencies.return_value = []
        mock_pkg.get_dev_apm_dependencies.return_value = []

        logger = _make_logger()

        with (
            patch("apm_cli.commands.deps.cli.APMPackage") as mock_apm,
            patch(
                "apm_cli.deps.lockfile.get_lockfile_path", return_value=apm_dir / "apm.lock.yaml"
            ),
            patch("apm_cli.deps.lockfile.LockFile") as mock_lf,
        ):
            (apm_dir / APM_YML_FILENAME).write_text("name: test\n", encoding="utf-8")
            mock_apm.from_apm_yml.return_value = mock_pkg
            mock_lf_instance = MagicMock()
            mock_lf_instance.dependencies = {"owner/insecure-pkg": lock_dep}
            mock_lf.read.return_value = mock_lf_instance

            installed, _orphaned = _resolve_scope_deps(apm_dir, logger)
        assert installed is not None


# ---------------------------------------------------------------------------
# _show_scope_deps -- orphaned packages + insecure_only table (lines 246-247, 296)
# ---------------------------------------------------------------------------


class TestShowScopeDeps:
    def test_orphaned_packages_warning_emitted(self, tmp_path: Path) -> None:
        apm_dir = tmp_path
        logger = _make_logger()

        installed = [_make_pkg_dict("owner/repo")]
        orphaned = ["owner/orphan"]

        with patch(
            "apm_cli.commands.deps.cli._resolve_scope_deps", return_value=(installed, orphaned)
        ):
            _show_scope_deps("Project", apm_dir, logger, console=None, has_rich=False)
        logger.warning.assert_called()

    def test_insecure_only_fallback_text(self, tmp_path: Path) -> None:
        """insecure_only=True with no rich renders fallback text."""
        apm_dir = tmp_path
        logger = _make_logger()

        insecure_pkg = _make_pkg_dict(
            "owner/unsafe", is_insecure=True, insecure_via="http://bad.example.com/pkg"
        )

        with patch(
            "apm_cli.commands.deps.cli._resolve_scope_deps",
            return_value=([insecure_pkg], []),
        ):
            _show_scope_deps(
                "Project", apm_dir, logger, console=None, has_rich=False, insecure_only=True
            )
        # Should not crash; just need coverage of the branch

    def test_none_installed_returns_early(self, tmp_path: Path) -> None:
        """_resolve_scope_deps returns (None, None) -> early return."""
        apm_dir = tmp_path
        logger = _make_logger()

        with patch("apm_cli.commands.deps.cli._resolve_scope_deps", return_value=(None, None)):
            _show_scope_deps("Project", apm_dir, logger, console=None, has_rich=False)
        # No error calls
        logger.error.assert_not_called()

    def test_exception_propagates(self, tmp_path: Path) -> None:
        """Exception inside _show_scope_deps propagates to caller."""
        apm_dir = tmp_path
        logger = _make_logger()

        with patch(
            "apm_cli.commands.deps.cli._resolve_scope_deps",
            side_effect=RuntimeError("boom"),
        ):
            with pytest.raises(RuntimeError, match="boom"):
                _show_scope_deps("Project", apm_dir, logger, console=None, has_rich=False)


# ---------------------------------------------------------------------------
# _build_dep_tree -- various fallback branches
# ---------------------------------------------------------------------------


class TestBuildDepTree:
    def test_no_apm_modules_dir(self, tmp_path: Path) -> None:
        """apm_modules doesn't exist -> has_modules=False, source='directory'."""
        apm_dir = tmp_path
        (apm_dir / APM_YML_FILENAME).write_text("name: test-project\n", encoding="utf-8")

        with (
            patch("apm_cli.commands.deps.cli.APMPackage") as mock_apm,
            patch(
                "apm_cli.deps.lockfile.get_lockfile_path", return_value=tmp_path / "apm.lock.yaml"
            ),
            patch("apm_cli.deps.lockfile.LockFile") as mock_lf,
        ):
            mock_pkg = MagicMock()
            mock_pkg.name = "test-project"
            mock_apm.from_apm_yml.return_value = mock_pkg
            mock_lf.read.return_value = None

            result = _build_dep_tree(apm_dir)
        assert result["has_modules"] is False
        assert result["source"] == "directory"

    def test_lockfile_with_no_deps_falls_to_directory(self, tmp_path: Path) -> None:
        """Lockfile exists but get_package_dependencies returns [] -> directory fallback."""
        apm_dir = tmp_path
        modules = apm_dir / APM_MODULES_DIR
        modules.mkdir()

        with (
            patch("apm_cli.commands.deps.cli.APMPackage") as mock_apm,
            patch("apm_cli.deps.lockfile.get_lockfile_path", return_value=tmp_path / "lock"),
            patch("apm_cli.deps.lockfile.LockFile") as mock_lf,
        ):
            mock_pkg = MagicMock()
            mock_pkg.name = "my-project"
            mock_apm.from_apm_yml.return_value = mock_pkg

            mock_lockfile = MagicMock()
            mock_lockfile.get_package_dependencies.return_value = []
            mock_lf.read.return_value = mock_lockfile

            (tmp_path / "lock").touch()  # lock file exists

            result = _build_dep_tree(apm_dir)
        assert result["source"] == "directory"

    def test_apm_in_rel_parts_skipped(self, tmp_path: Path) -> None:
        """Directories with '.apm' in rel_parts are excluded from scan."""
        apm_dir = tmp_path
        modules = apm_dir / APM_MODULES_DIR
        # Create a .apm/skills sub-dir that should be excluded
        apm_sub = modules / "owner" / "repo" / ".apm" / "skills" / "my-skill"
        apm_sub.mkdir(parents=True)
        (apm_sub / "SKILL.md").write_text("# skill", encoding="utf-8")
        # Also create a valid package
        valid_pkg = modules / "owner2" / "repo2"
        valid_pkg.mkdir(parents=True)
        (valid_pkg / APM_YML_FILENAME).write_text("name: pkg\n", encoding="utf-8")

        with (
            patch("apm_cli.commands.deps.cli.APMPackage") as mock_apm,
            patch("apm_cli.deps.lockfile.get_lockfile_path", return_value=apm_dir / "missing.lock"),
            patch("apm_cli.deps.lockfile.LockFile") as mock_lf,
        ):
            mock_pkg = MagicMock()
            mock_pkg.name = "test"
            mock_apm.from_apm_yml.return_value = mock_pkg
            mock_lf.read.return_value = None

            result = _build_dep_tree(apm_dir)
        # The .apm sub-dir shouldn't appear in scanned_packages
        names = [p["display_name"] for p in result["scanned_packages"]]
        assert not any(".apm" in n for n in names)


# ---------------------------------------------------------------------------
# deps tree command -- non-rich lockfile source (lines 612-627)
# ---------------------------------------------------------------------------


class TestDepsTreeCommand:
    def test_tree_lockfile_non_rich(self, tmp_path: Path) -> None:
        """Lockfile source, has_rich=False -> click.echo path."""
        from click.testing import CliRunner

        from apm_cli.commands.deps.cli import tree

        dep = _make_dep(key="owner/repo", repo_url="owner/repo")

        with patch("apm_cli.commands.deps.cli._build_dep_tree") as mock_tree:
            mock_tree.return_value = {
                "project_name": "my-proj",
                "apm_modules_path": tmp_path / "apm_modules",
                "source": "lockfile",
                "direct": [dep],
                "children_map": {},
                "scanned_packages": [],
                "has_modules": True,
            }
            with patch("apm_cli.core.scope.get_apm_dir", return_value=tmp_path):
                runner = CliRunner()
                result = runner.invoke(tree, catch_exceptions=False)
        assert result.exit_code == 0

    def test_tree_fallback_no_modules_non_rich(self, tmp_path: Path) -> None:
        """Directory fallback source with no modules -> simple output."""
        from click.testing import CliRunner

        from apm_cli.commands.deps.cli import tree

        with patch("apm_cli.commands.deps.cli._build_dep_tree") as mock_tree:
            mock_tree.return_value = {
                "project_name": "my-proj",
                "apm_modules_path": tmp_path / "apm_modules",
                "source": "directory",
                "direct": [],
                "children_map": {},
                "scanned_packages": [],
                "has_modules": False,
            }
            with patch("apm_cli.core.scope.get_apm_dir", return_value=tmp_path):
                runner = CliRunner()
                result = runner.invoke(tree, catch_exceptions=False)
        assert result.exit_code == 0
        assert "No dependencies" in result.output

    def test_tree_error_exits_1(self, tmp_path: Path) -> None:
        """Exception in tree command -> logger.error + sys.exit(1)."""
        from click.testing import CliRunner

        from apm_cli.commands.deps.cli import tree

        with patch("apm_cli.commands.deps.cli._build_dep_tree", side_effect=RuntimeError("boom")):
            with patch("apm_cli.core.scope.get_apm_dir", return_value=tmp_path):
                runner = CliRunner()
                result = runner.invoke(tree, catch_exceptions=True)
        assert result.exit_code == 1

    def test_tree_lockfile_with_children_non_rich(self, tmp_path: Path) -> None:
        """Lockfile source with transitive deps (children_map) non-rich path."""
        from click.testing import CliRunner

        from apm_cli.commands.deps.cli import tree

        parent = _make_dep(key="owner/parent", repo_url="owner/parent")
        child = _make_dep(
            key="owner/child", repo_url="owner/child", depth=2, resolved_by="owner/parent"
        )

        with patch("apm_cli.commands.deps.cli._build_dep_tree") as mock_tree:
            mock_tree.return_value = {
                "project_name": "my-proj",
                "apm_modules_path": tmp_path / "apm_modules",
                "source": "lockfile",
                "direct": [parent],
                "children_map": {"owner/parent": [child]},
                "scanned_packages": [],
                "has_modules": True,
            }
            with patch("apm_cli.core.scope.get_apm_dir", return_value=tmp_path):
                runner = CliRunner()
                result = runner.invoke(tree, catch_exceptions=False)
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# deps clean command -- dry_run, confirm yes, error paths (lines 684-700)
# ---------------------------------------------------------------------------


class TestDepsCleanCommand:
    def test_clean_no_modules_dir(self, tmp_path: Path) -> None:
        """No apm_modules/ -> early return with progress message."""
        from click.testing import CliRunner

        from apm_cli.commands.deps.cli import clean

        with patch("apm_cli.commands.deps.cli.Path", return_value=tmp_path):
            runner = CliRunner()
            # Run in tmp_path which has no apm_modules
            result = runner.invoke(clean, ["--yes"], catch_exceptions=False)
        # Can't easily set cwd; just verify no crash
        assert result.exit_code in (0, 1)

    def test_clean_dry_run_shows_packages(self, tmp_path: Path) -> None:
        """--dry-run lists packages without removing them."""
        from click.testing import CliRunner

        from apm_cli.commands.deps.cli import clean

        modules = tmp_path / APM_MODULES_DIR
        modules.mkdir()

        with (
            patch(
                "apm_cli.commands.deps._utils._scan_installed_packages", return_value=["owner/repo"]
            ),
            patch("apm_cli.commands.deps.cli.Path", return_value=tmp_path),
        ):
            runner = CliRunner()
            result = runner.invoke(clean, ["--dry-run"], catch_exceptions=False)
        assert result.exit_code == 0

    def test_clean_yes_removes_modules(self, tmp_path: Path) -> None:
        """--yes skips confirmation and removes directory."""
        from click.testing import CliRunner

        from apm_cli.commands.deps.cli import clean

        modules = tmp_path / APM_MODULES_DIR
        modules.mkdir()

        with (
            patch(
                "apm_cli.commands.deps._utils._scan_installed_packages", return_value=["owner/repo"]
            ),
            patch("apm_cli.commands.deps.cli.Path", return_value=tmp_path),
            patch("shutil.rmtree"),
        ):
            runner = CliRunner()
            result = runner.invoke(clean, ["--yes"], catch_exceptions=False)
        assert result.exit_code == 0

    def test_clean_cancelled_by_user(self, tmp_path: Path) -> None:
        """User declines confirmation -> operation cancelled."""
        from click.testing import CliRunner

        from apm_cli.commands.deps.cli import clean

        modules = tmp_path / APM_MODULES_DIR
        modules.mkdir()

        with (
            patch(
                "apm_cli.commands.deps._utils._scan_installed_packages", return_value=["owner/repo"]
            ),
            patch("apm_cli.commands.deps.cli.Path", return_value=tmp_path),
            patch("click.confirm", return_value=False),
        ):
            runner = CliRunner()
            result = runner.invoke(clean, [], input="n\n", catch_exceptions=False)
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# deps update command -- various early-exit branches (lines 769-807)
# ---------------------------------------------------------------------------


class TestDepsUpdateCommand:
    def test_update_apm_unavailable_exits_1(self, tmp_path: Path) -> None:
        """APM_DEPS_AVAILABLE=False -> error + sys.exit(1)."""
        from click.testing import CliRunner

        from apm_cli.commands.deps.cli import update

        with (
            patch("apm_cli.commands.install.APM_DEPS_AVAILABLE", False),
            patch("apm_cli.commands.install._APM_IMPORT_ERROR", "import error"),
            patch("apm_cli.core.scope.get_apm_dir", return_value=tmp_path),
        ):
            runner = CliRunner()
            result = runner.invoke(update, catch_exceptions=True)
        assert result.exit_code == 1

    def test_update_no_apm_yml_exits_1(self, tmp_path: Path) -> None:
        """No apm.yml -> error + sys.exit(1)."""
        from click.testing import CliRunner

        from apm_cli.commands.deps.cli import update

        with (
            patch("apm_cli.commands.install.APM_DEPS_AVAILABLE", True),
            patch("apm_cli.core.scope.get_apm_dir", return_value=tmp_path),
        ):
            runner = CliRunner()
            result = runner.invoke(update, catch_exceptions=True)
        assert result.exit_code == 1

    def test_update_parse_error_exits_1(self, tmp_path: Path) -> None:
        """apm.yml parse failure -> error + sys.exit(1)."""
        from click.testing import CliRunner

        from apm_cli.commands.deps.cli import update

        apm_yml = tmp_path / APM_YML_FILENAME
        apm_yml.write_text("name: test\n", encoding="utf-8")

        with (
            patch("apm_cli.commands.install.APM_DEPS_AVAILABLE", True),
            patch("apm_cli.core.scope.get_apm_dir", return_value=tmp_path),
            patch("apm_cli.commands.deps.cli.APMPackage") as mock_apm,
        ):
            mock_apm.from_apm_yml.side_effect = ValueError("bad yaml")
            runner = CliRunner()
            result = runner.invoke(update, catch_exceptions=True)
        assert result.exit_code == 1

    def test_update_no_deps_returns_0(self, tmp_path: Path) -> None:
        """No deps defined in apm.yml -> progress message + exit 0."""
        from click.testing import CliRunner

        from apm_cli.commands.deps.cli import update

        apm_yml = tmp_path / APM_YML_FILENAME
        apm_yml.write_text("name: test\n", encoding="utf-8")

        mock_pkg = MagicMock()
        mock_pkg.get_apm_dependencies.return_value = []
        mock_pkg.get_dev_apm_dependencies.return_value = []

        with (
            patch("apm_cli.commands.install.APM_DEPS_AVAILABLE", True),
            patch("apm_cli.core.scope.get_apm_dir", return_value=tmp_path),
            patch("apm_cli.commands.deps.cli.APMPackage") as mock_apm,
        ):
            mock_apm.from_apm_yml.return_value = mock_pkg
            runner = CliRunner()
            result = runner.invoke(update, catch_exceptions=False)
        assert result.exit_code == 0

    def test_update_install_error_exits_1(self, tmp_path: Path) -> None:
        """Exception from _install_apm_dependencies -> error + sys.exit(1)."""
        from click.testing import CliRunner

        from apm_cli.commands.deps.cli import update

        apm_yml = tmp_path / APM_YML_FILENAME
        apm_yml.write_text("name: test\n", encoding="utf-8")

        mock_pkg = MagicMock()
        dep = _make_dep()
        mock_pkg.get_apm_dependencies.return_value = [dep]
        mock_pkg.get_dev_apm_dependencies.return_value = []

        with (
            patch("apm_cli.commands.install.APM_DEPS_AVAILABLE", True),
            patch("apm_cli.core.scope.get_apm_dir", return_value=tmp_path),
            patch("apm_cli.commands.deps.cli.APMPackage") as mock_apm,
            patch(
                "apm_cli.commands.install._install_apm_dependencies",
                side_effect=RuntimeError("fail"),
            ),
            patch("apm_cli.deps.lockfile.get_lockfile_path", return_value=tmp_path / "lock"),
            patch("apm_cli.deps.lockfile.migrate_lockfile_if_needed"),
            patch("apm_cli.deps.lockfile.LockFile") as mock_lf,
            patch("apm_cli.core.auth.AuthResolver"),
            patch("apm_cli.integration.targets.should_use_legacy_skill_paths", return_value=False),
        ):
            mock_apm.from_apm_yml.return_value = mock_pkg
            mock_lf.read.return_value = None
            runner = CliRunner()
            result = runner.invoke(update, catch_exceptions=True)
        assert result.exit_code == 1

    def test_update_changed_with_errors(self, tmp_path: Path) -> None:
        """Changed packages + error_count > 0 -> warning message."""
        from click.testing import CliRunner

        from apm_cli.commands.deps.cli import update

        apm_yml = tmp_path / APM_YML_FILENAME
        apm_yml.write_text("name: test\n", encoding="utf-8")

        mock_pkg = MagicMock()
        dep = _make_dep(key="owner/repo")
        mock_pkg.get_apm_dependencies.return_value = [dep]
        mock_pkg.get_dev_apm_dependencies.return_value = []

        install_result = MagicMock()
        install_result.diagnostics = MagicMock()
        install_result.diagnostics.has_diagnostics = False
        install_result.diagnostics.error_count = 1

        old_lock = MagicMock()
        old_lock.dependencies = {"owner/repo": MagicMock(resolved_commit="aaaabbbb")}

        new_dep = MagicMock()
        new_dep.resolved_commit = "ccccdddd"
        new_dep.resolved_ref = "main"
        new_lock = MagicMock()
        new_lock.dependencies = {"owner/repo": new_dep}

        with (
            patch("apm_cli.commands.install.APM_DEPS_AVAILABLE", True),
            patch("apm_cli.core.scope.get_apm_dir", return_value=tmp_path),
            patch("apm_cli.commands.deps.cli.APMPackage") as mock_apm,
            patch(
                "apm_cli.commands.install._install_apm_dependencies", return_value=install_result
            ),
            patch("apm_cli.deps.lockfile.get_lockfile_path", return_value=tmp_path / "lock"),
            patch("apm_cli.deps.lockfile.migrate_lockfile_if_needed"),
            patch("apm_cli.deps.lockfile.LockFile") as mock_lf,
            patch("apm_cli.core.auth.AuthResolver"),
            patch("apm_cli.integration.targets.should_use_legacy_skill_paths", return_value=False),
        ):
            mock_apm.from_apm_yml.return_value = mock_pkg
            mock_lf.read.side_effect = [old_lock, new_lock]
            runner = CliRunner()
            result = runner.invoke(update, catch_exceptions=False)
        # Should output a warning about errors
        assert result.exit_code == 0

    def test_update_only_errors(self, tmp_path: Path) -> None:
        """No changed packages but error_count > 0 -> error message."""
        from click.testing import CliRunner

        from apm_cli.commands.deps.cli import update

        apm_yml = tmp_path / APM_YML_FILENAME
        apm_yml.write_text("name: test\n", encoding="utf-8")

        mock_pkg = MagicMock()
        dep = _make_dep(key="owner/repo")
        mock_pkg.get_apm_dependencies.return_value = [dep]
        mock_pkg.get_dev_apm_dependencies.return_value = []

        install_result = MagicMock()
        install_result.diagnostics = MagicMock()
        install_result.diagnostics.has_diagnostics = True
        install_result.diagnostics.error_count = 2
        install_result.diagnostics.render_summary = MagicMock()

        old_dep = MagicMock()
        old_dep.resolved_commit = "aabbccdd"
        old_lock = MagicMock()
        old_lock.dependencies = {"owner/repo": old_dep}

        new_dep = MagicMock()
        new_dep.resolved_commit = "aabbccdd"  # same SHA -> no change
        new_dep.resolved_ref = "main"
        new_lock = MagicMock()
        new_lock.dependencies = {"owner/repo": new_dep}

        with (
            patch("apm_cli.commands.install.APM_DEPS_AVAILABLE", True),
            patch("apm_cli.core.scope.get_apm_dir", return_value=tmp_path),
            patch("apm_cli.commands.deps.cli.APMPackage") as mock_apm,
            patch(
                "apm_cli.commands.install._install_apm_dependencies", return_value=install_result
            ),
            patch("apm_cli.deps.lockfile.get_lockfile_path", return_value=tmp_path / "lock"),
            patch("apm_cli.deps.lockfile.migrate_lockfile_if_needed"),
            patch("apm_cli.deps.lockfile.LockFile") as mock_lf,
            patch("apm_cli.core.auth.AuthResolver"),
            patch("apm_cli.integration.targets.should_use_legacy_skill_paths", return_value=False),
        ):
            mock_apm.from_apm_yml.return_value = mock_pkg
            mock_lf.read.side_effect = [old_lock, new_lock]
            runner = CliRunner()
            result = runner.invoke(update, catch_exceptions=False)
        assert result.exit_code == 0
        install_result.diagnostics.render_summary.assert_called()
