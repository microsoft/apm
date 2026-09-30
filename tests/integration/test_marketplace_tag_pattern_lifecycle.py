"""Installed-binary lifecycle contracts for marketplace tag-pattern propagation."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

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
    tags = subprocess.run(
        ("git", "--git-dir", str(scenario.marketplace_repository.origin), "tag", "--list"),
        env=scenario.environment,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    assert tags.stdout == ""
    return commit


def _new_scenario(
    root: Path,
    binary: Path,
    *,
    tag_pattern: str = _CUSTOM_PATTERN,
    catalog_port: int | None = None,
) -> _Scenario:
    isolated = IsolatedApmEnvironment.create(root, base_env=dict(os.environ))
    environment = isolated.subprocess_env()
    # Frozen binaries cannot import the Python fixture's network guard.
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        environment[key] = "http://127.0.0.1:9"
    environment.update(NO_PROXY="", no_proxy="", APM_NO_CACHE="1")
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
    catalog_authority = f"{_HOST}:{catalog_port}" if catalog_port else _HOST
    marketplace_remote = f"https://{catalog_authority}/{_OWNER}/{_MARKETPLACE}"
    repositories.install_url_rewrite(
        marketplace_repository,
        marketplace_remote,
    )
    (isolated.home / ".gitconfig").write_bytes(Path(environment["GIT_CONFIG_GLOBAL"]).read_bytes())

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


@pytest.mark.parametrize(
    "catalog_port", [None, 8443], ids=["package-only-tags", "foreign-catalog-port"]
)
def test_custom_pattern_closes_install_update_outdated_lifecycle(
    tmp_path: Path,
    apm_binary_path: Path,
    catalog_port: int | None,
) -> None:
    """Producer metadata must control every consumer semver lifecycle journey."""
    scenario = _new_scenario(
        tmp_path / "custom-pattern", apm_binary_path, catalog_port=catalog_port
    )
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
    (scenario.marketplace_repository.worktree / "decoy.txt").write_text(
        "Only the catalog publishes version 2.\n", encoding="utf-8"
    )
    catalog_commit = scenario.repositories.commit(
        scenario.marketplace_repository,
        message="catalog-only decoy",
    )
    scenario.repositories.tag(
        scenario.marketplace_repository,
        "releases/tagged-skill-v2.0.0",
        catalog_commit,
    )

    no_match_consumer = scenario.consumers.create(
        "no-match-consumer",
        targets=("copilot",),
    )
    _register_marketplace(scenario, no_match_consumer)
    _install_range(scenario, no_match_consumer, "^1.0.0", scenario_id="seed-before-failure")
    user_file = no_match_consumer.root / ".agents" / "skills" / "user-skill" / "SKILL.md"
    user_file.parent.mkdir(parents=True)
    user_file.write_text("User-owned skill must survive failed resolution.\n", encoding="utf-8")
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
    )
    malformed_output = " ".join((malformed.stdout + malformed.stderr).split())
    assert "contains 1 unsupported or malformed plugin entry" in malformed_output
    malformed_validation = _run(
        scenario,
        malformed_workspace,
        ("marketplace", "validate", _MARKETPLACE),
        "tag-pattern-malformed-validation",
        expected_returncode=1,
    )
    malformed_output = malformed_validation.stdout + malformed_validation.stderr
    assert "source.tag_pattern" in malformed_output
    assert "must contain exactly one {version} placeholder" in malformed_output
    _assert_same_state(
        before_malformed,
        LifecycleStateSnapshot.capture(malformed_workspace),
    )


@pytest.mark.parametrize("transport", ["https-raw-tag", "ssh-custom-port", "ssh-negative"])
def test_package_remote_transport_and_raw_tag(
    tmp_path: Path, apm_binary_path: Path, transport: str
) -> None:
    """Only the declared package transport can reach the real local Git origin."""
    scenario = _new_scenario(tmp_path / transport, apm_binary_path)
    scenario.repositories.tag(scenario.package_repository, "v1.0.1", scenario.initial_commit)
    requested = f"ssh://deploy@{_HOST}:2222/{_OWNER}/{_PACKAGE}.git"
    if transport != "https-raw-tag":
        catalog = scenario.marketplace_repository.worktree / ".claude-plugin" / "marketplace.json"
        manifest = json.loads(catalog.read_text(encoding="utf-8"))
        manifest["plugins"][0]["source"]["url"] = requested
        catalog.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        scenario.repositories.commit(scenario.marketplace_repository, message="publish SSH source")
        if transport == "ssh-custom-port":
            rewrite_key = f"url.{scenario.package_repository.file_url}/.insteadOf"
            subprocess.run(
                ("git", "config", "--global", "--unset-all", rewrite_key),
                env=scenario.environment,
                check=True,
                capture_output=True,
                timeout=30,
            )
            subprocess.run(
                ("git", "config", "--global", "--add", rewrite_key, requested),
                env=scenario.environment,
                check=True,
                capture_output=True,
                timeout=30,
            )
            (scenario.isolated.home / ".gitconfig").write_bytes(
                Path(scenario.environment["GIT_CONFIG_GLOBAL"]).read_bytes()
            )
    trace = scenario.isolated.root / "git.trace"
    scenario.environment["GIT_TRACE"] = str(trace)
    consumer = scenario.consumers.create("transport-consumer", targets=("copilot",))
    _register_marketplace(scenario, consumer)
    if transport == "https-raw-tag":
        _run(
            scenario,
            consumer.root,
            (
                "install",
                f"{_PACKAGE}@{_MARKETPLACE}#v1.0.1",
                "--target",
                "copilot",
                "--no-policy",
                "--parallel-downloads",
                "0",
            ),
            "package-only-cli-raw-tag",
        )
        expected_ref = "v1.0.1"
    else:
        _declare_range(scenario, consumer, "^1.0.0")
        before = _snapshot(consumer)
        _run_install(
            scenario,
            consumer,
            scenario_id=transport,
            expected_returncode=1 if transport == "ssh-negative" else 0,
        )
        if transport == "ssh-negative":
            _assert_same_state(before, _snapshot(consumer))
            assert not (consumer.root / _SKILL_PATH).exists()
            return
        expected_ref = "releases/tagged-skill-v1.0.0"
        tag_commands = [line for line in trace.read_text().splitlines() if "ls-remote" in line]
        urls = [
            urlparse(word.strip("'\""))
            for line in tag_commands
            for word in line.split()
            if word.strip("'\"").startswith("ssh://")
        ]
        assert any(
            (url.hostname, url.port, url.username) == (_HOST, 2222, "deploy") for url in urls
        )
    dependency = _locked_dependency(consumer)
    assert dependency["resolved_ref"] == expected_ref
    assert dependency["resolved_commit"] == scenario.initial_commit.sha
    assert (consumer.root / _SKILL_PATH).read_bytes() == _skill_document("1.0.0").encode()
    installed = _snapshot(consumer)
    _run_install(scenario, consumer, scenario_id=f"{transport}-replay")
    _assert_same_state(installed, _snapshot(consumer))
    if transport == "https-raw-tag":
        _run(
            scenario,
            consumer.root,
            (
                "install",
                "--global",
                f"{_PACKAGE}@{_MARKETPLACE}#v1.0.1",
                "--target",
                "copilot",
                "--no-policy",
                "--parallel-downloads",
                "0",
            ),
            "package-only-global",
        )
        _assert_same_state(installed, _snapshot(consumer))
        global_lock = load_yaml(Path(scenario.environment["APM_HOME"]) / "apm.lock.yaml")
        assert global_lock["dependencies"][0]["resolved_commit"] == scenario.initial_commit.sha
        assert (scenario.isolated.home / _SKILL_PATH).read_bytes() == _skill_document(
            "1.0.0"
        ).encode()


def test_catalog_local_dictionary_semver_lifecycle(tmp_path: Path, apm_binary_path: Path) -> None:
    """A dictionary pointing inside a custom-port catalog still uses catalog tags."""
    scenario = _new_scenario(tmp_path / "catalog-local", apm_binary_path, catalog_port=8443)
    package = scenario.packages.create("in-catalog", version="1.0.0", targets=("copilot",))
    scenario.packages.add_skill(package, _PACKAGE, _skill_document("1.0.0"))
    shutil.copytree(package.root, scenario.marketplace_repository.worktree / "plugins" / "pkg")
    catalog = scenario.marketplace_repository.worktree / ".claude-plugin" / "marketplace.json"
    manifest = json.loads(catalog.read_text(encoding="utf-8"))
    manifest["plugins"][0]["source"] = {
        "type": "git-subdir",
        "repo": f"{_OWNER}/{_MARKETPLACE}",
        "path": "plugins/pkg",
        "tag_pattern": _CUSTOM_PATTERN,
    }
    catalog.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    commit = scenario.repositories.commit(
        scenario.marketplace_repository, message="in-catalog package"
    )
    scenario.repositories.tag(
        scenario.marketplace_repository, "releases/tagged-skill-v1.0.0", commit
    )
    consumer = scenario.consumers.create("catalog-local-consumer", targets=("copilot",))
    _register_marketplace(scenario, consumer)
    _install_range(scenario, consumer, "^1.0.0", scenario_id="catalog-local-install")
    assert _locked_dependency(consumer)["resolved_commit"] == commit.sha
    assert (consumer.root / _SKILL_PATH).read_bytes() == _skill_document("1.0.0").encode()
    installed = _snapshot(consumer)
    _run_install(scenario, consumer, scenario_id="catalog-local-replay")
    _assert_same_state(installed, _snapshot(consumer))


@pytest.mark.parametrize(
    "source_case",
    ["same-project-https", "same-project-ssh", "invalid-port", "bare-git-subdir", "bare-gitlab"],
)
def test_dictionary_lookup_and_install_share_identity(
    tmp_path: Path, apm_binary_path: Path, source_case: str
) -> None:
    """Explicit authorities and bare-source defaults survive lookup, install and replay."""
    scenario = _new_scenario(tmp_path / source_case, apm_binary_path, catalog_port=8443)
    expected_commit = scenario.initial_commit
    expected_ref = "releases/tagged-skill-v1.0.0"
    if source_case.startswith("bare-"):
        nested = scenario.packages.create("nested-pkg", targets=("copilot",))
        scenario.packages.add_skill(nested, _PACKAGE, _skill_document("1.0.0"))
        shutil.copytree(nested.root, scenario.package_repository.worktree / "plugin")
        expected_commit = scenario.repositories.commit(
            scenario.package_repository, message="publish nested package"
        )
        expected_ref = "releases/tagged-skill-v1.0.1"
        scenario.repositories.tag(scenario.package_repository, expected_ref, expected_commit)
        locator = f"{_OWNER}/{_PACKAGE}"
        package_remote = f"https://github.com/{locator}"
        plugin_source = {
            "type": source_case.removeprefix("bare-"),
            "repo": locator,
            "path": "plugin",
        }
    else:
        authority = f"deploy@{_HOST}:2222" if source_case == "same-project-ssh" else _HOST
        scheme = "ssh" if source_case == "same-project-ssh" else "https"
        if source_case == "invalid-port":
            authority = f"{_HOST}:invalid"
        package_remote = f"{scheme}://{authority}/{_OWNER}/{_MARKETPLACE}"
        if scheme == "ssh":
            package_remote += ".git"
        plugin_source = {"type": "github", "repo": package_remote}
    if source_case != "invalid-port":
        if source_case == "same-project-ssh":
            subprocess.run(
                (
                    "git",
                    "config",
                    "--global",
                    "--add",
                    f"url.{scenario.package_repository.file_url}/.insteadOf",
                    package_remote,
                ),
                env=scenario.environment,
                check=True,
                capture_output=True,
                timeout=30,
            )
        else:
            scenario.repositories.install_url_rewrite(scenario.package_repository, package_remote)
        (scenario.isolated.home / ".gitconfig").write_bytes(
            Path(scenario.environment["GIT_CONFIG_GLOBAL"]).read_bytes()
        )
    catalog = scenario.marketplace_repository.worktree / ".claude-plugin" / "marketplace.json"
    manifest = json.loads(catalog.read_text(encoding="utf-8"))
    manifest["plugins"][0]["source"] = {**plugin_source, "tag_pattern": _CUSTOM_PATTERN}
    catalog.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    decoy = scenario.repositories.commit(
        scenario.marketplace_repository, message="dictionary source"
    )
    scenario.repositories.tag(
        scenario.marketplace_repository, "releases/tagged-skill-v2.0.0", decoy
    )
    consumer = scenario.consumers.create("dictionary-consumer", targets=("copilot",))
    _register_marketplace(scenario, consumer)
    trace = scenario.isolated.root / "dictionary-install.trace"
    trace.write_text("", encoding="utf-8")
    scenario.environment["GIT_TRACE"] = str(trace)
    _declare_range(scenario, consumer, "^1.0.0")
    before = _snapshot(consumer)
    result = _run_install(
        scenario,
        consumer,
        scenario_id=source_case,
        expected_returncode=1 if source_case == "invalid-port" else 0,
    )
    if source_case == "invalid-port":
        validation = _run(
            scenario,
            consumer.root,
            ("marketplace", "validate", _MARKETPLACE),
            "invalid-port-admission",
            expected_returncode=1,
        )
        output = " ".join((validation.stdout + validation.stderr).split())
        assert "source: github requires a valid non-local owner/repository field" in output
        assert "Failed to resolve marketplace dependency" in result.stdout + result.stderr
        version_queries = [
            line
            for line in trace.read_text().splitlines()
            if "ls-remote" in line and "--tags" in line
        ]
        assert version_queries == []
        _assert_same_state(before, _snapshot(consumer))
        assert not (consumer.root / _SKILL_PATH).exists()
        return
    locked = _locked_dependency(consumer)
    assert locked["resolved_ref"] == expected_ref
    assert locked["resolved_commit"] == expected_commit.sha
    assert (consumer.root / _SKILL_PATH).read_bytes() == _skill_document("1.0.0").encode()
    installed = _snapshot(consumer)
    _run_install(scenario, consumer, scenario_id=f"{source_case}-replay")
    _assert_same_state(installed, _snapshot(consumer))
    _declare_range(scenario, consumer, "^2.0.0")
    before_decoy = _snapshot(consumer)
    _run_install(scenario, consumer, scenario_id=f"{source_case}-decoy", expected_returncode=1)
    _assert_same_state(before_decoy, _snapshot(consumer))
