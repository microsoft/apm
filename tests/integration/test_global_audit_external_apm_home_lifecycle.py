"""Installed CLI: ``apm install -g`` then ``apm audit --ci`` with APM_HOME outside HOME (#3901).

Global install accepts an absolute ``APM_HOME`` outside ``HOME``; audit replay
must project ``apm_modules`` at the same deploy-root-relative position so the
rewritten package-asset links it replays match the deployed bytes.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.artifact_snapshot import ArtifactSnapshot, assert_unchanged
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [pytest.mark.integration, pytest.mark.e2e, pytest.mark.requires_apm_binary]

_AUDIT = ("audit", "--ci", "--no-policy", "--no-fail-fast", "--format", "json")


def _write_package(root: Path) -> Path:
    package = root / "demo-package"
    (package / "docs").mkdir(parents=True)
    (package / "docs" / "guide.md").write_text("# Guide\n", encoding="utf-8")
    (package / "apm.yml").write_text("name: demo-package\nversion: 1.0.0\n", encoding="utf-8")
    skill = package / ".apm" / "skills" / "demo" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: demo\ndescription: Demo skill\n---\n# Demo\n"
        "See [guide](../../../docs/guide.md).\n",
        encoding="utf-8",
    )
    agent = package / ".apm" / "agents" / "helper.agent.md"
    agent.parent.mkdir(parents=True)
    agent.write_text(
        "---\ndescription: Helper\n---\n# Helper\nSee [guide](../../docs/guide.md).\n",
        encoding="utf-8",
    )
    return package


@pytest.mark.parametrize("apm_home_location", ["outside-home", "under-home"])
def test_global_install_then_audit_passes_with_apm_home_location(
    tmp_path: Path, apm_binary_path: Path, apm_home_location: str
) -> None:
    root = tmp_path.resolve()
    isolated = IsolatedApmEnvironment.create(root / "scene", base_env=os.environ)
    apm_home = (
        isolated.root / "metadata"
        if apm_home_location == "outside-home"
        else isolated.home / ".apm"
    )
    env = isolated.subprocess_env(overrides={"PYTHONDONTWRITEBYTECODE": "1"})
    env["APM_HOME"] = str(apm_home)
    env["APM_NO_CACHE"] = "1"
    for variable in ("COPILOT_HOME", "CLAUDE_CONFIG_DIR", "CODEX_HOME"):
        env.pop(variable, None)
    runner = ApmLifecycleRunner((str(apm_binary_path),))
    package = _write_package(isolated.package_root)

    install = runner.run(
        ("install", "--global", str(package), "--target", "copilot,claude", "--no-policy"),
        scenario_id="install-global",
        cwd=isolated.work_root,
        env=env,
    )
    assert install.returncode == 0, (install.stdout, install.stderr)
    assert (apm_home / "apm.lock.yaml").is_file()
    deployed_agent = isolated.home / ".copilot" / "agents" / "helper.agent.md"
    assert "apm_modules/_local/demo-package/docs/guide.md" in deployed_agent.read_text(
        encoding="utf-8"
    )

    before = ArtifactSnapshot.capture(isolated.home)
    audit = runner.run(_AUDIT, scenario_id="audit-ci", cwd=apm_home, env=env)
    assert audit.returncode == 0, (audit.stdout, audit.stderr)
    payload = json.loads(audit.stdout)
    checks = {check["name"]: check for check in payload["checks"]}
    assert checks["drift"]["passed"] is True, checks["drift"]
    assert payload["passed"] is True, payload["checks"]
    assert_unchanged(before, ArtifactSnapshot.capture(isolated.home))
