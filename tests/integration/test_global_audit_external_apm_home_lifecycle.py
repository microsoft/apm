"""Installed CLI: ``apm install -g`` then ``apm audit --ci`` with APM_HOME outside HOME (#3901).

Global install accepts an absolute ``APM_HOME`` outside ``HOME``. Audit replay
must render rewritten package-asset links in the live coordinates install used
(independent of where the scratch directory lands) and verify a local native
Agent Plugin against the image it materialized.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.unit.copilot_plugins._builders import write_agent_plugin
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.artifact_snapshot import ArtifactSnapshot, assert_unchanged
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [pytest.mark.integration, pytest.mark.e2e, pytest.mark.requires_apm_binary]

_AUDIT = ("audit", "--ci", "--no-policy", "--no-fail-fast", "--format", "json")
_GUIDE_LINK = "apm_modules/_local/demo-package/docs/guide.md"


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


@pytest.mark.parametrize(
    ("home_parts", "apm_home_parts", "tmpdir_parts"),
    [
        pytest.param(("home",), ("metadata",), None, id="outside-home"),
        pytest.param(("home",), ("home", ".apm"), None, id="under-home"),
        # Scratch lands beside APM_HOME: a lexical scratch projection would
        # share APM_HOME's parent and drop one "../" from replayed links.
        pytest.param(("x", "home"), ("y", "apm"), ("y",), id="scratch-beside-apm-home"),
    ],
)
def test_global_install_then_audit_passes_with_apm_home_location(
    tmp_path: Path,
    apm_binary_path: Path,
    home_parts: tuple[str, ...],
    apm_home_parts: tuple[str, ...],
    tmpdir_parts: tuple[str, ...] | None,
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path.resolve() / "scene", base_env=os.environ)
    home = isolated.root.joinpath(*home_parts)
    apm_home = isolated.root.joinpath(*apm_home_parts)
    home.mkdir(parents=True, exist_ok=True)
    apm_home.mkdir(parents=True, exist_ok=True)
    env = isolated.subprocess_env(overrides={"PYTHONDONTWRITEBYTECODE": "1"})
    env.update(
        {"HOME": str(home), "USERPROFILE": str(home), "APM_HOME": str(apm_home)},
        APM_NO_CACHE="1",
    )
    if tmpdir_parts is not None:
        env["TMPDIR"] = str(isolated.root.joinpath(*tmpdir_parts))
    for variable in ("COPILOT_HOME", "CLAUDE_CONFIG_DIR", "CODEX_HOME"):
        env.pop(variable, None)
    runner = ApmLifecycleRunner((str(apm_binary_path),))
    package = _write_package(isolated.package_root)
    plugin = write_agent_plugin(isolated.package_root / "plug", name="witness", with_mcp=False)
    (apm_home / "apm.yml").write_text(
        f"name: user\nversion: 0.0.0\ndependencies:\n  apm:\n    - {package}\n    - {plugin}\n",
        encoding="utf-8",
    )

    install = runner.run(
        ("install", "--global", "--target", "copilot", "--no-policy"),
        scenario_id="install-global",
        cwd=isolated.work_root,
        env=env,
    )
    assert install.returncode == 0, (install.stdout, install.stderr)
    assert (apm_home / "apm.lock.yaml").is_file()
    agent = (home / ".copilot" / "agents" / "helper.agent.md").read_text(encoding="utf-8")
    assert _GUIDE_LINK in agent
    assert (apm_home / "apm_modules" / "_local" / "plug" / "plugin.json").is_file()

    before = ArtifactSnapshot.capture(home)
    audit = runner.run(_AUDIT, scenario_id="audit-ci", cwd=apm_home, env=env)
    assert audit.returncode == 0, (audit.stdout, audit.stderr)
    payload = json.loads(audit.stdout)
    checks = {check["name"]: check for check in payload["checks"]}
    assert checks["drift"]["passed"] is True, checks["drift"]
    assert payload["passed"] is True, payload["checks"]
    assert_unchanged(before, ArtifactSnapshot.capture(home))
