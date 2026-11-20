"""Unit-level CLI-ergonomics folds for PR #2705.

These pin the lifecycle warning wording directly, without spinning up the
whole install pipeline, so each mutation-break gate is fast and precise.
"""

from __future__ import annotations

from pathlib import Path

import pytest


class _FakeLogger:
    """Record every logger call as ``(method, message, kwargs)`` tuples."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    def __getattr__(self, name: str):
        def _record(message: str = "", **kwargs) -> None:
            self.calls.append((name, message, kwargs))

        return _record


# ---------------------------------------------------------------------------
# Item 6c: the lifecycle resync warning hides the Python class name and
# appends a concrete recovery action.
# ---------------------------------------------------------------------------


def test_prune_resync_warning_hides_exception_class_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A prune resync failure warns without leaking the exception class."""
    from apm_cli.commands import prune as prune_mod
    from apm_cli.copilot_plugins.settings import CopilotSettingsCollisionError

    def _boom(**_kwargs):
        raise CopilotSettingsCollisionError("settings.local.json already defines apm")

    monkeypatch.setattr("apm_cli.copilot_plugins.registrar.resync_native_plugins", _boom)
    logger = _FakeLogger()

    prune_mod._resync_native_plugins(tmp_path, tmp_path, None, logger, False)

    warning = next(msg for method, msg, _kw in logger.calls if method == "warning")
    assert "CopilotSettingsCollisionError" not in warning
    assert "Re-run 'apm install' to re-register once resolved." in warning


def test_uninstall_resync_warning_hides_exception_class_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An uninstall resync failure warns without leaking the exception class."""
    from apm_cli.commands.uninstall import cli as uninstall_cli
    from apm_cli.copilot_plugins.settings import CopilotSettingsCollisionError

    def _boom(**_kwargs):
        raise CopilotSettingsCollisionError("settings.local.json already defines apm")

    monkeypatch.setattr("apm_cli.copilot_plugins.registrar.resync_native_plugins", _boom)
    logger = _FakeLogger()

    uninstall_cli._resync_native_registration_after_uninstall(
        deploy_root=tmp_path,
        modules_dir=tmp_path,
        scope=None,
        lockfile=None,
        logger=logger,
    )

    warning = next(msg for method, msg, _kw in logger.calls if method == "warning")
    assert "CopilotSettingsCollisionError" not in warning
    assert "Re-run 'apm install' to re-register once resolved." in warning
