"""Real preflight through Click without executing a native model."""

import os
import shlex
import shutil
from pathlib import Path
from unittest.mock import Mock

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli

pytestmark = [
    pytest.mark.component,
    pytest.mark.skipif(os.name != "posix", reason="Native leaf profile and shell actor are POSIX"),
]

FIXTURE = Path(__file__).resolve().parents[3] / "examples/contracts/first-contract"


def _prepare(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    from apm_cli import config

    project = tmp_path / "project"
    shutil.copytree(FIXTURE, project)
    home = tmp_path / "home"
    home.mkdir()
    tools = tmp_path / "tools"
    tools.mkdir()
    marker = tmp_path / "native-was-invoked"
    native = tools / "copilot"
    native.write_text(
        f"#!/bin/sh\nprintf invoked > {shlex.quote(str(marker))}\nexit 1\n",
        encoding="utf-8",
    )
    native.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tools}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("APM_POLICY_DISABLE", raising=False)
    monkeypatch.delenv("APM_NO_SCRIPTS", raising=False)
    monkeypatch.setattr(config, "_config_cache", {"experimental": {"contracts": True}})
    monkeypatch.chdir(project)
    return project, home, marker


@pytest.mark.parametrize("command", ["plan", "run"])
def test_contract_commands_require_experimental_opt_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    from apm_cli import config

    project, _, marker = _prepare(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "_config_cache", {"experimental": {}})
    result = CliRunner().invoke(cli, [command, "handoff.contract.md", "--on", "copilot"])
    assert result.exit_code == 21, result.output
    assert "apm experimental enable contracts" in result.output
    assert not marker.exists()
    assert not (project / ".apm" / "runs").exists()


def _files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_plan_neither_executes_native_nor_mutates_project_or_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, home, marker = _prepare(tmp_path, monkeypatch)
    project_before = _files(project)
    home_before = _files(home)
    network = Mock(side_effect=AssertionError("planning must not access the network"))
    monkeypatch.setattr("requests.Session.request", network)
    result = CliRunner().invoke(
        cli, ["plan", "handoff.contract.md", "--on", "copilot", "--model", "gpt-6-astra"]
    )
    assert result.exit_code == 0, result.output
    assert not marker.exists()
    assert _files(project) == project_before
    assert _files(home) == home_before
    network.assert_not_called()
    assert "VERIFIED" not in result.output


def test_run_without_consent_refuses_without_native_or_run_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, _, marker = _prepare(tmp_path, monkeypatch)
    result = CliRunner().invoke(cli, ["run", "handoff.contract.md", "--on", "copilot"])
    assert result.exit_code == 21, result.output
    assert "--allow-host-access" in result.output
    assert "available login details" in result.output
    assert "***" not in result.output
    assert not marker.exists()
    assert not (project / ".apm" / "runs").exists()


@pytest.mark.parametrize("field", ["run: echo no", "budget: {usd: 1}", "sandbox: {network: none}"])
def test_unsupported_source_cannot_be_waived_with_consent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    project, _, marker = _prepare(tmp_path, monkeypatch)
    source = project / "handoff.contract.md"
    source.write_text(
        source.read_text(encoding="utf-8").replace("---\n", f"---\n{field}\n", 1),
        encoding="utf-8",
    )
    result = CliRunner().invoke(
        cli, ["run", "handoff.contract.md", "--on", "copilot", "--allow-host-access"]
    )
    assert result.exit_code in {21, 22}, result.output
    assert not marker.exists()
    assert not (project / ".apm" / "runs").exists()


def test_preflight_cancellation_is_halted_not_legacy_abort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, _, marker = _prepare(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "apm_cli.contracts.frontend.plan_contract",
        Mock(side_effect=KeyboardInterrupt),
    )
    result = CliRunner().invoke(cli, ["plan", "handoff.contract.md", "--on", "copilot"])
    assert result.exit_code == 22, result.output
    assert "interrupted" in result.output.lower()
    assert "terminated" not in result.output.lower()
    assert not marker.exists()
    assert not (project / ".apm" / "runs").exists()
