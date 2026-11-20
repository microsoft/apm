"""Installed-CLI parity: ``targets:`` on APM and MCP dependency entries.

``dependencies.apm[].targets`` and ``dependencies.mcp[].targets`` are parsed
by one target-subset owner, so for every value class (valid, unknown, ``all``,
MCP-only runtimes, a target outside the project's active set, malformed
shapes) both entries get the same verdict and the same reason.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
import yaml

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.lifecycle_state import LifecycleStateSnapshot

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
    pytest.mark.promise("P-LAW-routing.authorized_targets_only"),
]

_RULE = "---\napplyTo: '**'\ndescription: Parity rule\n---\nParity.\n"
# value id -> (targets value, expected exit code, reason both entries report)
_CASES = {
    "valid": (["copilot"], 0, None),
    "unknown": (["nope"], 1, "Unknown target 'nope'"),
    "all": (["all"], 1, "Unknown target 'all'"),
    "mcp-only-intellij": (["intellij"], 1, "Unknown target 'intellij'"),
    "mcp-only-copilot-app": (
        ["copilot-app"],
        0,
        "targets [copilot-app] do not overlap active install targets",
    ),
    "outside-project": (["claude"], 0, "targets [claude] do not overlap active install targets"),
    "outside-path": (["../outside"], 1, "Unknown target '../outside'"),
    "not-a-list": ("copilot", 1, "'targets' field must be a list of target names"),
    "empty": ([], 1, "targets: must contain at least one target"),
}


def _evidence(result: CommandResult) -> str:
    return f"command={result.command!r}\nstdout={result.stdout!r}\nstderr={result.stderr!r}"


def _entry(kind: str, targets: object) -> dict[str, object]:
    if kind == "apm":
        return {"apm": [{"path": "./packages/parity-dep", "targets": targets}]}
    return {
        "mcp": [
            {
                "name": "parity-server",
                "registry": False,
                "transport": "stdio",
                "command": "python",
                "args": ["-m", "parity_server"],
                "targets": targets,
            }
        ]
    }


def _install(tmp_path: Path, apm_binary_path: Path, kind: str, targets: object):
    isolated = IsolatedApmEnvironment.create(tmp_path / kind, base_env=dict(os.environ))
    root = isolated.work_root / "parity"
    dependency = root / "packages/parity-dep"
    (dependency / ".apm/instructions").mkdir(parents=True)
    (dependency / "apm.yml").write_text("name: parity-dep\nversion: 1.0.0\n", encoding="utf-8")
    (dependency / ".apm/instructions/parity.instructions.md").write_text(_RULE, encoding="utf-8")
    manifest = {
        "name": "parity",
        "version": "1.0.0",
        "targets": ["copilot"],
        "dependencies": _entry(kind, targets),
    }
    (root / "apm.yml").write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    before = LifecycleStateSnapshot.capture(root, targets=("copilot",))
    result = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=120).run(
        ("install", "--trust-transitive-mcp"),
        cwd=root,
        env=isolated.subprocess_env(),
        scenario_id=f"parity-{kind}",
    )
    after = LifecycleStateSnapshot.capture(root, targets=("copilot",))
    return result, before, after


def _reason(result: CommandResult) -> str:
    """The output as one line, so wrapped diagnostics compare by content."""
    return re.sub(r"\s+", " ", result.stdout + result.stderr)


@pytest.mark.parametrize("case", sorted(_CASES))
def test_apm_and_mcp_dependency_targets_share_verdict_and_reason(
    tmp_path: Path, apm_binary_path: Path, case: str
) -> None:
    targets, exit_code, reason = _CASES[case]

    outcomes = {kind: _install(tmp_path, apm_binary_path, kind, targets) for kind in ("apm", "mcp")}

    for kind, (result, before, after) in outcomes.items():
        assert result.returncode == exit_code, (kind, _evidence(result))
        if reason is not None:
            assert reason in _reason(result), (kind, _evidence(result))
        if exit_code != 0:
            # A refused manifest changes nothing on disk.
            assert after.files == before.files, (kind, _evidence(result))
            assert after.lockfile_bytes is None, kind
