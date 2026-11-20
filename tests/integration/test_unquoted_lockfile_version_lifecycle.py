"""Installed-CLI contract: a lockfile whose version lost its YAML quotes still replays.

Hand edits and YAML tools such as ``yq`` can rewrite ``lockfile_version: '1'``
as the integer ``1``. ``apm install --frozen``, ``apm audit --ci`` and a plain
``apm install`` must read it as the same version and change no deployed file
(#3692).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
]

_QUOTED = re.compile(r"^lockfile_version: '(\d+)'$", re.MULTILINE)


def test_unquoted_lockfile_version_replays_unchanged(tmp_path: Path, apm_binary_path: Path) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    env = isolated.subprocess_env()
    project = isolated.work_root / "consumer"
    package = project / "pkgs" / "alpha"
    rules = package / ".apm" / "instructions"
    rules.mkdir(parents=True)
    dump_yaml({"name": "alpha", "version": "1.0.0"}, package / "apm.yml")
    (rules / "alpha.instructions.md").write_text(
        '---\napplyTo: "**"\n---\n# alpha\n', encoding="ascii"
    )
    dump_yaml(
        {
            "name": "consumer",
            "version": "0.1.0",
            "target": ["claude"],
            "dependencies": {"apm": ["./pkgs/alpha"]},
        },
        project / "apm.yml",
    )
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=180)

    def apm(*args: str) -> None:
        result = runner.run(args, cwd=project, env=env)
        assert result.returncode == 0, f"{args}\n{result.stdout}{result.stderr}"

    apm("install", "--no-policy")
    lock = project / "apm.lock.yaml"
    deployed = project / ".claude" / "rules" / "alpha.md"
    deployed_bytes = deployed.read_bytes()
    text = lock.read_text(encoding="utf-8")
    match = _QUOTED.search(text)
    assert match, text
    version = match.group(1)
    lock.write_text(_QUOTED.sub(f"lockfile_version: {version}", text), encoding="utf-8")

    for args in (
        ("install", "--frozen", "--no-policy"),
        ("audit", "--ci", "--no-policy"),
        ("install", "--no-policy"),
        ("audit", "--ci", "--no-policy"),
    ):
        apm(*args)
        assert deployed.read_bytes() == deployed_bytes, args
