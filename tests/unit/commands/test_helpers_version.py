"""Unit tests for the verbose_version experimental branch in print_version.

Covers three scenarios:
  1. Baseline: flag disabled -- output is the standard version string only.
  2. Enabled:  flag enabled -- output adds Python, Platform, Install path lines
               with 14-character left-justified labels and 2-space indentation.
  3. Graceful failure: if is_enabled raises, --version still prints the baseline
     version string and exits 0 (the try/except wrapper must not swallow it).
"""

from __future__ import annotations

import re
from typing import Any
from unittest.mock import patch

import pytest
from click.testing import CliRunner

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def runner() -> CliRunner:
    """CliRunner -- stderr is merged into stdout by default in Click 8."""
    return CliRunner()


@pytest.fixture(autouse=True)
def _reset_config_cache():
    """Reset the in-process config cache before and after every test."""
    from apm_cli.config import _invalidate_config_cache

    _invalidate_config_cache()
    yield
    _invalidate_config_cache()


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path, monkeypatch) -> None:
    """Point config I/O at a throw-away temp directory."""
    import apm_cli.config as _conf

    config_dir = tmp_path / ".apm"
    monkeypatch.setattr(_conf, "CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(_conf, "CONFIG_FILE", str(config_dir / "config.json"))
    monkeypatch.setattr(_conf, "_config_cache", None)


@pytest.fixture(autouse=True)
def _reset_helpers_console(monkeypatch) -> None:
    """Reset the cached Rich console in _helpers so it is recreated fresh
    inside each CliRunner invocation (pointing at the captured stdout).
    """
    import apm_cli.commands._helpers as _h

    monkeypatch.setattr(_h, "_console", None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _invoke_version(runner: CliRunner) -> Any:
    """Invoke `apm --version` with update-check and experimental imports isolated."""
    from apm_cli.cli import cli

    with patch("apm_cli.commands._helpers._check_and_notify_updates"):
        return runner.invoke(cli, ["--version"])


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPrintVersionVerboseVersionFlag:
    """Tests for the experimental verbose_version branch in print_version."""

    def test_baseline_output_when_flag_disabled(self, runner: CliRunner, monkeypatch) -> None:
        """Standard --version output when verbose_version is disabled (default)."""
        import apm_cli.config as _conf

        # Explicitly no experimental override -- flag stays at default (False).
        monkeypatch.setattr(_conf, "_config_cache", {})

        result = _invoke_version(runner)

        assert result.exit_code == 0
        # Baseline string must contain the CLI name or version label.
        assert "APM" in result.output or "version" in result.output.lower()
        # Verbose fields must NOT appear.
        assert "Python:" not in result.output
        assert "Platform:" not in result.output
        assert "Install path:" not in result.output

    def test_verbose_version_enabled_adds_python_platform_installpath(
        self, runner: CliRunner, monkeypatch
    ) -> None:
        """With verbose_version=True, three labelled lines appear after the version."""
        import apm_cli.config as _conf

        monkeypatch.setattr(
            _conf,
            "_config_cache",
            {"experimental": {"verbose_version": True}},
        )

        result = _invoke_version(runner)

        assert result.exit_code == 0

        # All three label lines must be present.
        assert "Python:" in result.output
        assert "Platform:" in result.output
        assert "Install path:" in result.output

        # Each label must be left-justified in a 14-character field and indented
        # with two leading spaces.  Pattern: "  <label padded to 14><value>".
        #   "Python:"       is  7 chars  -> padded to 14 = "Python:       "
        #   "Platform:"     is  9 chars  -> padded to 14 = "Platform:     "
        #   "Install path:" is 13 chars  -> padded to 14 = "Install path: "
        assert re.search(r"  Python: {7}\S", result.output), (
            "Python: label not found with 14-char padding and 2-space indent"
        )
        assert re.search(r"  Platform: {5}\S", result.output), (
            "Platform: label not found with 14-char padding and 2-space indent"
        )
        assert re.search(r"  Install path: \S", result.output) or (
            "  Install path: " in result.output
        ), "Install path: label not found with 14-char padding and 2-space indent"

    def test_graceful_failure_when_is_enabled_raises(self, runner: CliRunner, monkeypatch) -> None:
        """If is_enabled throws, --version still prints the baseline and exits 0."""
        import apm_cli.config as _conf

        monkeypatch.setattr(_conf, "_config_cache", {})

        def _always_raise(name: str) -> bool:
            raise RuntimeError("simulated experimental subsystem failure")

        monkeypatch.setattr("apm_cli.core.experimental.is_enabled", _always_raise)

        result = _invoke_version(runner)

        assert result.exit_code == 0
        # Baseline output must still appear.
        assert "APM" in result.output or "version" in result.output.lower()
        # Verbose fields must NOT appear (exception was caught).
        assert "Python:" not in result.output
