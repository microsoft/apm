"""Shared harness for executable remediation proofs (#4090).

A proof induces the state one ``core/remediation.py`` catalog entry claims to
clear, observes the installed CLI report that state and print the entry's
advice, runs exactly the advised command through the same binary, and
observes the state gone.

Every ``tests/integration/test_remediation_proofs_*.py`` module (and the
audit-JSON proofs in ``test_remediation_advice_lifecycle.py``) declares a
module-level ``PROVES`` tuple of string literals naming the catalog ids it
proves; ``tests/unit/core/test_remediation_catalog.py`` requires the union to
equal the catalog, each id proven exactly once.
"""

from __future__ import annotations

import json
import shlex
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from apm_cli.core.remediation import Remediation, catalog
from tests.integration.test_required_lifecycle_state_machine import (
    _INSTALL_ARGS,
    _new_scenario,
    _publish,
    _PublishedPackage,
    _result_evidence,
    _Scenario,
)
from tests.utils.apm_lifecycle_runner import CommandResult
from tests.utils.local_package import LocalPackage

AUDIT_CI_JSON = ("audit", "--ci", "--no-policy", "--no-fail-fast", "--format", "json")


def flat(text: str) -> str:
    """Collapse whitespace so wrapped terminal output compares as one line."""
    return " ".join(text.split())


@dataclass
class ProofWorld:
    """One isolated APM environment with a published package and a consumer project."""

    scenario: _Scenario
    source: _PublishedPackage
    consumer: LocalPackage
    env: dict[str, str] = field(default_factory=dict)

    @property
    def root(self) -> Path:
        return self.consumer.root

    def run(
        self,
        args: tuple[str, ...],
        *,
        scenario_id: str,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        expect: int | None = 0,
    ) -> CommandResult:
        """Run the installed binary; ``expect=None`` accepts any exit code."""
        result = self.scenario.runner.run(
            args,
            scenario_id=scenario_id,
            cwd=cwd or self.root,
            env={**self.env, **(env or {})},
        )
        if expect is not None:
            assert result.returncode == expect, _result_evidence(result)
        return result

    def install(self, *extra: str, scenario_id: str = "proof-install") -> CommandResult:
        return self.run((*_INSTALL_ARGS, *extra), scenario_id=scenario_id)

    def audit_ci(self, scenario_id: str) -> dict[str, object]:
        """Run ``audit --ci --format json`` (exit 0 or 1) and return the payload."""
        result = self.run(AUDIT_CI_JSON, scenario_id=scenario_id, expect=None)
        assert result.returncode in (0, 1), _result_evidence(result)
        payload = json.loads(result.stdout)
        assert isinstance(payload, dict)
        return payload


def installed_world(
    tmp_path: Path,
    apm_binary_path: Path,
    *,
    name: str = "proof",
    targets: tuple[str, ...] = ("copilot",),
    skill: str | None = None,
    instruction: str | None = "proof",
    hook_command: str | None = None,
    install: bool = True,
) -> ProofWorld:
    """Publish one local package, declare it in a consumer project, and install it."""
    scenario = _new_scenario(tmp_path / name, apm_binary_path)
    source = _publish(
        scenario,
        f"{name}-kit",
        skill=skill,
        instruction=instruction,
        hook_command=hook_command,
    )
    consumer = scenario.consumers.create(
        f"{name}-consumer", dependencies=(source.dependency,), targets=targets
    )
    world = ProofWorld(scenario, source, consumer, dict(source.environment))
    if install:
        world.install(scenario_id=f"{name}-install")
    return world


def advised(remediation_id: str, output: str, **values: object) -> Remediation:
    """Return the bound catalog entry after asserting *output* shows its advice."""
    remediation = catalog()[remediation_id]
    if values:
        remediation = remediation.bind(**values)
    shown = flat(output)
    assert f"'{remediation.command}'" in shown, (
        f"{remediation_id}: advice {remediation.command!r} not shown in:\n{output}"
    )
    assert flat(remediation.fixes_promise) in shown, (
        f"{remediation_id}: promise {remediation.fixes_promise!r} not shown in:\n{output}"
    )
    return remediation


def advised_json(remediation_id: str, check: Mapping[str, object]) -> Remediation:
    """Return the catalog entry an ``audit --ci --format json`` check carries."""
    entries = check.get("remediations")
    assert isinstance(entries, list), f"{check.get('name')} carries no structured remediation"
    match = [entry for entry in entries if entry.get("id") == remediation_id]
    assert len(match) == 1, f"{remediation_id} not in {entries!r}"
    entry = match[0]
    expected = catalog()[remediation_id]
    assert entry["fixes"] and entry["clears"] == expected.clears
    return Remediation(
        entry["id"],
        entry["command"],
        entry["fixes"],
        entry.get("before"),
        entry.get("then"),
        entry["clears"],
    )


def advised_argv(
    remediation: Remediation, substitutions: Mapping[str, str] | None = None
) -> tuple[str, ...]:
    """Split the advised command, replacing ``<placeholders>`` the user supplies."""
    command = remediation.command
    for placeholder, value in (substitutions or {}).items():
        command = command.replace(placeholder, shlex.quote(value))
    assert "<" not in command, f"unfilled placeholder in {command!r}"
    argv = tuple(shlex.split(command))
    assert argv[0] == "apm", command
    return argv[1:]


def run_advised(
    world: ProofWorld,
    remediation: Remediation,
    *,
    substitutions: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    expect: int | None = 0,
) -> CommandResult:
    """Run exactly the advised command through the installed binary."""
    return world.run(
        advised_argv(remediation, substitutions),
        scenario_id=f"proof-run-{remediation.id}",
        cwd=cwd,
        env=env,
        expect=expect,
    )


def check(payload: Mapping[str, object], name: str) -> dict[str, object]:
    for item in payload["checks"]:
        if item["name"] == name:
            return item
    raise AssertionError(f"audit did not run check {name!r}: {payload!r}")
