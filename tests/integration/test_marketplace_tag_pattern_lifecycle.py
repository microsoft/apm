"""Installed-binary lifecycle contracts for marketplace tag-pattern propagation."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.lifecycle_state import LifecycleStateSnapshot
from tests.utils.local_git_repository import (
    GitCommit,
    LocalGitRepository,
    LocalGitRepositoryFactory,
)
from tests.utils.local_marketplace import LocalMarketplace, LocalMarketplaceFactory
from tests.utils.local_package import LocalPackage, LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.lifecycle_merge_group,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
]

_OWNER = "apm-lifecycle"
_HOST = "git.example.invalid"
_PACKAGE = "tagged-skill"
_MARKETPLACE = "tag-pattern-marketplace"
_CUSTOM_PATTERN = "releases/{name}-v{version}"
_LEGACY_PATTERN = "{name}--v{version}"
_SKILL_PATH = ".agents/skills/tagged-skill/SKILL.md"


@dataclass(frozen=True)
class _Scenario:
    isolated: IsolatedApmEnvironment
    environment: dict[str, str]
    runner: ApmLifecycleRunner
    packages: LocalPackageFactory
    consumers: LocalPackageFactory
    repositories: LocalGitRepositoryFactory
    marketplace: LocalMarketplace
    package_repository: LocalGitRepository
    marketplace_repository: LocalGitRepository
    initial_commit: GitCommit
    package_remote: str
    marketplace_remote: str


def _skill_document(version: str) -> str:
    return (
        "---\n"
        f"name: {_PACKAGE}\n"
        f"description: Marketplace tag-pattern lifecycle fixture {version}\n"
        "---\n"
        f"# {_PACKAGE} {version}\n"
    )


def _evidence(result: CommandResult) -> str:
    return (
        f"cwd={result.cwd!s}\n"
        f"command={result.command!r}\n"
        f"returncode={result.returncode}\n"
        f"stdout={result.stdout!r}\n"
        f"stderr={result.stderr!r}"
    )


def _run(
    scenario: _Scenario,
    cwd: Path,
    args: tuple[str, ...],
    scenario_id: str,
    *,
    expected_returncode: int = 0,
) -> CommandResult:
    result = scenario.runner.run(
        args,
        scenario_id=scenario_id,
        cwd=cwd,
        env=scenario.environment,
    )
    assert result.returncode == expected_returncode, _evidence(result)
    return result


def _snapshot(consumer: LocalPackage) -> LifecycleStateSnapshot:
    return LifecycleStateSnapshot.capture(consumer.root, targets=("copilot",))


def _assert_same_state(
    expected: LifecycleStateSnapshot,
    actual: LifecycleStateSnapshot,
) -> None:
    assert actual.manifest_bytes == expected.manifest_bytes
    assert actual.lockfile_bytes == expected.lockfile_bytes
    assert actual.deployment_records == expected.deployment_records
    assert actual.files == expected.files
    assert actual.semantic_bytes == expected.semantic_bytes


def _locked_dependency(consumer: LocalPackage) -> dict[str, object]:
    lock = load_yaml(consumer.root / "apm.lock.yaml")
    dependencies = lock["dependencies"]
    assert isinstance(dependencies, list)
    assert len(dependencies) == 1
    dependency = dependencies[0]
    assert isinstance(dependency, dict)
    return dependency


def _pack_and_publish_marketplace(
    scenario: _Scenario,
    *,
    message: str,
    tag_name: str,
) -> GitCommit:
    result = _run(
        scenario,
        scenario.marketplace_repository.worktree,
        ("pack",),
        f"tag-pattern-{message}-pack",
    )
    assert result.stdout or result.stderr
    output = scenario.marketplace_repository.worktree / ".claude-plugin" / "marketplace.json"
    assert output.is_file()
    commit = scenario.repositories.commit(
        scenario.marketplace_repository,
        message=message,
    )
    scenario.repositories.tag(
        scenario.marketplace_repository,
        tag_name,
        commit,
    )
    return commit


def _new_scenario(
    root: Path,
    binary: Path,
    *,
    tag_pattern: str = _CUSTOM_PATTERN,
) -> _Scenario:
    isolated = IsolatedApmEnvironment.create(root, base_env=dict(os.environ))
    environment = isolated.subprocess_env()
    packages = LocalPackageFactory(isolated.package_root / "packages")
    consumers = LocalPackageFactory(isolated.work_root)
    repositories = LocalGitRepositoryFactory(
        isolated.repository_root,
        env=environment,
    )
    package = packages.create(_PACKAGE, version="1.0.0", targets=("copilot",))
    packages.add_skill(package, _PACKAGE, _skill_document("1.0.0"))
    package_repository = repositories.create(_PACKAGE, source_tree=package.root)
    initial_commit = repositories.commit(
        package_repository,
        message="seed tagged skill 1.0.0",
    )
    initial_tag = tag_pattern.replace("{name}", _PACKAGE).replace("{version}", "1.0.0")
    repositories.tag(package_repository, initial_tag, initial_commit)
    package_remote = f"https://{_HOST}/{_OWNER}/{_PACKAGE}"
    repositories.install_url_rewrite(package_repository, package_remote)

    marketplace_factory = LocalMarketplaceFactory(
        LocalPackageFactory(isolated.package_root / "marketplaces")
    )
    marketplace = marketplace_factory.create(
        _MARKETPLACE,
        source_base=f"https://{_HOST}/{_OWNER}",
        tag_pattern=tag_pattern,
        packages=(
            {
                "name": _PACKAGE,
                "description": "Tag-pattern lifecycle package",
                "source": _PACKAGE,
                "version": "^1.0.0",
            },
        ),
    )
    marketplace_repository = repositories.create(
        _MARKETPLACE,
        source_tree=marketplace.package.root,
    )
    marketplace_remote = f"https://{_HOST}/{_OWNER}/{_MARKETPLACE}"
    marketplace_remote_forms = repositories.install_url_rewrite(
        marketplace_repository,
        marketplace_remote,
    )
    environment = repositories.url_rewrite_subprocess_env(
        package_repository,
        package_remote,
    )
    rewrite_key = f"url.{marketplace_repository.file_url}/.insteadOf"
    rewrite_count = int(environment["GIT_CONFIG_COUNT"])
    for offset, remote_form in enumerate(marketplace_remote_forms):
        index = rewrite_count + offset
        environment[f"GIT_CONFIG_KEY_{index}"] = rewrite_key
        environment[f"GIT_CONFIG_VALUE_{index}"] = remote_form
    environment["GIT_CONFIG_COUNT"] = str(rewrite_count + len(marketplace_remote_forms))

    scenario = _Scenario(
        isolated=isolated,
        environment=environment,
        runner=ApmLifecycleRunner(
            (str(binary),),
            timeout_seconds=120,
            scenario_timeout_seconds=300,
        ),
        packages=packages,
        consumers=consumers,
        repositories=repositories,
        marketplace=marketplace,
        package_repository=package_repository,
        marketplace_repository=marketplace_repository,
        initial_commit=initial_commit,
        package_remote=package_remote,
        marketplace_remote=marketplace_remote,
    )
    _pack_and_publish_marketplace(
        scenario,
        message="publish marketplace 1.0.0",
        tag_name=initial_tag,
    )
    return scenario


def _register_marketplace(scenario: _Scenario, consumer: LocalPackage) -> None:
    _run(
        scenario,
        consumer.root,
        (
            "marketplace",
            "add",
            scenario.marketplace_remote,
            "--name",
            _MARKETPLACE,
            "--ref",
            "main",
        ),
        "tag-pattern-marketplace-add",
    )


def _install_range(
    scenario: _Scenario,
    consumer: LocalPackage,
    version_range: str,
    *,
    scenario_id: str,
    expected_returncode: int = 0,
) -> CommandResult:
    _declare_range(scenario, consumer, version_range)
    return _run_install(
        scenario,
        consumer,
        scenario_id=scenario_id,
        expected_returncode=expected_returncode,
    )


def _declare_range(
    scenario: _Scenario,
    consumer: LocalPackage,
    version_range: str,
) -> None:
    scenario.consumers.replace_apm_dependencies(
        consumer,
        (
            {
                "name": _PACKAGE,
                "marketplace": _MARKETPLACE,
                "version": version_range,
            },
        ),
    )


def _run_install(
    scenario: _Scenario,
    consumer: LocalPackage,
    *,
    scenario_id: str,
    expected_returncode: int = 0,
) -> CommandResult:
    return _run(
        scenario,
        consumer.root,
        (
            "install",
            "--target",
            "copilot",
            "--no-policy",
            "--parallel-downloads",
            "0",
            "--verbose",
        ),
        scenario_id,
        expected_returncode=expected_returncode,
    )


def test_custom_pattern_closes_install_update_outdated_lifecycle(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Producer metadata must control every consumer semver lifecycle journey."""
    scenario = _new_scenario(tmp_path / "custom-pattern", apm_binary_path)
    consumer = scenario.consumers.create("custom-consumer", targets=("copilot",))
    _register_marketplace(scenario, consumer)

    install = _install_range(
        scenario,
        consumer,
        "^1.0.0",
        scenario_id="tag-pattern-custom-install",
    )
    assert "legacy default" not in (install.stdout + install.stderr)
    first_lock = _locked_dependency(consumer)
    assert first_lock["resolved_ref"] == "releases/tagged-skill-v1.0.0"
    assert first_lock["resolved_commit"] == scenario.initial_commit.sha
    assert (consumer.root / _SKILL_PATH).read_bytes() == _skill_document("1.0.0").encode()
    first_snapshot = _snapshot(consumer)

    replay = _run(
        scenario,
        consumer.root,
        ("install", "--target", "copilot", "--no-policy", "--parallel-downloads", "0"),
        "tag-pattern-custom-install-replay",
    )
    assert replay.stdout or replay.stderr
    _assert_same_state(first_snapshot, _snapshot(consumer))

    repository_skill = scenario.package_repository.worktree / "skills" / _PACKAGE / "SKILL.md"
    repository_skill.write_text(_skill_document("1.1.0"), encoding="utf-8")
    package_manifest = load_yaml(scenario.package_repository.worktree / "apm.yml")
    package_manifest["version"] = "1.1.0"
    from apm_cli.utils.yaml_io import dump_yaml

    dump_yaml(package_manifest, scenario.package_repository.worktree / "apm.yml")
    next_commit = scenario.repositories.commit(
        scenario.package_repository,
        message="advance tagged skill 1.1.0",
    )
    scenario.repositories.tag(
        scenario.package_repository,
        "releases/tagged-skill-v1.1.0",
        next_commit,
    )
    _pack_and_publish_marketplace(
        scenario,
        message="publish marketplace 1.1.0",
        tag_name="releases/tagged-skill-v1.1.0",
    )
    _run(
        scenario,
        consumer.root,
        ("marketplace", "update", _MARKETPLACE),
        "tag-pattern-marketplace-refresh",
    )

    outdated = _run(
        scenario,
        consumer.root,
        ("outdated", "--parallel-checks", "0", "--verbose"),
        "tag-pattern-custom-outdated",
    )
    outdated_output = outdated.stdout + outdated.stderr
    assert "marketplace" in outdated_output.lower()
    assert "outdated" in outdated_output.lower()

    update = _run(
        scenario,
        consumer.root,
        (
            "update",
            "--yes",
            "--target",
            "copilot",
            "--parallel-downloads",
            "0",
            "--verbose",
        ),
        "tag-pattern-custom-update",
    )
    assert "1 updated" in (update.stdout + update.stderr)
    updated_lock = _locked_dependency(consumer)
    assert updated_lock["resolved_ref"] == "releases/tagged-skill-v1.1.0"
    assert updated_lock["resolved_commit"] == next_commit.sha
    assert (consumer.root / _SKILL_PATH).read_bytes() == _skill_document("1.1.0").encode()
    updated_snapshot = _snapshot(consumer)

    converged = _run(
        scenario,
        consumer.root,
        ("update", "--yes", "--target", "copilot", "--parallel-downloads", "0"),
        "tag-pattern-custom-update-converged",
    )
    assert "All dependencies already at their latest matching refs." in (
        converged.stdout + converged.stderr
    )
    _assert_same_state(updated_snapshot, _snapshot(consumer))

    final_outdated = _run(
        scenario,
        consumer.root,
        ("outdated", "--parallel-checks", "0"),
        "tag-pattern-custom-outdated-converged",
    )
    assert "All dependencies are up-to-date" in (final_outdated.stdout + final_outdated.stderr)


def test_old_marketplace_metadata_uses_legacy_pattern(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Metadata produced before tag_pattern existed must keep its old behavior."""
    scenario = _new_scenario(
        tmp_path / "legacy-pattern",
        apm_binary_path,
        tag_pattern=_LEGACY_PATTERN,
    )
    marketplace_path = (
        scenario.marketplace_repository.worktree / ".claude-plugin" / "marketplace.json"
    )
    marketplace = json.loads(marketplace_path.read_text(encoding="utf-8"))
    for plugin in marketplace["plugins"]:
        plugin["source"].pop("tag_pattern", None)
    marketplace_path.write_text(
        json.dumps(marketplace, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    scenario.repositories.commit(
        scenario.marketplace_repository,
        message="publish legacy marketplace metadata",
    )

    consumer = scenario.consumers.create("legacy-consumer", targets=("copilot",))
    _register_marketplace(scenario, consumer)
    install = _install_range(
        scenario,
        consumer,
        "^1.0.0",
        scenario_id="tag-pattern-legacy-install",
    )
    assert install.stdout or install.stderr
    dependency = _locked_dependency(consumer)
    assert dependency["resolved_ref"] == "tagged-skill--v1.0.0"
    assert dependency["resolved_commit"] == scenario.initial_commit.sha
    assert (consumer.root / _SKILL_PATH).read_bytes() == _skill_document("1.0.0").encode()


def test_invalid_or_unmatched_patterns_fail_without_consumer_writes(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Malformed metadata and ranges with no matching tag must fail closed."""
    scenario = _new_scenario(tmp_path / "failure-patterns", apm_binary_path)

    no_match_consumer = scenario.consumers.create(
        "no-match-consumer",
        targets=("copilot",),
    )
    _register_marketplace(scenario, no_match_consumer)
    _declare_range(scenario, no_match_consumer, "^2.0.0")
    before_no_match = _snapshot(no_match_consumer)
    no_match = _run_install(
        scenario,
        no_match_consumer,
        scenario_id="tag-pattern-no-match",
        expected_returncode=1,
    )
    no_match_output = no_match.stdout + no_match.stderr
    assert "No tag matching version '^2.0.0'" in no_match_output
    assert "releases/{name}-v{version}" in no_match_output
    _assert_same_state(before_no_match, _snapshot(no_match_consumer))

    bare_consumer = scenario.consumers.create(
        "bare-no-match-consumer",
        targets=("copilot",),
    )
    _register_marketplace(scenario, bare_consumer)
    _declare_range(scenario, bare_consumer, "2.0.0")
    before_bare = _snapshot(bare_consumer)
    bare_no_match = _run_install(
        scenario,
        bare_consumer,
        scenario_id="tag-pattern-bare-no-match",
        expected_returncode=1,
    )
    bare_output = bare_no_match.stdout + bare_no_match.stderr
    assert "No tag matching version '2.0.0'" in bare_output
    assert "releases/{name}-v{version}" in bare_output
    _assert_same_state(before_bare, _snapshot(bare_consumer))

    marketplace_path = (
        scenario.marketplace_repository.worktree / ".claude-plugin" / "marketplace.json"
    )
    marketplace = json.loads(marketplace_path.read_text(encoding="utf-8"))
    marketplace["plugins"][0]["source"]["tag_pattern"] = "release-{name}"
    marketplace_path.write_text(
        json.dumps(marketplace, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    scenario.repositories.commit(
        scenario.marketplace_repository,
        message="publish malformed marketplace metadata",
    )

    malformed_workspace = scenario.isolated.work_root / "malformed-registration"
    malformed_workspace.mkdir()
    before_malformed = LifecycleStateSnapshot.capture(malformed_workspace)
    malformed = _run(
        scenario,
        malformed_workspace,
        (
            "marketplace",
            "add",
            scenario.marketplace_remote,
            "--name",
            _MARKETPLACE,
            "--ref",
            "main",
        ),
        "tag-pattern-malformed",
        expected_returncode=1,
    )
    malformed_output = malformed.stdout + malformed.stderr
    assert "source.tag_pattern" in malformed_output
    assert "must contain exactly one {version} placeholder" in malformed_output
    _assert_same_state(
        before_malformed,
        LifecycleStateSnapshot.capture(malformed_workspace),
    )
