"""Unit tests for finalize.py install-time global-instructions hint.

Covers _hint_global_root_context and its integration in run():

* hint fires when global instructions land on a root-context-only target
* hint suppressed when no global instructions were installed
* hint suppressed when only directory-native targets are active
* hint suppressed on dry-run
* hint writes NO file (read-only)
* run(): does NOT hint for PROJECT scope
* run(): DOES hint for USER scope
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_target(name, compile_family, *, user_supported=True):
    """Create a fake TargetProfile whose for_scope(user_scope=True) returns self.

    When *user_supported* is False, for_scope returns None to model a target
    that does not support user scope.
    """
    profile = SimpleNamespace(name=name, compile_family=compile_family)
    profile.for_scope = MagicMock(return_value=profile if user_supported else None)
    return profile


def _make_install_context(scope=None, targets=None, dry_run=False):
    """Create a mock InstallContext for finalize.run()/hint tests."""
    ctx = MagicMock()
    ctx.scope = scope
    ctx.logger = MagicMock()
    ctx.dry_run = dry_run
    ctx.targets = targets if targets is not None else []
    ctx.total_links_resolved = 0
    ctx.total_commands_integrated = 0
    ctx.total_hooks_integrated = 0
    ctx.total_instructions_integrated = 0
    ctx.installed_count = 1
    ctx.unpinned_count = 0
    ctx.installed_packages = []
    ctx.package_types = {}
    ctx.diagnostics = MagicMock()
    return ctx


# ---------------------------------------------------------------------------
# _hint_global_root_context tests
# ---------------------------------------------------------------------------


class TestHintGlobalRootContext:
    """Tests for _hint_global_root_context()."""

    def test_hint_fires_for_root_context_target(self):
        """Global instructions + a root-context target -> one hint line."""
        from apm_cli.core.scope import InstallScope
        from apm_cli.install.phases.finalize import _hint_global_root_context

        ctx = _make_install_context(
            scope=InstallScope.USER,
            targets=[_make_target("Claude Code", "claude")],
        )

        with (
            patch(
                "apm_cli.core.scope.get_apm_dir",
                return_value=Path.home() / ".apm",
            ),
            patch(
                "apm_cli.compilation.user_root_context.discover_global_instructions",
                return_value=[SimpleNamespace(apply_to=None)],
            ),
        ):
            _hint_global_root_context(ctx)

        ctx.logger.info.assert_called_once()
        message = ctx.logger.info.call_args.args[0]
        assert "apm compile -g" in message
        assert "Claude Code" in message
        assert "root context files" in message
        assert ctx.logger.info.call_args.kwargs.get("symbol") == "info"

    def test_hint_lists_multiple_root_context_targets(self):
        """All distinct root-context target names are listed once each."""
        from apm_cli.core.scope import InstallScope
        from apm_cli.install.phases.finalize import _hint_global_root_context

        ctx = _make_install_context(
            scope=InstallScope.USER,
            targets=[
                _make_target("Codex", "agents"),
                _make_target("Gemini CLI", "gemini"),
                # duplicate family/name should be de-duped
                _make_target("Codex", "agents"),
            ],
        )

        with (
            patch(
                "apm_cli.core.scope.get_apm_dir",
                return_value=Path.home() / ".apm",
            ),
            patch(
                "apm_cli.compilation.user_root_context.discover_global_instructions",
                return_value=[SimpleNamespace(apply_to=None)],
            ),
        ):
            _hint_global_root_context(ctx)

        message = ctx.logger.info.call_args.args[0]
        assert message.count("Codex") == 1
        assert "Gemini CLI" in message

    def test_no_hint_when_no_global_instructions(self):
        """No global instructions installed -> no hint."""
        from apm_cli.core.scope import InstallScope
        from apm_cli.install.phases.finalize import _hint_global_root_context

        ctx = _make_install_context(
            scope=InstallScope.USER,
            targets=[_make_target("Claude Code", "claude")],
        )

        with (
            patch(
                "apm_cli.core.scope.get_apm_dir",
                return_value=Path.home() / ".apm",
            ),
            patch(
                "apm_cli.compilation.user_root_context.discover_global_instructions",
                return_value=[],
            ),
        ):
            _hint_global_root_context(ctx)

        ctx.logger.info.assert_not_called()

    def test_no_hint_when_only_directory_native_targets(self):
        """Only directory-native (vscode/copilot) targets active -> no hint."""
        from apm_cli.core.scope import InstallScope
        from apm_cli.install.phases.finalize import _hint_global_root_context

        ctx = _make_install_context(
            scope=InstallScope.USER,
            targets=[_make_target("GitHub Copilot", "vscode")],
        )

        with (
            patch(
                "apm_cli.core.scope.get_apm_dir",
                return_value=Path.home() / ".apm",
            ),
            patch(
                "apm_cli.compilation.user_root_context.discover_global_instructions",
                return_value=[SimpleNamespace(apply_to=None)],
            ),
        ):
            _hint_global_root_context(ctx)

        ctx.logger.info.assert_not_called()

    def test_no_hint_for_user_scope_native_rules_target(self):
        """Targets with native user-scope rules are ignored even if family is agents."""
        from apm_cli.core.scope import InstallScope
        from apm_cli.install.phases.finalize import _hint_global_root_context

        ctx = _make_install_context(
            scope=InstallScope.USER,
            targets=[_make_target("cursor", "agents")],
        )

        with (
            patch(
                "apm_cli.core.scope.get_apm_dir",
                return_value=Path.home() / ".apm",
            ),
            patch(
                "apm_cli.compilation.user_root_context.discover_global_instructions",
                return_value=[SimpleNamespace(apply_to=None)],
            ),
        ):
            _hint_global_root_context(ctx)

        ctx.logger.info.assert_not_called()

    def test_hint_uses_context_logger_when_available(self):
        """The install hint routes through the command logger when present."""
        from apm_cli.core.scope import InstallScope
        from apm_cli.install.phases.finalize import _hint_global_root_context

        logger = MagicMock()
        ctx = _make_install_context(
            scope=InstallScope.USER,
            targets=[_make_target("Codex", "agents")],
        )
        ctx.logger = logger

        with (
            patch(
                "apm_cli.core.scope.get_apm_dir",
                return_value=Path.home() / ".apm",
            ),
            patch(
                "apm_cli.compilation.user_root_context.discover_global_instructions",
                return_value=[SimpleNamespace(apply_to=None)],
            ),
        ):
            _hint_global_root_context(ctx)

        logger.info.assert_called_once()

    def test_no_hint_for_targets_without_user_scope(self):
        """Targets whose for_scope returns None are ignored."""
        from apm_cli.core.scope import InstallScope
        from apm_cli.install.phases.finalize import _hint_global_root_context

        ctx = _make_install_context(
            scope=InstallScope.USER,
            targets=[_make_target("Claude Code", "claude", user_supported=False)],
        )

        with (
            patch(
                "apm_cli.core.scope.get_apm_dir",
                return_value=Path.home() / ".apm",
            ),
            patch(
                "apm_cli.compilation.user_root_context.discover_global_instructions",
                return_value=[SimpleNamespace(apply_to=None)],
            ),
        ):
            _hint_global_root_context(ctx)

        ctx.logger.info.assert_not_called()

    def test_no_hint_on_dry_run(self):
        """Dry-run installs do not emit the hint and skip discovery entirely."""
        from apm_cli.core.scope import InstallScope
        from apm_cli.install.phases.finalize import _hint_global_root_context

        ctx = _make_install_context(
            scope=InstallScope.USER,
            targets=[_make_target("Claude Code", "claude")],
            dry_run=True,
        )

        with (
            patch(
                "apm_cli.compilation.user_root_context.discover_global_instructions",
            ) as mock_discover,
        ):
            _hint_global_root_context(ctx)

        mock_discover.assert_not_called()
        ctx.logger.info.assert_not_called()

    def test_hint_writes_no_file(self):
        """The hint never calls compile_user_root_contexts (read-only)."""
        from apm_cli.core.scope import InstallScope
        from apm_cli.install.phases.finalize import _hint_global_root_context

        ctx = _make_install_context(
            scope=InstallScope.USER,
            targets=[_make_target("Codex", "agents")],
        )

        with (
            patch(
                "apm_cli.core.scope.get_apm_dir",
                return_value=Path.home() / ".apm",
            ),
            patch(
                "apm_cli.compilation.user_root_context.discover_global_instructions",
                return_value=[SimpleNamespace(apply_to=None)],
            ),
            patch(
                "apm_cli.compilation.user_root_context.compile_user_root_contexts",
            ) as mock_compile,
        ):
            _hint_global_root_context(ctx)

        mock_compile.assert_not_called()


# ---------------------------------------------------------------------------
# finalize.run() integration tests
# ---------------------------------------------------------------------------


class TestFinalizeRunIntegration:
    """Tests for run() function's integration of the hint hook."""

    def test_project_scope_no_hint(self):
        """When ctx.scope is PROJECT, hint hook is NOT called."""
        from apm_cli.core.scope import InstallScope
        from apm_cli.install.phases.finalize import run

        ctx = _make_install_context(scope=InstallScope.PROJECT)

        with patch(
            "apm_cli.install.phases.finalize._hint_global_root_context",
        ) as mock_hint:
            run(ctx)

        mock_hint.assert_not_called()

    def test_user_scope_hint_called(self):
        """When ctx.scope is USER, hint hook IS called with the context."""
        from apm_cli.core.scope import InstallScope
        from apm_cli.install.phases.finalize import run

        ctx = _make_install_context(scope=InstallScope.USER)

        with patch(
            "apm_cli.install.phases.finalize._hint_global_root_context",
        ) as mock_hint:
            result = run(ctx)

        mock_hint.assert_called_once_with(ctx)
        assert result is not None

    def test_none_scope_no_hint(self):
        """When ctx.scope is None, hint hook is NOT called."""
        from apm_cli.install.phases.finalize import run

        ctx = _make_install_context(scope=None)

        with patch(
            "apm_cli.install.phases.finalize._hint_global_root_context",
        ) as mock_hint:
            run(ctx)

        mock_hint.assert_not_called()

    def test_run_returns_install_result(self):
        """run() returns an InstallResult object."""
        from apm_cli.core.scope import InstallScope
        from apm_cli.install.phases.finalize import run
        from apm_cli.models.results import InstallResult

        ctx = _make_install_context(scope=InstallScope.USER)

        with patch(
            "apm_cli.install.phases.finalize._hint_global_root_context",
        ):
            result = run(ctx)

        assert isinstance(result, InstallResult)
        assert result.installed_count == 1
