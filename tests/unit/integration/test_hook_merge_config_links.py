"""Merged hook config reads refuse a project path through a symlink."""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.integration.hook_ownership import read_merged_hook_config
from apm_cli.utils.path_security import PathTraversalError


def _linked_settings(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    linked = tmp_path / "linked.json"
    linked.write_text('{"outside": true}', encoding="utf-8")
    settings = project / ".claude" / "settings.json"
    try:
        settings.symlink_to(linked)
    except OSError:
        pytest.skip("symlinks are unavailable")
    return project, settings


def test_project_settings_link_is_refused(tmp_path: Path) -> None:
    project, settings = _linked_settings(tmp_path)

    with pytest.raises(PathTraversalError, match="symlinked config file"):
        read_merged_hook_config(settings, project, user_scope=False)


def test_user_scope_settings_link_is_read(tmp_path: Path) -> None:
    project, settings = _linked_settings(tmp_path)

    assert read_merged_hook_config(settings, project, user_scope=True) == {"outside": True}


def test_regular_settings_are_read_and_bad_json_is_empty(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    assert read_merged_hook_config(settings, tmp_path, user_scope=False) == {}
    settings.write_text('{"hooks": {}}', encoding="utf-8")
    assert read_merged_hook_config(settings, tmp_path, user_scope=False) == {"hooks": {}}
    settings.write_text("{not json", encoding="utf-8")
    assert read_merged_hook_config(settings, tmp_path, user_scope=False) == {}
