"""Regression tests for environment-selected user target roots."""

from pathlib import Path

from apm_cli.integration.targets import _resolve_env_user_root, resolve_hermes_root


def test_relative_claude_config_dir_uses_static_default(monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "relative-claude")

    assert (
        _resolve_env_user_root("CLAUDE_CONFIG_DIR", ".claude")
        == (Path.home() / ".claude").resolve()
    )


def test_relative_hermes_home_uses_static_default(monkeypatch):
    monkeypatch.setenv("HERMES_HOME", "relative-hermes")

    assert resolve_hermes_root() == (Path.home() / ".hermes").resolve()
