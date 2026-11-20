"""Every structured audit remediation clears the check that reported it (#4090).

Each proof induces one reported state on an installed project, reads the
structured remediation that ``apm audit --ci --format json`` attached to the
failed check, runs exactly the advised command through the same binary, and
observes the check pass. The advice under test is what the CLI emits, never a
copy of the owner's catalog.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.remediation_proof import (
    ProofWorld,
    advised_json,
    check,
    flat,
    installed_world,
    run_advised,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
]

_INSTRUCTION = ".github/instructions/proof.instructions.md"


@dataclass(frozen=True)
class _AuditProof:
    remediation_id: str
    check: str
    induce: Callable[[ProofWorld], None]


def _drop_lockfile(world: ProofWorld) -> None:
    (world.root / "apm.lock.yaml").unlink()


def _delete_deployed_file(world: ProofWorld) -> None:
    (world.root / _INSTRUCTION).unlink()


def _undeclare_dependency(world: ProofWorld) -> None:
    manifest = load_yaml(world.consumer.manifest_path)
    manifest["dependencies"]["apm"] = []
    dump_yaml(manifest, world.consumer.manifest_path)


AUDIT_PROOFS: tuple[_AuditProof, ...] = (
    _AuditProof("deployed-files-missing", "deployed-files-present", _delete_deployed_file),
    _AuditProof("lockfile-missing", "lockfile-exists", _drop_lockfile),
    _AuditProof("orphaned-packages", "no-orphaned-packages", _undeclare_dependency),
)

PROVES = ("deployed-files-missing", "lockfile-missing", "orphaned-packages")


@pytest.mark.parametrize("proof", AUDIT_PROOFS, ids=lambda proof: proof.remediation_id)
def test_audit_remediation_clears_the_reported_check(
    tmp_path: Path, apm_binary_path: Path, proof: _AuditProof
) -> None:
    world = installed_world(tmp_path, apm_binary_path)
    assert world.audit_ci("proof-audit-clean")["passed"] is True

    proof.induce(world)
    reported = check(world.audit_ci("proof-audit-reported"), proof.check)
    assert reported["passed"] is False, reported
    remediation = advised_json(proof.remediation_id, reported)
    assert f"'{remediation.command}' to {remediation.fixes_promise}" in flat(reported["message"])

    run_advised(world, remediation)
    cleared = world.audit_ci("proof-audit-cleared")
    assert check(cleared, proof.check)["passed"] is True
    assert cleared["passed"] is True, cleared
