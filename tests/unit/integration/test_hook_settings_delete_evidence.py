"""An emptied Claude settings file is deleted only with evidence APM created it (#4962).

PROMISE: P-LAW-ownership.preserve_unowned -- whole-file deletion needs recorded
deployment evidence (req-lk-024); the #4852 content rule alone never deletes.
CELL: OP=uninstall PRIM=hook TGT=claude ROOT=project|default-user|outside-home
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from apm_cli.integration.config_file_provenance import recorded_config_write
from apm_cli.integration.hook_integrator import HookIntegrator
from apm_cli.integration.targets import KNOWN_TARGETS

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-LAW-ownership.preserve_unowned")]

_ENTRY = {
    "matcher": "Bash",
    "hooks": [{"type": "command", "command": "echo guard"}],
    "_apm_source": "hookify",
}
_BODY = json.dumps({"hooks": {"PreToolUse": [_ENTRY]}}, indent=2) + "\n"


def _scene(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root: str):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("APM_HOME", str(tmp_path / "apm-home"))
    if root == "project":
        project = tmp_path / "project"
        (project / ".claude").mkdir(parents=True)
        return project, KNOWN_TARGETS["claude"], project / ".claude"
    if root == "default-user":
        (home / ".claude").mkdir()
        return home, KNOWN_TARGETS["claude"], home / ".claude"
    profile = tmp_path / "claude-profile"
    profile.mkdir()
    return home, replace(KNOWN_TARGETS["claude"], root_dir=str(profile)), profile


@pytest.mark.parametrize("root", ["project", "default-user", "outside-home"])
@pytest.mark.parametrize("apm_created", [False, True], ids=["user-created", "apm-created"])
def test_an_emptied_settings_file_is_deleted_only_when_apm_created_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root: str, apm_created: bool
) -> None:
    project_root, target, claude_root = _scene(tmp_path, monkeypatch, root)
    settings = claude_root / "settings.json"
    if apm_created:
        with recorded_config_write(settings, manifest_root=project_root):
            settings.write_text(_BODY, encoding="utf-8")
    else:
        settings.write_text(_BODY, encoding="utf-8")

    stats = HookIntegrator().sync_integration(None, project_root, targets=[target])

    assert stats["errors"] == 0
    if apm_created:
        assert not settings.exists()
        assert not stats.get("kept_paths")
    else:
        assert json.loads(settings.read_text(encoding="utf-8")) == {}
        assert len(stats["kept_paths"]) == 1
        assert stats["kept_paths"][0].endswith("settings.json")
