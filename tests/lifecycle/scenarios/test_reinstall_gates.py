"""A re-install over unchanged inputs still enforces gates that moved elsewhere.

PROMISE: P-INV-I4 -- gates are monotone: a stricter org policy, or an approval
withdrawn from the approval store, never leaves an executable deployed; the next
``apm install`` retires it and says so (source: docs/src/content/docs/reference/cli/install.md,
policy/install_preflight.py, install/exec_gate.py).

Both cells run a SECOND ``apm install`` whose ``apm.yml`` and ``apm.lock.yaml`` are
byte-identical to the end of the previous run: the change lives outside the
project files (the org policy behind the git remote, or the user approval store).
A no-op or ``--frozen`` fast path keyed on those two files must not skip this
enforcement; the byte assertions pin that precondition.
"""

from __future__ import annotations

import json
import re

import pytest
import yaml

from tests.lifecycle import driver
from tests.lifecycle.world import World, use_policy

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
    pytest.mark.promise("P-INV-I4"),
]

_HOOK = ".github/hooks/p-exec-session.json"


def _inputs(world: World) -> dict[str, bytes]:
    """The project files a fast path could key on."""
    return {name: (world.project / name).read_bytes() for name in ("apm.yml", "apm.lock.yaml")}


def _run(world: World, *args: str, exit: int = 0):
    result = driver.run(world, args)
    output = f"{result.stdout}\n{result.stderr}"
    assert result.returncode == exit, f"apm {' '.join(args)} exited {result.returncode}\n{output}"
    return result, output


def _declare_p_exec(world: World) -> None:
    manifest = world.project / "apm.yml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data["dependencies"] = {"apm": [world.packages["p-exec"].spec]}
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")


def test_reinstall_enforces_changed_org_policy(lifecycle_world: World) -> None:
    world = lifecycle_world
    use_policy(world, "allow")
    _declare_p_exec(world)
    _run(world, "install")
    _run(world, "approve", "p-exec")
    _run(world, "install")
    hook = world.project / _HOOK
    assert hook.is_file(), "the approved hook deploys under the allow policy"
    converged = _inputs(world)

    use_policy(world, "exec-deny-all")  # only the org policy behind the git remote moves
    assert _inputs(world) == converged, "the policy change must not touch apm.yml or the lock"
    # Before the re-install the deployed hook violates the tightened policy (I3: audit
    # --ci fails where install must act).
    audit, audit_output = _run(world, "audit", "--ci", exit=1)
    assert audit.returncode == 1, audit_output
    assert _inputs(world) == converged, "audit must not write apm.yml or the lock"
    _, output = _run(world, "install")
    assert not hook.exists(), f"a tightened org policy leaves the hook deployed\n{output}"
    # The package-specific denial, not the policy banner (which names the deny policy).
    assert re.search(r"p-exec: hooks skipped \(denied\)\. Run 'apm policy explain ", output), (
        f"the hook denial is not announced for p-exec\n{output}"
    )
    # Converged under the new policy: audit --ci agrees with install (I3).
    _run(world, "audit", "--ci")


def test_reinstall_retires_executable_after_out_of_band_approval_removal(
    lifecycle_world: World,
) -> None:
    world = lifecycle_world
    use_policy(world, "allow")
    _declare_p_exec(world)
    _run(world, "install")
    # A personal grant lives in the user approval store, never in apm.yml or the lock.
    _run(world, "approve", "--user", "p-exec")
    _run(world, "install")
    hook = world.project / _HOOK
    assert hook.is_file(), "the user-approved hook deploys"
    converged = _inputs(world)

    config = world.apm_home / "config.json"
    data = json.loads(config.read_text(encoding="utf-8"))
    assert data.get("executables", {}).get("allow"), data
    data["executables"]["allow"] = {}
    config.write_text(json.dumps(data, indent=2), encoding="utf-8")
    assert _inputs(world) == converged, "the store change must not touch apm.yml or the lock"
    _, output = _run(world, "install")
    assert not hook.exists(), f"a withdrawn approval leaves the hook deployed\n{output}"
    assert re.search(r"p-exec: hooks skipped \(not approved in executables\.allow\)", output), (
        f"the parked hook is not announced for p-exec\n{output}"
    )
