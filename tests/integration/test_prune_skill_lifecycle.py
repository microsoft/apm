"""Real CLI install/removal/prune contracts for manifestless skill packages."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from apm_cli.deps.lockfile import LockFile
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.artifact_snapshot import ArtifactSnapshot, assert_unchanged
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.windows_compat,
]


@pytest.mark.parametrize("alias", [None, "azure-ai-alias"])
def test_install_remove_install_prune_skill(
    tmp_path_factory: pytest.TempPathFactory, apm_binary_path: Path, alias: str | None
) -> None:
    """Prune removes unlocked skill bytes while retaining a sibling and user files."""
    tmp_path = tmp_path_factory.mktemp("ps")
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=os.environ)
    repositories = LocalGitRepositoryFactory(
        isolated.repository_root, env=isolated.subprocess_env()
    )
    repository = repositories.create("skills")
    skill_parent = ".github/plugins/azure-skills/skills"
    for name in ("azure-ai", "retained"):
        skill = repository.worktree / skill_parent / name
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Fixture skill\n---\n# {name}\n",
            encoding="utf-8",
        )
        (skill / "references").mkdir()
        (skill / "references" / "guide.md").write_text("Reference bytes\n", encoding="utf-8")
    commit = repositories.commit(repository, message="Add fixture skills")
    # Git transport is redirected to a real local repository; no downloader,
    # install, lockfile, integration, or prune code is mocked.
    remote = "https://gitlab.com/fixture/skills"
    environment = repositories.url_rewrite_subprocess_env(repository, remote)
    dependencies = [
        {"git": remote, "path": f"{skill_parent}/{name}", "ref": commit.sha}
        for name in ("azure-ai", "retained")
    ]
    if alias is not None:
        dependencies[0]["alias"] = alias
    project = LocalPackageFactory(isolated.work_root).create(
        "consumer", dependencies=dependencies, targets=("copilot",)
    )
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=30)

    def run(*args: str) -> str:
        result = runner.run(args, cwd=project.root, env=environment, scenario_id="prune-skill")
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout + result.stderr

    install_args = ("install", "--no-policy", "--parallel-downloads", "0")
    run(*install_args)
    lock_path = project.root / "apm.lock.yaml"
    lock = LockFile.read(lock_path)
    removed_key, removed = next(
        (key, dep)
        for key, dep in lock.dependencies.items()
        if dep.virtual_path.endswith("azure-ai")
    )
    modules = project.root / "apm_modules"
    removed_root = removed.to_dependency_ref().get_install_path(modules)
    retained = next(
        dep for dep in lock.dependencies.values() if dep.virtual_path.endswith("retained")
    )
    retained_root = retained.to_dependency_ref().get_install_path(modules)
    assert (removed_root / "SKILL.md").is_file()
    assert not (removed_root / "apm.yml").exists()
    deployed = project.root / ".agents" / "skills"
    removed_deployment = deployed / (alias or "azure-ai")
    assert (removed_deployment / "SKILL.md").is_file()
    retained_before = ArtifactSnapshot.capture(retained_root)
    deployed_before = ArtifactSnapshot.capture(deployed / "retained")

    manifest = load_yaml(project.manifest_path)
    manifest["dependencies"]["apm"] = dependencies[1:]
    dump_yaml(manifest, project.manifest_path)
    run(*install_args)
    assert removed_key not in LockFile.read(lock_path).dependencies
    assert not removed_deployment.exists()
    assert removed_root.exists(), "Install leaves orphan source bytes for prune"
    if alias is None:
        (removed_root / ".apm-pin").unlink(missing_ok=True)
    (removed_root / "personal-note.txt").write_text("Managed-root tradeoff\n", encoding="utf-8")
    sentinel = modules / "user-notes.txt"
    sentinel.write_text("Keep these notes\n", encoding="utf-8")
    unrecognized = modules / "personal-sources"
    unrecognized.mkdir()
    (unrecognized / "note.txt").write_text("Not a recognized package\n", encoding="utf-8")
    before_dry_run = ArtifactSnapshot.capture(project.root)

    preview = run("prune", "--dry-run")
    assert "1 orphaned package(s)" in preview
    assert_unchanged(before_dry_run, ArtifactSnapshot.capture(project.root))
    assert "Pruned 1 orphaned package(s)" in run("prune")
    assert not removed_root.exists()
    assert_unchanged(retained_before, ArtifactSnapshot.capture(retained_root))
    assert_unchanged(deployed_before, ArtifactSnapshot.capture(deployed / "retained"))
    assert sentinel.read_text(encoding="utf-8") == "Keep these notes\n"
    assert (unrecognized / "note.txt").read_text(encoding="utf-8") == "Not a recognized package\n"
    before_repeat = ArtifactSnapshot.capture(project.root)
    assert "No orphaned packages" in run("prune")
    assert_unchanged(before_repeat, ArtifactSnapshot.capture(project.root))


@pytest.mark.parametrize("alias", [None, "bundle-alias"])
def test_prune_preserves_declared_skill_bundle(
    tmp_path_factory: pytest.TempPathFactory, apm_binary_path: Path, alias: str | None
) -> None:
    """Real manifestless bundles survive prune and produce no compile orphan warning."""
    tmp_path = tmp_path_factory.mktemp("pb")
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=os.environ)
    repositories = LocalGitRepositoryFactory(
        isolated.repository_root, env=isolated.subprocess_env()
    )
    repository = repositories.create("bundle")
    for name in ("alpha", "beta"):
        skill = repository.worktree / "skills" / name
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Bundle fixture skill\n---\n# {name}\n",
            encoding="utf-8",
        )
    commit = repositories.commit(repository, message="Add manifestless skill bundle")
    remote = "https://gitlab.com/fixture/bundle"
    environment = repositories.url_rewrite_subprocess_env(repository, remote)
    dependency = {"git": remote, "ref": commit.sha}
    if alias is not None:
        dependency["alias"] = alias
    project = LocalPackageFactory(isolated.work_root).create(
        "consumer", dependencies=(dependency,), targets=("copilot",)
    )
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=30)

    def run(*args: str) -> str:
        result = runner.run(args, cwd=project.root, env=environment, scenario_id="prune-bundle")
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout + result.stderr

    run("install", "--no-policy", "--parallel-downloads", "0")
    modules = project.root / "apm_modules"
    bundle = modules / (alias or "fixture/bundle")
    assert not (bundle / "apm.yml").exists()
    assert not (bundle / "SKILL.md").exists()
    assert not (bundle / ".apm").exists()
    for name in ("alpha", "beta"):
        assert (bundle / "skills" / name / "SKILL.md").is_file()
        assert (project.root / ".agents" / "skills" / name / "SKILL.md").is_file()
    before = ArtifactSnapshot.capture(project.root)
    for args in (("prune", "--dry-run"), ("prune",)):
        assert "No orphaned packages" in run(*args)
        assert_unchanged(before, ArtifactSnapshot.capture(project.root))
    output = run("compile")
    assert "orphaned package(s)" not in output
    assert "Run 'apm prune'" not in output
    for name in ("alpha", "beta"):
        assert (bundle / "skills" / name / "SKILL.md").is_file()


@pytest.mark.parametrize("retention", ["direct", "dev", "transitive"])
def test_prune_retains_installed_ancestor_after_root_declaration_removed(
    tmp_path_factory: pytest.TempPathFactory, apm_binary_path: Path, retention: str
) -> None:
    """The installed root survives lock removal while a nested dependency needs it."""
    isolated = IsolatedApmEnvironment.create(
        tmp_path_factory.mktemp("pc") / "i", base_env=os.environ
    )
    repositories = LocalGitRepositoryFactory(
        isolated.repository_root, env=isolated.subprocess_env()
    )
    repository = repositories.create("family")
    child_path = ".github/plugins/skills/child"
    for relative, name in (("", "family"), (child_path, "child")):
        skill = repository.worktree / relative
        skill.mkdir(parents=True, exist_ok=True)
        (skill / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Retained-root fixture\n---\n# {name}\n",
            encoding="utf-8",
        )
    commit = repositories.commit(repository, message="Add root and nested skill")
    remote = "https://gitlab.com/fixture/family"
    environment = repositories.url_rewrite_subprocess_env(repository, remote)
    root_dep = {"git": remote, "ref": commit.sha}
    child_dep = {**root_dep, "path": child_path}
    factory = LocalPackageFactory(isolated.work_root)
    consumer = factory.create("consumer", dependencies=(root_dep,), targets=("copilot",))
    manifest = load_yaml(consumer.manifest_path)
    if retention == "direct":
        manifest["dependencies"]["apm"].append(child_dep)
    elif retention == "dev":
        manifest["devDependencies"] = {"apm": [child_dep]}
    else:
        keeper = factory.create("keeper", dependencies=(child_dep,), targets=("copilot",))
        factory.add_relative_dependency(consumer, keeper)
        manifest = load_yaml(consumer.manifest_path)
    dump_yaml(manifest, consumer.manifest_path)
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=30)

    def run(*args: str) -> str:
        result = runner.run(
            args, cwd=consumer.root, env=environment, scenario_id=f"retained-root-{retention}"
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return " ".join((result.stdout + result.stderr).split())

    install_args = ("install", "--no-policy", "--parallel-downloads", "0")
    run(*install_args)
    lock_path = consumer.root / "apm.lock.yaml"
    installed = LockFile.read(lock_path)
    root_key, root_package = next(
        (key, package)
        for key, package in installed.dependencies.items()
        if package.repo_url == "fixture/family" and not package.virtual_path
    )
    root = root_package.to_dependency_ref().get_install_path(consumer.root / "apm_modules")
    assert (root / "SKILL.md").is_file()
    assert (root / child_path / "SKILL.md").is_file()
    manifest = load_yaml(consumer.manifest_path)
    manifest["dependencies"]["apm"] = manifest["dependencies"]["apm"][1:]
    dump_yaml(manifest, consumer.manifest_path)
    run(*install_args)
    lock = LockFile.read(lock_path)
    assert root_key not in lock.dependencies
    assert any(package.virtual_path == child_path for package in lock.dependencies.values())
    (root / "personal-note.txt").write_text("Keep the entire containing root\n", encoding="utf-8")
    before = ArtifactSnapshot.capture(consumer.root)

    for args in (("prune", "--dry-run"), ("prune",), ("prune",)):
        output = run(*args)
        assert "Retained fixture/family" in output
        assert "No orphaned packages" in output
        assert "would be removed" not in output
        assert_unchanged(before, ArtifactSnapshot.capture(consumer.root))
    output = run("compile")
    assert "orphaned package(s)" not in output
    assert (root / child_path / "SKILL.md").is_file()
