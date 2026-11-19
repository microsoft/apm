"""``apm pack --format apm`` keeps shipping Claude rules routed to AGENTS.md (#3939).

An APM-format bundle carries a dependency's deployed files, and its consumers
do not compile. A Claude install now compiles always-on instructions into
``AGENTS.md`` instead of deploying ``.claude/rules/<name>.md``, so the pack
renders that rule through the install projection: the bundle carries the same
bytes a legacy-layout install deploys.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import load_yaml
from tests.integration.test_required_lifecycle_state_machine import (
    _INSTALL_ARGS,
    _instruction,
    _new_scenario,
    _publish,
    _run_success,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
    pytest.mark.promise("P-OWN-claude-agents-md-instruction-routing"),
]

_RULE = ".claude/rules/guard.md"


def test_apm_format_pack_ships_rules_claude_compiles_into_agents_md(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    scenario = _new_scenario(tmp_path / "claude-pack", apm_binary_path)
    source = _publish(scenario, "guard-kit", instruction="guard")

    # Legacy layout: a hand-authored CLAUDE.md keeps every rule deployed by
    # the same projection a pre-#3939 install used.
    legacy = scenario.consumers.create(
        "legacy-consumer", dependencies=(source.dependency,), targets=("claude",)
    )
    (legacy.root / "CLAUDE.md").write_text("# Team notes\n", encoding="ascii")
    _run_success(
        scenario, legacy, _INSTALL_ARGS, environment=source.environment, scenario_id="legacy"
    )
    legacy_rule = (legacy.root / _RULE).read_bytes()

    producer = scenario.consumers.create(
        "producer", dependencies=(source.dependency,), targets=("claude",)
    )
    _run_success(
        scenario, producer, _INSTALL_ARGS, environment=source.environment, scenario_id="install"
    )
    assert not (producer.root / _RULE).exists(), "always-on rule now compiles into AGENTS.md"

    out = tmp_path / "bundles"
    _run_success(
        scenario,
        producer,
        ("pack", "--format", "apm", "-o", str(out)),
        environment=source.environment,
        scenario_id="pack",
    )
    (bundle,) = [path for path in out.iterdir() if path.is_dir()]
    assert (bundle / _RULE).read_bytes() == legacy_rule
    lock = load_yaml(bundle / "apm.lock.yaml")
    (dependency,) = [dep for dep in lock["dependencies"] if _RULE in dep.get("deployed_files", [])]
    assert dependency["deployed_file_hashes"][_RULE].startswith("sha256:")
    assert _instruction("guard").splitlines()[-1] in legacy_rule.decode("ascii")


def _pack(scenario, consumer, source, out: Path, *args: str) -> Path:
    _run_success(
        scenario,
        consumer,
        ("pack", *args, "-o", str(out)),
        environment=source.environment,
        scenario_id=f"pack-{out.name}",
    )
    (bundle,) = [path for path in out.iterdir() if path.is_dir()]
    return bundle


def _files(bundle: Path) -> dict[str, bytes]:
    skip = {"apm.lock.yaml", "plugin.json", ".claude-plugin/plugin.json"}
    return {
        path.relative_to(bundle).as_posix(): path.read_bytes()
        for path in bundle.rglob("*")
        if path.is_file() and path.relative_to(bundle).as_posix() not in skip
    }


def test_default_plugin_pack_ships_routed_instructions_like_legacy_layout(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    scenario = _new_scenario(tmp_path / "plugin-pack", apm_binary_path)
    source = _publish(scenario, "guard-kit", instruction="guard")
    legacy = scenario.consumers.create(
        "legacy-consumer", dependencies=(source.dependency,), targets=("claude",)
    )
    (legacy.root / "CLAUDE.md").write_text("# Team notes\n", encoding="ascii")
    producer = scenario.consumers.create(
        "producer", dependencies=(source.dependency,), targets=("claude",)
    )
    for consumer in (legacy, producer):
        _run_success(
            scenario,
            consumer,
            _INSTALL_ARGS,
            environment=source.environment,
            scenario_id=f"install-{consumer.root.name}",
        )
    assert not (producer.root / _RULE).exists()

    expected = _files(_pack(scenario, legacy, source, tmp_path / "legacy-out"))
    shipped = _files(_pack(scenario, producer, source, tmp_path / "producer-out"))

    assert "instructions/guard.md" in expected
    assert shipped == expected


def test_pack_target_detection_never_adds_rules_for_a_copilot_only_producer(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    scenario = _new_scenario(tmp_path / "copilot-pack", apm_binary_path)
    source = _publish(scenario, "guard-kit", instruction="guard")
    producer = scenario.consumers.create(
        "producer", dependencies=(source.dependency,), targets=("copilot",)
    )
    (producer.root / ".claude").mkdir()
    (producer.root / ".claude/settings.local.json").write_text("{}\n", encoding="ascii")
    _run_success(
        scenario, producer, _INSTALL_ARGS, environment=source.environment, scenario_id="install"
    )

    apm_bundle = _pack(scenario, producer, source, tmp_path / "apm-out", "--format", "apm")
    assert not (apm_bundle / ".claude").exists()
    lock = load_yaml(apm_bundle / "apm.lock.yaml")
    shipped = {path for dep in lock["dependencies"] for path in dep.get("deployed_files", [])}
    assert shipped and all(path.startswith(".github/") for path in shipped), shipped

    plugin_bundle = _pack(scenario, producer, source, tmp_path / "plugin-out")
    assert sorted(_files(plugin_bundle)) == ["instructions/guard.instructions.md"]


@pytest.mark.parametrize("with_skill", [True, False], ids=["recorded", "unrecorded"])
def test_cli_target_claude_install_with_ambiguous_folders_never_drops_rules_silently(
    tmp_path: Path, apm_binary_path: Path, with_skill: bool
) -> None:
    """``--target claude`` on a ``.claude/`` + ``.cursor/`` producer without ``targets:``.

    Pack cannot see the CLI target. A recorded Claude deployment decides
    (recorded targets win); without one, pack warns instead of dropping the
    routed rule silently.
    """
    scenario = _new_scenario(tmp_path / "cli-target", apm_binary_path)
    source = _publish(
        scenario, "guard-kit", instruction="guard", skill="helper" if with_skill else None
    )
    producer = scenario.consumers.create("producer", dependencies=(source.dependency,))
    for folder in (".claude", ".cursor"):
        (producer.root / folder).mkdir(exist_ok=True)
    _run_success(
        scenario,
        producer,
        (*_INSTALL_ARGS, "--target", "claude"),
        environment=source.environment,
        scenario_id="install",
    )
    assert not (producer.root / _RULE).exists()

    out = tmp_path / "apm-out"
    packed = _run_success(
        scenario,
        producer,
        ("pack", "--format", "apm", "-o", str(out)),
        environment=source.environment,
        scenario_id="pack",
    )
    (bundle,) = [path for path in out.iterdir() if path.is_dir()]
    if with_skill:
        assert (bundle / _RULE).is_file()
    else:
        assert not (bundle / _RULE).exists()
        text = packed.stdout + packed.stderr
        assert "Skipped 1 Claude rule(s) routed to AGENTS.md" in " ".join(text.split()), text
