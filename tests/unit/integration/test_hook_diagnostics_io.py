"""Unit tests for target-aware hook event diagnostics (filesystem, process and CLI boundary cases)."""

import json
import logging
import shutil
import tempfile
from pathlib import Path

import pytest

from apm_cli.integration.hook_integrator import (
    HookIntegrator,
)
from apm_cli.integration.targets import KNOWN_TARGETS
from apm_cli.models.apm_package import APMPackage, PackageInfo

pytestmark = pytest.mark.component


def _make_package_info(install_path: Path, name: str = "test-pkg") -> PackageInfo:
    package = APMPackage(name=name, version="1.0.0")
    return PackageInfo(package=package, install_path=install_path)


# ---------------------------------------------------------------------------
# _detect_event_casing
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _HOOK_EVENT_EXPECTED_CASING coverage
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _emit_hook_event_diagnostics -- INFO logging
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _emit_hook_event_diagnostics -- mismatch warnings
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Multi-target integration fixture
# ---------------------------------------------------------------------------


class TestMultiTargetHookDiagnostics:
    """Full integration: integrate_hooks_for_target emits per-target diagnostics."""

    @pytest.fixture
    def temp_project(self):
        temp_dir = tempfile.mkdtemp()
        project = Path(temp_dir)
        (project / ".github").mkdir()
        (project / ".claude").mkdir()
        yield project
        shutil.rmtree(temp_dir, ignore_errors=True)

    def _make_package_with_hooks(
        self, project: Path, hooks_data: dict, pkg_name: str = "diag-pkg"
    ) -> PackageInfo:
        """Create a package with hooks in the .apm/hooks dir."""
        pkg_dir = project / "apm_modules" / pkg_name
        hooks_dir = pkg_dir / ".apm" / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        (hooks_dir / "hooks.json").write_text(json.dumps(hooks_data, indent=2))
        return _make_package_info(pkg_dir, pkg_name)

    def test_copilot_target_logs_events(self, temp_project, caplog):
        """Copilot (VSCode) deploy logs events at INFO."""
        pkg_info = self._make_package_with_hooks(
            temp_project,
            {
                "hooks": {
                    "preToolUse": [{"type": "command", "bash": "echo pre"}],
                    "postToolUse": [{"type": "command", "bash": "echo post"}],
                }
            },
        )
        integrator = HookIntegrator()
        copilot = KNOWN_TARGETS["copilot"]

        with caplog.at_level(logging.INFO, logger="apm_cli.integration.hook_integrator"):
            integrator.integrate_hooks_for_target(copilot, pkg_info, temp_project)

        log_text = caplog.text
        assert "copilot" in log_text
        assert "preToolUse" in log_text

    def test_claude_target_logs_events(self, temp_project, caplog):
        """Claude deploy logs events at INFO."""
        pkg_info = self._make_package_with_hooks(
            temp_project,
            {
                "hooks": {
                    "PreToolUse": [{"hooks": [{"type": "command", "command": "echo pre"}]}],
                    "Stop": [{"hooks": [{"type": "command", "command": "echo stop"}]}],
                }
            },
        )
        integrator = HookIntegrator()
        claude = KNOWN_TARGETS["claude"]

        with caplog.at_level(logging.INFO, logger="apm_cli.integration.hook_integrator"):
            integrator.integrate_hooks_for_target(claude, pkg_info, temp_project)

        log_text = caplog.text
        assert "claude" in log_text
        assert "PreToolUse" in log_text or "Stop" in log_text

    def test_multi_target_deploy_logs_per_target_events(self, temp_project, caplog):
        """Deploying to both copilot and claude logs distinct per-target event records."""
        pkg_info = self._make_package_with_hooks(
            temp_project,
            {
                "hooks": {
                    "preToolUse": [{"type": "command", "bash": "echo pre"}],
                    "PostToolUse": [{"hooks": [{"type": "command", "command": "echo post"}]}],
                }
            },
        )
        integrator = HookIntegrator()
        copilot = KNOWN_TARGETS["copilot"]
        claude = KNOWN_TARGETS["claude"]

        with caplog.at_level(logging.INFO, logger="apm_cli.integration.hook_integrator"):
            integrator.integrate_hooks_for_target(copilot, pkg_info, temp_project)
            integrator.integrate_hooks_for_target(claude, pkg_info, temp_project)

        info_records = [r for r in caplog.records if r.levelno == logging.INFO]
        targets_logged = {
            ("copilot" if "copilot" in r.getMessage() else "claude")
            for r in info_records
            if "copilot" in r.getMessage() or "claude" in r.getMessage()
        }
        # Both targets must have at least one INFO record
        assert "copilot" in targets_logged, "No INFO log for copilot target"
        assert "claude" in targets_logged, "No INFO log for claude target"

    def test_session_start_alias_on_claude_target_emits_no_warning(self, temp_project, capsys):
        """Claude recognizes the portable camelCase session-start alias."""
        pkg_info = self._make_package_with_hooks(
            temp_project,
            {
                "hooks": {
                    "sessionStart": [{"type": "command", "bash": "echo start"}],
                }
            },
        )
        integrator = HookIntegrator()
        claude = KNOWN_TARGETS["claude"]

        integrator.integrate_hooks_for_target(claude, pkg_info, temp_project)

        captured = capsys.readouterr()
        assert "may not be recognized" not in captured.out

    def test_collision_suppresses_diagnostics(self, temp_project, caplog, capsys):
        """No diagnostics are emitted for a hook file skipped due to collision.

        Regression trap: diagnostics must fire AFTER the collision gate, not before.
        A pre-existing user-authored hook file causes integrate_package_hooks to
        skip that file via check_collision. No casing warning or INFO log for
        events in the skipped file should reach the user.
        """
        pkg_info = self._make_package_with_hooks(
            temp_project,
            {
                "hooks": {
                    # PascalCase event on copilot (camelCase) target -> would warn
                    "PreToolUse": [{"type": "command", "bash": "echo pre"}],
                }
            },
        )
        integrator = HookIntegrator()
        copilot = KNOWN_TARGETS["copilot"]

        # Pre-create the target file to trigger a collision (user-authored, not managed)
        hooks_dir = temp_project / ".github" / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        collision_file = hooks_dir / "diag-pkg-hooks.json"
        collision_file.write_text("{}", encoding="utf-8")

        with caplog.at_level(logging.INFO, logger="apm_cli.integration.hook_integrator"):
            integrator.integrate_hooks_for_target(
                copilot, pkg_info, temp_project, managed_files=set()
            )

        # No INFO diagnostic about events from the skipped file
        info_records = [r for r in caplog.records if r.levelno == logging.INFO]
        event_logs = [r for r in info_records if "PreToolUse" in r.getMessage()]
        assert not event_logs, "INFO diagnostic must not fire for collision-skipped hook files"

        # No casing mismatch warning (PascalCase on camelCase target would warn if emitted)
        captured = capsys.readouterr()
        assert "may not be recognized" not in captured.out, (
            "Casing warning must not fire for collision-skipped hook files"
        )
