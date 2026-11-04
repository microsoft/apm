"""Independent frozen-content control for ordinary-only claim repair."""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.integration.test_required_lifecycle_state_machine import (
    _INSTALL_ARGS,
    _new_scenario,
    _skill,
)
from tests.utils.artifact_snapshot import ArtifactSnapshot, assert_unchanged

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
]


@pytest.mark.parametrize("user_scope", [False, True], ids=["project", "user"])
@pytest.mark.parametrize("source_change", ["modified", "removed"])
def test_frozen_does_not_accept_changed_source_or_repair_historical_claims(
    tmp_path: Path,
    apm_binary_path: Path,
    user_scope: bool,
    source_change: str,
) -> None:
    """Frozen refusal must preserve bytes and every historical ownership view."""
    scenario = _new_scenario(tmp_path / "scenario", apm_binary_path)
    source = scenario.sources.create("frozen-source")
    skill = scenario.sources.add_skill(source, "demo", _skill("demo"))
    extra = skill.parent / "extra.md"
    extra.write_text("Locked dependency asset.\n", encoding="ascii")
    consumer = scenario.consumers.create(
        "consumer",
        targets=("claude", "codex"),
        dependencies=({"path": str(source.root)},),
    )
    scope_args = ("--global",) if user_scope else ()
    state_root = scenario.isolated.config_root if user_scope else consumer.root
    deploy_root = scenario.isolated.home if user_scope else consumer.root
    if user_scope:
        dump_yaml(load_yaml(consumer.manifest_path), state_root / "apm.yml")

    installed = scenario.runner.run(
        (*_INSTALL_ARGS, *scope_args),
        scenario_id=f"frozen-control-baseline-{user_scope}-{source_change}",
        cwd=consumer.root,
        env=scenario.environment,
    )
    assert installed.returncode == 0, (installed.stdout, installed.stderr)
    lock_path = state_root / "apm.lock.yaml"
    baseline = load_yaml(lock_path)
    paths = (".agents/skills/demo/extra.md", ".claude/skills/demo/extra.md")
    for path in paths:
        assert (deploy_root / path).read_bytes() == extra.read_bytes()
        assert path in baseline["dependencies"][0]["deployed_files"]
        assert path in baseline["dependencies"][0]["deployed_file_hashes"]
        assert any(row["value"] == path for row in baseline["deployments"])

    # Convert only the APM-produced attribution to an explicit historical format.
    for row in baseline["deployments"]:
        if row["value"].startswith(".agents/"):
            row["target"] = "legacy"
    dump_yaml(baseline, lock_path)
    if source_change == "removed":
        extra.unlink()
    else:
        extra.write_text("Changed dependency asset.\n", encoding="ascii")
    before = ArtifactSnapshot.capture(tmp_path)
    lock_bytes = lock_path.read_bytes()

    result = scenario.runner.run(
        (*_INSTALL_ARGS, *scope_args, "--frozen"),
        scenario_id=f"frozen-control-refusal-{user_scope}-{source_change}",
        cwd=consumer.root,
        env=scenario.environment,
    )

    assert result.returncode == 1, (result.command, result.stdout, result.stderr)
    assert lock_path.read_bytes() == lock_bytes
    assert load_yaml(lock_path) == baseline
    assert_unchanged(before, ArtifactSnapshot.capture(tmp_path))
