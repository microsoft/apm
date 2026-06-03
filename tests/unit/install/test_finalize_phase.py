"""Unit tests for the install finalize phase."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from apm_cli.core.scope import InstallScope
from apm_cli.install.phases.finalize import run
from apm_cli.utils.diagnostics import DiagnosticCollector

# ---------------------------------------------------------------------------
# Minimal InstallContext stub
# ---------------------------------------------------------------------------


@dataclass
class _FakeCtx:
    """Minimal stub of InstallContext for finalize phase tests."""

    project_root: Path = Path("/fake/root")
    apm_dir: Path = Path("/fake/root/.apm")
    installed_count: int = 0
    unpinned_count: int = 0
    installed_packages: list[Any] = field(default_factory=list)
    total_prompts_integrated: int = 0
    total_agents_integrated: int = 0
    total_links_resolved: int = 0
    total_commands_integrated: int = 0
    total_hooks_integrated: int = 0
    total_instructions_integrated: int = 0
    diagnostics: Any = field(default_factory=DiagnosticCollector)
    logger: Any = None
    package_types: dict[str, str] = field(default_factory=dict)
    scope: Any = field(default_factory=lambda: InstallScope.PROJECT)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _make_ctx(**kwargs: Any) -> _FakeCtx:
    ctx = _FakeCtx()
    for k, v in kwargs.items():
        setattr(ctx, k, v)
    return ctx


# ---------------------------------------------------------------------------
# Basic result shape
# ---------------------------------------------------------------------------


class TestFinalizeRunReturnsInstallResult:
    """run() always returns an InstallResult."""

    def test_returns_install_result_with_installed_count(self) -> None:
        ctx = _make_ctx(installed_count=3)
        result = run(ctx)
        assert result.installed_count == 3

    def test_returns_install_result_with_prompts_and_agents(self) -> None:
        ctx = _make_ctx(
            installed_count=1,
            total_prompts_integrated=4,
            total_agents_integrated=2,
        )
        result = run(ctx)
        assert result.prompts_integrated == 4
        assert result.agents_integrated == 2

    def test_returns_diagnostics(self) -> None:
        diag = DiagnosticCollector()
        ctx = _make_ctx(diagnostics=diag)
        result = run(ctx)
        assert result.diagnostics is diag

    def test_package_types_forwarded(self) -> None:
        ctx = _make_ctx(package_types={"dep1": "apm", "dep2": "mcp"})
        result = run(ctx)
        assert result.package_types == {"dep1": "apm", "dep2": "mcp"}


# ---------------------------------------------------------------------------
# Verbose stats emitted through logger
# ---------------------------------------------------------------------------


class TestFinalizeVerboseStats:
    """Verbose stats blocks are logged when logger is present."""

    def test_links_resolved_logged(self) -> None:
        logger = MagicMock()
        ctx = _make_ctx(total_links_resolved=5, logger=logger)
        run(ctx)
        logger.verbose_detail.assert_any_call("Resolved 5 context file links")

    def test_links_resolved_zero_not_logged(self) -> None:
        logger = MagicMock()
        ctx = _make_ctx(total_links_resolved=0, logger=logger)
        run(ctx)
        for call in logger.verbose_detail.call_args_list:
            assert "context file links" not in str(call)

    def test_commands_integrated_logged(self) -> None:
        logger = MagicMock()
        ctx = _make_ctx(total_commands_integrated=2, logger=logger)
        run(ctx)
        logger.verbose_detail.assert_any_call("Integrated 2 command(s)")

    def test_hooks_integrated_logged(self) -> None:
        logger = MagicMock()
        ctx = _make_ctx(total_hooks_integrated=1, logger=logger)
        run(ctx)
        logger.verbose_detail.assert_any_call("Integrated 1 hook(s)")

    def test_instructions_integrated_logged(self) -> None:
        logger = MagicMock()
        ctx = _make_ctx(total_instructions_integrated=3, logger=logger)
        run(ctx)
        logger.verbose_detail.assert_any_call("Integrated 3 instruction(s)")

    def test_no_logger_stats_silent(self) -> None:
        # Should not raise when logger is None and stats are non-zero
        ctx = _make_ctx(
            total_links_resolved=1,
            total_commands_integrated=1,
            total_hooks_integrated=1,
            total_instructions_integrated=1,
            logger=None,
        )
        result = run(ctx)
        assert result.installed_count == 0


# ---------------------------------------------------------------------------
# Bare-success fallback (no logger path)
# ---------------------------------------------------------------------------


class TestFinalizeBareSuccessFallback:
    """When no logger is provided, _rich_success is called."""

    def test_no_logger_calls_rich_success(self) -> None:
        ctx = _make_ctx(installed_count=2, logger=None)
        with patch("apm_cli.commands.install._rich_success") as mock_success:
            run(ctx)
        mock_success.assert_called_once_with("Installed 2 APM dependencies")

    def test_with_logger_does_not_call_rich_success(self) -> None:
        logger = MagicMock()
        ctx = _make_ctx(installed_count=1, logger=logger)
        with patch("apm_cli.commands.install._rich_success") as mock_success:
            run(ctx)
        mock_success.assert_not_called()


# ---------------------------------------------------------------------------
# Unpinned dependency warnings
# ---------------------------------------------------------------------------


def _make_pkg(repo_url: str | None = None, reference: str | None = None) -> MagicMock:
    pkg = MagicMock()
    dep_ref = MagicMock()
    dep_ref.reference = reference  # None means unpinned
    dep_ref.repo_url = repo_url
    pkg.dep_ref = dep_ref
    return pkg


class TestFinalizeUnpinnedWarning:
    """Unpinned-dependency warning formatting."""

    def test_no_warning_when_all_pinned(self) -> None:
        diag = DiagnosticCollector()
        ctx = _make_ctx(unpinned_count=0, diagnostics=diag)
        run(ctx)
        assert not diag.has_diagnostics

    def test_single_unpinned_uses_dependency_singular(self) -> None:
        diag = DiagnosticCollector()
        pkg = _make_pkg(repo_url="github.com/owner/repo", reference=None)
        ctx = _make_ctx(unpinned_count=1, installed_packages=[pkg], diagnostics=diag)
        run(ctx)
        assert diag.has_diagnostics
        messages = [d.message for d in diag._diagnostics]
        assert any("dependency" in m and "dependencies" not in m for m in messages)

    def test_multiple_unpinned_uses_dependencies_plural(self) -> None:
        diag = DiagnosticCollector()
        pkgs = [_make_pkg(repo_url=f"github.com/o/r{i}", reference=None) for i in range(3)]
        ctx = _make_ctx(unpinned_count=3, installed_packages=pkgs, diagnostics=diag)
        run(ctx)
        messages = [d.message for d in diag._diagnostics]
        assert any("dependencies" in m for m in messages)

    def test_unpinned_names_shown_in_warning(self) -> None:
        diag = DiagnosticCollector()
        pkg = _make_pkg(repo_url="github.com/owner/repo", reference=None)
        ctx = _make_ctx(unpinned_count=1, installed_packages=[pkg], diagnostics=diag)
        run(ctx)
        messages = [d.message for d in diag._diagnostics]
        assert any("github.com/owner/repo" in m for m in messages)

    def test_more_than_five_unpinned_shows_and_more(self) -> None:
        diag = DiagnosticCollector()
        pkgs = [_make_pkg(repo_url=f"github.com/o/r{i}", reference=None) for i in range(8)]
        ctx = _make_ctx(unpinned_count=8, installed_packages=pkgs, diagnostics=diag)
        run(ctx)
        messages = [d.message for d in diag._diagnostics]
        assert any("and 3 more" in m for m in messages)

    def test_unpinned_with_no_repo_url_falls_back_to_count_only(self) -> None:
        diag = DiagnosticCollector()
        pkg = MagicMock()
        dep_ref = MagicMock()
        dep_ref.reference = None
        dep_ref.repo_url = None
        dep_ref.local_path = None
        pkg.dep_ref = dep_ref
        ctx = _make_ctx(unpinned_count=1, installed_packages=[pkg], diagnostics=diag)
        run(ctx)
        messages = [d.message for d in diag._diagnostics]
        # Falls back to count-only message (no names list)
        assert any("1 dependency unpinned" in m for m in messages)

    def test_pinned_packages_not_included_in_unpinned_names(self) -> None:
        diag = DiagnosticCollector()
        unpinned = _make_pkg(repo_url="github.com/o/unpinned", reference=None)
        pinned = _make_pkg(repo_url="github.com/o/pinned", reference="v1.0.0")
        ctx = _make_ctx(
            unpinned_count=1,
            installed_packages=[unpinned, pinned],
            diagnostics=diag,
        )
        run(ctx)
        messages = [d.message for d in diag._diagnostics]
        assert any("github.com/o/pinned" not in m for m in messages)

    def test_duplicate_unpinned_names_deduped(self) -> None:
        diag = DiagnosticCollector()
        pkgs = [_make_pkg(repo_url="github.com/o/same", reference=None) for _ in range(3)]
        ctx = _make_ctx(unpinned_count=3, installed_packages=pkgs, diagnostics=diag)
        run(ctx)
        messages = [d.message for d in diag._diagnostics]
        # "same" should only appear once in the names list
        for m in messages:
            if "github.com/o/same" in m:
                assert m.count("github.com/o/same") == 1

    def test_pkg_without_dep_ref_attribute_handled(self) -> None:
        diag = DiagnosticCollector()
        pkg = MagicMock(spec=[])  # no dep_ref attribute
        ctx = _make_ctx(unpinned_count=1, installed_packages=[pkg], diagnostics=diag)
        run(ctx)  # should not raise
