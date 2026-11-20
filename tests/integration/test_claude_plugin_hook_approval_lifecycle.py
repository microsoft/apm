"""Approving a project-scope Claude plugin takes effect on the next install.

A Claude plugin is native at project scope (#3921): ``apm approve`` pins
the whole plugin (path and bytes), install deploys it intact to
``.agents/plugins/`` and enables it in ``.claude/settings.json``. A source
whose bytes no longer match the approved pin parks again.
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

    def enabled() -> list[str]:
        settings = project / ".claude/settings.json"
        if not settings.exists():
            return []
        plugins = json.loads(settings.read_text(encoding="utf-8")).get("enabledPlugins", {})
        return [key for key, value in plugins.items() if value is True]

    def deployed() -> str:
        hook = project / ".agents/plugins/hooky/hooks/hooks.json"
        return hook.read_text(encoding="utf-8") if hook.exists() else ""

    run("install")
    assert enabled() == []
    assert "HOOKY_RAN" not in deployed()

    run("approve", "--all")
    grants = load_yaml(project / "apm.yml")["executables"]["allow"]
    grant = next(iter(grants.values()))
    assert str(grant["plugin"]).startswith("sha256:")
    run("install")
    assert [key.split("@")[0] for key in enabled()] == ["hooky"]
    assert "HOOKY_RAN" in deployed()

    # A source whose bytes no longer match the approved pin parks again.
    hooks = plugin / "hooks/hooks.json"
    hooks.write_text(hooks.read_text().replace("HOOKY_RAN", "TAMPERED"))
    run("install")
    assert enabled() == []
    assert "TAMPERED" not in deployed()
