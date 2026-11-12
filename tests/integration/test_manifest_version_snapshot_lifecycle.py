"""Installed-CLI regressions for manifest-derived lock versions after checkout."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
]

_INSTALL = ("install", "--target", "claude", "--no-policy", "--parallel-downloads", "0")
_REMOTE = "https://github.com/apm-fixture-org/version-snapshots"


@dataclass(frozen=True)
class _Scenario:
    """A genuine branch switch over an older installed package."""

    root: Path
    environment: dict[str, str]
    runner: ApmLifecycleRunner
    commit: str | None
    installed_manifest: Path
    deployed_skill: Path
    cache_root: Path

    def install(self, *extra: str) -> CommandResult:
        """Run the installed entry point in the isolated consumer."""
        return self.runner.run(
            (*_INSTALL, *extra),
            scenario_id="manifest-version-snapshot",
            cwd=self.root,
            env=self.environment,
        )


def _git(root: Path, environment: dict[str, str], *args: str) -> str:
    """Run real Git only against an owned fixture repository."""
    return subprocess.run(
        ("git", *args),
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout.strip()


def _write_package(root: Path, version: str) -> None:
    """Write a version and independently observable skill payload."""
    root.mkdir(parents=True, exist_ok=True)
    dump_yaml({"name": "demo", "version": version}, root / "apm.yml")
    skill = root / ".apm" / "skills" / "demo" / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text(
        f"---\nname: demo\ndescription: Snapshot fixture\n---\n\nBody {version}.\n",
        encoding="utf-8",
    )


def _clear_installed_fixture(root: Path) -> None:
    """Remove only generated install state inside the owned test project."""
    for name in ("apm_modules", ".claude"):
        path = root / name
        if path.exists():
            shutil.rmtree(path)


def _branch_switch(tmp_path: Path, binary: Path, source: str) -> _Scenario:
    """Generate both locks with the CLI, then check out new over old modules."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "environment", base_env=dict(os.environ))
    environment = isolated.subprocess_env()
    consumer = LocalPackageFactory(isolated.work_root).create("consumer", targets=("claude",))
    root = consumer.root
    new_commit = None
    if source == "git-subdirectory":
        repositories = LocalGitRepositoryFactory(isolated.repository_root, env=environment)
        repository = repositories.create("version-snapshots")
        for version, tag in (("1.0.0", "release-old"), ("1.1.0", "release-new")):
            _write_package(repository.worktree / "pkgs" / "demo", version)
            commit = repositories.commit(repository, message=f"package {version}")
            repositories.tag(repository, tag, commit)
            new_commit = commit.sha
        environment = repositories.url_rewrite_subprocess_env(repository, _REMOTE)

    runner = ApmLifecycleRunner((str(binary),), timeout_seconds=180)
    installed = root / "apm_modules" / "_local" / "demo" / "apm.yml"
    if source == "git-subdirectory":
        from apm_cli.models.dependency.reference import DependencyReference

        ref = DependencyReference.parse(f"{_REMOTE}/pkgs/demo#release-new")
        installed = ref.get_install_path(root / "apm_modules") / "apm.yml"
    scenario = _Scenario(
        root,
        environment,
        runner,
        new_commit,
        installed,
        root / ".claude" / "skills" / "demo" / "SKILL.md",
        isolated.cache_root,
    )
    _git(root, environment, "init", "--initial-branch=main")
    (root / ".gitignore").write_text("apm_modules/\n.claude/\n", encoding="utf-8")
    for version, tag in (("1.0.0", "release-old"), ("1.1.0", "release-new")):
        if version == "1.1.0":
            _git(root, environment, "checkout", "-b", "bump")
            _clear_installed_fixture(root)
        manifest = load_yaml(consumer.manifest_path)
        if source == "local":
            _write_package(root / "pkgs" / "demo", version)
            dependency: str | dict[str, str] = "./pkgs/demo"
        else:
            dependency = {"git": _REMOTE, "path": "pkgs/demo", "ref": tag}
        manifest["dependencies"] = {"apm": [dependency]}
        dump_yaml(manifest, consumer.manifest_path)
        result = scenario.install()
        assert result.returncode == 0, result.stdout + result.stderr
        lock = load_yaml(root / "apm.lock.yaml")["dependencies"][0]
        assert lock["version"] == version
        assert load_yaml(installed)["version"] == version
        assert scenario.deployed_skill.read_text().endswith(f"Body {version}.\n")
        _git(root, environment, "add", "--all")
        _git(root, environment, "commit", "-m", f"consumer {version}")
    _git(root, environment, "checkout", "main")
    _clear_installed_fixture(root)
    result = scenario.install()
    assert result.returncode == 0, result.stdout + result.stderr
    assert load_yaml(installed)["version"] == "1.0.0"
    assert scenario.deployed_skill.read_text().endswith("Body 1.0.0.\n")
    _git(root, environment, "checkout", "bump")
    assert load_yaml(root / "apm.lock.yaml")["dependencies"][0]["version"] == "1.1.0"
    assert load_yaml(installed)["version"] == "1.0.0"
    return scenario


@pytest.mark.parametrize("source", ["local", "git-subdirectory"])
def test_first_install_after_checkout_records_current_manifest_snapshot(
    tmp_path: Path, apm_binary_path: Path, source: str
) -> None:
    """The first and repeated installs retain the CLI-generated new baseline."""
    scenario = _branch_switch(tmp_path, apm_binary_path, source)
    manifest = scenario.root / "apm.yml"
    lockfile = scenario.root / "apm.lock.yaml"
    baseline = (manifest.read_bytes(), lockfile.read_bytes())
    for attempt in range(3):
        result = scenario.install()
        assert result.returncode == 0, result.stdout + result.stderr
        lock = load_yaml(lockfile)["dependencies"][0]
        assert load_yaml(scenario.installed_manifest)["version"] == "1.1.0"
        assert scenario.deployed_skill.read_text().endswith("Body 1.1.0.\n")
        if scenario.commit:
            assert lock["resolved_commit"] == scenario.commit
            assert lock["resolved_ref"] == "release-new"
        assert lock["version"] == "1.1.0", (
            f"attempt={attempt + 1}; installed manifest=1.1.0; lock={lock}; "
            f"stdout={result.stdout}; stderr={result.stderr}"
        )
        assert (manifest.read_bytes(), lockfile.read_bytes()) == baseline


@pytest.mark.parametrize("source", ["local", "git-subdirectory"])
@pytest.mark.parametrize("cold", [False, True], ids=["warm", "cold"])
def test_frozen_replays_pins_without_using_inventory_version(
    tmp_path: Path, apm_binary_path: Path, source: str, cold: bool
) -> None:
    """Inventory alone does not veto replay or rewrite the committed lock."""
    scenario = _branch_switch(tmp_path, apm_binary_path, source)
    manifest = scenario.root / "apm.yml"
    lockfile = scenario.root / "apm.lock.yaml"
    lock = load_yaml(lockfile)
    lock["dependencies"][0]["version"] = "1.0.0"
    dump_yaml(lock, lockfile)
    baseline = (manifest.read_bytes(), lockfile.read_bytes())
    if cold:
        shutil.rmtree(scenario.root / "apm_modules")
        shutil.rmtree(scenario.cache_root)
    result = scenario.install("--frozen")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (manifest.read_bytes(), lockfile.read_bytes()) == baseline
    replayed_lock = load_yaml(lockfile)["dependencies"][0]
    assert replayed_lock["version"] == "1.0.0"
    assert load_yaml(scenario.installed_manifest)["version"] == "1.1.0"
    assert scenario.deployed_skill.read_text().endswith("Body 1.1.0.\n")
    if scenario.commit:
        assert replayed_lock["resolved_commit"] == scenario.commit
        assert replayed_lock["resolved_ref"] == "release-new"

    recovered = scenario.install()
    assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    assert manifest.read_bytes() == baseline[0]
    corrected_lock = load_yaml(lockfile)["dependencies"][0]
    assert corrected_lock["version"] == "1.1.0"
    assert load_yaml(scenario.installed_manifest)["version"] == "1.1.0"
    assert scenario.deployed_skill.read_text().endswith("Body 1.1.0.\n")
    if scenario.commit:
        assert corrected_lock["resolved_commit"] == scenario.commit
    corrected_bytes = lockfile.read_bytes()
    replay = scenario.install("--frozen")
    assert replay.returncode == 0, replay.stdout + replay.stderr
    assert (manifest.read_bytes(), lockfile.read_bytes()) == (baseline[0], corrected_bytes)
