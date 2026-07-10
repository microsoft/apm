"""End-to-end regression coverage for transitive MCP audit consistency."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.integration

CLI = [sys.executable, "-m", "apm_cli.cli"]
TIMEOUT = 180


def _run(project: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the APM CLI in the fixture project."""
    return subprocess.run(
        [*CLI, *args],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
        check=False,
    )


def _write_workspace(root: Path) -> Path:
    """Create a root project with an MCP-contributing local package."""
    project = root / "project"
    package = project / "packages" / "agent-config"
    package.mkdir(parents=True)
    (project / ".github").mkdir()
    (project / ".github" / "copilot-instructions.md").write_text(
        "# test target\n", encoding="utf-8"
    )
    (project / "apm.yml").write_text(
        """name: transitive-mcp-consumer
version: 1.0.0
dependencies:
  apm:
    - ./packages/agent-config
""",
        encoding="utf-8",
    )
    (package / "apm.yml").write_text(
        """name: agent-config
version: 1.0.0
dependencies:
  mcp:
    - name: shadcn
      registry: false
      transport: stdio
      command: echo
      args: ["ready"]
""",
        encoding="utf-8",
    )
    return project


def test_force_install_then_ci_audit_accepts_transitive_mcp(tmp_path: Path) -> None:
    """A clean forced install must not report the transitive server as orphaned."""
    project = _write_workspace(tmp_path)

    install = _run(
        project,
        "install",
        "--force",
        "--trust-transitive-mcp",
        "--target",
        "copilot",
    )
    assert install.returncode == 0, (
        f"install stdout:\n{install.stdout}\ninstall stderr:\n{install.stderr}"
    )

    lock_data = yaml.safe_load((project / "apm.lock.yaml").read_text(encoding="utf-8"))
    assert lock_data["mcp_config_provenance"] == {"shadcn": "agent-config"}

    audit = _run(project, "audit", "--ci", "--no-policy", "-f", "json")
    assert audit.returncode == 0, f"audit stdout:\n{audit.stdout}\naudit stderr:\n{audit.stderr}"
    payload = json.loads(audit.stdout)
    config_check = next(
        check for check in payload["checks"] if check["name"] == "config-consistency"
    )
    assert config_check["passed"] is True, config_check


def test_ci_audit_rejects_removed_transitive_mcp_declaration(tmp_path: Path) -> None:
    """Removing an installed local package's MCP declaration must fail audit."""
    project = _write_workspace(tmp_path)

    install = _run(
        project,
        "install",
        "--force",
        "--trust-transitive-mcp",
        "--target",
        "copilot",
        "--no-policy",
    )
    assert install.returncode == 0, (
        f"install stdout:\n{install.stdout}\ninstall stderr:\n{install.stderr}"
    )

    control = _run(
        project,
        "audit",
        "--ci",
        "--no-policy",
        "--no-fail-fast",
        "-f",
        "json",
    )
    assert control.returncode == 0, (
        f"audit stdout:\n{control.stdout}\naudit stderr:\n{control.stderr}"
    )

    package_manifest = project / "packages" / "agent-config" / "apm.yml"
    package_manifest.write_text(
        """name: agent-config
version: 1.0.0
""",
        encoding="utf-8",
    )

    audit = _run(
        project,
        "audit",
        "--ci",
        "--no-policy",
        "--no-fail-fast",
        "-f",
        "json",
    )
    assert audit.returncode == 1, f"audit stdout:\n{audit.stdout}\naudit stderr:\n{audit.stderr}"
    payload = json.loads(audit.stdout)
    config_check = next(
        check for check in payload["checks"] if check["name"] == "config-consistency"
    )
    assert config_check["passed"] is False
    assert config_check["details"] == [
        "shadcn: in lockfile but not in current source (declared by agent-config)"
    ]
