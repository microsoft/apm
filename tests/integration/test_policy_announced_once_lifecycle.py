"""APMLifecycle: a blocking org policy is announced once per install (#4267).

PROMISE: I9 -- no duplicate diagnostics: the policy discovery line appears once
per command, even when the MCP phase re-checks the merged MCP set against the
same org policy (source: issue #4267).
CELL: OP=install SRC=local PRIM=mcp SCOPE=project GATE=org-block-mcp-deny NET=offline
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
]

_TARGET = "copilot"
_GIT_REMOTE = "https://github.com/acme/project.git"
_POLICY_REPO = "acme/.github-private"
_SEED = (
    "import sys\nfrom pathlib import Path\n"
    "from apm_cli.policy.discovery import _write_cache\n"
    "from apm_cli.policy.parser import load_policy\n"
    "policy, _ = load_policy(Path(sys.argv[2]).read_text())\n"
    f"_write_cache({_POLICY_REPO!r}, policy, Path(sys.argv[1]))\n"
)


def _evidence(result: CommandResult) -> str:
    return (
        f"command={result.command!r}\nexit={result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )


def test_blocking_policy_line_printed_once_when_mcp_phase_is_denied(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "world", base_env=dict(os.environ))
    env = isolated.subprocess_env()
    env["APM_NON_INTERACTIVE"] = "1"
    env["COLUMNS"] = "400"
    packages = LocalPackageFactory(isolated.work_root)
    server = {
        "name": "p-exec-mcp",
        "registry": False,
        "transport": "stdio",
        "command": "printf",
        "args": ["hello"],
    }
    executable = packages.create("p-exec", mcp_dependencies=[server])
    project = packages.create("consumer", targets=(_TARGET,))
    packages.add_relative_dependency(project, executable)
    for argv in (("init", "-q"), ("remote", "add", "origin", _GIT_REMOTE)):
        subprocess.run(("git", *argv), cwd=project.root, env=env, check=True, capture_output=True)
    policy = isolated.root / "policy.yml"
    dump_yaml(
        {"name": "org", "version": "1.0", "enforcement": "block", "mcp": {"deny": ["*"]}},
        policy,
    )
    subprocess.run(
        [sys.executable, "-c", _SEED, str(project.root), str(policy)], env=env, check=True
    )
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=120)

    result = runner.run(("install",), cwd=project.root, env=env)

    assert result.returncode == 1, _evidence(result)
    output = result.stdout + "\n" + result.stderr
    assert output.isascii(), _evidence(result)
    announced = [line for line in output.splitlines() if "Policy: org:" in line]
    assert len(announced) == 1, _evidence(result)
    assert "enforcement=block" in announced[0], _evidence(result)
    assert "p-exec-mcp" in output, _evidence(result)
    assert "blocked by org policy" in output, _evidence(result)
