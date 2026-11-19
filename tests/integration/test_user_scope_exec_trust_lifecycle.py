"""User-scope executable trust is read from the user manifest (#3902).

At user scope the deploy root is ``$HOME`` but the manifest and its
``executables`` grants live in ``~/.apm/apm.yml``. Install and global
uninstall survivor rebuild must both honor that block.
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


def _hook_package(root: Path, name: str) -> Path:
    package = root / name
    (package / ".apm/hooks").mkdir(parents=True)
    (package / "apm.yml").write_text(f"name: {name}\nversion: 1.0.0\n", encoding="utf-8")
    hooks = {
        "hooks": {
            "SessionStart": [
                {"hooks": [{"type": "command", "command": f"echo {name.upper()}_HOOK"}]}
            ]
        }
    }
    (package / ".apm/hooks/session.json").write_text(json.dumps(hooks), encoding="utf-8")
    return package


def test_user_manifest_grants_gate_global_install_and_uninstall(
    tmp_path: Path, apm_engine_command: tuple[str, ...]
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "scenario", base_env=dict(os.environ))
    env = isolated.subprocess_env()
    env["CLAUDE_CONFIG_DIR"] = str(isolated.home / ".claude")
    (isolated.home / ".claude").mkdir()
    alpha = _hook_package(tmp_path / "src", "alpha")
    beta = _hook_package(tmp_path / "src", "beta")
    manifest = {
        "name": "consumer",
        "version": "1.0.0",
        "targets": ["claude"],
        "dependencies": {"apm": [str(alpha), str(beta)]},
        # The gate is on and nothing is approved yet.
        "executables": {"allow": {}},
    }
    (isolated.config_root / "apm.yml").write_text(json.dumps(manifest), encoding="utf-8")
    runner = ApmLifecycleRunner(apm_engine_command)
    settings = isolated.home / ".claude/settings.json"

    def run(*args: str, cwd: Path | None = None) -> None:
        result = runner.run(
            args, scenario_id="user-scope-exec-trust", cwd=cwd or isolated.work_root, env=env
        )
        assert result.returncode == 0, result.stdout + result.stderr

    def deployed() -> str:
        return settings.read_text(encoding="utf-8") if settings.exists() else ""

    run("install", "--global", "--target", "claude", "--only", "apm")
    assert "ALPHA_HOOK" not in deployed()
    assert "BETA_HOOK" not in deployed()

    run("approve", str(beta), cwd=isolated.config_root)
    grants = load_yaml(isolated.config_root / "apm.yml")["executables"]["allow"]
    assert "sha256:" in json.dumps(grants)
    # A deploy-root manifest is not the user manifest: its grant must not admit alpha.
    alpha_key = next(iter(grants)).replace("beta", "alpha")
    decoy = {
        "name": "decoy",
        "version": "1.0.0",
        "executables": {"allow": {alpha_key: {"hooks": True}}},
    }
    (isolated.home / "apm.yml").write_text(json.dumps(decoy), encoding="utf-8")
    run("install", "--global", "--target", "claude", "--only", "apm")
    assert "BETA_HOOK" in deployed()
    assert "ALPHA_HOOK" not in deployed()

    # Survivor rebuild after a global uninstall keeps the unapproved package parked.
    run("uninstall", "--global", str(beta))
    assert "BETA_HOOK" not in deployed()
    assert "ALPHA_HOOK" not in deployed()
