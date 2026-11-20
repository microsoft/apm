"""Approving a Claude plugin takes effect on the next project install (#3921).

A project that targets Claude installs the plugin intact under the committed
``.agents/plugins/<plugin>/``. ``apm approve`` pins the whole plugin by
content, so its hooks load from the deployed root once approved; publisher
content that changes after approval parks the plugin and retires it.
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
        hooks = project / ".agents/plugins/hooky/hooks/hooks.json"
        return hooks.read_text(encoding="utf-8") if hooks.exists() else ""

    def enabled() -> dict:
        settings = project / ".claude/settings.json"
        if not settings.exists():
            return {}
        return json.loads(settings.read_text(encoding="utf-8")).get("enabledPlugins", {})

    run("install")
    assert "HOOKY_RAN" not in deployed()
    assert not enabled()

    run("approve", "--all")
    grants = load_yaml(project / "apm.yml")["executables"]["allow"]
    assert next(iter(grants.values()))["plugin"].startswith("sha256:")
    run("install")
    assert "HOOKY_RAN" in deployed()
    assert list(enabled().values()) == [True]
    # Split install is gone: no loose hook entry reaches Claude's settings.
    assert "HOOKY_RAN" not in (project / ".claude/settings.json").read_text(encoding="utf-8")

    # Publisher content that no longer matches the approval parks and retires.
    hooks = plugin / "hooks/hooks.json"
    hooks.write_text(hooks.read_text().replace("HOOKY_RAN", "TAMPERED"))
    run("install")
    assert "TAMPERED" not in deployed()
    assert not enabled()
