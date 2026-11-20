"""Failure-phase lifecycle scenarios: a fault at each install commit point (#4103).

PROMISE: P-INV-I7 / P-LAW-transaction.failed_command_preserves_state -- an
install that fails at any commit point, or is stopped by SIGTERM, leaves the
manifest, the lockfile and the deployed surfaces as they were, announces the
failure, and the next clean run converges (source: tests/lifecycle/oracles.py,
src/apm_cli/install/transaction.py).
PROMISE: P-OWN-process-interrupt-signals -- SIGTERM takes the Ctrl+C path, so
the install rolls back (source: .apm/architecture/owners/core-runtime.json).

Faults reuse ``tests/integration/_fault_injection_kit.py`` (``remove``) and the
product's own test seam ``APM_TEST_FAIL_LOCK_REPLACE`` (utils/atomic_io.py).
Permission faults are not used: CI and dev containers may run as root, where
a read-only bit blocks nothing; every fault here is an obstruction (a file
where APM needs a directory or the reverse), an org policy, the env seam, or a
signal. SIGTERM is delivered by a one-shot ``git`` shim on PATH that signals
its parent (the running ``apm``) when install looks up the project's origin
remote for policy discovery, after apm.yml already holds the new package.

Each row: GIVEN a converged world; WHEN the fault is armed and the command
runs; THEN exit 1, the exact durable change set (I5), the announcement (I7),
and I1-I9 from ``check_cross_cutting``; HEAL removes the fault, the same
command converges, a repeat is a byte no-op (I2) and audit agrees (I3).

CELL: OP=install SRC=local-path PRIM={skill,hook,mcp} SCOPE=project FAULT=<commit point>
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field

import pytest
import yaml

from tests.integration._fault_injection_kit import remove
from tests.lifecycle.scenarios._kit import Known, Scenario, assert_changes
from tests.lifecycle.world import use_policy

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.lifecycle_smoke,
    pytest.mark.skipif(os.name == "nt", reason="POSIX shell shim and signals"),
    pytest.mark.promise(
        "P-INV-I1",
        "P-INV-I2",
        "P-INV-I3",
        "P-INV-I5",
        "P-INV-I7",
        "P-INV-I8",
        "P-LAW-transaction.failed_command_preserves_state",
        "P-OWN-process-interrupt-signals",
    ),
]

# A failed install that already deployed: the lockfile or the MCP config write
# fails after target files were written, and the deploy is not rolled back.
_DEPLOYED_BEFORE_FAILURE = "#4164"
_UNTRACKED_SKILL = Known(
    _DEPLOYED_BEFORE_FAILURE, r"deployed file is not in the lock: .agents/skills/p-child/SKILL.md"
)
_DEPLOY_CHANGED = Known(_DEPLOYED_BEFORE_FAILURE, r"failed install changed deployed surfaces")
_LOCK_CHANGED = Known(
    _DEPLOYED_BEFORE_FAILURE,
    r"failed install changed the lockfile|failed install changed deployed surfaces",
)


@dataclass(frozen=True)
class FaultRow:
    """One commit point: how to arm the fault, what the failed run may change, how to heal."""

    id: str
    when: str
    arm: Callable[[Scenario], None]
    heal: Callable[[Scenario], None]
    announce: str
    changed: tuple[str, ...] = ()
    exec_given: bool = False
    known: dict[str, Known] = field(default_factory=dict)


def _obstruct_with_file(relative: str) -> Callable[[Scenario], None]:
    def arm(scenario: Scenario) -> None:
        path = scenario.world.project / relative
        remove(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("user notes\n", encoding="ascii")

    return arm


def _obstruct_with_dir(relative: str) -> Callable[[Scenario], None]:
    def arm(scenario: Scenario) -> None:
        path = scenario.world.project / relative
        remove(path)
        path.mkdir(parents=True)
        (path / "notes.md").write_text("user notes\n", encoding="ascii")

    return arm


def _remove_project_path(relative: str) -> Callable[[Scenario], None]:
    return lambda scenario: remove(scenario.world.project / relative)


def _arm_receipts(scenario: Scenario) -> None:
    receipts = scenario.world.home / ".apm" / "deployment-receipts"
    shutil.move(receipts, scenario.world.root / "receipts-aside")
    receipts.write_text("user notes\n", encoding="ascii")


def _heal_receipts(scenario: Scenario) -> None:
    receipts = scenario.world.home / ".apm" / "deployment-receipts"
    receipts.unlink()
    shutil.move(scenario.world.root / "receipts-aside", receipts)


def _arm_env(name: str) -> Callable[[Scenario], None]:
    return lambda scenario: scenario.world.env.__setitem__(name, "1")


def _heal_env(name: str) -> Callable[[Scenario], None]:
    return lambda scenario: scenario.world.env.pop(name)


def _arm_policy(scenario: Scenario) -> None:
    use_policy(scenario.world, "hash-mismatch")


def _heal_policy(scenario: Scenario) -> None:
    """Drop the stale ``policy.hash`` pin and point the project at an allow policy."""
    manifest = scenario.world.project / "apm.yml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    del data["policy"]
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")
    use_policy(scenario.world, "allow")


def _arm_sigterm(scenario: Scenario) -> None:
    """A one-shot git shim: the first ``git remote get-url`` sends SIGTERM to apm."""
    world = scenario.world
    real = shutil.which("git", path=world.env["PATH"])
    shim = world.root / "sigterm-shim"
    shim.mkdir()
    armed = shim / "armed"
    armed.write_text("", encoding="ascii")
    script = (
        "#!/bin/sh\n"
        f'if [ "$1 $2" = "remote get-url" ] && mv "{armed}" "{armed}.fired" 2>/dev/null; then\n'
        '  kill -TERM "$PPID"\n'
        "  sleep 1\n"
        "fi\n"
        f'exec "{real}" "$@"\n'
    )
    (shim / "git").write_text(script, encoding="ascii")
    (shim / "git").chmod(0o755)
    world.env["PATH"] = os.pathsep.join((str(shim), world.env["PATH"]))


def _heal_sigterm(scenario: Scenario) -> None:
    world = scenario.world
    assert (world.root / "sigterm-shim" / "armed.fired").exists(), "SIGTERM was never sent"
    world.env["PATH"] = world.env["PATH"].split(os.pathsep, 1)[1]


_INSTALL_CHILD = "install p-child"
ROWS = (
    FaultRow(
        "policy-gate",
        _INSTALL_CHILD,
        _arm_policy,
        _heal_policy,
        announce="apm.yml restored to its previous state",
    ),
    FaultRow(
        "resolution-staging",
        _INSTALL_CHILD,
        _obstruct_with_file("apm_modules/.apm-resolution-staging"),
        _remove_project_path("apm_modules/.apm-resolution-staging"),
        announce="No install transaction changes were committed",
    ),
    FaultRow(
        "package-activation",
        _INSTALL_CHILD,
        _obstruct_with_file("apm_modules/_local"),
        _remove_project_path("apm_modules"),
        announce="The candidate was not activated",
        # Announced recovery data, kept for a safe manual restore.
        changed=("project:apm_modules/.apm-resolution-staging*",),
    ),
    FaultRow(
        "skill-integration",
        _INSTALL_CHILD,
        _obstruct_with_dir(".agents/skills/p-child/SKILL.md"),
        _remove_project_path(".agents/skills/p-child"),
        announce="conflicts with unmanaged file '.agents/skills/p-child/SKILL.md'",
    ),
    FaultRow(
        "hook-integration",
        "install",
        _obstruct_with_file(".github/hooks"),
        _remove_project_path(".github/hooks"),
        announce="No install transaction changes were committed",
        exec_given=True,
    ),
    FaultRow(
        "deployment-receipts",
        _INSTALL_CHILD,
        _arm_receipts,
        _heal_receipts,
        announce="apm.yml restored to its previous state",
    ),
    FaultRow(
        "lockfile-write",
        _INSTALL_CHILD,
        _arm_env("APM_TEST_FAIL_LOCK_REPLACE"),
        _heal_env("APM_TEST_FAIL_LOCK_REPLACE"),
        announce="Could not generate apm.lock.yaml",
        changed=("project:.agents/skills/p-child*",),
        known={"I1": _UNTRACKED_SKILL, "I7": _DEPLOY_CHANGED},
    ),
    FaultRow(
        "mcp-config-write",
        "install",
        _obstruct_with_dir(".mcp.json"),
        _remove_project_path(".mcp.json"),
        announce="MCP configuration failed for selected runtime(s): p-exec-mcp",
        changed=("project:apm.lock.yaml", "project:.github/hooks*"),
        exec_given=True,
        known={"I7": _LOCK_CHANGED},
    ),
    FaultRow(
        "sigterm",
        _INSTALL_CHILD,
        _arm_sigterm,
        _heal_sigterm,
        announce="Install interrupted",
    ),
)


def _given(scenario: Scenario, row: FaultRow) -> None:
    scenario.run("install p-skill")
    if row.exec_given:
        scenario.run("install p-exec")
        scenario.run(["approve", scenario.specs["p-exec"]])


@pytest.mark.parametrize("row", ROWS, ids=[row.id for row in ROWS])
def test_failed_install_preserves_state_and_recovers(lifecycle_world, row: FaultRow) -> None:
    scenario = Scenario(lifecycle_world, ["p-skill", "p-child", "p-exec"])
    _given(scenario, row)

    # WHEN: the fault is armed at the commit point.
    row.arm(scenario)
    failed = scenario.run(row.when, exit=1, known=row.known)
    # THEN: nothing durable changed beyond the row's declared residue.
    assert_changes(failed.changed, row.changed, label=f"{row.id}: failed install")
    assert row.announce in failed.output, f"I7: {row.id} not announced: {failed.output}"
    assert failed.after.state.manifest_bytes == failed.before.state.manifest_bytes

    # HEAL: the same command converges, repeats as a byte no-op, and audit agrees.
    row.heal(scenario)
    scenario.run(row.when)
    scenario.run("install")
    scenario.pair()
    scenario.assert_deployed()
    assert not (lifecycle_world.project / "apm_modules" / ".apm-resolution-staging").exists()
    scenario.finish()
