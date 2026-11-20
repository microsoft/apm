"""Installed-CLI regression: a policy-blocked ``install --mcp`` prints its reason.

Promise (MCP install path, ``src/apm_cli/install/mcp/command.py``): when org
policy cannot be fetched and the project sets ``policy.fetch_failure_default:
block``, ``apm install --mcp`` exits 1 and says why (as ``apm install <pkg>``
does), never a bare ``Error installing dependencies: 1``. The printed way out
(``--no-policy``) works when run.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-OWN-policy-outcome-routing"),
]

_MCP = ("install", "--mcp", "fetch", "--", "npx", "-y", "@modelcontextprotocol/server-fetch")


def _run(binary: Path, cwd: Path, env: dict[str, str], *argv: str):
    return subprocess.run(
        (str(binary), *argv),
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


def test_blocked_mcp_install_prints_the_reason(apm_binary_path: Path, tmp_path: Path) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "environment", base_env=dict(os.environ))
    env = isolated.subprocess_env()
    project = isolated.work_root / "consumer"
    project.mkdir()
    (project / "apm.yml").write_text(
        "name: consumer\nversion: 1.0.0\ntargets: [claude]\n"
        "policy:\n  fetch_failure_default: block\n",
        encoding="utf-8",
    )
    for argv in (("init", "-q"), ("remote", "add", "origin", "https://github.com/acme-org/p.git")):
        subprocess.run(("git", *argv), cwd=project, env=env, check=True, capture_output=True)
    manifest = (project / "apm.yml").read_bytes()

    blocked = _run(apm_binary_path, project, env, *_MCP)

    output = blocked.stdout + blocked.stderr
    assert blocked.returncode == 1, output
    assert "Error installing dependencies: 1" not in output, output
    assert "fetch_failure_default=block" in blocked.stderr, output
    assert (project / "apm.yml").read_bytes() == manifest
    assert "--no-policy" in output, output

    retried = _run(apm_binary_path, project, env, "install", "--no-policy", *_MCP[1:])
    assert retried.returncode == 0, retried.stdout + retried.stderr
    assert "fetch" in (project / "apm.yml").read_text(encoding="utf-8")
