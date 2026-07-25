"""Real-binary lifecycle contracts across the supported package shapes."""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.lifecycle_state import LifecycleStateSnapshot
from tests.utils.local_git_repository import (
    GitCommit,
    LocalGitRepository,
    LocalGitRepositoryFactory,
)
from tests.utils.local_package import LocalPackage, LocalPackageFactory

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.requires_e2e_mode,
    pytest.mark.requires_apm_binary,
]

_AUDIT_ARGS = (
    "audit",
    "--ci",
    "--no-policy",
    "--format",
    "json",
    "--output",
    "reports/audit.json",
)
_TARGET_ARGS = ("--target", "copilot", "--no-policy")
_SKILL_BYTES = (
    b"---\nname: shape-skill\ndescription: Package-shape lifecycle skill\n---\n# Shape skill\n"
)
_INSTRUCTION_BYTES = (
    b"---\napplyTo: '**'\ndescription: Package-shape lifecycle instruction\n---\n"
    b"# Shape instruction\n"
)


@dataclass(frozen=True)
class _Case:
    """One product-supported package shape and its transition contract."""

    id: str
    execute: Callable[[_Fixture], None]


@dataclass(frozen=True)
class _Fixture:
    """One fully isolated command boundary with real local Git transport."""

    isolated: IsolatedApmEnvironment
    environment: dict[str, str]
    packages: LocalPackageFactory
    repositories: LocalGitRepositoryFactory
    runner: ApmLifecycleRunner


@dataclass(frozen=True)
class _RemotePackage:
    """A committed source package reachable through a production-shaped URL."""

    package: LocalPackage
    repository: LocalGitRepository
    commit: GitCommit
    remote_url: str


def _fixture(root: Path, binary: Path) -> _Fixture:
    """Build the isolated filesystem and process inputs for one row."""
    isolated = IsolatedApmEnvironment.create(root / "isolated", base_env=dict(os.environ))
    environment = isolated.subprocess_env()
    return _Fixture(
        isolated=isolated,
        environment=environment,
        packages=LocalPackageFactory(isolated.package_root),
        repositories=LocalGitRepositoryFactory(isolated.repository_root, env=environment),
        runner=ApmLifecycleRunner(
            (str(binary),),
            timeout_seconds=120,
            scenario_timeout_seconds=240,
        ),
    )


def _skill_document(name: str) -> str:
    """Return stable source text for a conventional skill."""
    return f"---\nname: {name}\ndescription: Package-shape lifecycle skill\n---\n# {name}\n"


def _instruction_document(name: str) -> str:
    """Return stable source text for a conventional instruction."""
    return f"---\napplyTo: '**'\ndescription: Package-shape lifecycle instruction\n---\n# {name}\n"


def _commit_package(
    fixture: _Fixture,
    package: LocalPackage,
    *,
    remote_url: str,
) -> _RemotePackage:
    """Publish one authored package to a rewritten production-shaped remote."""
    repository = fixture.repositories.create(package.name, source_tree=package.root)
    commit = fixture.repositories.commit(repository, message=f"seed {package.name}")
    fixture.repositories.install_url_rewrite(repository, remote_url)
    return _RemotePackage(
        package=package,
        repository=repository,
        commit=commit,
        remote_url=remote_url,
    )


def _commit_virtual_source(
    fixture: _Fixture,
    *,
    name: str,
    relative_path: str,
    content: bytes,
    remote_url: str,
) -> tuple[LocalGitRepository, GitCommit]:
    """Publish manifestless virtual source bytes through a real local Git remote."""
    source = fixture.isolated.package_root / name
    path = source / relative_path
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    repository = fixture.repositories.create(name, source_tree=source)
    commit = fixture.repositories.commit(repository, message=f"seed {name}")
    fixture.repositories.install_url_rewrite(repository, remote_url)
    return repository, commit


def _consumer(
    fixture: _Fixture,
    name: str,
    *,
    dependencies: tuple[dict[str, object], ...] = (),
) -> LocalPackage:
    """Author a regular consumer manifest with explicit copilot targeting."""
    factory = LocalPackageFactory(fixture.isolated.work_root)
    return factory.create(name, dependencies=dependencies, targets=("copilot",))


def _git_dependency(remote: _RemotePackage, *, path: str | None = None) -> dict[str, object]:
    """Return the source-only dependency form accepted by the public CLI."""
    dependency: dict[str, object] = {
        "git": remote.remote_url,
        "type": "gitlab",
        "ref": remote.commit.sha,
        "alias": remote.package.name,
    }
    if path is not None:
        dependency["path"] = path
    return dependency


def _virtual_dependency(
    remote_url: str,
    commit: GitCommit,
    *,
    path: str,
) -> dict[str, object]:
    """Return a manifestless remote subpath declaration without generated state."""
    return {
        "git": remote_url,
        "type": "gitlab",
        "path": path,
        "ref": commit.sha,
    }


def _run(
    fixture: _Fixture,
    project: Path,
    scenario_id: str,
    commands: tuple[tuple[str, ...], ...],
    *,
    expected_returncodes: tuple[int, ...] | None = None,
    env: Mapping[str, str] | None = None,
) -> None:
    """Run one bounded public-CLI transition chain with captured failure evidence."""
    fixture.runner.run_sequence(
        commands,
        expected_returncodes=expected_returncodes or (0,) * len(commands),
        scenario_id=scenario_id,
        cwd=project,
        env=env or fixture.environment,
    )


def _snapshot(project: Path) -> LifecycleStateSnapshot:
    """Capture exact lock, ledger, compiled, and deployed state for copilot."""
    return LifecycleStateSnapshot.capture(project, targets=("copilot",))


def _dependencies(project: Path) -> list[dict[str, object]]:
    """Load complete lock dependency records produced by the CLI."""
    lock = load_yaml(project / "apm.lock.yaml")
    dependencies = lock["dependencies"]
    assert isinstance(dependencies, list)
    assert all(isinstance(dependency, dict) for dependency in dependencies)
    return dependencies


def _assert_clean_audit(project: Path) -> None:
    """Assert the emitted public audit report proves complete durable consistency."""
    report = json.loads((project / "reports" / "audit.json").read_text(encoding="utf-8"))
    assert report["passed"] is True
    assert report["summary"] == {
        "total": len(report["checks"]),
        "passed": len(report["checks"]),
        "failed": 0,
    }


def _assert_single_remote_state(
    project: Path,
    remote: _RemotePackage,
    *,
    expected_skill: bytes,
    expected_instruction: bytes | None = None,
    virtual_path: str | None = None,
) -> LifecycleStateSnapshot:
    """Assert the exact remote identity, owner ledger, hashes, and deployed bytes."""
    snapshot = _snapshot(project)
    dependencies = _dependencies(project)
    assert len(dependencies) == 1
    dependency = dependencies[0]
    assert dependency["resolved_commit"] == remote.commit.sha
    assert dependency["resolved_ref"] == remote.commit.sha
    assert dependency["host"] == "gitlab.example.invalid"
    assert dependency["repo_url"] == f"shapes/{remote.package.name}"
    assert dependency.get("virtual_path") == virtual_path
    assert dependency.get("is_virtual", False) is (virtual_path is not None)
    deployed_files = dependency["deployed_files"]
    deployed_hashes = dependency["deployed_file_hashes"]
    assert isinstance(deployed_files, list)
    assert isinstance(deployed_hashes, dict)
    skill_path = project / ".agents" / "skills" / "shape-skill" / "SKILL.md"
    assert skill_path.read_bytes() == expected_skill
    assert snapshot.file(".agents/skills/shape-skill/SKILL.md").content == expected_skill
    expected_files = {
        ".agents/skills/shape-skill",
        ".agents/skills/shape-skill/SKILL.md",
    }
    expected_hashes = {
        ".agents/skills/shape-skill/SKILL.md": f"sha256:{sha256(expected_skill).hexdigest()}",
    }
    if expected_instruction is not None:
        instruction_path = project / ".github" / "instructions" / "shape.instructions.md"
        assert instruction_path.read_bytes() == expected_instruction
        assert (
            snapshot.file(".github/instructions/shape.instructions.md").content
            == expected_instruction
        )
        expected_files.add(".github/instructions/shape.instructions.md")
        expected_hashes[".github/instructions/shape.instructions.md"] = (
            f"sha256:{sha256(expected_instruction).hexdigest()}"
        )
    assert set(deployed_files) == expected_files
    assert deployed_hashes == expected_hashes
    owner = f"gitlab.example.invalid/shapes/{remote.package.name}"
    records = {record.locator.value: record for record in snapshot.deployment_records}
    assert set(records) == expected_files
    assert {record.active_owner for record in records.values()} == {owner}
    assert {record.owners for record in records.values()} == {(owner,)}
    return snapshot


def _regular_git_reinstall_audit(fixture: _Fixture) -> None:
    """Exercise regular Git install, compile, reinstall, and audit convergence."""
    source = fixture.packages.create("regular-shape", targets=("copilot",))
    fixture.packages.add_skill(source, "shape-skill", _skill_document("shape-skill"))
    fixture.packages.add_instruction(source, "shape", _instruction_document("shape"))
    remote = _commit_package(
        fixture,
        source,
        remote_url="https://gitlab.example.invalid/shapes/regular-shape.git",
    )
    consumer = _consumer(fixture, "regular-consumer", dependencies=(_git_dependency(remote),))
    manifest_bytes = consumer.manifest_path.read_bytes()
    _run(
        fixture,
        consumer.root,
        "regular-git-reinstall-audit",
        (
            ("install", *_TARGET_ARGS),
            ("compile", "--target", "copilot", "--force-instructions"),
        ),
    )
    before_reinstall = _assert_single_remote_state(
        consumer.root,
        remote,
        expected_skill=(source.root / "skills/shape-skill/SKILL.md").read_bytes(),
        expected_instruction=(source.root / ".apm/instructions/shape.instructions.md").read_bytes(),
    )
    _run(
        fixture,
        consumer.root,
        "regular-git-reinstall-audit-replay",
        (("install", *_TARGET_ARGS), _AUDIT_ARGS),
    )
    after_reinstall = _assert_single_remote_state(
        consumer.root,
        remote,
        expected_skill=(source.root / "skills/shape-skill/SKILL.md").read_bytes(),
        expected_instruction=(source.root / ".apm/instructions/shape.instructions.md").read_bytes(),
    )
    assert consumer.manifest_path.read_bytes() == manifest_bytes
    assert after_reinstall.semantic_bytes == before_reinstall.semantic_bytes
    assert (consumer.root / "AGENTS.md").is_file()
    _assert_clean_audit(consumer.root)


def _marketplace_plugin_normalize_reinstall(fixture: _Fixture) -> None:
    """Pack a local marketplace, then install and replay its normalized plugin source."""
    source = fixture.packages.create("shape-plugin", targets=("copilot",))
    fixture.packages.add_skill(source, "shape-plugin", _skill_document("shape-plugin"))
    remote = _commit_package(
        fixture,
        source,
        remote_url="https://gitlab.example.invalid/shapes/shape-plugin.git",
    )
    producer = _consumer(fixture, "shape-marketplace")
    producer_manifest = load_yaml(producer.manifest_path)
    producer_manifest["marketplace"] = {
        "owner": {"name": "APM Lifecycle Tests", "url": "https://example.invalid"},
        "sourceBase": "https://gitlab.example.invalid/shapes",
        "packages": [
            {
                "name": source.name,
                "description": "Normalized lifecycle plugin",
                "source": source.name,
                "ref": remote.commit.sha,
            }
        ],
    }
    dump_yaml(producer_manifest, producer.manifest_path)
    _run(fixture, producer.root, "marketplace-plugin-pack", (("pack",),))
    marketplace_path = producer.root / ".claude-plugin" / "marketplace.json"
    marketplace = json.loads(marketplace_path.read_text(encoding="utf-8"))
    plugins = [plugin for plugin in marketplace["plugins"] if plugin["name"] == source.name]
    assert len(plugins) == 1
    generated_source = plugins[0]["source"]
    assert generated_source["ref"] == remote.commit.sha
    assert generated_source["sha"] == remote.commit.sha

    consumer = _consumer(
        fixture,
        "marketplace-consumer",
        dependencies=(_git_dependency(remote),),
    )
    child_env = fixture.repositories.url_rewrite_subprocess_env(
        remote.repository, remote.remote_url
    )
    _run(
        fixture,
        consumer.root,
        "marketplace-plugin-normalize-reinstall",
        (
            ("marketplace", "add", str(producer.root), "--name", "shape-catalog"),
            ("install", *_TARGET_ARGS),
        ),
        env=child_env,
    )
    first = _snapshot(consumer.root)
    dependency = _dependencies(consumer.root)[0]
    assert dependency["resolved_commit"] == remote.commit.sha
    assert dependency["repo_url"] == "shapes/shape-plugin"
    assert generated_source["url"].endswith(dependency["repo_url"])
    assert (consumer.root / ".agents/skills/shape-plugin/SKILL.md").read_bytes() == (
        source.root / "skills/shape-plugin/SKILL.md"
    ).read_bytes()
    _run(
        fixture,
        consumer.root,
        "marketplace-plugin-normalize-reinstall-replay",
        (("install", *_TARGET_ARGS), _AUDIT_ARGS),
        env=child_env,
    )
    assert _snapshot(consumer.root).semantic_bytes == first.semantic_bytes
    _assert_clean_audit(consumer.root)


def _virtual_skill_frozen_rehydrate(fixture: _Fixture) -> None:
    """Prove a manifestless virtual skill survives a cache-free frozen replay."""
    remote_url = "https://gitlab.example.invalid/shapes/virtual-skill.git"
    repository, commit = _commit_virtual_source(
        fixture,
        name="virtual-skill",
        relative_path="skills/shape-skill/SKILL.md",
        content=_SKILL_BYTES,
        remote_url=remote_url,
    )
    consumer = _consumer(
        fixture,
        "virtual-skill-consumer",
        dependencies=(_virtual_dependency(remote_url, commit, path="skills/shape-skill"),),
    )
    child_env = fixture.repositories.url_rewrite_subprocess_env(repository, remote_url)
    manifest_bytes = consumer.manifest_path.read_bytes()
    _run(
        fixture,
        consumer.root,
        "virtual-skill-frozen-rehydrate",
        (("install", *_TARGET_ARGS),),
        env=child_env,
    )
    initial = _snapshot(consumer.root)
    dependency = _dependencies(consumer.root)[0]
    assert dependency["is_virtual"] is True
    assert dependency["virtual_path"] == "skills/shape-skill"
    assert dependency["resolved_commit"] == commit.sha
    assert dependency["package_type"] == "claude_skill"
    assert (consumer.root / ".agents/skills/shape-skill/SKILL.md").read_bytes() == _SKILL_BYTES
    shutil.rmtree(consumer.root / "apm_modules")
    _run(
        fixture,
        consumer.root,
        "virtual-skill-frozen-rehydrate-replay",
        (("install", *_TARGET_ARGS, "--frozen"), _AUDIT_ARGS),
        env=child_env,
    )
    assert repository.origin.is_dir()
    assert consumer.manifest_path.read_bytes() == manifest_bytes
    assert _snapshot(consumer.root).semantic_bytes == initial.semantic_bytes
    _assert_clean_audit(consumer.root)


def _virtual_file_subdirectory_frozen_rehydrate(fixture: _Fixture) -> None:
    """Prove a manifestless virtual instruction file survives a frozen replay."""
    remote_url = "https://gitlab.example.invalid/shapes/virtual-file.git"
    _repository, commit = _commit_virtual_source(
        fixture,
        name="virtual-file",
        relative_path="instructions/shape/guard.instructions.md",
        content=_INSTRUCTION_BYTES,
        remote_url=remote_url,
    )
    consumer = _consumer(
        fixture,
        "virtual-file-consumer",
        dependencies=(
            _virtual_dependency(
                remote_url,
                commit,
                path="instructions/shape/guard.instructions.md",
            ),
        ),
    )
    child_env = fixture.repositories.url_rewrite_subprocess_env(_repository, remote_url)
    manifest_bytes = consumer.manifest_path.read_bytes()
    _run(
        fixture,
        consumer.root,
        "virtual-file-subdirectory-frozen-rehydrate",
        (("install", *_TARGET_ARGS),),
        env=child_env,
    )
    initial = _snapshot(consumer.root)
    dependency = _dependencies(consumer.root)[0]
    assert dependency["is_virtual"] is True
    assert dependency["virtual_path"] == "instructions/shape/guard.instructions.md"
    assert dependency["resolved_commit"] == commit.sha
    instruction = consumer.root / ".github/instructions/guard.instructions.md"
    assert instruction.read_bytes() == _INSTRUCTION_BYTES
    shutil.rmtree(consumer.root / "apm_modules")
    _run(
        fixture,
        consumer.root,
        "virtual-file-subdirectory-frozen-rehydrate-replay",
        (("install", *_TARGET_ARGS, "--frozen"), _AUDIT_ARGS),
        env=child_env,
    )
    assert consumer.manifest_path.read_bytes() == manifest_bytes
    assert instruction.read_bytes() == _INSTRUCTION_BYTES
    assert _snapshot(consumer.root).semantic_bytes == initial.semantic_bytes
    _assert_clean_audit(consumer.root)


def _packed_local_bundle_consumer(fixture: _Fixture) -> None:
    """Pack a real installed package and consume that exact local bundle elsewhere."""
    source = fixture.packages.create("packed-shape", targets=("copilot",))
    fixture.packages.add_skill(source, "shape-skill", _skill_document("shape-skill"))
    fixture.packages.add_instruction(source, "shape", _instruction_document("shape"))
    remote = _commit_package(
        fixture,
        source,
        remote_url="https://gitlab.example.invalid/shapes/packed-shape.git",
    )
    producer = _consumer(fixture, "bundle-producer", dependencies=(_git_dependency(remote),))
    _run(
        fixture,
        producer.root,
        "packed-local-bundle-producer",
        (
            ("install", *_TARGET_ARGS),
            ("compile", "--target", "copilot", "--force-instructions"),
            ("pack", "--format", "plugin", "--offline"),
        ),
    )
    bundle = producer.root / "build" / f"{producer.name}-0.1.0"
    bundle_snapshot = _snapshot(bundle)
    consumer = _consumer(fixture, "bundle-consumer")
    manifest_bytes = consumer.manifest_path.read_bytes()
    _run(
        fixture,
        consumer.root,
        "packed-local-bundle-consumer",
        (
            ("install", str(bundle), *_TARGET_ARGS),
            ("install", str(bundle), *_TARGET_ARGS),
            _AUDIT_ARGS,
        ),
    )
    consumer_snapshot = _snapshot(consumer.root)
    assert _snapshot(bundle).semantic_bytes == bundle_snapshot.semantic_bytes
    assert consumer.manifest_path.read_bytes() == manifest_bytes
    lock = load_yaml(consumer.root / "apm.lock.yaml")
    assert set(lock["local_deployed_file_hashes"]) == set(lock["local_deployed_files"])
    assert all(
        record.active_owner == "local-bundle" for record in consumer_snapshot.deployment_records
    )
    assert (consumer.root / ".agents/skills/shape-skill/SKILL.md").read_bytes() == (
        source.root / "skills/shape-skill/SKILL.md"
    ).read_bytes()
    assert (consumer.root / ".github/instructions/shape.instructions.md").read_bytes() == (
        source.root / ".apm/instructions/shape.instructions.md"
    ).read_bytes()
    _assert_clean_audit(consumer.root)


def _mixed_transitive_regular_virtual_uninstall(fixture: _Fixture) -> None:
    """Install a regular root plus virtual child, then prove removal converges."""
    virtual_url = "https://gitlab.example.invalid/shapes/transitive-virtual.git"
    _repository, virtual_commit = _commit_virtual_source(
        fixture,
        name="transitive-virtual",
        relative_path="skills/shape-skill/SKILL.md",
        content=_SKILL_BYTES,
        remote_url=virtual_url,
    )
    root = fixture.packages.create(
        "transitive-root",
        dependencies=(_virtual_dependency(virtual_url, virtual_commit, path="skills/shape-skill"),),
        targets=("copilot",),
    )
    fixture.packages.add_instruction(root, "shape", _instruction_document("shape"))
    remote = _commit_package(
        fixture,
        root,
        remote_url="https://gitlab.example.invalid/shapes/transitive-root.git",
    )
    consumer = _consumer(
        fixture,
        "mixed-transitive-consumer",
        dependencies=(_git_dependency(remote),),
    )
    child_env = fixture.repositories.url_rewrite_subprocess_env(_repository, virtual_url)
    _run(
        fixture,
        consumer.root,
        "mixed-transitive-regular-virtual-install",
        (
            ("install", *_TARGET_ARGS),
            ("compile", "--target", "copilot", "--force-instructions"),
        ),
        env=child_env,
    )
    installed = _snapshot(consumer.root)
    dependencies = _dependencies(consumer.root)
    assert len(dependencies) == 2
    root_dependency = next(
        dep for dep in dependencies if dep["repo_url"] == "shapes/transitive-root"
    )
    virtual_dependency = next(dep for dep in dependencies if dep.get("is_virtual") is True)
    assert root_dependency.get("depth", 1) == 1
    assert virtual_dependency["resolved_commit"] == virtual_commit.sha
    assert installed.deployment_records
    _run(
        fixture,
        consumer.root,
        "mixed-transitive-regular-virtual-uninstall",
        (("uninstall", remote.remote_url), ("prune",), _AUDIT_ARGS),
        env=child_env,
    )
    remaining = _snapshot(consumer.root)
    if (consumer.root / "apm.lock.yaml").exists():
        assert _dependencies(consumer.root) == []
    assert remaining.deployment_records == ()
    assert not (consumer.root / ".agents/skills/shape-skill").exists()
    assert not (consumer.root / ".github/instructions/shape.instructions.md").exists()
    _assert_clean_audit(consumer.root)


_CASES = (
    _Case("regular-git-reinstall-audit", _regular_git_reinstall_audit),
    _Case("marketplace-plugin-normalize-reinstall", _marketplace_plugin_normalize_reinstall),
    _Case("virtual-skill-frozen-rehydrate", _virtual_skill_frozen_rehydrate),
    _Case(
        "virtual-file-subdirectory-frozen-rehydrate", _virtual_file_subdirectory_frozen_rehydrate
    ),
    _Case("packed-local-bundle-consumer", _packed_local_bundle_consumer),
    _Case(
        "mixed-transitive-regular-virtual-uninstall", _mixed_transitive_regular_virtual_uninstall
    ),
)


@pytest.mark.parametrize("case", _CASES, ids=lambda case: case.id)
def test_package_shape_lifecycle_contract(
    tmp_path: Path,
    apm_binary_path: Path,
    case: _Case,
) -> None:
    """Cover each supported shape through a real evolving workspace transition."""
    case.execute(_fixture(tmp_path / case.id, apm_binary_path))


def test_tampered_local_bundle_fails_before_consumer_state_writes(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """A packed bundle with altered selected bytes must not partially deploy."""
    fixture = _fixture(tmp_path, apm_binary_path)
    source = fixture.packages.create("tamper-source", targets=("copilot",))
    fixture.packages.add_skill(source, "shape-skill", _skill_document("shape-skill"))
    remote = _commit_package(
        fixture,
        source,
        remote_url="https://gitlab.example.invalid/shapes/tamper-source.git",
    )
    producer = _consumer(fixture, "tamper-producer", dependencies=(_git_dependency(remote),))
    _run(
        fixture,
        producer.root,
        "tampered-local-bundle-producer",
        (("install", *_TARGET_ARGS), ("pack", "--format", "plugin", "--offline")),
    )
    bundle = producer.root / "build" / f"{producer.name}-0.1.0"
    tampered = fixture.isolated.work_root / "tampered-bundle"
    shutil.copytree(bundle, tampered)
    (tampered / "skills/shape-skill/SKILL.md").write_bytes(b"# altered bundle payload\n")
    consumer = _consumer(fixture, "tamper-consumer")
    before = _snapshot(consumer.root)
    result = fixture.runner.run(
        ("install", str(tampered), *_TARGET_ARGS),
        scenario_id="tampered-local-bundle-consumer",
        cwd=consumer.root,
        env=fixture.environment,
    )
    assert result.returncode != 0
    failure_output = result.stdout + result.stderr
    assert "Bundle integrity check failed" in failure_output
    assert "Hash mismatch for skills/shape-skill/SKILL.md" in failure_output
    after = _snapshot(consumer.root)
    assert after.semantic_bytes == before.semantic_bytes
    assert after.deployment_records == ()
    assert not (consumer.root / ".agents/skills/shape-skill").exists()
