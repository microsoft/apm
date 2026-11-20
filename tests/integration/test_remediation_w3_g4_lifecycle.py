"""W3 G4 catalog remediations clear the state that reported them (#4090).

Each proof induces one reported state on a hermetic project, reads the advice
the installed CLI printed, takes the manual step the advice names, runs exactly
the printed command, and re-runs a reporting command to observe the state is
gone. Shares the harness of ``test_remediation_advice_lifecycle.py``.
"""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path

import pytest

from apm_cli.core.remediation import catalog
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.integration.test_remediation_advice_lifecycle import (
    _INSTRUCTION,
    _corrupt_lockfile,
    _expect_ok,
    _hooked_world,
    _installed_world,
    _normalized,
    _reprobe_args,
    _run,
    _slug,
    _TextProof,
    _World,
)
from tests.integration.test_required_lifecycle_state_machine import (
    _INSTALL_ARGS,
    _new_scenario,
    _publish,
    _result_evidence,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
]

_GHOST_OWNER = "removed/ghost-kit"
_LOCAL_CONTENT = "# locally authored, not managed by APM\n"
_TAG_CHARACTER = "\U000e0041"
_CLAUDE_PLUGIN_MANIFEST = ".claude-plugin/plugin.json"


def _printed_command(result, remediation_id: str) -> tuple[str, ...]:
    """Return the advised command line, matching ``{bound}`` values by shape."""
    entry = catalog()[remediation_id]
    parts = re.split(r"(\{[a-z_]+\})", entry.command)
    pattern = "".join(r"[^'\s]+" if part.startswith("{") else re.escape(part) for part in parts)
    text = _normalized(result)
    match = re.search(rf"'({pattern})' to {re.escape(entry.fixes_promise)}", text)
    assert match, f"no advice for {remediation_id!r} in output:\n{_result_evidence(result)}"
    return tuple(shlex.split(match.group(1)))


# -- deployment-owners-invalid -------------------------------------------------


def _ghost_owner(world: _World) -> None:
    lock_path = world.consumer.root / "apm.lock.yaml"
    lock = load_yaml(lock_path)
    rows = [row for row in lock["deployments"] if row["value"] == _INSTRUCTION]
    assert rows, lock["deployments"]
    rows[0]["owners"] = [*rows[0]["owners"], _GHOST_OWNER]
    dump_yaml(lock, lock_path)


def _owners_repaired(world: _World, result) -> None:
    _expect_ok(world, result)
    lock = load_yaml(world.consumer.root / "apm.lock.yaml")
    assert all(_GHOST_OWNER not in row["owners"] for row in lock["deployments"])


# -- local-files-unmanaged -----------------------------------------------------


def _unmanaged_file_world(tmp_path: Path, apm_binary_path: Path) -> _World:
    scenario = _new_scenario(tmp_path / "remediation", apm_binary_path)
    source = _publish(scenario, "advice-kit", instruction="advice")
    consumer = scenario.consumers.create(
        "advice-consumer", dependencies=(source.dependency,), targets=("copilot",)
    )
    local = consumer.root / _INSTRUCTION
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text(_LOCAL_CONTENT, encoding="ascii")
    return _World(scenario=scenario, source=source, consumer=consumer)


def _package_file_deployed(world: _World, result) -> None:
    _expect_ok(world, result)
    deployed = (world.consumer.root / _INSTRUCTION).read_text(encoding="utf-8")
    assert deployed != _LOCAL_CONTENT
    assert "# advice" in deployed


# -- hidden-characters-flagged -------------------------------------------------


def _hidden_characters_world(tmp_path: Path, apm_binary_path: Path) -> _World:
    scenario = _new_scenario(tmp_path / "remediation", apm_binary_path)
    content = f"---\napplyTo: '**'\n---\n# advice{_TAG_CHARACTER}\n"
    source = _publish(scenario, "advice-kit", instruction="advice", instruction_content=content)
    consumer = scenario.consumers.create(
        "advice-consumer", dependencies=(source.dependency,), targets=("copilot",)
    )
    return _World(scenario=scenario, source=source, consumer=consumer)


def _hidden_characters_stripped(world: _World, result) -> None:
    _expect_ok(world, result)
    deployed = (world.consumer.root / _INSTRUCTION).read_text(encoding="utf-8")
    assert "# advice" in deployed
    assert _TAG_CHARACTER not in deployed


# -- executable-pin-invalid ----------------------------------------------------


def _invalid_pin(world: _World) -> None:
    manifest = load_yaml(world.consumer.manifest_path)
    manifest["executables"]["allow"][_slug(world.source)] = {"hooks": {"pretool.json": "bad"}}
    dump_yaml(manifest, world.consumer.manifest_path)


def _remove_invalid_pin(world: _World) -> None:
    manifest = load_yaml(world.consumer.manifest_path)
    del manifest["executables"]["allow"][_slug(world.source)]["hooks"]["pretool.json"]
    dump_yaml(manifest, world.consumer.manifest_path)


def _pin_recorded(world: _World, result) -> None:
    _expect_ok(world, result)
    manifest = load_yaml(world.consumer.manifest_path)
    pins = manifest["executables"]["allow"][_slug(world.source)]["hooks"]
    assert pins and all(str(value).startswith("sha256:") for value in pins.values()), pins


# -- dependency-alias-invalid --------------------------------------------------


def _set_alias(alias: str):
    def _apply(world: _World) -> None:
        manifest = load_yaml(world.consumer.manifest_path)
        manifest["dependencies"]["apm"][0]["alias"] = alias
        dump_yaml(manifest, world.consumer.manifest_path)

    return _apply


def _installed_under_new_alias(world: _World, result) -> None:
    _expect_ok(world, result)
    assert (world.consumer.root / "apm_modules" / "my-skill.v2").is_dir()


# -- compile-dry-run -----------------------------------------------------------


def _uncompiled_world(tmp_path: Path, apm_binary_path: Path) -> _World:
    scenario = _new_scenario(tmp_path / "remediation", apm_binary_path)
    source = _publish(scenario, "advice-kit", instruction="advice")
    consumer = scenario.consumers.create("compile-project", targets=("codex",))
    scenario.consumers.add_instruction(
        consumer, "own", "---\napplyTo: '**'\ndescription: own\n---\n# own\n"
    )
    return _World(scenario=scenario, source=source, consumer=consumer)


def _not_compiled(world: _World) -> None:
    assert not (world.consumer.root / "AGENTS.md").exists()


def _compiled_written(world: _World, result) -> None:
    _expect_ok(world, result)
    assert "# own" in (world.consumer.root / "AGENTS.md").read_text(encoding="utf-8")


# -- agent-plugin-output-empty -------------------------------------------------


def _claude_only_world(tmp_path: Path, apm_binary_path: Path) -> _World:
    scenario = _new_scenario(tmp_path / "remediation", apm_binary_path)
    source = _publish(scenario, "advice-kit", instruction="advice")
    consumer = scenario.consumers.create("plugin-project", targets=("claude",))
    return _World(scenario=scenario, source=source, consumer=consumer)


def _no_claude_layout(world: _World) -> None:
    assert not (world.consumer.root / _CLAUDE_PLUGIN_MANIFEST).exists()


def _claude_layout_built(world: _World, result) -> None:
    _expect_ok(world, result)
    assert (world.consumer.root / _CLAUDE_PLUGIN_MANIFEST).is_file(), _result_evidence(result)


# -- plugin-mcp-ownership-unknown ----------------------------------------------


def _root_mcp_world(tmp_path: Path, apm_binary_path: Path) -> _World:
    scenario = _new_scenario(tmp_path / "remediation", apm_binary_path)
    source = _publish(scenario, "advice-kit", instruction="advice")
    consumer = scenario.consumers.create("mcp-plugin-project", targets=("claude",))
    servers = {"mcpServers": {"authored-mcp": {"command": "echo"}}}
    (consumer.root / ".mcp.json").write_text(json.dumps(servers), encoding="ascii")
    return _World(scenario=scenario, source=source, consumer=consumer)


def _relock(world: _World) -> None:
    (world.consumer.root / "apm.lock.yaml").unlink()
    _expect_ok(world, _run(world, _INSTALL_ARGS, "remediation-relock"))


def _root_mcp_packed(world: _World, result) -> None:
    _expect_ok(world, result)
    manifest = json.loads((world.consumer.root / _CLAUDE_PLUGIN_MANIFEST).read_text("utf-8"))
    assert "authored-mcp" in manifest.get("mcpServers", {}), manifest


G4_PROOFS: tuple[_TextProof, ...] = (
    _TextProof(
        "deployment-owners-invalid",
        _installed_world,
        _ghost_owner,
        lambda _world: ("audit",),
        1,
        _owners_repaired,
    ),
    _TextProof(
        "local-files-unmanaged",
        _unmanaged_file_world,
        lambda _world: None,
        lambda _world: _INSTALL_ARGS,
        0,
        _package_file_deployed,
    ),
    _TextProof(
        "hidden-characters-flagged",
        _hidden_characters_world,
        lambda _world: None,
        lambda _world: ("install", "--force", "--no-policy", "--parallel-downloads", "0"),
        0,
        _hidden_characters_stripped,
        reprobe=lambda _world: ("audit", "--no-drift"),
    ),
    _TextProof(
        "executable-pin-invalid",
        lambda tmp, binary: _hooked_world(tmp, binary, approved=True),
        _invalid_pin,
        lambda _world: ("approve", "--list"),
        1,
        _pin_recorded,
        before=_remove_invalid_pin,
        reprobe=lambda _world: ("approve", "--list"),
    ),
    _TextProof(
        "dependency-alias-invalid",
        _installed_world,
        _set_alias(".."),
        lambda _world: _INSTALL_ARGS,
        1,
        _installed_under_new_alias,
        before=_set_alias("my-skill.v2"),
    ),
    _TextProof(
        "compile-dry-run",
        _uncompiled_world,
        lambda _world: None,
        lambda _world: ("compile", "--dry-run"),
        0,
        _compiled_written,
        before=_not_compiled,
        reprobe=lambda _world: ("compile",),
    ),
    _TextProof(
        "agent-plugin-output-empty",
        _claude_only_world,
        lambda _world: None,
        lambda _world: ("pack", "--format", "agent-plugin"),
        1,
        _claude_layout_built,
        before=_no_claude_layout,
        reprobe=lambda _world: ("pack", "--claude-plugin"),
    ),
    _TextProof(
        "plugin-mcp-ownership-unknown",
        _root_mcp_world,
        _corrupt_lockfile,
        lambda _world: ("pack",),
        0,
        _root_mcp_packed,
        before=_relock,
        reprobe=lambda _world: ("pack",),
    ),
)


@pytest.mark.parametrize("proof", G4_PROOFS, ids=lambda proof: proof.remediation_id)
def test_g4_remediation_clears_the_reported_state(
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

    again = _run(world, _reprobe_args(proof, world), "remediation-cleared")
    proof.cleared(world, again)
    promise = catalog()[proof.remediation_id].fixes_promise
    advice = f"'{' '.join(command)}' to {promise}"
    assert advice not in _normalized(again), _result_evidence(again)
