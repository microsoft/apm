"""Unit-level CLI-ergonomics folds for PR #2705.

These pin logger-call and drop-path guards directly, without spinning up the
whole install pipeline, so each mutation-break gate is fast and precise.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.copilot_plugins.catalog import NativePluginEntry
from apm_cli.copilot_plugins.registrar import (
    ResolvedPluginCandidate,
    _log_registration,
    discover_native_plugins,
)


class _FakeLogger:
    """Record every logger call as ``(method, message, kwargs)`` tuples."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    def __getattr__(self, name: str):
        def _record(message: str = "", **kwargs) -> None:
            self.calls.append((name, message, kwargs))

        return _record


def _entry(name: str) -> NativePluginEntry:
    return NativePluginEntry(
        dependency_key=f"owner/{name}",
        plugin_name=name,
        version="1.0.0",
        description=name,
        source=f"./{name}",
        root=Path("/tmp") / name,
    )


# ---------------------------------------------------------------------------
# Item 6a: the registration success line uses the green success layer.
# ---------------------------------------------------------------------------


def test_log_registration_uses_success_not_info() -> None:
    """The 'Registered ...' line is logged through ``success`` (green)."""
    logger = _FakeLogger()

    _log_registration(logger, [_entry("sentinel")], Path("/proj/settings.local.json"))

    methods = [method for method, _msg, _kw in logger.calls]
    assert "success" in methods
    assert "info" not in methods


# ---------------------------------------------------------------------------
# Item 6b: the registration summary caps its inline roster at three names.
# ---------------------------------------------------------------------------


def test_log_registration_caps_inline_roster_and_defers_full_list() -> None:
    """Four plugins => 'and 1 more' inline, full roster in verbose_detail."""
    logger = _FakeLogger()
    entries = [_entry(n) for n in ("alpha", "bravo", "charlie", "delta")]

    _log_registration(logger, entries, Path("/proj/settings.local.json"))

    success = next(msg for method, msg, _kw in logger.calls if method == "success")
    assert "and 1 more" in success
    assert "delta" not in success
    verbose = " ".join(msg for method, msg, _kw in logger.calls if method == "verbose_detail")
    assert "delta" in verbose


# ---------------------------------------------------------------------------
# Item 2: a dropped candidate emits an actionable verbose_detail.
# ---------------------------------------------------------------------------


def test_discover_drop_emits_verbose_detail_naming_dependency(tmp_path: Path) -> None:
    """A non-directory install path is dropped with a named verbose line."""
    logger = _FakeLogger()
    candidate = ResolvedPluginCandidate(
        dependency_key="owner/ghost",
        install_path=tmp_path / "missing",
        direct=True,
    )

    entries, collisions = discover_native_plugins([candidate], modules_dir=tmp_path, logger=logger)

    assert entries == []
    assert collisions == []
    verbose = [msg for method, msg, _kw in logger.calls if method == "verbose_detail"]
    assert any("owner/ghost" in msg and "not a directory" in msg for msg in verbose)


def test_discover_drop_names_excluded_target_subset(tmp_path: Path) -> None:
    """A target subset that excludes copilot drops with a named verbose line."""
    logger = _FakeLogger()
    plugin_root = tmp_path / "claude_only"
    plugin_root.mkdir()
    candidate = ResolvedPluginCandidate(
        dependency_key="owner/claude-only",
        install_path=plugin_root,
        direct=True,
        target_subset=("claude",),
    )

    discover_native_plugins([candidate], modules_dir=tmp_path, logger=logger)

    verbose = [msg for method, msg, _kw in logger.calls if method == "verbose_detail"]
    assert any("owner/claude-only" in msg and "copilot" in msg for msg in verbose)


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
