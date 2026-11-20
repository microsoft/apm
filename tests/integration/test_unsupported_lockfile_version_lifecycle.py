"""Installed-CLI contract: a lockfile from a newer APM names both remedies (#4233, req-lk-004).

A lockfile whose ``lockfile_version`` this APM cannot read must fail closed
with an ``[x]`` diagnostic that offers both ways out: regenerate the lockfile
from ``apm.yml`` (the exact ``apm`` command), or upgrade APM. The failed
command changes nothing, and running the printed regenerate step writes a
lockfile at a supported version.
"""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

import pytest

from apm_cli.deps.lockfile import SUPPORTED_LOCKFILE_VERSIONS
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
    pytest.mark.promise(
        "req-lk-004", "P-INV-I7", "P-INV-I8", "P-LAW-transaction.failed_command_preserves_state"
    ),
]

_FUTURE = "99"
_REMEDY = re.compile(
    r"Move (?P<path>\S+) aside, then run '(?P<command>apm [^']+)' to regenerate it from "
    r"apm\.yml at a supported version, or 'apm self-update' to install an APM release "
    rf"that reads lockfile version '{_FUTURE}'\."
)


@pytest.mark.parametrize(
    "args",
    [
        ("install", "--no-policy"),
        ("install", "--frozen", "--no-policy"),
        ("lock",),
        ("audit", "--ci"),
    ],
    ids=lambda args: " ".join(args),
)
def test_unsupported_lockfile_version_names_both_remedies(
    tmp_path: Path, apm_binary_path: Path, args: tuple[str, ...]
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    # Wide output keeps each diagnostic, including audit's table cell, on one line.
    env = {**isolated.subprocess_env(), "COLUMNS": "400"}
    project = isolated.work_root / "consumer"
    project.mkdir(parents=True)
    dump_yaml({"name": "consumer", "version": "0.1.0", "target": ["claude"]}, project / "apm.yml")
    lock = project / "apm.lock.yaml"
    dump_yaml({"lockfile_version": _FUTURE, "dependencies": []}, lock)
    before = {path.name: path.read_bytes() for path in project.iterdir()}
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=180)

    result = runner.run(args, cwd=project, env=env)

    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert re.search(rf"^.*\[x\].*Unsupported lockfile version '{_FUTURE}'", output, re.M), output
    remedy = _REMEDY.search(" ".join(output.split()))
    assert remedy, output
    assert remedy["path"] == "apm.lock.yaml" and remedy["command"] == "apm lock", remedy.group(0)
    assert {path.name: path.read_bytes() for path in project.iterdir()} == before

    # Follow the printed regenerate remedy literally.
    lock.rename(project / f"{remedy['path']}.aside")
    regenerated = runner.run(tuple(shlex.split(remedy["command"])[1:]), cwd=project, env=env)
    assert regenerated.returncode == 0, regenerated.stdout + regenerated.stderr
    assert str(load_yaml(lock)["lockfile_version"]) in SUPPORTED_LOCKFILE_VERSIONS
