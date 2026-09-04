"""Unit tests for ``apm_cli.install.presentation.dry_run.render_and_exit``.

Covers all rendering branches: APM deps, MCP deps, no-deps, lockfile
orphan preview, dev_apm_deps, and the dry-run notice / success message.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock, patch

from apm_cli.install.dry_run_plan import ProspectiveInstallPlan
from apm_cli.install.presentation.dry_run import render_and_exit as _render_and_exit
from apm_cli.models.dependency.reference import DependencyReference


def render_and_exit(**kwargs) -> None:
    """Adapt legacy test fixtures to the frozen dry-run plan input."""
    if "plan" not in kwargs:
        apm_dependencies = tuple(kwargs.pop("apm_deps"))
        dev_apm_dependencies = tuple(kwargs.pop("dev_apm_deps"))
        kwargs["plan"] = ProspectiveInstallPlan(
            apm_dependencies=apm_dependencies,
            dev_apm_dependencies=dev_apm_dependencies,
            selected_apm_dependencies=apm_dependencies + dev_apm_dependencies,
            mcp_dependencies=tuple(kwargs.pop("mcp_deps")),
            should_install_apm=kwargs.pop("should_install_apm"),
            should_install_mcp=kwargs.pop("should_install_mcp"),
            only_packages=(
                tuple(only_packages)
                if (only_packages := kwargs.pop("only_packages", None)) is not None
                else None
            ),
            lsp_dependencies=tuple(kwargs.pop("lsp_deps", ())),
        )
    _render_and_exit(**kwargs)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_logger() -> MagicMock:
    """Return a MagicMock that satisfies the InstallLogger duck-type."""
    return MagicMock()


def _make_apm_dep(
    repo_url: str = "owner/repo",
    reference: str = "main",
) -> MagicMock:
    dep = MagicMock()
    dep.repo_url = repo_url
    dep.reference = reference
    dep.get_unique_key.return_value = repo_url
    dep.to_display_reference.return_value = f"{repo_url}#{reference or 'main'}"
    return dep


@dataclass(frozen=True)
class _DisplayDependency:
    name: str

    def __str__(self) -> str:
        return self.name


def _make_mcp_dep(name: str = "my-server") -> _DisplayDependency:
    return _DisplayDependency(name)


def _make_lsp_dep(name: str = "pyright") -> _DisplayDependency:
    return _DisplayDependency(name)


# ---------------------------------------------------------------------------
# APM, MCP, and LSP dependency rendering
# ---------------------------------------------------------------------------


class TestRenderApmDeps:
    def test_plan_counts_only_selected_dependency_types(self) -> None:
        """Preview counts exclude dependency types filtered from the install."""
        plan = ProspectiveInstallPlan(
            apm_dependencies=(_make_apm_dep(),),
            dev_apm_dependencies=(),
            selected_apm_dependencies=(_make_apm_dep(),),
            mcp_dependencies=(_make_mcp_dep(),),
            should_install_apm=False,
            should_install_mcp=True,
            only_packages=None,
        )

        assert plan.apm_dependency_count == 0
        assert plan.mcp_dependency_count == 1
        assert plan.lsp_dependency_count == 0
        assert plan.dependency_counts == (0, 1, 0)

    def test_plan_selects_only_requested_dependency_without_reparsing_it(self) -> None:
        """Preview selection retains the interpreted dependency object."""
        existing = DependencyReference.parse("owner/existing#1.0.0")
        requested = DependencyReference.parse("owner/requested#2.0.0")
        requested.source = "registry"
        requested.registry_name = "private"
        package = MagicMock()
        package.get_apm_dependencies.return_value = [existing, requested]
        package.get_dev_apm_dependencies.return_value = []
        package.get_all_mcp_dependencies.return_value = []
        package.get_lsp_dependencies.return_value = []

        plan = ProspectiveInstallPlan.from_apm_package(
            package,
            should_install_apm=True,
            should_install_mcp=True,
            only_packages=["owner/requested#2.0.0"],
        )

        assert plan.selected_apm_dependencies == (requested,)
        assert plan.selected_apm_dependencies[0].source == "registry"
        assert plan.selected_apm_dependencies[0].registry_name == "private"

    def test_plan_excludes_apm_selection_when_only_mcp_is_requested(self) -> None:
        """The prospective plan does not leak APM work into --only=mcp previews."""
        dep = DependencyReference.parse("owner/repo#main")
        package = MagicMock()
        package.get_apm_dependencies.return_value = [dep]
        package.get_dev_apm_dependencies.return_value = []
        package.get_all_mcp_dependencies.return_value = []
        package.get_lsp_dependencies.return_value = []

        plan = ProspectiveInstallPlan.from_apm_package(
            package,
            should_install_apm=False,
            should_install_mcp=True,
            only_packages=None,
        )

        assert plan.selected_apm_dependencies == ()
        assert plan.apm_dependency_count == 0

    def test_apm_deps_install_action_shown(self, tmp_path: Path) -> None:
        """APM deps rendered with 'install' when update=False (lines 42-45)."""
        logger = _make_logger()
        dep = _make_apm_dep("owner/repo", "main")

        with patch("apm_cli.deps.lockfile.LockFile.read", side_effect=Exception):
            render_and_exit(
                logger=logger,
                should_install_apm=True,
                apm_deps=[dep],
                mcp_deps=[],
                dev_apm_deps=[],
                should_install_mcp=False,
                update=False,
                apm_dir=tmp_path,
            )

        all_calls = " ".join(str(c) for c in logger.progress.call_args_list)
        assert "APM dependencies" in all_calls
        assert "install" in all_calls

    def test_apm_deps_update_action_shown(self, tmp_path: Path) -> None:
        """APM deps rendered with 'update' when update=True (lines 42-45)."""
        logger = _make_logger()
        dep = _make_apm_dep()

        with patch("apm_cli.deps.lockfile.LockFile.read", side_effect=Exception):
            render_and_exit(
                logger=logger,
                should_install_apm=True,
                apm_deps=[dep],
                mcp_deps=[],
                dev_apm_deps=[],
                should_install_mcp=False,
                update=True,
                apm_dir=tmp_path,
            )

        all_calls = " ".join(str(c) for c in logger.progress.call_args_list)
        assert "update" in all_calls

    def test_apm_reference_none_falls_back_to_main(self, tmp_path: Path) -> None:
        """Dep with reference=None uses 'main' as the reference label."""
        logger = _make_logger()
        dep = _make_apm_dep("owner/repo")
        dep.reference = None

        with patch("apm_cli.deps.lockfile.LockFile.read", side_effect=Exception):
            render_and_exit(
                logger=logger,
                should_install_apm=True,
                apm_deps=[dep],
                mcp_deps=[],
                dev_apm_deps=[],
                should_install_mcp=False,
                update=False,
                apm_dir=tmp_path,
            )

        all_calls = " ".join(str(c) for c in logger.progress.call_args_list)
        assert "main" in all_calls

    def test_mcp_deps_rendered(self, tmp_path: Path) -> None:
        """MCP deps block is rendered when should_install_mcp=True and mcp_deps non-empty (lines 48-50)."""
        logger = _make_logger()
        dep = _make_mcp_dep("my-server")

        with patch("apm_cli.deps.lockfile.LockFile.read", side_effect=Exception):
            render_and_exit(
                logger=logger,
                should_install_apm=False,
                apm_deps=[],
                mcp_deps=[dep],
                dev_apm_deps=[],
                should_install_mcp=True,
                update=False,
                apm_dir=tmp_path,
            )

        all_calls = " ".join(str(c) for c in logger.progress.call_args_list)
        assert "MCP dependencies" in all_calls

    def test_lsp_only_deps_are_rendered_without_empty_message(self, tmp_path: Path) -> None:
        """LSP-only manifests render servers and do not report an empty plan."""
        logger = _make_logger()
        dep = _make_lsp_dep("gopls")

        with patch("apm_cli.deps.lockfile.LockFile.read", side_effect=Exception):
            render_and_exit(
                logger=logger,
                should_install_apm=False,
                apm_deps=[],
                mcp_deps=[],
                lsp_deps=[dep],
                dev_apm_deps=[],
                should_install_mcp=True,
                update=False,
                apm_dir=tmp_path,
            )

        all_calls = " ".join(str(c) for c in logger.progress.call_args_list)
        assert "LSP servers to configure" in all_calls
        assert "gopls" in all_calls
        assert "No dependencies" not in all_calls

    def test_filtered_lsp_only_deps_show_empty_message(self, tmp_path: Path) -> None:
        """An APM-only dry run explains that its selected plan is empty."""
        logger = _make_logger()

        with patch("apm_cli.deps.lockfile.LockFile.read", side_effect=Exception):
            render_and_exit(
                logger=logger,
                should_install_apm=True,
                apm_deps=[],
                mcp_deps=[],
                lsp_deps=[_make_lsp_dep("gopls")],
                dev_apm_deps=[],
                should_install_mcp=False,
                update=False,
                apm_dir=tmp_path,
            )

        all_calls = " ".join(str(c) for c in logger.progress.call_args_list)
        assert "LSP servers to configure" not in all_calls
        assert "gopls" not in all_calls
        assert "No APM dependencies selected by --only=apm" in all_calls
        assert "Drop --only to preview MCP/LSP dependencies" in all_calls

    def test_no_deps_shows_empty_message(self, tmp_path: Path) -> None:
        """When all dep lists are empty the 'No dependencies found' message is shown (line 53)."""
        logger = _make_logger()

        with patch("apm_cli.deps.lockfile.LockFile.read", side_effect=Exception):
            render_and_exit(
                logger=logger,
                should_install_apm=False,
                apm_deps=[],
                mcp_deps=[],
                dev_apm_deps=[],
                should_install_mcp=False,
                update=False,
                apm_dir=tmp_path,
            )

        all_calls = " ".join(str(c) for c in logger.progress.call_args_list)
        assert "No dependencies" in all_calls

    def test_lockfile_read_exception_does_not_crash(self, tmp_path: Path) -> None:
        """An exception from LockFile.read is caught; function still completes (lines 59-60)."""
        logger = _make_logger()

        with patch(
            "apm_cli.deps.lockfile.LockFile.read",
            side_effect=RuntimeError("no lock"),
        ):
            render_and_exit(
                logger=logger,
                should_install_apm=True,
                apm_deps=[_make_apm_dep()],
                mcp_deps=[],
                dev_apm_deps=[],
                should_install_mcp=False,
                update=False,
                apm_dir=tmp_path,
            )

        logger.success.assert_not_called()


# ---------------------------------------------------------------------------
# Orphan preview rendering
# ---------------------------------------------------------------------------


class TestOrphanPreview:
    def test_orphan_preview_header_shown(self, tmp_path: Path) -> None:
        """Orphan preview header is printed when orphans are found (lines 64-81)."""
        logger = _make_logger()
        mock_lock = MagicMock()

        with (
            patch("apm_cli.deps.lockfile.LockFile.read", return_value=mock_lock),
            patch(
                "apm_cli.deps.lockfile.get_lockfile_path",
                return_value=tmp_path / "apm.lock.yaml",
            ),
            patch("apm_cli.drift.detect_orphans", return_value=["a.md", "b.md"]),
        ):
            render_and_exit(
                logger=logger,
                should_install_apm=True,
                apm_deps=[_make_apm_dep()],
                mcp_deps=[],
                dev_apm_deps=[],
                should_install_mcp=False,
                update=False,
                apm_dir=tmp_path,
            )

        all_calls = " ".join(str(c) for c in logger.progress.call_args_list)
        assert "Files that would be removed" in all_calls

    def test_orphan_files_listed(self, tmp_path: Path) -> None:
        """Individual orphan files are printed (lines 80-81)."""
        logger = _make_logger()
        mock_lock = MagicMock()

        with (
            patch("apm_cli.deps.lockfile.LockFile.read", return_value=mock_lock),
            patch(
                "apm_cli.deps.lockfile.get_lockfile_path",
                return_value=tmp_path / "apm.lock.yaml",
            ),
            patch("apm_cli.drift.detect_orphans", return_value=["alpha.md", "beta.md"]),
        ):
            render_and_exit(
                logger=logger,
                should_install_apm=True,
                apm_deps=[_make_apm_dep()],
                mcp_deps=[],
                dev_apm_deps=[],
                should_install_mcp=False,
                update=False,
                apm_dir=tmp_path,
            )

        all_calls = " ".join(str(c) for c in logger.progress.call_args_list)
        assert "alpha.md" in all_calls

    def test_orphan_preview_over_10_shows_more_line(self, tmp_path: Path) -> None:
        """When more than 10 orphans exist, a '... and N more' line is shown (lines 82-83)."""
        logger = _make_logger()
        mock_lock = MagicMock()
        orphans = [f"file{i}.md" for i in range(15)]

        with (
            patch("apm_cli.deps.lockfile.LockFile.read", return_value=mock_lock),
            patch(
                "apm_cli.deps.lockfile.get_lockfile_path",
                return_value=tmp_path / "apm.lock.yaml",
            ),
            patch("apm_cli.drift.detect_orphans", return_value=orphans),
        ):
            render_and_exit(
                logger=logger,
                should_install_apm=True,
                apm_deps=[_make_apm_dep()],
                mcp_deps=[],
                dev_apm_deps=[],
                should_install_mcp=False,
                update=False,
                apm_dir=tmp_path,
            )

        all_calls = " ".join(str(c) for c in logger.progress.call_args_list)
        assert "more" in all_calls

    def test_no_orphans_no_preview_header(self, tmp_path: Path) -> None:
        """When detect_orphans returns empty, no orphan header is shown."""
        logger = _make_logger()
        mock_lock = MagicMock()

        with (
            patch("apm_cli.deps.lockfile.LockFile.read", return_value=mock_lock),
            patch(
                "apm_cli.deps.lockfile.get_lockfile_path",
                return_value=tmp_path / "apm.lock.yaml",
            ),
            patch("apm_cli.drift.detect_orphans", return_value=[]),
        ):
            render_and_exit(
                logger=logger,
                should_install_apm=True,
                apm_deps=[_make_apm_dep()],
                mcp_deps=[],
                dev_apm_deps=[],
                should_install_mcp=False,
                update=False,
                apm_dir=tmp_path,
            )

        all_calls = " ".join(str(c) for c in logger.progress.call_args_list)
        assert "Files that would be removed" not in all_calls

    def test_dev_deps_contribute_to_orphan_detection(self, tmp_path: Path) -> None:
        """dev_apm_deps are included in the intended-keys set passed to detect_orphans."""
        logger = _make_logger()
        mock_lock = MagicMock()
        dev_dep = _make_apm_dep("owner/dev-repo")

        with (
            patch("apm_cli.deps.lockfile.LockFile.read", return_value=mock_lock),
            patch(
                "apm_cli.deps.lockfile.get_lockfile_path",
                return_value=tmp_path / "apm.lock.yaml",
            ),
            patch("apm_cli.drift.detect_orphans", return_value=[]) as mock_orphans,
        ):
            render_and_exit(
                logger=logger,
                should_install_apm=False,
                apm_deps=[],
                mcp_deps=[],
                dev_apm_deps=[dev_dep],
                should_install_mcp=False,
                update=False,
                apm_dir=tmp_path,
            )

        mock_orphans.assert_called_once()
        _lock_arg, intended_keys_arg = mock_orphans.call_args[0][:2]
        assert "owner/dev-repo" in intended_keys_arg

    def test_get_unique_key_exception_skipped(self, tmp_path: Path) -> None:
        """If dep.get_unique_key() raises, it is silently skipped (noqa: SIM105 block)."""
        logger = _make_logger()
        mock_lock = MagicMock()
        dep = _make_apm_dep()
        dep.get_unique_key.side_effect = AttributeError("no key")

        with (
            patch("apm_cli.deps.lockfile.LockFile.read", return_value=mock_lock),
            patch(
                "apm_cli.deps.lockfile.get_lockfile_path",
                return_value=tmp_path / "apm.lock.yaml",
            ),
            patch("apm_cli.drift.detect_orphans", return_value=[]) as mock_orphans,
        ):
            render_and_exit(
                logger=logger,
                should_install_apm=True,
                apm_deps=[dep],
                mcp_deps=[],
                dev_apm_deps=[],
                should_install_mcp=False,
                update=False,
                apm_dir=tmp_path,
            )

        # detect_orphans still called; intended_keys is empty (failed silently)
        mock_orphans.assert_called_once()


# ---------------------------------------------------------------------------
# Dry-run notice and success message
# ---------------------------------------------------------------------------


class TestDryRunNoticeAndSuccess:
    def test_dry_run_notice_shown_when_apm_deps_present(self, tmp_path: Path) -> None:
        """dry_run_notice is called when apm_deps is non-empty (lines 85-90)."""
        logger = _make_logger()

        with patch("apm_cli.deps.lockfile.LockFile.read", side_effect=Exception):
            render_and_exit(
                logger=logger,
                should_install_apm=True,
                apm_deps=[_make_apm_dep()],
                mcp_deps=[],
                dev_apm_deps=[],
                should_install_mcp=False,
                update=False,
                apm_dir=tmp_path,
            )

        logger.dry_run_notice.assert_called_once()

    def test_dry_run_notice_shown_when_dev_apm_deps_present(self, tmp_path: Path) -> None:
        """dry_run_notice is called when dev_apm_deps is non-empty (lines 85-90)."""
        logger = _make_logger()

        with patch("apm_cli.deps.lockfile.LockFile.read", side_effect=Exception):
            render_and_exit(
                logger=logger,
                should_install_apm=True,
                apm_deps=[],
                mcp_deps=[],
                dev_apm_deps=[_make_apm_dep("owner/dev")],
                should_install_mcp=False,
                update=False,
                apm_dir=tmp_path,
            )

        logger.dry_run_notice.assert_called_once()

    def test_dry_run_notice_not_shown_when_no_apm_deps(self, tmp_path: Path) -> None:
        """dry_run_notice is NOT called when both apm_deps and dev_apm_deps are empty."""
        logger = _make_logger()

        with patch("apm_cli.deps.lockfile.LockFile.read", side_effect=Exception):
            render_and_exit(
                logger=logger,
                should_install_apm=False,
                apm_deps=[],
                mcp_deps=[_make_mcp_dep()],
                dev_apm_deps=[],
                should_install_mcp=True,
                update=False,
                apm_dir=tmp_path,
            )

        logger.dry_run_notice.assert_not_called()

    def test_renderer_leaves_completion_summary_to_caller(self, tmp_path: Path) -> None:
        """The command lifecycle owns the one final dry-run summary."""
        logger = _make_logger()

        with patch("apm_cli.deps.lockfile.LockFile.read", side_effect=Exception):
            render_and_exit(
                logger=logger,
                should_install_apm=False,
                apm_deps=[],
                mcp_deps=[],
                dev_apm_deps=[],
                should_install_mcp=False,
                update=False,
                apm_dir=tmp_path,
            )

        logger.success.assert_not_called()

    def test_only_packages_forwarded_to_detect_orphans(self, tmp_path: Path) -> None:
        """only_packages argument is passed through to detect_orphans."""
        logger = _make_logger()
        mock_lock = MagicMock()

        with (
            patch("apm_cli.deps.lockfile.LockFile.read", return_value=mock_lock),
            patch(
                "apm_cli.deps.lockfile.get_lockfile_path",
                return_value=tmp_path / "apm.lock.yaml",
            ),
            patch("apm_cli.drift.detect_orphans", return_value=[]) as mock_orphans,
        ):
            render_and_exit(
                logger=logger,
                should_install_apm=False,
                apm_deps=[],
                mcp_deps=[],
                dev_apm_deps=[],
                should_install_mcp=False,
                update=False,
                only_packages=["owner/repo"],
                apm_dir=tmp_path,
            )

        call_args = mock_orphans.call_args
        assert call_args.kwargs.get("only_packages") == ["owner/repo"]
