"""Regression tests for environment-selected user target roots."""

from dataclasses import replace
from pathlib import Path

import pytest

from apm_cli.integration.targets import (
    KNOWN_TARGETS,
    _resolve_env_user_root,
    resolve_hermes_root,
    resolve_targets,
)
from apm_cli.utils.path_security import PathTraversalError


def test_relative_claude_config_dir_uses_static_default(monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "relative-claude")

    assert (
        _resolve_env_user_root("CLAUDE_CONFIG_DIR", ".claude")
        == (Path.home() / ".claude").resolve()
    )


def test_relative_hermes_home_uses_static_default(monkeypatch):
    monkeypatch.setenv("HERMES_HOME", "relative-hermes")

    assert resolve_hermes_root() == (Path.home() / ".hermes").resolve()


def test_opencode_scope_preserves_lexical_root(monkeypatch, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    lexical = tmp_path / "link"
    lexical.symlink_to(real, target_is_directory=True)
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(lexical))
    profile = KNOWN_TARGETS["opencode"].for_scope(user_scope=True)
    assert profile is not None
    assert profile.lexical_deploy_root == lexical
    # Static user-scope targets retain the historical contract: deployment
    # uses root_dir, while lexical_deploy_root is the separate symlink-safety
    # identity.
    assert profile.resolved_deploy_root is None


def test_resolve_targets_rejects_symlinked_opencode_user_root(monkeypatch, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    lexical = tmp_path / "link"
    lexical.symlink_to(real, target_is_directory=True)
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(lexical))

    with pytest.raises(PathTraversalError, match="symlinked target root"):
        resolve_targets(tmp_path, user_scope=True, explicit_target="opencode")


def test_custom_opencode_profile_root_wins_over_changed_environment(monkeypatch, tmp_path):
    custom = tmp_path / "persisted-opencode"
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(tmp_path / "new-default"))
    profile = replace(KNOWN_TARGETS["opencode"].for_scope(user_scope=True), root_dir=str(custom))

    assert profile.managed_deploy_root == custom
