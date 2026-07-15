"""Real-binary contract for selected artifacts across pack and local install."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.artifact_snapshot import (
    ArtifactSnapshot,
    assert_paths_absent,
    assert_paths_present,
    assert_unchanged,
)
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackageFactory
from tests.utils.scenario_rows import (
    LifecycleAction,
    ScenarioObservation,
    ScenarioRow,
)

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.requires_e2e_mode,
    pytest.mark.requires_apm_binary,
]

_DECLARED_SKILLS = ("bare-skill", "productivity/grill-me")
_EXPECTED_ARTIFACTS = frozenset(
    {
        "instructions/produce-rules.instructions.md",
        "skills/bare-skill/SKILL.md",
        "skills/grill-me/SKILL.md",
    }
)
_EXCLUDED_SKILL = "internal-only"
_AUDIT_ARGS = (
    "audit",
    "--ci",
    "--no-policy",
    "--format",
    "json",
    "--output",
    "reports/audit.json",
)


@dataclass(frozen=True)
class _ProduceContractReceipt:
    """Captured command and filesystem evidence for one Produce lifecycle."""

    producer_row: ScenarioRow
    producer_observation: ScenarioObservation
    consumer_row: ScenarioRow
    consumer_observation: ScenarioObservation
    producer_lock: dict[str, object]
    bundle_lock: dict[str, object]
    consumer_lock: dict[str, object]
    audit_report: dict[str, object]


def _artifact_hashes(
    snapshot: ArtifactSnapshot,
    prefixes: tuple[tuple[str, str], ...],
) -> dict[str, str]:
    """Project stage-specific paths onto canonical primitive-relative hashes."""
    hashes: dict[str, str] = {}
    for entry in snapshot.entries:
        if entry.kind != "file" or entry.fingerprint is None:
            continue
        for stage_prefix, canonical_prefix in prefixes:
            if not entry.relative_path.startswith(stage_prefix):
                continue
            canonical_path = canonical_prefix + entry.relative_path.removeprefix(stage_prefix)
            if canonical_path in _EXPECTED_ARTIFACTS:
                hashes[canonical_path] = entry.fingerprint
            break
    return hashes


def _dependency(lockfile: dict[str, object]) -> dict[str, object]:
    """Return the single dependency record persisted by the producer."""
    dependencies = lockfile["dependencies"]
    assert isinstance(dependencies, list)
    assert len(dependencies) == 1
    dependency = dependencies[0]
    assert isinstance(dependency, dict)
    return dependency


def _run_produce_contract(
    root: Path,
    binary: Path,
) -> _ProduceContractReceipt:
    """Author, install, compile, pack, consume, and audit one local scenario."""
    isolated = IsolatedApmEnvironment.create(root / "isolated", base_env=dict(os.environ))
    environment = isolated.subprocess_env()

    package_factory = LocalPackageFactory(isolated.package_root)
    source = package_factory.create("produce-fixture", targets=("copilot",))
    package_factory.add_skill(
        source,
        "bare-skill",
        (
            "---\n"
            "name: bare-skill\n"
            "description: Bare-name Produce contract skill\n"
            "---\n"
            "# Bare skill\n"
        ),
    )
    package_factory.add_skill(
        source,
        "grill-me",
        ("---\nname: grill-me\ndescription: Prefixed Produce contract skill\n---\n# Grill me\n"),
    )
    package_factory.add_skill(
        source,
        _EXCLUDED_SKILL,
        (
            "---\n"
            f"name: {_EXCLUDED_SKILL}\n"
            "description: Excluded Produce contract skill\n"
            "---\n"
            "# Internal only\n"
        ),
    )
    package_factory.add_instruction(
        source,
        "produce-rules",
        (
            "---\n"
            "applyTo: '**'\n"
            "description: Produce contract compilation input\n"
            "---\n"
            "# Produce rules\n"
        ),
    )
    source_before = ArtifactSnapshot.capture(source.root)

    repository_factory = LocalGitRepositoryFactory(
        isolated.repository_root,
        env=environment,
    )
    repository = repository_factory.create("produce-fixture", source_tree=source.root)
    commit = repository_factory.commit(repository, message="seed Produce fixture")
    git_source = "git@gitlab.example.invalid:fixtures/produce-fixture.git"
    subprocess.run(
        (
            "git",
            "config",
            "--global",
            f"url.{repository.file_url}.insteadOf",
            git_source,
        ),
        env=environment,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )

    project_factory = LocalPackageFactory(isolated.work_root)
    producer = project_factory.create(
        "produce-contract-producer",
        dependencies=(
            {
                "git": git_source,
                "type": "gitlab",
                "ref": commit.sha,
                "alias": source.name,
                "skills": list(_DECLARED_SKILLS),
            },
        ),
        targets=("copilot",),
    )
    producer_row = ScenarioRow(
        id="produce-selected-artifacts",
        source_inputs=(source.root, producer.manifest_path),
        lifecycle_actions=(
            LifecycleAction(("install", "--target", "copilot")),
            LifecycleAction(("compile", "--target", "copilot", "--force-instructions")),
            LifecycleAction(("pack", "--format", "plugin", "--offline")),
        ),
    )
    runner = ApmLifecycleRunner((str(binary),), scenario_timeout_seconds=240)
    producer_results = runner.run_sequence(
        tuple(action.args for action in producer_row.lifecycle_actions),
        expected_returncodes=tuple(
            action.expected_returncode for action in producer_row.lifecycle_actions
        ),
        scenario_id=producer_row.id,
        cwd=producer.root,
        env=environment,
    )

    source_after = ArtifactSnapshot.capture(source.root)
    producer_snapshot = ArtifactSnapshot.capture(producer.root)
    bundle = producer.root / "build" / "produce-contract-producer-0.1.0"
    bundle_before_install = ArtifactSnapshot.capture(bundle)
    producer_observation = ScenarioObservation(
        source_inputs=producer_row.source_inputs,
        results=producer_results,
        snapshots=(
            source_before,
            source_after,
            producer_snapshot,
            bundle_before_install,
        ),
    )

    consumer = project_factory.create(
        "produce-contract-consumer",
        targets=("copilot",),
    )
    consumer_row = ScenarioRow(
        id="consume-genuine-pack-output",
        source_inputs=(bundle, consumer.manifest_path),
        lifecycle_actions=(
            LifecycleAction(("install", str(bundle), "--target", "copilot")),
            LifecycleAction(_AUDIT_ARGS),
        ),
    )
    consumer_results = runner.run_sequence(
        tuple(action.args for action in consumer_row.lifecycle_actions),
        expected_returncodes=tuple(
            action.expected_returncode for action in consumer_row.lifecycle_actions
        ),
        scenario_id=consumer_row.id,
        cwd=consumer.root,
        env=environment,
    )
    bundle_after_install = ArtifactSnapshot.capture(bundle)
    consumer_snapshot = ArtifactSnapshot.capture(consumer.root)
    consumer_observation = ScenarioObservation(
        source_inputs=consumer_row.source_inputs,
        results=consumer_results,
        snapshots=(bundle_before_install, bundle_after_install, consumer_snapshot),
    )

    return _ProduceContractReceipt(
        producer_row=producer_row,
        producer_observation=producer_observation,
        consumer_row=consumer_row,
        consumer_observation=consumer_observation,
        producer_lock=load_yaml(producer.root / "apm.lock.yaml"),
        bundle_lock=load_yaml(bundle / "apm.lock.yaml"),
        consumer_lock=load_yaml(consumer.root / "apm.lock.yaml"),
        audit_report=json.loads((consumer.root / "reports" / "audit.json").read_text()),
    )


def test_real_pack_output_matches_installed_artifacts_and_identity(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Selected bytes and subset identity survive every Produce boundary."""
    receipt = _run_produce_contract(tmp_path, apm_binary_path)
    source_before, source_after, producer_snapshot, bundle_snapshot = (
        receipt.producer_observation.snapshots
    )
    bundle_before, bundle_after, consumer_snapshot = receipt.consumer_observation.snapshots

    assert tuple(result.returncode for result in receipt.producer_observation.results) == (0, 0, 0)
    assert tuple(result.returncode for result in receipt.consumer_observation.results) == (0, 0)
    assert_unchanged(source_before, source_after)
    assert_unchanged(bundle_before, bundle_after)
    compiled = receipt.producer_row.source_inputs[1].parent / "AGENTS.md"
    assert compiled.is_file()
    assert "# Produce rules" in compiled.read_text(encoding="utf-8")

    source_hashes = _artifact_hashes(
        source_before,
        (
            ("skills/", "skills/"),
            (".apm/instructions/", "instructions/"),
        ),
    )
    producer_hashes = _artifact_hashes(
        producer_snapshot,
        (
            (".agents/skills/", "skills/"),
            (".github/instructions/", "instructions/"),
        ),
    )
    bundle_hashes = _artifact_hashes(
        bundle_snapshot,
        (
            ("skills/", "skills/"),
            ("instructions/", "instructions/"),
        ),
    )
    consumer_hashes = _artifact_hashes(
        consumer_snapshot,
        (
            (".agents/skills/", "skills/"),
            (".github/instructions/", "instructions/"),
        ),
    )
    assert (
        source_hashes
        == producer_hashes
        == bundle_hashes
        == consumer_hashes
        == {path: source_hashes[path] for path in _EXPECTED_ARTIFACTS}
    )

    assert _dependency(receipt.producer_lock)["skill_subset"] == list(_DECLARED_SKILLS)
    assert _dependency(receipt.bundle_lock)["skill_subset"] == list(_DECLARED_SKILLS)
    pack = receipt.bundle_lock["pack"]
    assert isinstance(pack, dict)
    bundle_file_hashes = pack["bundle_files"]
    assert isinstance(bundle_file_hashes, dict)
    assert {path: bundle_file_hashes[path] for path in _EXPECTED_ARTIFACTS} == bundle_hashes

    expected_deployed = {
        ".agents/skills/bare-skill/SKILL.md",
        ".agents/skills/grill-me/SKILL.md",
        ".github/instructions/produce-rules.instructions.md",
    }
    assert set(receipt.consumer_lock["local_deployed_files"]) == expected_deployed
    deployed_hashes = receipt.consumer_lock["local_deployed_file_hashes"]
    assert isinstance(deployed_hashes, dict)
    assert set(deployed_hashes) == expected_deployed
    assert all(str(value).startswith("sha256:") for value in deployed_hashes.values())
    deployment_rows = receipt.consumer_lock["deployments"]
    assert isinstance(deployment_rows, list)
    bundle_rows = {
        row["value"]: row
        for row in deployment_rows
        if isinstance(row, dict) and row.get("value") in expected_deployed
    }
    assert set(bundle_rows) == expected_deployed
    assert {row["active_owner"] for row in bundle_rows.values()} == {"local-bundle"}

    assert receipt.audit_report["passed"] is True
    assert receipt.audit_report["summary"] == {
        "total": len(receipt.audit_report["checks"]),
        "passed": len(receipt.audit_report["checks"]),
        "failed": 0,
    }


def test_excluded_skill_never_reaches_pack_or_consumer(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """A source skill outside the selected subset must not leak downstream."""
    receipt = _run_produce_contract(tmp_path, apm_binary_path)
    source_snapshot, _, producer_snapshot, bundle_snapshot = receipt.producer_observation.snapshots
    consumer_snapshot = receipt.consumer_observation.snapshots[-1]

    assert_paths_present(
        source_snapshot,
        {f"skills/{_EXCLUDED_SKILL}/SKILL.md"},
    )
    assert_paths_absent(
        producer_snapshot,
        {f".agents/skills/{_EXCLUDED_SKILL}/SKILL.md"},
    )
    assert_paths_absent(
        bundle_snapshot,
        {f"skills/{_EXCLUDED_SKILL}/SKILL.md"},
    )
    assert_paths_absent(
        consumer_snapshot,
        {f".agents/skills/{_EXCLUDED_SKILL}/SKILL.md"},
    )
    pack = receipt.bundle_lock["pack"]
    assert isinstance(pack, dict)
    bundle_files = pack["bundle_files"]
    assert isinstance(bundle_files, dict)
    assert set(bundle_files).isdisjoint({f"skills/{_EXCLUDED_SKILL}/SKILL.md"})
