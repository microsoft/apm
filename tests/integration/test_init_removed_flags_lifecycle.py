"""Installed-CLI contract: ``apm init --plugin`` / ``--marketplace`` were removed (#4073).

Each flag stays a hidden stub that writes nothing, exits 2 and names the
replacement command; ``apm plugin init`` carries the metadata options the
old flag offered.
"""

from __future__ import annotations

import json
import os

import pytest

from apm_cli.utils.yaml_io import load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [
    pytest.mark.integration,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_apm_binary,
]


@pytest.mark.parametrize(
    ("flag", "replacement"),
    [("--plugin", "apm plugin init"), ("--marketplace", "apm marketplace init")],
)
def test_removed_init_flag_exits_2_and_writes_nothing(
    tmp_path, apm_binary_path, flag, replacement
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=120)
    work = isolated.work_root / "fresh"
    work.mkdir(parents=True)

    result = runner.run(("init", "demo", flag, "--yes"), cwd=work, env=isolated.subprocess_env())

    assert result.returncode == 2, result.stdout + result.stderr
    assert f"Run: {replacement}" in result.stderr
    assert list(work.iterdir()) == []
    help_text = runner.run(("init", "--help"), cwd=work, env=isolated.subprocess_env()).stdout
    assert flag not in help_text


def test_plugin_init_writes_the_metadata_the_old_flag_offered(tmp_path, apm_binary_path) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=120)
    work = isolated.work_root
    work.mkdir(parents=True, exist_ok=True)

    result = runner.run(
        (
            "plugin",
            "init",
            "demo",
            "--yes",
            "--version",
            "2.3.4",
            "--author",
            "Ada",
            "--description",
            "Says hi",
        ),
        cwd=work,
        env=isolated.subprocess_env(),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    manifest = load_yaml(work / "demo" / "apm.yml")
    assert (manifest["version"], manifest["author"], manifest["description"]) == (
        "2.3.4",
        "Ada",
        "Says hi",
    )
    plugin = json.loads((work / "demo" / "plugin.json").read_text(encoding="utf-8"))
    assert plugin["version"] == "2.3.4"
    assert plugin["description"] == "Says hi"
