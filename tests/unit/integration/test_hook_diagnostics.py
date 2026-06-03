"""Unit tests for target-aware hook event diagnostics.

Covers:
- _emit_hook_event_diagnostics logs event names per target at INFO level
- Naming-convention mismatch triggers a user-visible warning
- Multi-target fixture: per-target diagnostics distinguish events correctly
- Full integration: integrate_hooks_for_target emits diagnostics during deploy
- Regression: diagnostics suppressed when collision forces hook file skip
"""

import json
import logging
import shutil
import tempfile
from pathlib import Path

import pytest

from apm_cli.integration.hook_integrator import (
    _HOOK_EVENT_EXPECTED_CASING,
    HookIntegrator,
    _detect_event_casing,
    _emit_hook_event_diagnostics,
)
from apm_cli.integration.targets import KNOWN_TARGETS
from apm_cli.models.apm_package import APMPackage, PackageInfo


def _make_package_info(install_path: Path, name: str = "test-pkg") -> PackageInfo:
    package = APMPackage(name=name, version="1.0.0")
    return PackageInfo(package=package, install_path=install_path)


# ---------------------------------------------------------------------------
# _detect_event_casing
# ---------------------------------------------------------------------------


class TestDetectEventCasing:
    def test_camel_case(self):
        assert _detect_event_casing("preToolUse") == "camelCase"

    def test_camel_case_session_start(self):
        assert _detect_event_casing("sessionStart") == "camelCase"

    def test_pascal_case(self):
        assert _detect_event_casing("PreToolUse") == "PascalCase"

    def test_pascal_case_session_start(self):
        assert _detect_event_casing("SessionStart") == "PascalCase"

    def test_lowercase_only(self):
        assert _detect_event_casing("stop") is None

    def test_empty_string(self):
        assert _detect_event_casing("") is None


# ---------------------------------------------------------------------------
# _HOOK_EVENT_EXPECTED_CASING coverage
# ---------------------------------------------------------------------------


class TestHookEventExpectedCasing:
    def test_copilot_expects_camel(self):
        assert _HOOK_EVENT_EXPECTED_CASING["copilot"] == "camelCase"

    def test_claude_expects_pascal(self):
        assert _HOOK_EVENT_EXPECTED_CASING["claude"] == "PascalCase"

    def test_cursor_expects_pascal(self):
        assert _HOOK_EVENT_EXPECTED_CASING["cursor"] == "PascalCase"

    def test_vscode_expects_pascal(self):
        assert _HOOK_EVENT_EXPECTED_CASING["vscode"] == "PascalCase"


# ---------------------------------------------------------------------------
# _emit_hook_event_diagnostics -- INFO logging
# ---------------------------------------------------------------------------


class TestEmitHookEventDiagnosticsLogging:
    """Verify that per-target event names appear in structured INFO logs."""

    def test_logs_events_at_info_level(self, caplog):
        with caplog.at_level(logging.INFO, logger="apm_cli.integration.hook_integrator"):
            _emit_hook_event_diagnostics(["PreToolUse", "Stop"], "claude", {})

        assert "claude" in caplog.text
        assert "PreToolUse" in caplog.text
        assert "Stop" in caplog.text

    def test_logs_events_for_copilot_target(self, caplog):
        with caplog.at_level(logging.INFO, logger="apm_cli.integration.hook_integrator"):
            _emit_hook_event_diagnostics(["preToolUse", "postToolUse"], "copilot", {})

        assert "copilot" in caplog.text
        assert "preToolUse" in caplog.text
        assert "postToolUse" in caplog.text

    def test_no_log_for_empty_events(self, caplog):
        with caplog.at_level(logging.INFO, logger="apm_cli.integration.hook_integrator"):
            _emit_hook_event_diagnostics([], "claude", {})

        assert caplog.text == ""

    def test_multi_target_logs_are_distinct(self, caplog):
        """Events logged per target must be distinguishable in log output."""
        with caplog.at_level(logging.INFO, logger="apm_cli.integration.hook_integrator"):
            _emit_hook_event_diagnostics(["PreToolUse", "Stop"], "claude", {})
            _emit_hook_event_diagnostics(["preToolUse", "postToolUse"], "copilot", {})

        records = [r for r in caplog.records if r.levelno == logging.INFO]
        claude_records = [r for r in records if "claude" in r.getMessage()]
        copilot_records = [r for r in records if "copilot" in r.getMessage()]

        assert claude_records, "Expected INFO record for claude target"
        assert copilot_records, "Expected INFO record for copilot target"
        # Each target's record contains that target's events
        assert "PreToolUse" in claude_records[0].getMessage()
        assert "preToolUse" in copilot_records[0].getMessage()


# ---------------------------------------------------------------------------
# _emit_hook_event_diagnostics -- mismatch warnings
# ---------------------------------------------------------------------------


class TestEmitHookEventDiagnosticsMismatch:
    """Convention-mismatch events without a known mapping trigger user warning."""

    def test_warns_camel_event_on_pascal_target(self, capsys):
        # "sessionStart" is camelCase but claude expects PascalCase;
        # no mapping exists so it may not be recognized.
        _emit_hook_event_diagnostics(["sessionStart"], "claude", {})
        captured = capsys.readouterr()
        assert "sessionStart" in captured.out

    def test_warns_pascal_event_on_camel_target(self, capsys):
        # "PreToolUse" is PascalCase but copilot expects camelCase;
        # no mapping exists so it may not be recognized.
        _emit_hook_event_diagnostics(["PreToolUse"], "copilot", {})
        captured = capsys.readouterr()
        assert "PreToolUse" in captured.out

    def test_no_warn_when_event_is_mapped(self, capsys):
        # "preToolUse" is remapped to "PreToolUse" for claude, so no warning.
        event_map = {"preToolUse": "PreToolUse"}
        _emit_hook_event_diagnostics(["preToolUse"], "claude", event_map)
        captured = capsys.readouterr()
        # No user-visible warning about mismatched casing for a mapped event
        assert "may not be recognized" not in captured.out

    def test_no_warn_when_casing_matches_target(self, capsys):
        _emit_hook_event_diagnostics(["PreToolUse", "Stop"], "claude", {})
        captured = capsys.readouterr()
        assert "may not be recognized" not in captured.out


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

    def test_camel_event_on_claude_target_emits_warning(self, temp_project, capsys):
        """camelCase event deployed to claude (PascalCase target) produces warning."""
        pkg_info = self._make_package_with_hooks(
            temp_project,
            {
                "hooks": {
                    # sessionStart is camelCase but has NO mapping for claude
                    "sessionStart": [{"type": "command", "bash": "echo start"}],
                }
            },
        )
        integrator = HookIntegrator()
        claude = KNOWN_TARGETS["claude"]

        integrator.integrate_hooks_for_target(claude, pkg_info, temp_project)

        captured = capsys.readouterr()
        assert "sessionStart" in captured.out

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
