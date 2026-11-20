"""Every structured remediation clears the state that reported it (#4090).

Each proof induces one reported state on an installed project, reads the
structured remediation the installed CLI attached to that report, runs exactly
the advised command through the same binary, and observes that the reported
state is gone. The advice under test is what the user sees, never a copy of
the owner's catalog.
"""

from __future__ import annotations

import json
import shlex
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.integration.test_required_lifecycle_state_machine import (
    _INSTALL_ARGS,
    _PublishedPackage,
    _publish,
    _result_evidence,
    _run_success,
    _Scenario,
    _new_scenario,
)
from tests.utils.local_package import LocalPackage

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
]

_AUDIT_ARGS = ("audit", "--ci", "--no-policy", "--no-fail-fast", "--format", "json")
_INSTRUCTION = ".github/instructions/advice.instructions.md"


@dataclass(frozen=True)
class _World:
    scenario: _Scenario
    source: _PublishedPackage
    consumer: LocalPackage


@dataclass(frozen=True)
class _AuditProof:
    """One reported audit state and the catalog remediation that must clear it."""

    remediation_id: str
    check: str
    induce: Callable[[_World], None]


def _drop_lockfile(world: _World) -> None:
    (world.consumer.root / "apm.lock.yaml").unlink()


def _delete_deployed_file(world: _World) -> None:
    (world.consumer.root / _INSTRUCTION).unlink()


def _undeclare_dependency(world: _World) -> None:
    manifest = load_yaml(world.consumer.manifest_path)
    manifest["dependencies"]["apm"] = []
    dump_yaml(manifest, world.consumer.manifest_path)


AUDIT_PROOFS: tuple[_AuditProof, ...] = (
    _AuditProof("deployed-files-missing", "deployed-files-present", _delete_deployed_file),
    _AuditProof("lockfile-missing", "lockfile-exists", _drop_lockfile),
    _AuditProof("orphaned-packages", "no-orphaned-packages", _undeclare_dependency),
)


def _installed_world(tmp_path: Path, apm_binary_path: Path) -> _World:
    scenario = _new_scenario(tmp_path / "remediation", apm_binary_path)
    source = _publish(scenario, "advice-kit", instruction="advice")
    consumer = scenario.consumers.create(
        "advice-consumer",
        dependencies=(source.dependency,),
        targets=("copilot",),
    )
    world = _World(scenario=scenario, source=source, consumer=consumer)
    _run_success(
        scenario,
        consumer,
        _INSTALL_ARGS,
        environment=source.environment,
        scenario_id="remediation-install",
    )
    assert (consumer.root / _INSTRUCTION).is_file()
    return world


def _audit(world: _World, scenario_id: str) -> dict[str, object]:
    result = world.scenario.runner.run(
        _AUDIT_ARGS,
        scenario_id=scenario_id,
        cwd=world.consumer.root,
        env=world.source.environment,
    )
    assert result.returncode in (0, 1), _result_evidence(result)
    payload = json.loads(result.stdout)
    assert isinstance(payload, dict)
    assert payload["passed"] is (result.returncode == 0), _result_evidence(result)
    return payload


def _check(payload: dict[str, object], name: str) -> dict[str, object]:
    for check in payload["checks"]:
        if check["name"] == name:
            return check
    raise AssertionError(f"audit did not run check {name!r}: {payload!r}")


@pytest.mark.parametrize("proof", AUDIT_PROOFS, ids=lambda proof: proof.remediation_id)
def test_audit_remediation_clears_the_reported_check(
    tmp_path: Path, apm_binary_path: Path, proof: _AuditProof
) -> None:
    world = _installed_world(tmp_path, apm_binary_path)
    assert _audit(world, "remediation-audit-clean")["passed"] is True

    proof.induce(world)
    reported = _check(_audit(world, "remediation-audit-reported"), proof.check)
    assert reported["passed"] is False, reported
    remediation = reported.get("remediation")
    assert isinstance(remediation, dict), f"{proof.check} carries no structured remediation"
    assert remediation["id"] == proof.remediation_id
    assert remediation["fixes"]
    command = shlex.split(remediation["command"])
    assert command[0] == "apm", remediation
    assert remediation["command"] in reported["message"] + " ".join(reported["details"])

    _run_success(
        world.scenario,
        world.consumer,
        tuple(command[1:]),
        environment=world.source.environment,
        scenario_id=f"remediation-run-{proof.remediation_id}",
    )
    cleared = _audit(world, "remediation-audit-cleared")
    assert _check(cleared, proof.check)["passed"] is True
    assert cleared["passed"] is True, cleared
