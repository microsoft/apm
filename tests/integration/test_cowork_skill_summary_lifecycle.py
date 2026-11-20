"""Installed-CLI contract: the Cowork install summary names the configured skills dir (#4502).

With ``copilot-cowork-skills-dir`` set, ``apm install -g --target copilot-cowork``
deploys skills into that directory, and the summary line must name it rather
than a ``<label>/skills/`` placeholder that does not exist on disk.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml
from tests.integration._policy_scope_kit import (
    PolicyScopeWorld,
    output,
    run_step,
    skill_package_tree,
)
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
]


@pytest.mark.parametrize("under_home", [True, False], ids=["under-home", "outside-home"])
def test_cowork_install_summary_names_the_configured_skills_dir(
    tmp_path: Path, apm_binary_path: Path, under_home: bool
) -> None:
    world = PolicyScopeWorld(tmp_path, dict(os.environ))
    isolated = world.isolated
    source = isolated.work_root / "kit"
    skill_package_tree(source, "kit")
    package = str(source)
    base = isolated.home if under_home else tmp_path / "onedrive"
    skills_dir = (base / "Cowork Skills").resolve()
    skills_dir.mkdir(parents=True)
    dump_yaml(
        {"name": "user", "version": "1.0.0", "dependencies": {"apm": [package]}},
        isolated.config_root / "apm.yml",
    )
    env = {**isolated.subprocess_env(), "COLUMNS": "1000"}
    env.pop("APM_COPILOT_COWORK_SKILLS_DIR", None)
    runner = ApmLifecycleRunner((str(apm_binary_path),), scenario_timeout_seconds=900)

    def apm(*args: str, step: str) -> str:
        result = run_step(runner, args, cwd=isolated.work_root, env=env, step=step)
        return " ".join(output(result).split())

    apm("experimental", "enable", "copilot-cowork", step="1-enable")
    apm("config", "set", "copilot-cowork-skills-dir", str(skills_dir), step="2-config")
    installed = apm("install", "-g", "--target", "copilot-cowork", "--no-policy", step="3-install")

    assert (skills_dir / "kit-skill" / "SKILL.md").is_file(), installed
    assert not (isolated.home / "copilot-cowork").exists(), installed
    # Under the user-scope root the line is home-relative, like every other target.
    shown = "Cowork Skills" if under_home else skills_dir.as_posix()
    assert f"integrated -> {shown}/ " in installed, installed
    assert "/skills/" not in installed, installed
