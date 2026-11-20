"""APMLifecycle: ``apm lock`` keeps parked executables' approval state (#4213).

PROMISE: idempotency.byte_stable (I2) -- on a converged project ``apm lock``
and ``apm install`` repeat as byte no-ops; the ``exec_status`` that install
records for a package whose executables are parked pending approval survives
``apm lock`` (source: tests/fixtures/lifecycle_bug_ledger.json
property_catalog; issue #4213).
CELL: OP=lock SRC=local PRIM={hook,lsp,mcp} SCOPE=project TGT=copilot
GATE=pending-approval LOCK=converged
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from apm_cli.deps.lockfile import LockFile
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.lifecycle_state import LifecycleStateSnapshot
from tests.utils.local_package import LocalPackage, LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-LAW-outcome.status_matches_state"),
]

_TARGET = "copilot"
_GATED = "gated_pending_approval"


def _evidence(result: CommandResult) -> str:
    return (
        f"command={result.command!r}\nexit={result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )


def _assert_output_oracles(result: CommandResult) -> None:
    """I6 ASCII and I9 no duplicate status lines per stream."""
    for stream in (result.stdout, result.stderr):
        assert stream.isascii(), _evidence(result)
        lines = [line.strip() for line in stream.splitlines() if line.lstrip().startswith("[")]
        assert len(lines) == len(set(lines)), _evidence(result)


def _hook_package(factory: LocalPackageFactory) -> LocalPackage:
    package = factory.create("hook-kit")
    factory.add_hook(
        package,
        "guard",
        {
            "hooks": {
                "PreToolUse": [
                    {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo parked"}]}
                ]
            }
        },
    )
    return package


def _lsp_package(factory: LocalPackageFactory) -> LocalPackage:
    return factory.create(
        "lsp-kit",
        lsp_dependencies=(
            {
                "name": "kit-lsp",
                "command": "inert-language-server",
                "extensionToLanguage": {".kit": "kit"},
            },
        ),
    )


def _mcp_package(factory: LocalPackageFactory) -> LocalPackage:
    return factory.create(
        "mcp-kit",
        mcp_dependencies=(
            {
                "name": "kit-mcp",
                "registry": False,
                "transport": "stdio",
                "command": "inert-mcp-server",
            },
        ),
    )


_PACKAGES = {"hook": _hook_package, "lsp": _lsp_package, "mcp": _mcp_package}


def _exec_statuses(project: Path) -> dict[str, str | None]:
    lock = LockFile.read(project / "apm.lock.yaml")
    assert lock is not None
    return {key: dep.exec_status for key, dep in lock.dependencies.items()}


@pytest.mark.parametrize("primitive", sorted(_PACKAGES))
def test_lock_keeps_parked_exec_status_and_converges(
    tmp_path: Path, apm_binary_path: Path, primitive: str
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "world", base_env=dict(os.environ))
    env = isolated.subprocess_env()
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=180)
    factory = LocalPackageFactory(isolated.work_root)
    package = _PACKAGES[primitive](factory)
    project = factory.create("consumer", targets=(_TARGET,))
    factory.add_relative_dependency(project, package)
    manifest = load_yaml(project.manifest_path)
    manifest["executables"] = {"allow": {}}
    dump_yaml(manifest, project.manifest_path)

    def apm(*args: str) -> CommandResult:
        result = runner.run(args, cwd=project.root, env=env)
        _assert_output_oracles(result)
        assert result.returncode == 0, _evidence(result)
        return result

    def snap() -> LifecycleStateSnapshot:
        return LifecycleStateSnapshot.capture(project.root, targets=(_TARGET,))

    apm("install")
    assert set(_exec_statuses(project.root).values()) == {_GATED}
    converged = snap()
    lock_bytes = (project.root / "apm.lock.yaml").read_bytes()

    locked = apm("lock")
    assert "[+] Lockfile written to apm.lock.yaml" in locked.stdout, _evidence(locked)
    assert set(_exec_statuses(project.root).values()) == {_GATED}
    assert (project.root / "apm.lock.yaml").read_bytes() == lock_bytes, "I2: lock changed bytes"
    assert snap() == converged, "I2: lock changed durable state"

    apm("install")
    assert (project.root / "apm.lock.yaml").read_bytes() == lock_bytes, "I2: install changed bytes"
    assert snap() == converged, "I2: install after lock changed durable state"
