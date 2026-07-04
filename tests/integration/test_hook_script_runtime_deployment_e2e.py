"""Runtime proof for deployed hook scripts that require local siblings."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from apm_cli.integration.hook_integrator import HookIntegrator
from apm_cli.models.apm_package import APMPackage, PackageInfo

pytestmark = pytest.mark.integration


def _node_binary() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required to execute deployed JavaScript hook scripts")
    return node


def _package_info(package_root: Path) -> PackageInfo:
    package = APMPackage(
        name="ponytail",
        version="1.0.0",
        source="DietrichGebert/ponytail",
    )
    return PackageInfo(package=package, install_path=package_root)


def _seed_ponytail_runtime_package(project_root: Path, hook_payload: dict) -> PackageInfo:
    package_root = project_root / "apm_modules" / "DietrichGebert" / "ponytail"
    hooks_dir = package_root / "hooks"
    hooks_dir.mkdir(parents=True)
    (package_root / "package.json").write_text(
        json.dumps({"name": "ponytail", "type": "commonjs"}) + "\n",
        encoding="utf-8",
    )
    (hooks_dir / "hooks.json").write_text(json.dumps(hook_payload, indent=2), encoding="utf-8")
    (hooks_dir / "ponytail-activate.js").write_text(
        "const config = require('./ponytail-config');\nprocess.stdout.write(config.message);\n",
        encoding="utf-8",
    )
    (hooks_dir / "ponytail-config.js").write_text(
        "module.exports = { message: 'PONYTAIL MODE ACTIVE' };\n",
        encoding="utf-8",
    )
    return _package_info(package_root)


def _assert_deployed_hook_runs(project_root: Path, deployed_script: Path) -> None:
    assert deployed_script.exists()
    assert (deployed_script.parent / "ponytail-config.js").exists()
    assert (
        json.loads((deployed_script.parent / "package.json").read_text(encoding="utf-8"))["type"]
        == "commonjs"
    )

    completed = subprocess.run(
        [_node_binary(), str(deployed_script)],
        cwd=project_root,
        input="{}\n",
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "PONYTAIL MODE ACTIVE"


def test_copilot_deployed_hook_runs_with_sibling_modules_and_package_scope(
    tmp_path: Path,
) -> None:
    """Copilot hook copies must be runnable inside a type=module consumer project."""
    project_root = tmp_path / "project"
    (project_root / ".github").mkdir(parents=True)
    (project_root / ".github" / "copilot-instructions.md").write_text(
        "# Copilot instructions\n",
        encoding="utf-8",
    )
    (project_root / "package.json").write_text('{"type":"module"}\n', encoding="utf-8")
    package_info = _seed_ponytail_runtime_package(
        project_root,
        {
            "hooks": {
                "preToolUse": [
                    {
                        "type": "command",
                        "bash": "node ${PLUGIN_ROOT}/hooks/ponytail-activate.js",
                    }
                ]
            }
        },
    )

    result = HookIntegrator().integrate_package_hooks(package_info, project_root)

    assert result.scripts_copied >= 3
    _assert_deployed_hook_runs(
        project_root,
        project_root
        / ".github"
        / "hooks"
        / "scripts"
        / "ponytail"
        / "hooks"
        / "ponytail-activate.js",
    )


def test_claude_deployed_hook_runs_with_sibling_modules_and_package_scope(
    tmp_path: Path,
) -> None:
    """Merged Claude hooks use the same deployed runtime context."""
    project_root = tmp_path / "project"
    (project_root / ".claude").mkdir(parents=True)
    (project_root / "package.json").write_text('{"type":"module"}\n', encoding="utf-8")
    package_info = _seed_ponytail_runtime_package(
        project_root,
        {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "node ${CLAUDE_PLUGIN_ROOT}/hooks/ponytail-activate.js",
                            }
                        ]
                    }
                ]
            }
        },
    )

    result = HookIntegrator().integrate_package_hooks_claude(package_info, project_root)

    assert result.scripts_copied >= 3
    _assert_deployed_hook_runs(
        project_root,
        project_root / ".claude" / "hooks" / "ponytail" / "hooks" / "ponytail-activate.js",
    )
