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


def _mcp_install(target: str, flag: str = "--target") -> tuple[str, ...]:
    return ("install", "-g", "--no-policy", "--mcp", "local-echo", flag, target, "--", "echo")


@pytest.mark.parametrize(
    ("flag", "target"),
    [("--target", "intellij"), ("--target", "claude,intellij"), ("--runtime", "intellij")],
)
def test_bootstrap_refuses_a_target_with_no_targets_spelling(
    tmp_path: Path, apm_binary_path: Path, flag: str, target: str
) -> None:
    isolated, env, work = _env(tmp_path)
    manifest = isolated.config_root / "apm.yml"
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=180)

    result = runner.run(_mcp_install(target, flag), cwd=work, env=env)
    output = " ".join((result.stdout + result.stderr).split())
    assert result.returncode != 0, output
    # #3878: an IDE is refused with guidance on every apm.yml-writing path.
    assert (
        f"for {flag} {target}: 'intellij' is an IDE, not a target, so apm.yml cannot "
        "record it. Use '--target copilot' for GitHub Copilot in JetBrains IDEs; "
        "once apm.yml exists, '--target intellij' writes JetBrains MCP config."
    ) in output, output
    assert "Nothing was written" in output, output
    assert f"{flag} {target} will apply to that install only" in output, output
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


def test_refusal_recovery_installs_without_recording_the_target(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """Following the refusal's advice installs the IntelliJ server and stays readable."""
    isolated, env, work = _env(tmp_path)
    manifest = isolated.config_root / "apm.yml"
    manifest.write_text("name: me\nversion: 0.1.0\n", encoding="ascii")
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=180)

    first = runner.run(_mcp_install("intellij"), cwd=work, env=env)
    assert first.returncode == 0, first.stdout + first.stderr
    assert "targets" not in load_yaml(manifest)
    assert (Path(env["XDG_CONFIG_HOME"]) / "github-copilot" / "intellij" / "mcp.json").is_file()

    again = runner.run(("install", "-g", "--no-policy"), cwd=work, env=env)
    assert again.returncode == 0, again.stdout + again.stderr
