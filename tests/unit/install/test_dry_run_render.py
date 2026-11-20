"""Unit tests for ``apm_cli.install.presentation.dry_run.render_and_exit``.

Covers all rendering branches: APM deps, MCP deps, no-deps, lockfile
orphan preview, dev_apm_deps, and the dry-run notice / success message.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.install.dry_run_plan import ProspectiveInstallPlan
from apm_cli.install.orphans import OrphanOperation, OrphanRemovalDecision
from apm_cli.install.presentation.dry_run import render_and_exit as _render_and_exit

pytestmark = pytest.mark.component


def _decision(files) -> OrphanRemovalDecision:
    return OrphanRemovalDecision(
        operation=OrphanOperation.INSTALL, removed_files={"owner/gone": tuple(files)}
    )


def _raise(_lock: object) -> bool:
    raise ValueError("malformed ledger")


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
    def test_incomplete_plan_never_claims_orphan_removals(self, tmp_path: Path) -> None:
        logger = _make_logger()
        plan = ProspectiveInstallPlan(
            apm_dependencies=(),
            dev_apm_dependencies=(),
            selected_apm_dependencies=(),
            mcp_dependencies=(),
            should_install_apm=True,
            should_install_mcp=False,
            only_packages=None,
            dependency_preview_errors=("unknown parent metadata",),
        )
        with (
            patch("apm_cli.deps.lockfile.LockFile.read", return_value=MagicMock()),
            patch("apm_cli.install.orphans.decide_orphan_removal") as orphans,
        ):
            render_and_exit(logger=logger, plan=plan, update=False, apm_dir=tmp_path)
        orphans.assert_not_called()
        logger.dry_run_notice.assert_called_once()
        assert "Dependency preview incomplete" in logger.dry_run_notice.call_args.args[0]

    def test_declaration_errors_show_without_verbose(self, tmp_path: Path) -> None:
        """A refusal install always makes is shown, not hidden behind --verbose (#3967)."""
        logger = _make_logger()
        collision = "a and b are different packages that would both install to apm_modules/x"
        plan = ProspectiveInstallPlan(
            apm_dependencies=(),
            dev_apm_dependencies=(),
            selected_apm_dependencies=(),
            mcp_dependencies=(),
            should_install_apm=True,
            should_install_mcp=False,
            only_packages=None,
            dependency_preview_errors=(collision, "unknown parent metadata"),
            dependency_declaration_errors=(collision,),
        )
        with patch("apm_cli.deps.lockfile.LockFile.read", return_value=None):
            render_and_exit(logger=logger, plan=plan, update=False, apm_dir=tmp_path)
        logger.warning.assert_called_once_with(f"apm install would refuse: {collision}")
        details = [call.args[0] for call in logger.verbose_detail.call_args_list]
        assert details == ["unknown parent metadata"]

    def test_retired_mcp_moves_are_previewed_for_project_scope(self, tmp_path: Path) -> None:
        """A 0.36 project sees which servers move to .mcp.json (#3967)."""
        from apm_cli.install.mcp.retired_destinations import RetiredMcpPreview

        logger = _make_logger()
        plan = ProspectiveInstallPlan(
            apm_dependencies=(),
            dev_apm_dependencies=(),
            selected_apm_dependencies=(),
            mcp_dependencies=(),
            should_install_apm=False,
            should_install_mcp=True,
            only_packages=None,
        )
        with (
            patch("apm_cli.deps.lockfile.LockFile.read", return_value=MagicMock()),
            patch(
                "apm_cli.install.mcp.retired_destinations.preview_retired_mcp_moves",
                return_value=RetiredMcpPreview(
                    moves={".github/mcp.json": ["echo"]},
                    removals={".vscode/mcp.json": ["gone"]},
                ),
            ) as preview,
        ):
            render_and_exit(
                logger=logger, plan=plan, update=False, apm_dir=tmp_path, project_root=tmp_path
            )
        preview.assert_called_once()
        lines = [call.args[0] for call in logger.progress.call_args_list]
        assert "MCP servers that would move from .github/mcp.json to .mcp.json:" in lines
        assert "  - echo" in lines
        assert "MCP servers that would be removed from .vscode/mcp.json:" in lines
        assert "  - gone" in lines

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
            patch(
                "apm_cli.install.orphans.decide_orphan_removal",
                return_value=_decision(["a.md", "b.md"]),
            ),
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
            patch(
                "apm_cli.install.orphans.decide_orphan_removal",
                return_value=_decision(["alpha.md", "beta.md"]),
            ),
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
            patch("apm_cli.install.orphans.decide_orphan_removal", return_value=_decision(orphans)),
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
        """When the orphan owner removes nothing, no orphan header is shown."""
        logger = _make_logger()
        mock_lock = MagicMock()

        with (
            patch("apm_cli.deps.lockfile.LockFile.read", return_value=mock_lock),
            patch(
                "apm_cli.deps.lockfile.get_lockfile_path",
                return_value=tmp_path / "apm.lock.yaml",
            ),
            patch("apm_cli.install.orphans.decide_orphan_removal", return_value=_decision([])),
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
        """dev_apm_deps are included in the intent passed to the orphan owner."""
        logger = _make_logger()
        mock_lock = MagicMock()
        dev_dep = _make_apm_dep("owner/dev-repo")

        with (
            patch("apm_cli.deps.lockfile.LockFile.read", return_value=mock_lock),
            patch(
                "apm_cli.deps.lockfile.get_lockfile_path",
                return_value=tmp_path / "apm.lock.yaml",
            ),
            patch(
                "apm_cli.install.orphans.decide_orphan_removal", return_value=_decision([])
            ) as mock_orphans,
        ):
            render_and_exit(
                logger=logger,
                should_install_apm=True,
                apm_deps=[],
                mcp_deps=[],
                dev_apm_deps=[dev_dep],
                should_install_mcp=False,
                update=False,
                apm_dir=tmp_path,
            )

        mock_orphans.assert_called_once()
        assert "owner/dev-repo" in mock_orphans.call_args.kwargs["intended"]

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
            patch(
                "apm_cli.install.orphans.decide_orphan_removal", return_value=_decision([])
            ) as mock_orphans,
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

        # the orphan owner is still called; intended_keys is empty (failed silently)
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

    @pytest.mark.parametrize(
        ("predicate", "shown"),
        [(lambda _lock: True, True), (lambda _lock: False, False), (_raise, False)],
        ids=["cleanup-pending", "nothing-pending", "unreadable-ledger"],
    )
    def test_dry_run_notice_follows_local_cleanup_predicate(
        self, tmp_path: Path, predicate: Callable[[object], bool], shown: bool
    ) -> None:
        """Pending local cleanup triggers the notice; a failing predicate never fails a preview."""
        logger = _make_logger()

        with (
            patch("apm_cli.deps.lockfile.LockFile.read", return_value=MagicMock()),
            patch("apm_cli.install.orphans.decide_orphan_removal", return_value=_decision([])),
        ):
            render_and_exit(
                logger=logger,
                should_install_apm=True,
                apm_deps=[],
                mcp_deps=[],
                dev_apm_deps=[],
                should_install_mcp=False,
                update=False,
                apm_dir=tmp_path,
                local_cleanup_pending=predicate,
            )

        assert logger.dry_run_notice.called is shown

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

    def test_targeted_install_previews_no_orphan_removals(self, tmp_path: Path) -> None:
        """A targeted install never previews unrelated orphan removals."""
        logger = _make_logger()
        mock_lock = MagicMock()

        with (
            patch("apm_cli.deps.lockfile.LockFile.read", return_value=mock_lock),
            patch(
                "apm_cli.deps.lockfile.get_lockfile_path",
                return_value=tmp_path / "apm.lock.yaml",
            ),
            patch(
                "apm_cli.install.orphans.decide_orphan_removal", return_value=_decision([])
            ) as mock_orphans,
        ):
            render_and_exit(
                logger=logger,
                should_install_apm=True,
                apm_deps=[],
                mcp_deps=[],
                dev_apm_deps=[],
                should_install_mcp=False,
                update=False,
                only_packages=["owner/repo"],
                apm_dir=tmp_path,
            )

        mock_orphans.assert_not_called()
