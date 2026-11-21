"""Approving a Claude plugin's hook file takes effect on the next install.

``apm approve`` pins a local plugin's hook file on its source tree
(``hooks/hooks.json``) while install gates APM's normalized cache copy
(``.apm/hooks/hooks.json``). Both name the hook by its publisher path, so
the pin admits the copy; a copy whose bytes differ still parks.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [pytest.mark.integration, pytest.mark.e2e]


def _claude_plugin(root: Path) -> Path:
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin/plugin.json").write_text('{"name": "hooky", "version": "1.0.0"}')
    (root / "hooks").mkdir()
    command = 'node "${CLAUDE_PLUGIN_ROOT}/scripts/run.js" HOOKY_RAN'
    hooks = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": command}]}]}}
    (root / "hooks/hooks.json").write_text(json.dumps(hooks))
    (root / "scripts").mkdir()
    (root / "scripts/run.js").write_text("console.log(process.argv[2]);\n")
    return root


def test_approved_claude_plugin_hook_deploys_on_reinstall(
    tmp_path: Path, apm_engine_command: tuple[str, ...]
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "scenario", base_env=dict(os.environ))
    env = isolated.subprocess_env()
    plugin = _claude_plugin(tmp_path / "plugin")
    project = isolated.work_root
    manifest = {
        "name": "consumer",
        "version": "1.0.0",
        "targets": ["claude"],
        "dependencies": {"apm": [str(plugin)]},
        "executables": {"allow": {}},
    }
    (project / "apm.yml").write_text(json.dumps(manifest), encoding="utf-8")
    runner = ApmLifecycleRunner(apm_engine_command)

    def run(*args: str) -> None:
        result = runner.run(args, scenario_id="claude-plugin-hook-approval", cwd=project, env=env)
        assert result.returncode == 0, result.stdout + result.stderr

    def deployed() -> str:
        settings = project / ".claude/settings.json"
        return settings.read_text(encoding="utf-8") if settings.exists() else ""

    run("install")
    assert "HOOKY_RAN" not in deployed()

    run("approve", "--all")
    grants = load_yaml(project / "apm.yml")["executables"]["allow"]
    assert list(next(iter(grants.values()))["hooks"]) == ["hooks/hooks.json"]
    run("install")
    assert "HOOKY_RAN" in deployed()

    # A normalized copy that no longer matches the approved bytes parks again.
    copy = next((project / "apm_modules").rglob(".apm/hooks/hooks.json"))
    copy.write_text(copy.read_text().replace("HOOKY_RAN", "TAMPERED"))
    run("install")
    assert "TAMPERED" not in deployed()
