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


def _hide_characters(world: _World) -> None:
    hidden = world.consumer.root / ".github" / "instructions" / "hidden.instructions.md"
    hidden.parent.mkdir(parents=True, exist_ok=True)
    hidden.write_text("---\napplyTo: '**'\n---\nhello \u202e world\n", encoding="utf-8")


def _manifest_created(world: _World, result) -> None:
    _expect_ok(world, result)
    assert world.consumer.manifest_path.is_file()


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
    _TextProof(
        "hidden-characters-found",
        _bare_world,
        _hide_characters,
        lambda _world: ("audit",),
        1,
        _expect_ok,
    ),
    _TextProof(
        "hidden-characters-previewed",
        _bare_world,
        _hide_characters,
        lambda _world: ("audit", "--strip", "--dry-run"),
        0,
        _expect_ok,
    ),
    _TextProof(
        "project-manifest-bootstrap",
        _bare_world,
        lambda world: (world.consumer.root / ".claude").mkdir(),
        lambda _world: ("install",),
        1,
        _manifest_created,
        values=lambda world: {"<org/repo>": _slug(world.source)},
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
#
# Marketplaces are local directories, so the real source parser, probe, fetch
# and registry run hermetically against the isolated user root.

_W2_MARKETPLACE = "w2-acme"


def _w2_marketplace_root(world: _World) -> Path:
    return world.scenario.isolated.package_root / _W2_MARKETPLACE


def _w2_create_marketplace(world: _World) -> None:
    root = _w2_marketplace_root(world)
    root.mkdir()
    catalog = {
        "name": _W2_MARKETPLACE,
        "plugins": [{"name": "w2-plugin", "description": "demo plugin", "source": "./p"}],
    }
    (root / "marketplace.json").write_text(json.dumps(catalog), encoding="utf-8")


def _w2_marketplaces_yaml(world: _World) -> str:
    source = _w2_marketplace_root(world).as_posix()
    return f"marketplaces:\n  - name: {_W2_MARKETPLACE}\n    source: {source}\n"


def _w2_declare_in_user_manifest(world: _World) -> None:
    manifest = world.scenario.isolated.config_root / "apm.yml"
    manifest.write_text(
        f"name: me\nversion: 1.0.0\n{_w2_marketplaces_yaml(world)}", encoding="utf-8"
    )


def _w2_declare_in_project(world: _World) -> None:
    _w2_create_marketplace(world)
    world.consumer.manifest_path.write_text(
        f"name: proj\nversion: 1.0.0\n{_w2_marketplaces_yaml(world)}", encoding="utf-8"
    )


def _w2_move_to_user_manifest(world: _World) -> None:
    world.consumer.manifest_path.write_text("name: proj\nversion: 1.0.0\n", encoding="utf-8")
    _w2_declare_in_user_manifest(world)


def _w2_registered(world: _World, result) -> None:
    _expect_ok(world, result)
    registry = world.scenario.isolated.config_root / "marketplaces.json"
    names = {entry["name"] for entry in json.loads(registry.read_text("utf-8"))["marketplaces"]}
    assert _W2_MARKETPLACE in names, names


W2_MARKETPLACE_PROOFS: tuple[_TextProof, ...] = (
    _TextProof(
        "marketplace-reconcile-failed",
        _bare_world,
        _w2_declare_in_user_manifest,
        lambda _world: ("install", "-g"),
        1,
        _w2_registered,
        before=_w2_create_marketplace,
    ),
    _TextProof(
        "marketplaces-outside-user-manifest",
        _bare_world,
        _w2_declare_in_project,
        lambda _world: ("install",),
        1,
        _w2_registered,
        before=_w2_move_to_user_manifest,
    ),
)


@pytest.mark.parametrize("proof", W2_MARKETPLACE_PROOFS, ids=lambda proof: proof.remediation_id)
def test_w2_marketplace_remediation_clears_the_reported_state(
    tmp_path: Path, apm_binary_path: Path, proof: _TextProof
) -> None:
    test_command_remediation_clears_the_reported_state(tmp_path, apm_binary_path, proof)


# -- end W2 marketplace proofs --
# -- W2 bundle proofs --
# 'apm pack' refuses an installed project whose lockfile or attested files are
# gone or edited; the advised install restores them and the pack succeeds.


def _tamper_deployed_file(world: _World) -> None:
    deployed = world.consumer.root / _INSTRUCTION
    deployed.write_text(deployed.read_text(encoding="utf-8") + "\ntampered\n", encoding="utf-8")


def _pack(_world: _World) -> tuple[str, ...]:
    return ("pack",)


def _pack_apm_format(_world: _World) -> tuple[str, ...]:
    # The default plugin format packs a lockfile-free project; the APM bundle
    # layout requires apm.lock.yaml.
    return ("pack", "--format", "apm")


W2_BUNDLE_PROOFS: tuple[_TextProof, ...] = (
    _TextProof(
        "pack-lockfile-missing",
        _installed_world,
        _unlink("apm.lock.yaml"),
        _pack_apm_format,
        1,
        _expect_ok,
    ),
    _TextProof(
        "pack-installed-files-missing",
        _installed_world,
        _unlink(_INSTRUCTION),
        _pack,
        1,
        _expect_ok,
    ),
    _TextProof(
        "pack-installed-file-tampered",
        _installed_world,
        _tamper_deployed_file,
        _pack,
        1,
        _expect_ok,
        before=_unlink(_INSTRUCTION),
    ),
)


@pytest.mark.parametrize("proof", W2_BUNDLE_PROOFS, ids=lambda proof: proof.remediation_id)
def test_w2_bundle_remediation_clears_the_reported_state(
    tmp_path: Path, apm_binary_path: Path, proof: _TextProof
) -> None:
    test_command_remediation_clears_the_reported_state(tmp_path, apm_binary_path, proof)


# -- end W2 bundle proofs --
# -- end W2 executed proofs ---------------------------------------------------


# -- W3 executed proofs (modules outside commands/ and the W2 areas) ----------
# -- W3 deps-compilation proofs --
#
# Compilation reports a hand-authored root context file or an orphaned
# AGENTS.md as a warning; the same text-proof loop runs the printed command.

_LOCAL_INSTRUCTION = "---\napplyTo: '**'\ndescription: Local advice\n---\n# local\n"


def _compile_world(tmp_path: Path, apm_binary_path: Path) -> _World:
    scenario = _new_scenario(tmp_path / "remediation", apm_binary_path)
    source = _publish(scenario, "advice-kit", instruction="advice")
    consumer = scenario.consumers.create("compile-consumer")
    scenario.consumers.add_instruction(consumer, "local", _LOCAL_INSTRUCTION)
    return _World(scenario=scenario, source=source, consumer=consumer)


def _hand_author_agents_md(world: _World) -> None:
    (world.consumer.root / "AGENTS.md").write_text("# mine\n", encoding="ascii")


def _orphan_agents_md(world: _World) -> None:
    from apm_cli.compilation.constants import DISTRIBUTED_AGENTS_MD_GENERATED_MARKER

    orphan = world.consumer.root / "docs" / "AGENTS.md"
    orphan.parent.mkdir()
    orphan.write_text(f"# AGENTS.md\n{DISTRIBUTED_AGENTS_MD_GENERATED_MARKER}\n", encoding="ascii")


def _agents_md_generated(world: _World, result) -> None:
    from apm_cli.compilation.constants import DISTRIBUTED_AGENTS_MD_GENERATED_MARKER

    _expect_ok(world, result)
    text = (world.consumer.root / "AGENTS.md").read_text(encoding="utf-8")
    assert DISTRIBUTED_AGENTS_MD_GENERATED_MARKER in text, text


def _orphan_removed(world: _World, result) -> None:
    _expect_ok(world, result)
    assert not (world.consumer.root / "docs" / "AGENTS.md").exists()


W3_DEPS_COMPILATION_PROOFS: tuple[_TextProof, ...] = (
    _TextProof(
        "root-context-hand-authored",
        _compile_world,
        _hand_author_agents_md,
        lambda _world: ("compile",),
        0,
        _agents_md_generated,
        before=_unlink("AGENTS.md"),
    ),
    _TextProof(
        "orphaned-agents-md",
        _compile_world,
        _orphan_agents_md,
        lambda _world: ("compile",),
        0,
        _orphan_removed,
    ),
)


@pytest.mark.parametrize(
    "proof", W3_DEPS_COMPILATION_PROOFS, ids=lambda proof: proof.remediation_id
)
def test_w3_deps_compilation_remediation_clears_the_reported_state(
    tmp_path: Path, apm_binary_path: Path, proof: _TextProof
) -> None:
    test_command_remediation_clears_the_reported_state(tmp_path, apm_binary_path, proof)


# -- end W3 deps-compilation proofs --
# -- W3 plugins-runtime proofs --
#
# A user-owned harness config APM merges into (JetBrains Copilot MCP config,
# Copilot plugin settings) turns unreadable; the install that reports it names
# the manual fix and the command. Deleting the file is the fix both name.

_FIXTURE_MCP = {
    "name": "fixture-mcp",
    "registry": False,
    "transport": "stdio",
    "command": "printf",
    "args": ["fixture"],
}
_COPILOT_SETTINGS = ".github/copilot/settings.local.json"
# 'intellij' is not an apm.yml targets: value; only --target selects it.
_INTELLIJ_INSTALL_ARGS = (*_INSTALL_ARGS, "--target", "intellij")


def _intellij_dir(scenario: _Scenario) -> Path:
    environment = scenario.isolated.process_environment
    return Path(environment["XDG_CONFIG_HOME"]) / "github-copilot" / "intellij"


def _intellij_config(world: _World) -> Path:
    return _intellij_dir(world.scenario) / "mcp.json"


def _w3_installed_world(
    tmp_path: Path,
    apm_binary_path: Path,
    publish,
    *,
    install_args: tuple[str, ...] = _INSTALL_ARGS,
    **consumer: object,
) -> _World:
    scenario = _new_scenario(tmp_path / "remediation", apm_binary_path)
    source = publish(scenario)
    created = scenario.consumers.create(
        "w3-consumer", dependencies=(source.dependency,), **consumer
    )
    world = _World(scenario=scenario, source=source, consumer=created)
    _run_success(
        scenario,
        created,
        install_args,
        environment=source.environment,
        scenario_id="remediation-w3-install",
    )
    return world


def _intellij_world(tmp_path: Path, apm_binary_path: Path) -> _World:
    world = _w3_installed_world(
        tmp_path,
        apm_binary_path,
        lambda scenario: _publish(scenario, "advice-kit", instruction="advice"),
        install_args=_INTELLIJ_INSTALL_ARGS,
        mcp_dependencies=(_FIXTURE_MCP,),
    )
    assert "fixture-mcp" in _intellij_config(world).read_text(encoding="utf-8")
    return world


def _agent_plugin_world(tmp_path: Path, apm_binary_path: Path) -> _World:
    from tests.integration.test_required_lifecycle_state_machine import _publish_agent_plugin

    world = _w3_installed_world(
        tmp_path,
        apm_binary_path,
        lambda scenario: _publish_agent_plugin(scenario, "native-kit", skill="native"),
        targets=("copilot",),
    )
    assert "native-kit" in (world.consumer.root / _COPILOT_SETTINGS).read_text(encoding="utf-8")
    return world


def _malform_intellij_config(world: _World) -> None:
    _intellij_config(world).write_text("{malformed\n", encoding="ascii")


def _intellij_reconciled(world: _World, result) -> None:
    _expect_ok(world, result)
    servers = json.loads(_intellij_config(world).read_text(encoding="utf-8"))["servers"]
    assert "fixture-mcp" in servers, servers


def _copilot_registered(world: _World, result) -> None:
    _expect_ok(world, result)
    settings = (world.consumer.root / _COPILOT_SETTINGS).read_text(encoding="utf-8")
    assert isinstance(json.loads(settings), dict)
    assert "native-kit" in settings, settings


W3_PLUGINS_RUNTIME_PROOFS: tuple[_TextProof, ...] = (
    _TextProof(
        "jetbrains-mcp-config-unusable",
        _intellij_world,
        _malform_intellij_config,
        lambda _world: _INTELLIJ_INSTALL_ARGS,
        1,
        _intellij_reconciled,
        before=lambda world: _intellij_config(world).unlink(),
    ),
    _TextProof(
        "copilot-settings-invalid",
        _agent_plugin_world,
        lambda world: (world.consumer.root / _COPILOT_SETTINGS).write_text(
            "{malformed\n", encoding="ascii"
        ),
        lambda _world: _INSTALL_ARGS,
        1,
        _copilot_registered,
        before=_unlink(_COPILOT_SETTINGS),
    ),
)


@pytest.mark.parametrize("proof", W3_PLUGINS_RUNTIME_PROOFS, ids=lambda proof: proof.remediation_id)
def test_w3_plugins_runtime_remediation_clears_the_reported_state(
    tmp_path: Path, apm_binary_path: Path, proof: _TextProof
) -> None:
    test_command_remediation_clears_the_reported_state(tmp_path, apm_binary_path, proof)


# -- end W3 plugins-runtime proofs --
# -- W3 core-models proofs --

_GHOST_OWNER = "removed/ghost-kit"


def _colliding_world(tmp_path: Path, apm_binary_path: Path) -> _World:
    scenario = _new_scenario(tmp_path / "remediation", apm_binary_path)
    source = _publish(scenario, "advice-kit", instruction="advice")
    consumer = scenario.consumers.create(
        "advice-consumer", dependencies=(source.dependency,), targets=("copilot",)
    )
    authored = consumer.root / _INSTRUCTION
    authored.parent.mkdir(parents=True, exist_ok=True)
    authored.write_text("USER authored instructions\n", encoding="ascii")
    return _World(scenario=scenario, source=source, consumer=consumer)


def _ghost_deployment_owner(world: _World) -> None:
    from dataclasses import replace

    from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
    from apm_cli.core.deployment_state import DeploymentLocator, DeploymentRecord, LocatorKind
    from apm_cli.deps.lockfile import LockFile

    path = world.consumer.root / "apm.lock.yaml"
    lockfile = LockFile.read(path)
    ledger = DeploymentLedgerCodec.from_lockfile(lockfile)
    locator = DeploymentLocator(
        kind=LocatorKind.PROJECT_RELATIVE,
        target="copilot",
        value=".github/instructions/ghost.instructions.md",
        runtime=None,
        scope="project",
    )
    ghost = DeploymentRecord(
        locator=locator,
        owners=(_GHOST_OWNER,),
        active_owner=_GHOST_OWNER,
        content_hash=f"sha256:{'a' * 64}",
    )
    records = {**ledger.records, locator.key: ghost}
    DeploymentLedgerCodec.apply_to_lockfile(replace(ledger, records=records), lockfile)
    lockfile.write(path)


def _user_file_overwritten(world: _World, result) -> None:
    _expect_ok(world, result)
    text = (world.consumer.root / _INSTRUCTION).read_text(encoding="utf-8")
    assert "USER authored" not in text, text


W3_CORE_MODELS_PROOFS: tuple[_TextProof, ...] = (
    _TextProof(
        "local-files-collide",
        _colliding_world,
        lambda _world: None,
        lambda _world: _INSTALL_ARGS,
        0,
        _user_file_overwritten,
    ),
    _TextProof(
        "deployment-owners-invalid",
        _installed_world,
        _ghost_deployment_owner,
        lambda _world: ("audit",),
        1,
        _expect_ok,
    ),
)


@pytest.mark.parametrize("proof", W3_CORE_MODELS_PROOFS, ids=lambda proof: proof.remediation_id)
def test_w3_core_models_remediation_clears_the_reported_state(
    tmp_path: Path, apm_binary_path: Path, proof: _TextProof
) -> None:
    test_command_remediation_clears_the_reported_state(tmp_path, apm_binary_path, proof)


# -- end W3 core-models proofs --
# -- end W3 executed proofs ---------------------------------------------------


# -- W4 executed proofs (install/, policy/) -----------------------------------
# -- W4 frozen-policy proofs --
# -- end W4 frozen-policy proofs --
# -- W4 drift-trust proofs --
#
# Drift replay, native plugin cache admission, agent-source planning and the
# hook gate report a state on an installed project; the advised command (after
# the manual step the advice names) clears it.

_W4_AGENT = ".claude/agents/linked.md"


def _w4_symlinked_agent_world(tmp_path: Path, apm_binary_path: Path) -> _World:
    scenario = _new_scenario(tmp_path / "remediation", apm_binary_path)
    source = _publish(scenario, "advice-kit", instruction="advice")
    consumer = scenario.consumers.create("agent-consumer", targets=("claude",))
    real = consumer.root / "agents"
    real.mkdir()
    (real / "linked.agent.md").write_text(
        "---\nname: linked\ndescription: A linked agent.\n---\n\nBody.\n", encoding="ascii"
    )
    (consumer.root / ".apm").mkdir(exist_ok=True)
    (consumer.root / ".apm" / "agents").symlink_to("../agents", target_is_directory=True)
    return _World(scenario=scenario, source=source, consumer=consumer)


def _w4_materialize_agents(world: _World) -> None:
    import shutil

    link = world.consumer.root / ".apm" / "agents"
    link.unlink()
    shutil.copytree(world.consumer.root / "agents", link)


def _w4_agent_deployed(world: _World, result) -> None:
    _expect_ok(world, result)
    assert (world.consumer.root / _W4_AGENT).is_file(), _result_evidence(result)


def _w4_tamper_native_cache(world: _World) -> None:
    skills = sorted((world.consumer.root / "apm_modules").rglob("skills/native/SKILL.md"))
    assert skills, "native plugin cache holds no skill"
    for skill in skills:
        skill.write_text(skill.read_text(encoding="utf-8") + "\ntampered\n", encoding="utf-8")


def _w4_audit_ci(_world: _World) -> tuple[str, ...]:
    return ("audit", "--no-policy", "--ci")


def _w4_hook_slug() -> str:
    from tests.integration.test_required_lifecycle_state_machine import _OWNER

    return f"{_OWNER}/{_HOOKED}"


W4_DRIFT_TRUST_PROOFS: tuple[_TextProof | _BoundProof, ...] = (
    _TextProof(
        "replay-cache-missing",
        _installed_world,
        _remove_modules,
        lambda _world: ("export-patch", "--dry-run"),
        1,
        _expect_ok,
    ),
    _TextProof(
        "agent-source-symlinked",
        _w4_symlinked_agent_world,
        lambda _world: None,
        lambda _world: _INSTALL_ARGS,
        0,
        _w4_agent_deployed,
        before=_w4_materialize_agents,
    ),
    _TextProof(
        "native-plugin-cache-differs",
        _agent_plugin_world,
        _w4_tamper_native_cache,
        _w4_audit_ci,
        1,
        _expect_ok,
    ),
    _BoundProof(
        "hooks-unapproved",
        lambda tmp, binary: _hooked_world(tmp, binary, approved=False),
        lambda _world: None,
        _INSTALL_ARGS,
        0,
        values=(("package", _w4_hook_slug()),),
    ),
)


@pytest.mark.parametrize("proof", W4_DRIFT_TRUST_PROOFS, ids=lambda proof: proof.remediation_id)
def test_w4_drift_trust_remediation_clears_the_reported_state(
    tmp_path: Path, apm_binary_path: Path, proof: _TextProof | _BoundProof
) -> None:
    if isinstance(proof, _BoundProof):
        test_w2_integration_remediation_clears_the_reported_state(tmp_path, apm_binary_path, proof)
    else:
        test_command_remediation_clears_the_reported_state(tmp_path, apm_binary_path, proof)


# -- end W4 drift-trust proofs --
# -- W4 phases-targets proofs --
# -- end W4 phases-targets proofs --
# -- end W4 executed proofs ---------------------------------------------------
