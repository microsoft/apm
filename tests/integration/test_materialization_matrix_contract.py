"""Genuine Produce/Consume coverage across supported materialization shapes."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import pytest

from apm_cli.utils.yaml_io import load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.artifact_snapshot import (
    ArtifactSnapshot,
    assert_paths_absent,
    assert_paths_present,
    assert_unchanged,
)
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackageFactory
from tests.utils.scenario_rows import LifecycleAction, ScenarioRow

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.requires_e2e_mode,
    pytest.mark.requires_apm_binary,
]

_SELECTED_SKILLS = ("companion", "shared")
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
class _ArtifactCase:
    """One source-to-consumer materialization row."""

    id: str
    source_path: str
    producer_path: str
    bundle_path: str
    consumer_path: str
    selected: bool = True


_MATRIX = (
    _ArtifactCase(
        "same-leaf-instruction",
        ".apm/instructions/shared.instructions.md",
        ".github/instructions/shared.instructions.md",
        "instructions/shared.instructions.md",
        ".github/instructions/shared.instructions.md",
    ),
    _ArtifactCase(
        "same-leaf-agent",
        ".apm/agents/shared.agent.md",
        ".github/agents/shared.agent.md",
        "agents/shared.agent.md",
        ".github/agents/shared.agent.md",
    ),
    _ArtifactCase(
        "same-leaf-skill",
        "skills/shared/SKILL.md",
        ".agents/skills/shared/SKILL.md",
        "skills/shared/SKILL.md",
        ".agents/skills/shared/SKILL.md",
    ),
    _ArtifactCase(
        "nested-skill-link",
        "skills/shared/references/checklist.md",
        ".agents/skills/shared/references/checklist.md",
        "skills/shared/references/checklist.md",
        ".agents/skills/shared/references/checklist.md",
    ),
    _ArtifactCase(
        "skill-relative-link",
        "skills/shared/README.md",
        ".agents/skills/shared/README.md",
        "skills/shared/README.md",
        ".agents/skills/shared/README.md",
    ),
    _ArtifactCase(
        "second-selected-skill",
        "skills/companion/SKILL.md",
        ".agents/skills/companion/SKILL.md",
        "skills/companion/SKILL.md",
        ".agents/skills/companion/SKILL.md",
    ),
    _ArtifactCase(
        "excluded-skill",
        "skills/internal-only/SKILL.md",
        ".agents/skills/internal-only/SKILL.md",
        "skills/internal-only/SKILL.md",
        ".agents/skills/internal-only/SKILL.md",
        selected=False,
    ),
)


@dataclass(frozen=True)
class _MatrixReceipt:
    """Captured state from the real lifecycle shared by matrix assertions."""

    isolated: IsolatedApmEnvironment
    binary: Path
    environment: dict[str, str]
    source_root: Path
    producer_root: Path
    bundle_root: Path
    consumer_root: Path
    source_before: ArtifactSnapshot
    source_after: ArtifactSnapshot
    producer_snapshot: ArtifactSnapshot
    bundle_before: ArtifactSnapshot
    bundle_after: ArtifactSnapshot
    consumer_snapshot: ArtifactSnapshot
    producer_row: ScenarioRow
    producer_results: tuple[CommandResult, ...]
    consumer_row: ScenarioRow
    consumer_results: tuple[CommandResult, ...]
    producer_lock: dict[str, object]
    bundle_lock: dict[str, object]
    consumer_lock: dict[str, object]
    audit_report: dict[str, object]
    commit_sha: str
    git_source: str


def _single_dependency(lockfile: dict[str, object]) -> dict[str, object]:
    dependencies = lockfile["dependencies"]
    assert isinstance(dependencies, list)
    assert len(dependencies) == 1
    dependency = dependencies[0]
    assert isinstance(dependency, dict)
    return dependency


def _fingerprint(snapshot: ArtifactSnapshot, path: str) -> str:
    entries = [entry for entry in snapshot.entries if entry.relative_path == path]
    assert len(entries) == 1
    fingerprint = entries[0].fingerprint
    assert fingerprint is not None
    return fingerprint


def _author_matrix_source(factory: LocalPackageFactory) -> Path:
    source = factory.create("materialization-fixture", targets=("copilot",))
    factory.add_instruction(
        source,
        "shared",
        (
            "---\n"
            "applyTo: '**'\n"
            "description: Same-leaf materialization instruction\n"
            "---\n"
            "# Shared instruction\n"
        ),
    )
    factory.add_agent(
        source,
        "shared",
        ("---\nname: shared\ndescription: Same-leaf materialization agent\n---\n# Shared agent\n"),
    )
    factory.add_skill(
        source,
        "shared",
        ("---\nname: shared\ndescription: Same-leaf materialization skill\n---\n# Shared skill\n"),
    )
    factory.add_relative_link(
        source,
        PurePosixPath("skills/shared/references/checklist.md"),
        PurePosixPath("../SKILL.md"),
        label="skill",
    )
    factory.add_relative_link(
        source,
        PurePosixPath("skills/shared/README.md"),
        PurePosixPath("references/checklist.md"),
        label="checklist",
    )
    factory.add_skill(
        source,
        "companion",
        (
            "---\n"
            "name: companion\n"
            "description: Second selected matrix skill\n"
            "---\n"
            "# Companion skill\n"
        ),
    )
    factory.add_skill(
        source,
        "internal-only",
        ("---\nname: internal-only\ndescription: Excluded matrix skill\n---\n# Internal only\n"),
    )
    return source.root


def _run_matrix(root: Path, binary: Path) -> _MatrixReceipt:
    isolated = IsolatedApmEnvironment.create(root / "isolated", base_env=dict(os.environ))
    environment = isolated.subprocess_env()
    source_factory = LocalPackageFactory(isolated.package_root)
    source_root = _author_matrix_source(source_factory)
    source_before = ArtifactSnapshot.capture(source_root)

    repository_factory = LocalGitRepositoryFactory(
        isolated.repository_root,
        env=environment,
    )
    repository = repository_factory.create(
        "materialization-fixture",
        source_tree=source_root,
    )
    commit = repository_factory.commit(repository, message="seed materialization matrix")
    git_source = "git@gitlab.example.invalid:fixtures/materialization-fixture.git"
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
        "materialization-producer",
        dependencies=(
            {
                "git": git_source,
                "type": "gitlab",
                "ref": commit.sha,
                "alias": "materialization-fixture",
                "skills": list(_SELECTED_SKILLS),
            },
        ),
        targets=("copilot",),
    )
    producer_row = ScenarioRow(
        id="produce-materialization-matrix",
        source_inputs=(source_root, producer.manifest_path),
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
    source_after = ArtifactSnapshot.capture(source_root)
    producer_snapshot = ArtifactSnapshot.capture(producer.root)
    producer_manifest = load_yaml(producer.manifest_path)
    bundle_root = producer.root / "build" / f"{producer.name}-{producer_manifest['version']}"
    bundle_before = ArtifactSnapshot.capture(bundle_root)

    consumer = project_factory.create(
        "materialization-consumer",
        targets=("copilot",),
    )
    consumer_row = ScenarioRow(
        id="consume-materialization-matrix",
        source_inputs=(bundle_root, consumer.manifest_path),
        lifecycle_actions=(
            LifecycleAction(("install", str(bundle_root), "--target", "copilot")),
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

    bundle_after = ArtifactSnapshot.capture(bundle_root)
    consumer_snapshot = ArtifactSnapshot.capture(consumer.root)
    return _MatrixReceipt(
        isolated=isolated,
        binary=binary,
        environment=environment,
        source_root=source_root,
        producer_root=producer.root,
        bundle_root=bundle_root,
        consumer_root=consumer.root,
        source_before=source_before,
        source_after=source_after,
        producer_snapshot=producer_snapshot,
        bundle_before=bundle_before,
        bundle_after=bundle_after,
        consumer_snapshot=consumer_snapshot,
        producer_row=producer_row,
        producer_results=producer_results,
        consumer_row=consumer_row,
        consumer_results=consumer_results,
        producer_lock=load_yaml(producer.root / "apm.lock.yaml"),
        bundle_lock=load_yaml(bundle_root / "apm.lock.yaml"),
        consumer_lock=load_yaml(consumer.root / "apm.lock.yaml"),
        audit_report=json.loads(
            (consumer.root / "reports" / "audit.json").read_text(encoding="utf-8")
        ),
        commit_sha=commit.sha,
        git_source=git_source,
    )


def test_selected_matrix_preserves_bytes_hashes_and_ownership(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Selected supported shapes survive every genuine lifecycle boundary."""
    receipt = _run_matrix(tmp_path, apm_binary_path)
    assert tuple(result.returncode for result in receipt.producer_results) == (0, 0, 0)
    assert tuple(result.returncode for result in receipt.consumer_results) == (0, 0)
    assert_unchanged(receipt.source_before, receipt.source_after)
    assert_unchanged(receipt.bundle_before, receipt.bundle_after)

    selected = tuple(case for case in _MATRIX if case.selected)
    excluded = next(case for case in _MATRIX if not case.selected)
    assert len(_MATRIX) == 7
    assert len(selected) == 6
    assert_paths_present(
        receipt.source_before,
        {case.source_path for case in _MATRIX},
    )
    assert_paths_present(
        receipt.producer_snapshot,
        {case.producer_path for case in selected},
    )
    assert_paths_present(
        receipt.bundle_before,
        {case.bundle_path for case in selected},
    )
    assert_paths_present(
        receipt.consumer_snapshot,
        {case.consumer_path for case in selected},
    )
    assert_paths_absent(receipt.producer_snapshot, {excluded.producer_path})
    assert_paths_absent(receipt.bundle_before, {excluded.bundle_path})
    assert_paths_absent(receipt.consumer_snapshot, {excluded.consumer_path})

    for case in selected:
        source_bytes = (receipt.source_root / case.source_path).read_bytes()
        assert (
            source_bytes
            == (receipt.producer_root / case.producer_path).read_bytes()
            == (receipt.bundle_root / case.bundle_path).read_bytes()
            == (receipt.consumer_root / case.consumer_path).read_bytes()
        )
        expected_hash = hashlib.sha256(source_bytes).hexdigest()
        assert (
            _fingerprint(receipt.source_before, case.source_path)
            == _fingerprint(receipt.producer_snapshot, case.producer_path)
            == _fingerprint(receipt.bundle_before, case.bundle_path)
            == _fingerprint(receipt.consumer_snapshot, case.consumer_path)
            == expected_hash
        )

    compiled = receipt.producer_root / "AGENTS.md"
    assert compiled.is_file()
    assert "# Shared instruction" in compiled.read_text(encoding="utf-8")

    manifest = load_yaml(receipt.producer_row.source_inputs[1])
    source_entry = manifest["dependencies"]["apm"][0]
    assert source_entry == {
        "git": receipt.git_source,
        "type": "gitlab",
        "ref": receipt.commit_sha,
        "alias": "materialization-fixture",
        "skills": list(_SELECTED_SKILLS),
    }
    assert len(receipt.commit_sha) == 40
    assert all(character in "0123456789abcdef" for character in receipt.commit_sha)

    producer_dependency = _single_dependency(receipt.producer_lock)
    bundle_dependency = _single_dependency(receipt.bundle_lock)
    assert producer_dependency["resolved_commit"] == receipt.commit_sha
    assert producer_dependency["skill_subset"] == list(_SELECTED_SKILLS)
    assert bundle_dependency["skill_subset"] == list(_SELECTED_SKILLS)

    producer_hashes = producer_dependency["deployed_file_hashes"]
    assert isinstance(producer_hashes, dict)
    expected_deployed = {case.producer_path for case in selected}
    assert set(producer_hashes) == expected_deployed
    for case in selected:
        assert producer_hashes[case.producer_path] == (
            f"sha256:{_fingerprint(receipt.producer_snapshot, case.producer_path)}"
        )

    pack = receipt.bundle_lock["pack"]
    assert isinstance(pack, dict)
    bundle_hashes = pack["bundle_files"]
    assert isinstance(bundle_hashes, dict)
    expected_bundle_files = {case.bundle_path for case in selected} | {"plugin.json"}
    assert set(bundle_hashes) == expected_bundle_files
    assert bundle_hashes["plugin.json"] == _fingerprint(
        receipt.bundle_before,
        "plugin.json",
    )
    for case in selected:
        assert bundle_hashes[case.bundle_path] == _fingerprint(
            receipt.bundle_before,
            case.bundle_path,
        )

    assert set(receipt.consumer_lock["local_deployed_files"]) == expected_deployed
    consumer_hashes = receipt.consumer_lock["local_deployed_file_hashes"]
    assert isinstance(consumer_hashes, dict)
    assert set(consumer_hashes) == expected_deployed
    assert all("\\" not in path for path in (*producer_hashes, *bundle_hashes, *consumer_hashes))
    for case in selected:
        assert consumer_hashes[case.consumer_path] == (
            f"sha256:{_fingerprint(receipt.consumer_snapshot, case.consumer_path)}"
        )

    deployments = receipt.consumer_lock["deployments"]
    assert isinstance(deployments, list)
    owned_rows = {
        row["value"]: row
        for row in deployments
        if isinstance(row, dict) and row.get("value") in expected_deployed
    }
    assert set(owned_rows) == expected_deployed
    assert {row["active_owner"] for row in owned_rows.values()} == {"local-bundle"}

    assert receipt.audit_report["passed"] is True
    assert receipt.audit_report["summary"] == {
        "total": len(receipt.audit_report["checks"]),
        "passed": len(receipt.audit_report["checks"]),
        "failed": 0,
    }


def test_tampered_genuine_bundle_fails_closed_before_deployment(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """A hash mutation in genuine pack output must deploy no partial matrix."""
    receipt = _run_matrix(tmp_path, apm_binary_path)
    tampered_bundle = receipt.isolated.work_root / "tampered-materialization-bundle"
    shutil.copytree(receipt.bundle_root, tampered_bundle)
    mutated_path = tampered_bundle / "agents" / "shared.agent.md"
    mutated_path.write_bytes(b"# tampered agent\n")

    negative_factory = LocalPackageFactory(receipt.isolated.work_root / "negative")
    negative_consumer = negative_factory.create(
        "tamper-consumer",
        targets=("copilot",),
    )
    result = ApmLifecycleRunner((str(receipt.binary),)).run(
        ("install", str(tampered_bundle), "--target", "copilot"),
        scenario_id="reject-tampered-materialization-bundle",
        cwd=negative_consumer.root,
        env=receipt.environment,
    )

    assert result.returncode == 1
    assert "Hash mismatch for agents/shared.agent.md" in result.stderr
    negative_snapshot = ArtifactSnapshot.capture(negative_consumer.root)
    assert_paths_absent(
        negative_snapshot,
        {case.consumer_path for case in _MATRIX if case.selected},
    )
    assert not (negative_consumer.root / "apm.lock.yaml").exists()
