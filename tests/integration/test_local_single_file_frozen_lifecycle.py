"""Local-file snapshot baselines through the canonical frozen-local owner."""

from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import load_yaml
from tests.integration.test_local_single_file_matrix import (
    _KINDS,
    _assert_state,
    _content,
    _scenario,
    _source,
)
from tests.utils.artifact_snapshot import ArtifactSnapshot, assert_unchanged

pytestmark = [pytest.mark.e2e, pytest.mark.requires_apm_binary]


@pytest.mark.parametrize(("kind", "directory"), _KINDS)
@pytest.mark.parametrize("user", [False, True], ids=["project", "user"])
def test_local_file_frozen_replay_never_accepts_changed_bytes_or_rewrites_lock(
    tmp_path: Path, apm_binary_path: Path, kind: str, directory: str, user: bool
) -> None:
    scenario = _scenario(tmp_path, apm_binary_path)
    source = _source(scenario, kind)
    reference = source.as_posix() if user else f"./ai/{source.name}"
    scope = ("--global",) if user else ()
    scenario.run("install", *scope, reference, "--target", "copilot", "--no-policy")
    root = scenario.isolated.config_root if user else scenario.project.root
    deployed_root = scenario.isolated.home / ".copilot" if user else root / ".github"
    lock_path = root / "apm.lock.yaml"
    baseline = lock_path.read_bytes()
    _assert_state(root, deployed_root, source, reference, directory, user=user)
    output = deployed_root / directory / source.name
    scenario.run("install", *scope, "--frozen", "--target", "copilot", "--no-policy")
    assert lock_path.read_bytes() == baseline
    assert output.read_bytes() == _content()
    source.write_bytes(_content("deliberately edited"))
    before = ArtifactSnapshot.capture(deployed_root)
    result = scenario.run(
        "install", *scope, "--frozen", "--target", "copilot", "--no-policy", expected=1
    )
    assert "locked" in result.stdout + result.stderr
    assert output.read_bytes() == _content()
    assert lock_path.read_bytes() == baseline
    assert_unchanged(before, ArtifactSnapshot.capture(deployed_root))
    materialized = root / "apm_modules" / "_local" / source.name / ".apm" / directory / source.name
    assert materialized.read_bytes() == _content()
    scenario.run("install", *scope, "--target", "copilot", "--no-policy")
    assert output.read_bytes() == _content("deliberately edited")
    accepted = lock_path.read_bytes()
    assert accepted != baseline
    scenario.run("install", *scope, "--frozen", "--target", "copilot", "--no-policy")
    assert lock_path.read_bytes() == accepted


@pytest.mark.parametrize(("kind", "directory"), _KINDS)
def test_nested_file_frozen_hash_uses_declaring_anchor_and_rejects_symlink(
    tmp_path: Path, apm_binary_path: Path, kind: str, directory: str
) -> None:
    """A nested file outside its parent's tree has its own locked baseline."""
    scenario = _scenario(tmp_path, apm_binary_path)
    source = scenario.isolated.work_root / "file-source" / f"nested.{kind}.md"
    source.parent.mkdir()
    source.write_bytes(_content())
    child_path = f"../file-source/{source.name}"
    scenario.factory.create("parent", dependencies=(child_path,), targets=("copilot",))
    scenario.factory.replace_apm_dependencies(scenario.project, ("../parent",))
    args = ("--target", "copilot", "--no-policy")
    scenario.run("install", *args)
    lock_path = scenario.project.root / "apm.lock.yaml"
    locked = lock_path.read_bytes()
    child = next(
        row for row in load_yaml(lock_path)["dependencies"] if row.get("local_path") == child_path
    )
    assert child["resolved_by"] == "_local/parent"
    output = scenario.project.root / ".github" / directory / source.name
    assert output.read_bytes() == _content()
    scenario.run("install", "--frozen", *args)
    assert lock_path.read_bytes() == locked
    source.write_bytes(_content("nested edit"))
    before = ArtifactSnapshot.capture(scenario.project.root)
    cache_before = ArtifactSnapshot.capture(scenario.isolated.cache_root)
    result = scenario.run("install", "--frozen", *args, expected=1)
    assert f"local dependency '{child_path}' content differs" in result.stdout + result.stderr
    assert_unchanged(before, ArtifactSnapshot.capture(scenario.project.root))
    assert_unchanged(cache_before, ArtifactSnapshot.capture(scenario.isolated.cache_root))
    source.write_bytes(_content())
    actual = source.with_name(f"actual.{kind}.md")
    source.rename(actual)
    source.symlink_to(actual)
    result = scenario.run("install", "--frozen", *args, expected=1)
    # The changed canonical source identity is rejected before leaf admission.
    diagnostic = " ".join((result.stdout + result.stderr).split())
    assert actual.name in diagnostic, result.stdout + result.stderr
    assert "missing from apm.lock.yaml" in diagnostic, result.stdout + result.stderr
    assert_unchanged(before, ArtifactSnapshot.capture(scenario.project.root))
    assert_unchanged(cache_before, ArtifactSnapshot.capture(scenario.isolated.cache_root))
    assert not list(scenario.isolated.temp_root.iterdir())
