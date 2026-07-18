"""Lifecycle regression for unchanged local-package MCP installs."""

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from click.testing import CliRunner, Result

from apm_cli.cli import cli
from apm_cli.deps.lockfile import LockFile

_INSTALL_ARGS = [
    "install",
    "--target",
    "copilot,codex",
    "--trust-transitive-mcp",
    "--no-policy",
]
_TARGET_SERVERS = {
    "codex": ["local-lifecycle-server"],
    "vscode": ["local-lifecycle-server"],
}


def _write_local_mcp_project(project_root: Path) -> None:
    """Create a consumer and local package with one self-defined MCP server."""
    package_root = project_root / "packages" / "local-mcp"
    package_root.mkdir(parents=True)
    (package_root / "apm.yml").write_text(
        yaml.safe_dump(
            {
                "name": "local-mcp",
                "version": "1.0.0",
                "targets": ["copilot", "codex"],
                "dependencies": {
                    "mcp": [
                        {
                            "name": "local-lifecycle-server",
                            "registry": False,
                            "transport": "stdio",
                            "command": "python",
                            "args": ["-m", "local_lifecycle_server"],
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    (project_root / "apm.yml").write_text(
        yaml.safe_dump(
            {
                "name": "mcp-lockfile-determinism",
                "version": "1.0.0",
                "targets": ["copilot", "codex"],
                "dependencies": {"apm": ["./packages/local-mcp"]},
            }
        ),
        encoding="utf-8",
    )
    github_dir = project_root / ".github"
    github_dir.mkdir()
    (github_dir / "copilot-instructions.md").write_text("# Test project\n", encoding="utf-8")


def _run_install(runner: CliRunner) -> Result:
    """Run the hermetic CLI install with update checks disabled."""
    with patch("apm_cli.commands._helpers.check_for_updates", return_value=None):
        return runner.invoke(cli, _INSTALL_ARGS, catch_exceptions=False)


@pytest.mark.lifecycle_smoke
def test_unchanged_local_mcp_install_keeps_lockfile_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repeated local MCP install preserves all lockfile state byte-for-byte."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    _write_local_mcp_project(project_root)
    monkeypatch.chdir(project_root)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    runner = CliRunner()

    first_result = _run_install(runner)
    assert first_result.exit_code == 0, first_result.output

    lock_path = project_root / "apm.lock.yaml"
    first_bytes = lock_path.read_bytes()
    first_data = yaml.safe_load(first_bytes)
    first_lock = LockFile.read(lock_path)
    assert first_lock is not None
    assert first_data["generated_at"] == first_lock.generated_at
    assert first_data["deployments"]
    assert first_data["mcp_target_servers"] == _TARGET_SERVERS
    assert first_lock.mcp_target_servers == _TARGET_SERVERS

    second_result = _run_install(runner)
    assert second_result.exit_code == 0, second_result.output

    second_bytes = lock_path.read_bytes()
    second_data = yaml.safe_load(second_bytes)
    second_lock = LockFile.read(lock_path)
    assert second_lock is not None
    assert second_data["generated_at"] == first_data["generated_at"]
    assert second_data["deployments"] == first_data["deployments"]
    assert second_data["mcp_target_servers"] == first_data["mcp_target_servers"]
    assert second_lock.generated_at == first_lock.generated_at
    assert second_lock.deployment_ledger == first_lock.deployment_ledger
    assert second_lock.mcp_target_servers == first_lock.mcp_target_servers
    assert second_bytes == first_bytes
