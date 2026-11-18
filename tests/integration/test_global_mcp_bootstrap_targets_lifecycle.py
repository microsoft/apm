"""Installed-CLI contract: user-manifest bootstrap never writes unreadable targets (#3870).

``apm install -g --mcp NAME --target intellij`` on a machine with no user
manifest used to write ``targets: [intellij]`` to ``~/.apm/apm.yml``; the next
``apm install -g`` then failed on the manifest APM itself wrote. A requested
target with no ``targets:`` spelling is now refused before anything is written,
and a representable one is recorded so the next run reads it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [pytest.mark.integration, pytest.mark.requires_apm_binary]


def _env(tmp_path: Path) -> tuple[IsolatedApmEnvironment, dict[str, str], Path]:
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    env = dict(isolated.subprocess_env())
    env.pop("CI", None)
    work = isolated.work_root / "anywhere"
    work.mkdir(parents=True)
    return isolated, env, work


def _mcp_install(target: str) -> tuple[str, ...]:
    return ("install", "-g", "--no-policy", "--mcp", "local-echo", "--target", target, "--", "echo")


@pytest.mark.parametrize("target", ["intellij", "claude,intellij"])
def test_bootstrap_refuses_a_target_with_no_targets_spelling(
    tmp_path: Path, apm_binary_path: Path, target: str
) -> None:
    isolated, env, work = _env(tmp_path)
    manifest = isolated.config_root / "apm.yml"
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=180)

    result = runner.run(_mcp_install(target), cwd=work, env=env)
    output = " ".join((result.stdout + result.stderr).split())
    assert result.returncode != 0, output
    assert "Target 'intellij' has no 'targets:' equivalent" in output, output
    assert "Nothing was written" in output, output
    assert "Traceback" not in output, output
    assert not manifest.exists()
    assert not (Path(env["XDG_CONFIG_HOME"]) / "github-copilot" / "intellij" / "mcp.json").exists()


def test_bootstrap_records_a_readable_target(tmp_path: Path, apm_binary_path: Path) -> None:
    isolated, env, work = _env(tmp_path)
    manifest = isolated.config_root / "apm.yml"
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=180)

    first = runner.run(_mcp_install("claude"), cwd=work, env=env)
    assert first.returncode == 0, first.stdout + first.stderr
    assert load_yaml(manifest)["targets"] == ["claude"]

    again = runner.run(("install", "-g", "--no-policy"), cwd=work, env=env)
    assert again.returncode == 0, again.stdout + again.stderr
