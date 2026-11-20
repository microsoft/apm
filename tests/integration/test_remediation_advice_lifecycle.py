"""Every structured remediation clears the state that reported it (#4090).

Each proof induces one reported state on an installed project, reads the
structured remediation the installed CLI attached to that report, runs exactly
the advised command through the same binary, and observes that the reported
state is gone. The advice under test is what the user sees, never a copy of
the owner's catalog.
"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.integration.test_required_lifecycle_state_machine import (
    _INSTALL_ARGS,
    _new_scenario,
    _publish,
    _PublishedPackage,
    _result_evidence,
    _run_success,
    _Scenario,
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


# -- commands/** advice (W1) ---------------------------------------------------
#
# A command reports a state in text and names the catalog remediation that
# clears it. Each proof induces the state, reads the advised command line from
# what the installed CLI printed, takes the manual step the advice names, runs
# exactly the printed command, and re-runs the reporting command to observe the
# state is gone.

_HOOKED = "hook-kit"


def _normalized(result) -> str:
    return " ".join((result.stdout + "\n" + result.stderr).split())


def _printed_command(result, remediation_id: str) -> tuple[str, ...]:
    from apm_cli.core.remediation import catalog

    promise = catalog()[remediation_id].fixes_promise
    text = _normalized(result)
    match = re.search(rf"'(apm [^']+)' to {re.escape(promise)}", text)
    assert match, f"no advice for {remediation_id!r} in output:\n{_result_evidence(result)}"
    assert match.group(1) == catalog()[remediation_id].command, match.group(1)
    return tuple(shlex.split(match.group(1)))


@dataclass(frozen=True)
class _TextProof:
    """One reported state, the command that reports it, and how it clears."""

    remediation_id: str
    world: Callable[[Path, Path], _World]
    induce: Callable[[_World], None]
    probe: Callable[[_World], tuple[str, ...]]
    reported_exit: int
    cleared: Callable[[_World, object], None]
    before: Callable[[_World], None] = lambda _world: None
    values: Callable[[_World], dict[str, str]] = lambda _world: {}
    # The command that observes the state afterwards; None when the reporting
    # command is itself the action (approve, deny) and ``cleared`` observes it.
    reprobe: Callable[[_World], tuple[str, ...]] | None = None


def _run(world: _World, args: tuple[str, ...], scenario_id: str):
    return world.scenario.runner.run(
        args, scenario_id=scenario_id, cwd=world.consumer.root, env=world.source.environment
    )


def _bare_world(tmp_path: Path, apm_binary_path: Path) -> _World:
    scenario = _new_scenario(tmp_path / "remediation", apm_binary_path)
    source = _publish(scenario, "advice-kit", instruction="advice")
    consumer = scenario.consumers.create("advice-consumer", dependencies=(source.dependency,))
    consumer.manifest_path.unlink()
    return _World(scenario=scenario, source=source, consumer=consumer)


def _hooked_world(tmp_path: Path, apm_binary_path: Path, *, approved: bool) -> _World:
    scenario = _new_scenario(tmp_path / "remediation", apm_binary_path)
    source = _publish(scenario, _HOOKED, instruction="hooked", hook_command="echo hooked")
    consumer = scenario.consumers.create(
        "hook-consumer", dependencies=(source.dependency,), targets=("claude",)
    )
    # Any executables block turns the gate on; only an approved package deploys hooks.
    manifest = load_yaml(consumer.manifest_path)
    allow = {_slug(source): {"hooks": True}} if approved else {}
    manifest["executables"] = {"allow": allow}
    dump_yaml(manifest, consumer.manifest_path)
    world = _World(scenario=scenario, source=source, consumer=consumer)
    _run_success(
        scenario,
        consumer,
        _INSTALL_ARGS,
        environment=source.environment,
        scenario_id="remediation-hooked-install",
    )
    assert _hooks_deployed(world) is approved
    return world


def _slug(source: _PublishedPackage) -> str:
    return source.remote_url.removeprefix("https://github.com/")


def _hooks_deployed(world: _World) -> bool:
    settings = world.consumer.root / ".claude" / "settings.json"
    return settings.is_file() and "PreToolUse" in settings.read_text(encoding="utf-8")


def _expect_ok(world: _World, result) -> None:
    assert result.returncode == 0, _result_evidence(result)


def _unlink(relative: str) -> Callable[[_World], None]:
    return lambda world: (world.consumer.root / relative).unlink()


def _corrupt_lockfile(world: _World) -> None:
    (world.consumer.root / "apm.lock.yaml").write_text("dependencies: [\n", encoding="ascii")


def _remove_modules(world: _World) -> None:
    import shutil

    shutil.rmtree(world.consumer.root / "apm_modules")


def _hooks_present(world: _World, _result) -> None:
    assert _hooks_deployed(world)


def _hooks_absent(world: _World, _result) -> None:
    assert not _hooks_deployed(world)


def _no_orphans(world: _World, result) -> None:
    _expect_ok(world, result)
    assert "orphaned" not in _normalized(result).lower(), _result_evidence(result)


def _stray_package(world: _World) -> None:
    stray = world.consumer.root / "apm_modules" / "stray-org" / "stray-kit"
    stray.mkdir(parents=True)
    (stray / "apm.yml").write_text("name: stray-kit\nversion: 1.0.0\n", encoding="ascii")


def _modify_installed_hook(world: _World) -> None:
    hooks = list((world.consumer.root / "apm_modules").rglob("hooks/*.json"))
    assert hooks, "hooked package has no installed hook file"
    hooks[0].write_text(hooks[0].read_text(encoding="utf-8") + "\n", encoding="utf-8")


def _status(_world: _World) -> tuple[str, ...]:
    return ("approve", "--list")


TEXT_PROOFS: tuple[_TextProof, ...] = (
    _TextProof(
        "project-manifest-missing",
        _bare_world,
        lambda _world: None,
        lambda _world: ("prune",),
        1,
        _expect_ok,
    ),
    _TextProof(
        "dependencies-not-installed",
        _installed_world,
        _remove_modules,
        lambda _world: ("deps", "info", "advice-kit"),
        1,
        _expect_ok,
    ),
    _TextProof(
        "orphaned-packages-prune",
        _installed_world,
        _stray_package,
        lambda _world: ("deps", "list"),
        0,
        _no_orphans,
    ),
    _TextProof(
        "lockfile-missing-lock",
        _installed_world,
        _unlink("apm.lock.yaml"),
        lambda _world: ("lock", "export"),
        1,
        _expect_ok,
    ),
    _TextProof(
        "lockfile-unreadable",
        _installed_world,
        _corrupt_lockfile,
        lambda world: ("export-patch", "--dry-run"),
        1,
        _expect_ok,
        before=_unlink("apm.lock.yaml"),
    ),
    _TextProof(
        "executables-denied",
        lambda tmp, binary: _hooked_world(tmp, binary, approved=True),
        lambda _world: None,
        lambda world: ("deny", _slug(world.source)),
        0,
        _hooks_absent,
        reprobe=_status,
    ),
    _TextProof(
        "executables-approved",
        lambda tmp, binary: _hooked_world(tmp, binary, approved=False),
        lambda _world: None,
        lambda world: ("approve", _slug(world.source)),
        0,
        _hooks_present,
        reprobe=_status,
    ),
    _TextProof(
        "executables-unapproved",
        lambda tmp, binary: _hooked_world(tmp, binary, approved=False),
        lambda _world: None,
        lambda _world: ("approve", "--pending"),
        0,
        lambda world, result: _expect_ok(world, result),
        values=lambda world: {"<package>": _slug(world.source)},
    ),
    _TextProof(
        "installed-files-differ",
        lambda tmp, binary: _hooked_world(tmp, binary, approved=False),
        _modify_installed_hook,
        lambda world: ("approve", _slug(world.source)),
        1,
        _expect_ok,
    ),
)


def _reprobe_args(proof: _TextProof, world: _World) -> tuple[str, ...]:
    return (proof.reprobe or proof.probe)(world)


@pytest.mark.parametrize("proof", TEXT_PROOFS, ids=lambda proof: proof.remediation_id)
def test_command_remediation_clears_the_reported_state(
    tmp_path: Path, apm_binary_path: Path, proof: _TextProof
) -> None:
    world = proof.world(tmp_path, apm_binary_path)
    proof.induce(world)

    reported = _run(world, proof.probe(world), "remediation-reported")
    assert reported.returncode == proof.reported_exit, _result_evidence(reported)
    command = _printed_command(reported, proof.remediation_id)
    assert command[0] == "apm"
    values = proof.values(world)
    argv = tuple(values.get(token, token) for token in command[1:])
    assert not any(token.startswith("<") for token in argv), argv

    proof.before(world)
    advised = _run(world, argv, f"remediation-run-{proof.remediation_id}")
    assert advised.returncode == 0, _result_evidence(advised)

    from apm_cli.core.remediation import catalog

    again = _run(world, _reprobe_args(proof, world), "remediation-cleared")
    proof.cleared(world, again)
    promise = catalog()[proof.remediation_id].fixes_promise
    advice = f"'{' '.join(command)}' to {promise}"
    assert advice not in _normalized(again), _result_evidence(again)


# -- W2 executed proofs (integration/, marketplace/, bundle/) -----------------
# -- W2 integration proofs --
#
# Advice printed by src/apm_cli/integration. The printed command may carry a
# bound value (a flag name), so each proof binds the catalog entry, finds its
# rendered "'command' to promise" in what the installed CLI printed, takes the
# manual step the advice names, runs that command, and re-runs the reporting
# command to observe the advice is gone.


@dataclass(frozen=True)
class _BoundProof:
    remediation_id: str
    world: Callable[[Path, Path], _World]
    induce: Callable[[_World], None]
    probe: tuple[str, ...]
    reported_exit: int
    before: Callable[[_World], None] = lambda _world: None
    values: tuple[tuple[str, str], ...] = ()


def _grok_world(tmp_path: Path, apm_binary_path: Path) -> _World:
    scenario = _new_scenario(tmp_path / "remediation", apm_binary_path)
    source = _publish(scenario, "grok-kit", skill="alpha")
    consumer = scenario.consumers.create("grok-consumer", dependencies=(source.dependency,))
    for step, args in enumerate(
        (
            ("experimental", "enable", "grok-cloud"),
            (*_INSTALL_ARGS, "--target", "grok-cloud"),
            ("experimental", "disable", "grok-cloud"),
        )
    ):
        _run_success(
            scenario,
            consumer,
            args,
            environment=source.environment,
            scenario_id=f"remediation-grok-{step}",
        )
    return _World(scenario=scenario, source=source, consumer=consumer)


_HOOK_SIDECAR = ".claude/apm-hooks.json"


def _malformed_hook_sidecar(world: _World) -> None:
    sidecar = world.consumer.root / _HOOK_SIDECAR
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text("{", encoding="ascii")


def _skill_world(tmp_path: Path, apm_binary_path: Path) -> _World:
    scenario = _new_scenario(tmp_path / "remediation", apm_binary_path)
    source = _publish(scenario, "skill-kit", skill="alpha")
    consumer = scenario.consumers.create(
        "skill-consumer",
        dependencies=({**source.dependency, "skills": ["missing"]},),
        targets=("copilot",),
    )
    return _World(scenario=scenario, source=source, consumer=consumer)


def _select_skill(world: _World) -> None:
    manifest = load_yaml(world.consumer.manifest_path)
    manifest["dependencies"]["apm"][0]["skills"] = ["alpha"]
    dump_yaml(manifest, world.consumer.manifest_path)


W2_INTEGRATION_PROOFS: tuple[_BoundProof, ...] = (
    _BoundProof(
        "audit-target-flag-disabled",
        _grok_world,
        lambda _world: None,
        ("audit", "--ci", "--no-policy"),
        3,
        values=(("flag", "grok-cloud"),),
    ),
    _BoundProof(
        "hook-sidecar-unreadable",
        _installed_world,
        _malformed_hook_sidecar,
        _INSTALL_ARGS,
        0,
        before=_unlink(_HOOK_SIDECAR),
    ),
    _BoundProof(
        "skill-selection-unmatched",
        _skill_world,
        lambda _world: None,
        _INSTALL_ARGS,
        1,
        before=_select_skill,
    ),
)


@pytest.mark.parametrize("proof", W2_INTEGRATION_PROOFS, ids=lambda proof: proof.remediation_id)
def test_w2_integration_remediation_clears_the_reported_state(
    tmp_path: Path, apm_binary_path: Path, proof: _BoundProof
) -> None:
    from apm_cli.core.remediation import catalog

    world = proof.world(tmp_path, apm_binary_path)
    proof.induce(world)
    entry = catalog()[proof.remediation_id].bind(**dict(proof.values))

    reported = _run(world, proof.probe, "remediation-reported")
    assert reported.returncode == proof.reported_exit, _result_evidence(reported)
    assert entry.option() in _normalized(reported), _result_evidence(reported)
    argv = entry.argv()
    assert argv[0] == "apm" and not any(token.startswith("<") for token in argv), argv

    proof.before(world)
    advised = _run(world, argv[1:], f"remediation-run-{proof.remediation_id}")
    assert advised.returncode == 0, _result_evidence(advised)

    again = _run(world, proof.probe, "remediation-cleared")
    assert again.returncode == 0, _result_evidence(again)
    assert entry.option() not in _normalized(again), _result_evidence(again)


# -- end W2 integration proofs --
# -- W2 marketplace proofs --
# -- end W2 marketplace proofs --
# -- W2 bundle proofs --
# -- end W2 bundle proofs --
# -- end W2 executed proofs ---------------------------------------------------
