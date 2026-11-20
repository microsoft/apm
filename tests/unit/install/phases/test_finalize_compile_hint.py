"""Project compile hint decided from the files an install left on disk."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytestmark = [pytest.mark.component, pytest.mark.promise("P-OWN-user-facing-output-diagnostics")]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_target(name, compile_family, *, user_supported=True):
    """Create a fake TargetProfile whose for_scope(user_scope=True) returns self.

    When *user_supported* is False, for_scope returns None to model a target
    that does not support user scope.
    """
    profile = SimpleNamespace(name=name, compile_family=compile_family)
    profile.include_scoped_in_user_root_context = False
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


# ---------------------------------------------------------------------------
# finalize.run() integration tests
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _hint_project_compile_needed tests  (regression: issue #2057)
# ---------------------------------------------------------------------------


class TestHintProjectCompileNeeded:
    """Tests for _hint_project_compile_needed() -- the project-scope compile hint.

    Regression guard: before the fix for #2057, installing a local Gemini package
    produced no hint.  The user had to know independently that ``apm compile``
    was required; GEMINI.md never received the dep's instructions.
    """

    def test_hint_fires_for_compile_only_target_with_instruction_files(self, tmp_path):
        """Gemini target + dep has instruction files -> hint is emitted."""
        from apm_cli.core.scope import InstallScope
        from apm_cli.install.phases.finalize import _hint_project_compile_needed

        # Plant a dep instruction file in apm_modules
        instr = tmp_path / "apm_modules" / "_local" / "mypkg" / ".apm" / "instructions"
        instr.mkdir(parents=True)
        (instr / "rules.instructions.md").write_text("# Rules\n")

        ctx = _make_install_context(
            scope=InstallScope.PROJECT,
            targets=[_make_target("Gemini CLI", "gemini")],
        )
        ctx.apm_modules_dir = tmp_path / "apm_modules"
        ctx.project_root = tmp_path

        _hint_project_compile_needed(ctx)

        ctx.logger.info.assert_called_once()
        message = ctx.logger.info.call_args.args[0]
        assert message == (
            "Instructions installed for Gemini CLI. "
            "Run 'apm compile' to update AGENTS.md / GEMINI.md."
        )

    def test_instruction_scan_ignores_symlink_outside_apm_modules(self, tmp_path):
        """An escaping instruction symlink cannot trigger the compile hint."""
        from apm_cli.install.phases.finalize import _has_dep_instruction_files

        apm_modules = tmp_path / "apm_modules"
        instructions = apm_modules / "pkg" / ".apm" / "instructions"
        instructions.mkdir(parents=True)
        outside = tmp_path / "outside.instructions.md"
        outside.write_text("# Outside\n", encoding="utf-8")
        linked_instruction = instructions / "linked.instructions.md"
        try:
            linked_instruction.symlink_to(outside)
        except (NotImplementedError, OSError) as exc:
            pytest.skip(f"symlink creation not supported here: {exc}")

        ctx = _make_install_context()
        ctx.apm_modules_dir = apm_modules
        ctx.project_root = tmp_path

        assert _has_dep_instruction_files(ctx) is False
        ctx.diagnostics.warn.assert_called_once_with(
            "Ignored unsafe instruction link that resolves outside "
            "apm_modules: pkg/.apm/instructions/linked.instructions.md"
        )

    def test_hint_lists_only_outputs_for_active_compile_families(self, tmp_path):
        """Mixed target hints name each output that compile will produce."""
        from apm_cli.install.phases.finalize import _hint_project_compile_needed

        instr = tmp_path / "apm_modules" / "pkg" / ".apm" / "instructions"
        instr.mkdir(parents=True)
        (instr / "rules.instructions.md").write_text("# Rules\n")
        ctx = _make_install_context(
            targets=[
                _make_target("Gemini CLI", "gemini"),
                _make_target("Codex", "agents"),
                _make_target("Gemini CLI", "gemini"),
            ]
        )
        ctx.apm_modules_dir = tmp_path / "apm_modules"
        ctx.project_root = tmp_path

        _hint_project_compile_needed(ctx)

        ctx.logger.info.assert_called_once_with(
            "Instructions installed for Gemini CLI, Codex. "
            "Run 'apm compile' to update AGENTS.md / GEMINI.md.",
            symbol="info",
        )

    def test_hint_not_fired_for_claude_target(self, tmp_path):
        """Claude receives dependency instructions directly in .claude/rules."""
        from apm_cli.install.phases.finalize import _hint_project_compile_needed

        instr = tmp_path / "apm_modules" / "pkg" / ".apm" / "instructions"
        instr.mkdir(parents=True)
        (instr / "rules.instructions.md").write_text("# Rules\n")
        ctx = _make_install_context(targets=[_make_target("Claude Code", "claude")])
        ctx.apm_modules_dir = tmp_path / "apm_modules"
        ctx.project_root = tmp_path

        _hint_project_compile_needed(ctx)

        ctx.logger.info.assert_not_called()

    def test_hint_ignores_target_without_project_scope(self, tmp_path):
        """A target unavailable at project scope cannot trigger the hint."""
        from apm_cli.install.phases.finalize import _hint_project_compile_needed

        instr = tmp_path / "apm_modules" / "pkg" / ".apm" / "instructions"
        instr.mkdir(parents=True)
        (instr / "rules.instructions.md").write_text("# Rules\n")
        target = _make_target("Unavailable", "agents")
        target.for_scope = MagicMock(return_value=None)
        ctx = _make_install_context(targets=[target])
        ctx.apm_modules_dir = tmp_path / "apm_modules"
        ctx.project_root = tmp_path

        _hint_project_compile_needed(ctx)

        ctx.logger.info.assert_not_called()

    def test_hint_not_fired_for_intellij_target(self, tmp_path):
        """IntelliJ is MCP-only and has no verified root-context reader."""
        from apm_cli.install.phases.finalize import _hint_project_compile_needed

        instr = tmp_path / "apm_modules" / "pkg" / ".apm" / "instructions"
        instr.mkdir(parents=True)
        (instr / "rules.instructions.md").write_text("# Rules\n")
        ctx = _make_install_context(targets=[_make_target("intellij", "agents")])
        ctx.apm_modules_dir = tmp_path / "apm_modules"
        ctx.project_root = tmp_path

        _hint_project_compile_needed(ctx)

        ctx.logger.info.assert_not_called()

    def test_hint_not_fired_for_copilot_target(self, tmp_path):
        """Copilot target (native per-file rules) -> no hint."""
        from apm_cli.core.scope import InstallScope
        from apm_cli.install.phases.finalize import _hint_project_compile_needed

        instr = tmp_path / "apm_modules" / "_local" / "pkg" / ".apm" / "instructions"
        instr.mkdir(parents=True)
        (instr / "rules.instructions.md").write_text("# Rules\n")

        ctx = _make_install_context(
            scope=InstallScope.PROJECT,
            targets=[_make_target("GitHub Copilot", "copilot")],
        )
        ctx.apm_modules_dir = tmp_path / "apm_modules"
        ctx.project_root = tmp_path

        _hint_project_compile_needed(ctx)

        ctx.logger.info.assert_not_called()

    def test_hint_not_fired_when_no_instruction_files(self, tmp_path):
        """Gemini target but no dep instruction files -> no hint."""
        from apm_cli.core.scope import InstallScope
        from apm_cli.install.phases.finalize import _hint_project_compile_needed

        (tmp_path / "apm_modules").mkdir()

        ctx = _make_install_context(
            scope=InstallScope.PROJECT,
            targets=[_make_target("Gemini CLI", "gemini")],
        )
        ctx.apm_modules_dir = tmp_path / "apm_modules"
        ctx.project_root = tmp_path

        _hint_project_compile_needed(ctx)

        ctx.logger.info.assert_not_called()

    def test_hint_not_fired_on_dry_run(self, tmp_path):
        """Dry-run installs do not emit the hint."""
        from apm_cli.core.scope import InstallScope
        from apm_cli.install.phases.finalize import _hint_project_compile_needed

        instr = tmp_path / "apm_modules" / "_local" / "pkg" / ".apm" / "instructions"
        instr.mkdir(parents=True)
        (instr / "rules.instructions.md").write_text("# Rules\n")

        ctx = _make_install_context(
            scope=InstallScope.PROJECT,
            targets=[_make_target("Gemini CLI", "gemini")],
            dry_run=True,
        )
        ctx.apm_modules_dir = tmp_path / "apm_modules"
        ctx.project_root = tmp_path

        _hint_project_compile_needed(ctx)

        ctx.logger.info.assert_not_called()

    def test_hint_not_fired_when_nothing_installed(self, tmp_path):
        """installed_count == 0 -> no hint (no-op install)."""
        from apm_cli.core.scope import InstallScope
        from apm_cli.install.phases.finalize import _hint_project_compile_needed

        instr = tmp_path / "apm_modules" / "_local" / "pkg" / ".apm" / "instructions"
        instr.mkdir(parents=True)
        (instr / "rules.instructions.md").write_text("# Rules\n")

        ctx = _make_install_context(
            scope=InstallScope.PROJECT,
            targets=[_make_target("Gemini CLI", "gemini")],
        )
        ctx.installed_count = 0
        ctx.apm_modules_dir = tmp_path / "apm_modules"
        ctx.project_root = tmp_path

        _hint_project_compile_needed(ctx)

        ctx.logger.info.assert_not_called()

    def test_hint_not_fired_for_excluded_target_name(self, tmp_path):
        """Targets in the exclusion set are skipped even if family matches."""
        from apm_cli.core.scope import InstallScope
        from apm_cli.install.phases.finalize import _hint_project_compile_needed

        instr = tmp_path / "apm_modules" / "_local" / "pkg" / ".apm" / "instructions"
        instr.mkdir(parents=True)
        (instr / "rules.instructions.md").write_text("# Rules\n")

        # "cursor" is in _ROOT_CONTEXT_HINT_EXCLUDED_TARGETS
        ctx = _make_install_context(
            scope=InstallScope.PROJECT,
            targets=[_make_target("cursor", "agents")],
        )
        ctx.apm_modules_dir = tmp_path / "apm_modules"
        ctx.project_root = tmp_path

        _hint_project_compile_needed(ctx)

        ctx.logger.info.assert_not_called()
