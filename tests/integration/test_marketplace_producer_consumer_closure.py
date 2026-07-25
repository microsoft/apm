"""Real-binary producer-to-consumer closure contracts for marketplace sources."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.artifact_snapshot import ArtifactSnapshot, assert_paths_present, assert_unchanged
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import GitCommit, LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
]

_MARKETPLACE_NAME = "closure-marketplace"
_MARKETPLACE_GIT_HOST = "git.example.invalid"
_MARKETPLACE_GIT_REPO = "catalog/closure-marketplace"
_MARKETPLACE_GIT_URL = f"https://{_MARKETPLACE_GIT_HOST}/{_MARKETPLACE_GIT_REPO}"
_CROSS_HOST = "github.enterprise.example.invalid"
_CROSS_REPO = "acme/cross-host-package"
_CROSS_PACKAGE = "cross-host-package"
_GITLAB_HOST = "gitlab.example.invalid"
_GITLAB_REPO = "team/platform/closure-monorepo"
_GITLAB_PACKAGE = "gitlab-subdir-package"
_GITLAB_SUBDIR = f"packages/{_GITLAB_PACKAGE}"
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
class _ClosureCase:
    """Expected producer output and consumer state for one package shape."""

    package_name: str
    source_type: str
    host: str
    repo_url: str
    virtual_path: str | None


@dataclass(frozen=True)
class _ClosureFixture:
    """Hermetic repositories, producer output, and command environment."""

    isolated: IsolatedApmEnvironment
    environment: dict[str, str]
    runner: ApmLifecycleRunner
    producer_root: Path
    marketplace_path: Path
    remote_marketplace_url: str
    commits: dict[str, GitCommit]


@dataclass(frozen=True)
class _UnsafeMutation:
    """One tampered producer artifact that must fail before project writes."""

    id: str
    package_name: str
    field: str
    value: str
    expected_error: str


_CASES = (
    _ClosureCase(
        package_name=_CROSS_PACKAGE,
        source_type="url",
        host=_CROSS_HOST,
        repo_url=_CROSS_REPO,
        virtual_path=None,
    ),
    _ClosureCase(
        package_name=_GITLAB_PACKAGE,
        source_type="git-subdir",
        host=_GITLAB_HOST,
        repo_url=_GITLAB_REPO,
        virtual_path=_GITLAB_SUBDIR,
    ),
)
_UNSAFE_MUTATIONS = (
    _UnsafeMutation(
        id="url-local-path",
        package_name=_CROSS_PACKAGE,
        field="url",
        value="../local-package",
        expected_error="local path",
    ),
    _UnsafeMutation(
        id="git-subdir-local-path",
        package_name=_GITLAB_PACKAGE,
        field="url",
        value="~/local-monorepo",
        expected_error="local path",
    ),
    _UnsafeMutation(
        id="git-subdir-traversal",
        package_name=_GITLAB_PACKAGE,
        field="path",
        value="../escape",
        expected_error="traversal",
    ),
)


def _skill_document(name: str) -> str:
    """Return one valid skill with stable content for artifact hashing."""
    return (
        "---\n"
        f"name: {name}\n"
        f"description: Marketplace closure contract skill {name}\n"
        "---\n"
        f"# {name}\n"
    )


def _configure_local_rewrite(
    remote_url: str,
    file_url: str,
    *,
    environment: dict[str, str],
) -> None:
    """Route one production-shaped HTTPS remote to a local bare repository."""
    for candidate in (f"{remote_url}.git", remote_url):
        commands = (
            (
                "git",
                "config",
                "--global",
                "--add",
                f"url.{file_url}.insteadOf",
                candidate,
            ),
            (
                "git",
                "config",
                "--file",
                str(Path(environment["HOME"]) / ".gitconfig"),
                "--add",
                f"url.{file_url}.insteadOf",
                candidate,
            ),
        )
        for command in commands:
            subprocess.run(
                command,
                env=environment,
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )


def _create_closure_fixture(
    root: Path,
    binary: Path,
) -> _ClosureFixture:
    """Pack two remote package shapes using only local Git transports."""
    isolated = IsolatedApmEnvironment.create(root / "isolated", base_env=dict(os.environ))
    environment = isolated.subprocess_env()
    repositories = LocalGitRepositoryFactory(isolated.repository_root, env=environment)

    package_factory = LocalPackageFactory(isolated.package_root)
    cross_package = package_factory.create(_CROSS_PACKAGE, targets=("copilot",))
    package_factory.add_skill(
        cross_package,
        _CROSS_PACKAGE,
        _skill_document(_CROSS_PACKAGE),
    )
    cross_repository = repositories.create(
        _CROSS_PACKAGE,
        source_tree=cross_package.root,
    )
    cross_commit = repositories.commit(cross_repository, message="seed cross-host package")
    repositories.tag(
        cross_repository,
        f"{_CROSS_PACKAGE}--v1.0.0",
        cross_commit,
    )
    _configure_local_rewrite(
        f"https://{_CROSS_HOST}/{_CROSS_REPO}",
        cross_repository.file_url,
        environment=environment,
    )

    monorepo_root = isolated.package_root / "closure-monorepo"
    monorepo_factory = LocalPackageFactory(monorepo_root / "packages")
    gitlab_package = monorepo_factory.create(_GITLAB_PACKAGE, targets=("copilot",))
    monorepo_factory.add_skill(
        gitlab_package,
        _GITLAB_PACKAGE,
        _skill_document(_GITLAB_PACKAGE),
    )
    gitlab_repository = repositories.create(
        "closure-monorepo",
        source_tree=monorepo_root,
    )
    gitlab_commit = repositories.commit(
        gitlab_repository,
        message="seed GitLab monorepo package",
    )
    repositories.tag(
        gitlab_repository,
        f"{_GITLAB_PACKAGE}--v1.0.0",
        gitlab_commit,
    )
    _configure_local_rewrite(
        f"https://{_GITLAB_HOST}/{_GITLAB_REPO}",
        gitlab_repository.file_url,
        environment=environment,
    )

    project_factory = LocalPackageFactory(isolated.work_root)
    producer = project_factory.create(_MARKETPLACE_NAME)
    producer_manifest = load_yaml(producer.manifest_path)
    producer_manifest["marketplace"] = {
        "owner": {
            "name": "APM Closure Tests",
            "url": "https://github.com/apm-closure-tests",
        },
        "sourceBase": f"https://{_GITLAB_HOST}/team/platform",
        "packages": [
            {
                "name": _CROSS_PACKAGE,
                "description": "Cross-host package",
                "source": f"{_CROSS_HOST}/{_CROSS_REPO}",
                "ref": cross_commit.sha,
            },
            {
                "name": _GITLAB_PACKAGE,
                "description": "Self-hosted GitLab monorepo package",
                "source": "closure-monorepo",
                "subdir": _GITLAB_SUBDIR,
                "ref": gitlab_commit.sha,
            },
        ],
    }
    dump_yaml(producer_manifest, producer.manifest_path)

    runner = ApmLifecycleRunner(
        (str(binary),),
        timeout_seconds=120,
        scenario_timeout_seconds=300,
    )
    pack = runner.run(
        ("pack",),
        scenario_id="marketplace-closure-pack",
        cwd=producer.root,
        env=environment,
    )
    assert pack.returncode == 0, _command_evidence(pack)
    marketplace_path = producer.root / ".claude-plugin" / "marketplace.json"
    assert marketplace_path.is_file()

    marketplace_repository = repositories.create(
        _MARKETPLACE_NAME,
        source_tree=producer.root,
    )
    marketplace_commit = repositories.commit(
        marketplace_repository,
        message="seed packed marketplace",
    )
    for case in _CASES:
        # Marketplace version selectors resolve catalog tags and apply the
        # selected tag name to the package source, so publish it on both repos.
        repositories.tag(
            marketplace_repository,
            f"{case.package_name}--v1.0.0",
            marketplace_commit,
        )
    _configure_local_rewrite(
        _MARKETPLACE_GIT_URL,
        marketplace_repository.file_url,
        environment=environment,
    )

    return _ClosureFixture(
        isolated=isolated,
        environment=environment,
        runner=runner,
        producer_root=producer.root,
        marketplace_path=marketplace_path,
        remote_marketplace_url=_MARKETPLACE_GIT_URL,
        commits={
            _CROSS_PACKAGE: cross_commit,
            _GITLAB_PACKAGE: gitlab_commit,
        },
    )


def _command_evidence(result: CommandResult) -> str:
    """Render stable command evidence for direct return-code assertions."""
    return (
        f"cwd={str(result.cwd)!r}\n"
        f"command={result.command!r}\n"
        f"returncode={result.returncode}\n"
        f"stdout={result.stdout!r}\n"
        f"stderr={result.stderr!r}"
    )


def _plugin_source(fixture: _ClosureFixture, package_name: str) -> dict[str, object]:
    """Return one generated source object from the real pack output."""
    marketplace = json.loads(fixture.marketplace_path.read_text(encoding="utf-8"))
    matches = [
        plugin["source"] for plugin in marketplace["plugins"] if plugin["name"] == package_name
    ]
    assert len(matches) == 1
    source = matches[0]
    assert isinstance(source, dict)
    return source


def _assert_generated_source(
    fixture: _ClosureFixture,
    case: _ClosureCase,
) -> None:
    """Assert host, path, and ref in the genuine producer artifact."""
    source = _plugin_source(fixture, case.package_name)
    commit = fixture.commits[case.package_name]
    assert source["source"] == case.source_type
    assert source["ref"] == commit.sha
    assert source["sha"] == commit.sha
    parsed = urlparse(str(source["url"]))
    assert (parsed.scheme, parsed.hostname, parsed.path) == (
        "https",
        case.host,
        f"/{case.repo_url}",
    )
    if case.virtual_path is None:
        assert "path" not in source
    else:
        assert source["path"] == case.virtual_path


def _assert_lock_contract(
    fixture: _ClosureFixture,
    consumer_root: Path,
    case: _ClosureCase,
) -> None:
    """Assert a portable lock snapshot for package identity and provenance."""
    lock = load_yaml(consumer_root / "apm.lock.yaml")
    dependencies = lock["dependencies"]
    assert isinstance(dependencies, list)
    assert len(dependencies) == 1
    dependency = dependencies[0]
    assert {
        "repo_url": dependency["repo_url"],
        "host": dependency["host"],
        "resolved_commit": dependency["resolved_commit"],
        "resolved_ref": dependency["resolved_ref"],
        "virtual_path": dependency.get("virtual_path"),
        "is_virtual": dependency.get("is_virtual", False),
        "discovered_via": dependency["discovered_via"],
        "marketplace_plugin_name": dependency["marketplace_plugin_name"],
    } == {
        "repo_url": case.repo_url,
        "host": case.host,
        "resolved_commit": fixture.commits[case.package_name].sha,
        "resolved_ref": fixture.commits[case.package_name].sha,
        "virtual_path": case.virtual_path,
        "is_virtual": case.virtual_path is not None,
        "discovered_via": _MARKETPLACE_NAME,
        "marketplace_plugin_name": case.package_name,
    }


def test_every_pack_emitted_remote_source_installs_and_audits(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Every remote source shape emitted by pack must be accepted by install."""
    fixture = _create_closure_fixture(tmp_path, apm_binary_path)
    for case in _CASES:
        _assert_generated_source(fixture, case)
        consumer = LocalPackageFactory(fixture.isolated.work_root).create(
            f"consumer-{case.source_type}",
            targets=("copilot",),
        )

        add, install, audit = fixture.runner.run_sequence(
            (
                (
                    "marketplace",
                    "add",
                    str(fixture.producer_root),
                    "--name",
                    _MARKETPLACE_NAME,
                ),
                (
                    "install",
                    f"{case.package_name}@{_MARKETPLACE_NAME}",
                    "--target",
                    "copilot",
                    "--no-policy",
                    "--verbose",
                ),
                _AUDIT_ARGS,
            ),
            expected_returncodes=(0, 0, 0),
            scenario_id=f"marketplace-closure-{case.source_type}",
            cwd=consumer.root,
            env=fixture.environment,
        )
        assert add.stdout
        assert install.stdout
        assert audit.stdout or audit.stderr

        snapshot = ArtifactSnapshot.capture(consumer.root)
        assert_paths_present(
            snapshot,
            {
                "apm.yml",
                "apm.lock.yaml",
                f".agents/skills/{case.package_name}/SKILL.md",
                "reports/audit.json",
            },
        )
        _assert_lock_contract(fixture, consumer.root, case)
        audit_report = json.loads((consumer.root / "reports" / "audit.json").read_text())
        assert audit_report["passed"] is True
        assert audit_report["summary"]["failed"] == 0


def test_pack_emitted_remote_sources_resolve_marketplace_version_constraint(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Bare marketplace versions resolve to package tags before install."""
    fixture = _create_closure_fixture(tmp_path, apm_binary_path)
    marketplace_name = "closure-versioned"

    for case in _CASES:
        consumer = LocalPackageFactory(fixture.isolated.work_root).create(
            f"versioned-{case.source_type}",
            targets=("copilot",),
        )
        fixture.runner.run_sequence(
            (
                (
                    "marketplace",
                    "add",
                    fixture.remote_marketplace_url,
                    "--name",
                    marketplace_name,
                    "--ref",
                    "main",
                ),
                (
                    "install",
                    f"{case.package_name}@{marketplace_name}#1.0.0",
                    "--target",
                    "copilot",
                    "--no-policy",
                ),
                _AUDIT_ARGS,
            ),
            expected_returncodes=(0, 0, 0),
            scenario_id=f"marketplace-version-{case.source_type}",
            cwd=consumer.root,
            env=fixture.environment,
        )
        lock = load_yaml(consumer.root / "apm.lock.yaml")
        dependency = lock["dependencies"][0]
        assert dependency["resolved_ref"] == f"{case.package_name}--v1.0.0"
        assert dependency["resolved_commit"] == fixture.commits[case.package_name].sha


def test_tampered_pack_output_fails_closed_before_project_writes(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Unsafe URL and subdirectory twins fail without lock or deployment writes."""
    fixture = _create_closure_fixture(tmp_path, apm_binary_path)
    original_marketplace = json.loads(fixture.marketplace_path.read_text(encoding="utf-8"))
    for mutation in _UNSAFE_MUTATIONS:
        marketplace = json.loads(json.dumps(original_marketplace))
        for plugin in marketplace["plugins"]:
            if plugin["name"] == mutation.package_name:
                plugin["source"][mutation.field] = mutation.value
        fixture.marketplace_path.write_text(
            json.dumps(marketplace, indent=2) + "\n",
            encoding="utf-8",
        )

        consumer = LocalPackageFactory(fixture.isolated.work_root).create(
            f"negative-{mutation.id}",
            targets=("copilot",),
        )
        add = fixture.runner.run(
            (
                "marketplace",
                "add",
                str(fixture.producer_root),
                "--name",
                _MARKETPLACE_NAME,
            ),
            scenario_id=f"marketplace-negative-add-{mutation.id}",
            cwd=consumer.root,
            env=fixture.environment,
        )
        assert add.returncode == 0, _command_evidence(add)
        before_install = ArtifactSnapshot.capture(consumer.root)

        install = fixture.runner.run(
            (
                "install",
                f"{mutation.package_name}@{_MARKETPLACE_NAME}",
                "--target",
                "copilot",
                "--no-policy",
            ),
            scenario_id=f"marketplace-negative-install-{mutation.id}",
            cwd=consumer.root,
            env=fixture.environment,
        )

        assert install.returncode != 0, _command_evidence(install)
        assert mutation.expected_error in (install.stdout + install.stderr).lower()
        assert_unchanged(before_install, ArtifactSnapshot.capture(consumer.root))
