"""``apm pack --format apm`` keeps shipping Claude rules routed to AGENTS.md (#3939).

An APM-format bundle carries a dependency's deployed files, and its consumers
do not compile. A Claude install now compiles always-on instructions into
``AGENTS.md`` instead of deploying ``.claude/rules/<name>.md``, so the pack
renders that rule through the install projection: the bundle carries the same
bytes a legacy-layout install deploys.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import load_yaml
from tests.integration.test_required_lifecycle_state_machine import (
    _INSTALL_ARGS,
    _OWNER,
    _instruction,
    _new_scenario,
    _publish,
    _run_success,
    _skill,
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


def test_pack_refuses_routed_rules_from_an_apm_modules_copy_the_lock_does_not_attest(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """Routed rules render only from a cached copy matching its locked content hash."""
    scenario = _new_scenario(tmp_path / "tampered-pack", apm_binary_path)
    source = _publish(scenario, "guard-kit", instruction="guard")
    producer = scenario.consumers.create(
        "producer", dependencies=(source.dependency,), targets=("claude",)
    )
    _run_success(
        scenario, producer, _INSTALL_ARGS, environment=source.environment, scenario_id="install"
    )
    (cached,) = (producer.root / "apm_modules").rglob("guard.instructions.md")
    cached.write_text(cached.read_text(encoding="ascii") + "Injected.\n", encoding="ascii")
    out = tmp_path / "bundles"

    for args in (("--format", "apm"), ()):
        result = scenario.runner.run(
            ("pack", *args, "-o", str(out)),
            scenario_id=f"pack-tampered{'-'.join(args)}",
            cwd=producer.root,
            env=source.environment,
        )
        output = " ".join((result.stdout + result.stderr).split())
        assert result.returncode != 0, output
        assert "does not match the content hash recorded in apm.lock.yaml" in output, output
        assert not out.exists() or not any(out.iterdir())


def test_apm_pack_refuses_a_routed_rule_another_dependency_deploys_differently(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """A routed rule never vanishes behind another dependency's same-path rule."""
    scenario = _new_scenario(tmp_path / "shared-path", apm_binary_path)
    routed = _publish(scenario, "guard-kit", instruction="guard")
    scoped = _publish(
        scenario,
        "python-kit",
        instruction="guard",
        instruction_content=_instruction("guard", apply_to="**/*.py"),
    )
    producer = scenario.consumers.create(
        "producer", dependencies=(routed.dependency, scoped.dependency), targets=("claude",)
    )
    environment = scenario.repositories.url_rewrite_subprocess_env_many(
        ((routed.repository, routed.remote_url), (scoped.repository, scoped.remote_url))
    )
    _run_success(scenario, producer, _INSTALL_ARGS, environment=environment, scenario_id="install")
    assert "**/*.py" in (producer.root / _RULE).read_text(encoding="ascii")

    out = tmp_path / "bundles"
    result = scenario.runner.run(
        ("pack", "--format", "apm", "-o", str(out)),
        scenario_id="pack-shared-path",
        cwd=producer.root,
        env=environment,
    )
    output = " ".join((result.stdout + result.stderr).split())
    assert result.returncode != 0, output
    assert f"Cannot pack {_RULE}" in output, output
    assert not out.exists() or not any(out.iterdir())


def test_plugin_pack_never_renders_a_dev_dependency_it_does_not_ship(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """An edited cached dev dependency cannot fail a pack that excludes it."""
    scenario = _new_scenario(tmp_path / "dev-pack", apm_binary_path)
    shipped_dep = _publish(scenario, "helper-kit", skill="helper")
    dev_dep = _publish(scenario, "guard-kit", instruction="guard")
    producer = scenario.consumers.create(
        "producer", dependencies=(shipped_dep.dependency,), targets=("claude",)
    )
    manifest = producer.manifest_path.read_text(encoding="ascii")
    dependency = "\n".join(f"      {key}: {value}" for key, value in dev_dep.dependency.items())
    producer.manifest_path.write_text(
        manifest + "devDependencies:\n  apm:\n    -\n" + dependency + "\n", encoding="ascii"
    )
    environment = scenario.repositories.url_rewrite_subprocess_env_many(
        ((shipped_dep.repository, shipped_dep.remote_url), (dev_dep.repository, dev_dep.remote_url))
    )
    _run_success(scenario, producer, _INSTALL_ARGS, environment=environment, scenario_id="install")
    (cached,) = (producer.root / "apm_modules").rglob("guard.instructions.md")
    cached.write_text(cached.read_text(encoding="ascii") + "Injected.\n", encoding="ascii")

    out = tmp_path / "plugin-out"
    _run_success(
        scenario, producer, ("pack", "-o", str(out)), environment=environment, scenario_id="pack"
    )
    (bundle,) = [path for path in out.iterdir() if path.is_dir()]

    shipped = _files(bundle)
    assert "skills/helper/SKILL.md" in shipped, sorted(shipped)
    assert not any("guard" in path for path in shipped), sorted(shipped)


def test_pack_refuses_when_the_cached_routed_instruction_was_deleted(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """Deleting the routed instruction must not ship the skill without its guidance."""
    scenario = _new_scenario(tmp_path / "deleted-pack", apm_binary_path)
    source = _publish(scenario, "guard-kit", instruction="guard", skill="helper")
    producer = scenario.consumers.create(
        "producer", dependencies=(source.dependency,), targets=("claude",)
    )
    _run_success(
        scenario, producer, _INSTALL_ARGS, environment=source.environment, scenario_id="install"
    )
    assert (producer.root / ".claude/skills/helper/SKILL.md").is_file()
    (cached,) = (producer.root / "apm_modules").rglob("guard.instructions.md")
    cached.unlink()
    out = tmp_path / "bundles"

    for args in (("--format", "apm"), ()):
        result = scenario.runner.run(
            ("pack", *args, "-o", str(out)),
            scenario_id=f"pack-deleted{'-'.join(args)}",
            cwd=producer.root,
            env=source.environment,
        )
        output = " ".join((result.stdout + result.stderr).split())
        assert result.returncode != 0, output
        assert "does not match the content hash recorded in apm.lock.yaml" in output, output
        assert not out.exists() or not any(out.iterdir())


def _refuses_both_formats(scenario, producer, environment, out: Path) -> list[str]:
    outputs = []
    for args in (("--format", "apm"), ()):
        result = scenario.runner.run(
            ("pack", *args, "-o", str(out)),
            scenario_id=f"pack-refused{'-'.join(args)}",
            cwd=producer.root,
            env=environment,
        )
        output = " ".join((result.stdout + result.stderr).split())
        assert result.returncode != 0, output
        assert not out.exists() or not any(out.iterdir()), output
        outputs.append(output)
    return outputs


def test_pack_refuses_a_claude_dependency_whose_cache_directory_is_missing(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """A missing apm_modules/<dep> must not ship the skill without its guidance (#4755)."""
    scenario = _new_scenario(tmp_path / "missing-cache", apm_binary_path)
    source = _publish(scenario, "guard-kit", instruction="guard", skill="helper")
    producer = scenario.consumers.create(
        "producer", dependencies=(source.dependency,), targets=("claude",)
    )
    _run_success(
        scenario, producer, _INSTALL_ARGS, environment=source.environment, scenario_id="install"
    )
    assert (producer.root / ".claude/skills/helper/SKILL.md").is_file()
    present = _pack(scenario, producer, source, tmp_path / "present-out", "--format", "apm")
    assert (present / _RULE).is_file(), "with the cache present the routed rule ships"

    shutil.rmtree(producer.root / "apm_modules" / "guard-kit")

    for output in _refuses_both_formats(
        scenario, producer, source.environment, tmp_path / "missing-out"
    ):
        assert "apm_modules/guard-kit is missing" in output, output
        assert "run 'apm install'" in output, output


def test_pack_keeps_packing_a_dependency_that_excludes_claude_without_its_cache(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """A package whose ``targets:`` exclude Claude has no routed rules to lose (#4755)."""
    scenario = _new_scenario(tmp_path / "copilot-dep", apm_binary_path)
    package = scenario.sources.create("guard-kit", targets=("copilot",))
    scenario.sources.add_skill(package, "helper", _skill("helper"))
    scenario.sources.add_instruction(package, "guard", _instruction("guard"))
    repository = scenario.repositories.create("guard-kit", source_tree=package.root)
    commit = scenario.repositories.commit(repository, message="seed guard-kit")
    remote_url = f"https://github.com/{_OWNER}/guard-kit"
    environment = scenario.repositories.url_rewrite_subprocess_env(repository, remote_url)
    dependency = {"git": remote_url, "ref": commit.sha, "alias": "guard-kit"}
    producer = scenario.consumers.create(
        "producer", dependencies=(dependency,), targets=("claude", "copilot")
    )
    _run_success(scenario, producer, _INSTALL_ARGS, environment=environment, scenario_id="install")
    assert not (producer.root / ".claude/skills/helper").exists()

    shutil.rmtree(producer.root / "apm_modules" / "guard-kit")

    for args, name in ((("--format", "apm"), "apm-out"), ((), "plugin-out")):
        out = tmp_path / name
        _run_success(
            scenario,
            producer,
            ("pack", *args, "-o", str(out)),
            environment=environment,
            scenario_id=f"pack-{name}",
        )
        (bundle,) = [path for path in out.iterdir() if path.is_dir()]
        assert any("helper" in path for path in _files(bundle)), sorted(_files(bundle))
