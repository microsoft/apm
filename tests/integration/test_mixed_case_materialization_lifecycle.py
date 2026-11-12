"""Installed-binary lifecycle coverage for mixed-case GitHub paths."""

from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath

import pytest

from apm_cli.deps.lockfile import LockFile
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.lifecycle_state import LifecycleStateSnapshot
from tests.utils.local_git_repository import LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackage, LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.lifecycle_merge_group,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
]

_OWNER = "MixedOrg"
_PARENT_REPO = "MixedParent"
_CHILD_REPO = "MixedChild"
_PARENT_REMOTE = f"https://github.com/{_OWNER}/{_PARENT_REPO}"
_CHILD_REMOTE = f"https://github.com/{_OWNER}/{_CHILD_REPO}"
_INSTALL = (
    "install",
    "--target",
    "copilot,cursor",
    "--no-policy",
    "--parallel-downloads",
    "0",
)
_MARKDOWN_LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


def _instruction(marker: str) -> str:
    return (
        "---\n"
        "applyTo: '**'\n"
        f"description: Mixed-case lifecycle instruction {marker}\n"
        "---\n"
        f"# {marker}\n\n"
        "[nested guide](../../skills/linked/references/NestedGuide.md)\n"
    )


def _agent(marker: str) -> str:
    return (
        "---\n"
        f"description: Mixed-case lifecycle agent {marker}\n"
        "---\n"
        f"# {marker}\n\n"
        "[nested guide](../../skills/linked/references/NestedGuide.md)\n"
    )


def _prompt(marker: str) -> str:
    return (
        "---\n"
        f"description: Mixed-case lifecycle prompt {marker}\n"
        "---\n"
        f"# {marker}\n\n"
        "[nested guide](../../skills/linked/references/NestedGuide.md)\n"
    )


def _run(
    runner: ApmLifecycleRunner,
    project: LocalPackage,
    args: tuple[str, ...],
    *,
    environment: dict[str, str],
    scenario_id: str,
) -> CommandResult:
    result = runner.run(
        args,
        scenario_id=scenario_id,
        cwd=project.root,
        env=environment,
    )
    assert result.returncode == 0, (
        f"{scenario_id} failed\nstdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    return result


def _locked_dependencies(project: LocalPackage) -> list:
    lock = LockFile.read(project.root / "apm.lock.yaml")
    assert lock is not None
    return lock.get_package_dependencies()


def _managed_markdown_with_links(project: LocalPackage) -> tuple[Path, ...]:
    paths: set[Path] = set()
    for dependency in _locked_dependencies(project):
        for relative in dependency.deployed_files:
            candidate = project.root / relative
            if (
                not candidate.is_file()
                or candidate.suffix != ".md"
                or not candidate.name.startswith("child")
            ):
                continue
            if _MARKDOWN_LINK.search(candidate.read_text(encoding="utf-8")):
                paths.add(candidate)
    return tuple(sorted(paths))


def _assert_all_managed_links_resolve(project: LocalPackage) -> tuple[Path, ...]:
    linked_files = _managed_markdown_with_links(project)
    assert linked_files
    for generated in linked_files:
        content = generated.read_text(encoding="utf-8")
        for raw_target in _MARKDOWN_LINK.findall(content):
            path_text = raw_target.split("#", 1)[0].split("?", 1)[0]
            assert path_text
            assert "\\" not in path_text
            target = Path(path_text)
            assert not target.is_absolute()
            resolved = (generated.parent / target).resolve()
            assert resolved.is_file(), f"{generated}: broken link {raw_target!r}"
            relative = resolved.relative_to(project.root.resolve())
            parts = relative.parts
            assert parts[:3] == ("apm_modules", _OWNER, _CHILD_REPO)
    return linked_files


def _rename_case_only(path: Path, new_name: str) -> Path:
    """Force an on-disk case change through a same-directory intermediate."""
    intermediate = path.with_name(f".case-stage-{new_name.casefold()}")
    destination = path.with_name(new_name)
    path.replace(intermediate)
    intermediate.replace(destination)
    return destination


def _author_packages(
    packages: LocalPackageFactory,
) -> tuple[LocalPackage, LocalPackage]:
    child = packages.create(_CHILD_REPO, targets=("copilot", "cursor"))
    packages.add_instruction(child, "child", _instruction("child-v1"))
    packages.add_agent(child, "child", _agent("child-v1"))
    packages.add_prompt(child, "child", _prompt("child-v1"))
    packages.add_skill(
        child,
        "linked",
        "---\nname: linked\ndescription: Mixed-case linked skill\n---\n# Linked\n",
    )
    packages.add_relative_link(
        child,
        PurePosixPath("skills/linked/references/NestedGuide.md"),
        PurePosixPath("../SKILL.md"),
        label="skill source",
    )

    parent = packages.create(
        _PARENT_REPO,
        dependencies=({"git": _CHILD_REMOTE, "ref": "main"},),
        targets=("copilot", "cursor"),
    )
    return parent, child


def test_mixed_case_links_survive_install_update_audit_and_uninstall(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Canonical dedupe and display-cased links remain closed over the lifecycle."""
    isolated = IsolatedApmEnvironment.create(
        tmp_path / "mixed-case-lifecycle",
        base_env=dict(os.environ),
    )
    environment = isolated.subprocess_env()
    packages = LocalPackageFactory(isolated.package_root)
    parent, child = _author_packages(packages)

    repositories = LocalGitRepositoryFactory(
        isolated.repository_root,
        env=environment,
    )
    child_repository = repositories.create(_CHILD_REPO, source_tree=child.root)
    child_v1 = repositories.commit(child_repository, message="seed mixed child")
    parent_repository = repositories.create(_PARENT_REPO, source_tree=parent.root)
    repositories.commit(parent_repository, message="seed mixed parent")
    child_env = repositories.url_rewrite_subprocess_env_many(
        (
            (parent_repository, _PARENT_REMOTE),
            (child_repository, _CHILD_REMOTE),
        )
    )

    consumer = LocalPackageFactory(isolated.work_root).create(
        "mixed-case-consumer",
        dependencies=({"git": _PARENT_REMOTE, "ref": "main"},),
        targets=("copilot", "cursor"),
    )
    runner = ApmLifecycleRunner(
        (str(apm_binary_path),),
        timeout_seconds=120,
        scenario_timeout_seconds=360,
    )

    _run(
        runner,
        consumer,
        _INSTALL,
        environment=child_env,
        scenario_id="mixed-case-install",
    )
    first_links = _assert_all_managed_links_resolve(consumer)
    first_state = LifecycleStateSnapshot.capture(
        consumer.root,
        targets=("copilot", "cursor"),
    )
    locked = _locked_dependencies(consumer)
    assert len(locked) == 2
    assert {dependency.repo_url for dependency in locked} == {
        "mixedorg/mixedparent",
        "mixedorg/mixedchild",
    }
    assert {dependency.materialization_repo_url for dependency in locked} == {
        f"{_OWNER}/{_PARENT_REPO}",
        f"{_OWNER}/{_CHILD_REPO}",
    }
    assert (consumer.root / "apm_modules" / _OWNER / _PARENT_REPO).is_dir()
    assert (consumer.root / "apm_modules" / _OWNER / _CHILD_REPO).is_dir()
    assert child_v1.sha in (consumer.root / "apm.lock.yaml").read_text(encoding="utf-8")

    owner_path = consumer.root / "apm_modules" / _OWNER
    _rename_case_only(owner_path / _PARENT_REPO, _PARENT_REPO.casefold())
    _rename_case_only(owner_path / _CHILD_REPO, _CHILD_REPO.casefold())
    _rename_case_only(owner_path, _OWNER.casefold())

    _run(
        runner,
        consumer,
        (*_INSTALL, "--verbose"),
        environment=child_env,
        scenario_id="mixed-case-repeat",
    )
    repeated_state = LifecycleStateSnapshot.capture(
        consumer.root,
        targets=("copilot", "cursor"),
    )
    assert repeated_state.semantic_bytes == first_state.semantic_bytes
    migrated_owner = consumer.root / "apm_modules" / _OWNER
    assert sorted(path.name for path in migrated_owner.iterdir() if path.is_dir()) == [
        _CHILD_REPO,
        _PARENT_REPO,
    ]
    _assert_all_managed_links_resolve(consumer)

    stale_managed = first_links[0]
    stale_managed.write_text(
        stale_managed.read_text(encoding="utf-8").replace(
            f"{_OWNER}/{_CHILD_REPO}",
            "mixedorg/mixedchild",
        ),
        encoding="utf-8",
    )
    user_file = consumer.root / ".github" / "instructions" / "user-owned.instructions.md"
    user_file.write_text(
        "# User-owned\n\n[user link](../../apm_modules/mixedorg/mixedchild/user.md)\n",
        encoding="utf-8",
    )
    user_bytes = user_file.read_bytes()

    _run(
        runner,
        consumer,
        _INSTALL,
        environment=child_env,
        scenario_id="mixed-case-repair-stale-link",
    )
    assert f"{_OWNER}/{_CHILD_REPO}" in stale_managed.read_text(encoding="utf-8")
    assert user_file.read_bytes() == user_bytes
    _assert_all_managed_links_resolve(consumer)

    child_instruction = (
        child_repository.worktree / ".apm" / "instructions" / "child.instructions.md"
    )
    child_instruction.write_text(_instruction("child-v2"), encoding="utf-8")
    child_v2 = repositories.commit(child_repository, message="advance mixed child")
    assert child_v2.sha != child_v1.sha
    parent_manifest = parent_repository.worktree / "apm.yml"
    parent_document = load_yaml(parent_manifest)
    parent_document["dependencies"]["apm"][0]["ref"] = child_v2.sha
    dump_yaml(parent_document, parent_manifest)
    parent_v2 = repositories.commit(parent_repository, message="pin advanced mixed child")
    consumer_document = load_yaml(consumer.manifest_path)
    consumer_document["dependencies"]["apm"][0]["ref"] = parent_v2.sha
    dump_yaml(consumer_document, consumer.manifest_path)

    _run(
        runner,
        consumer,
        _INSTALL,
        environment=child_env,
        scenario_id="mixed-case-update",
    )
    assert parent_v2.sha in (consumer.root / "apm.lock.yaml").read_text(encoding="utf-8")
    assert child_v2.sha in (consumer.root / "apm.lock.yaml").read_text(encoding="utf-8")
    assert any(
        "child-v2" in path.read_text(encoding="utf-8")
        for path in _assert_all_managed_links_resolve(consumer)
    )
    assert user_file.read_bytes() == user_bytes

    audit = _run(
        runner,
        consumer,
        ("audit", "--ci", "--no-policy", "--format", "json"),
        environment=child_env,
        scenario_id="mixed-case-audit",
    )
    assert '"passed": true' in audit.stdout.lower()

    _run(
        runner,
        consumer,
        ("uninstall", f"{_OWNER}/{_PARENT_REPO}"),
        environment=child_env,
        scenario_id="mixed-case-uninstall",
    )
    assert user_file.read_bytes() == user_bytes
    assert not (consumer.root / "apm_modules" / _OWNER / _PARENT_REPO).exists()
    assert not (consumer.root / "apm_modules" / _OWNER / _CHILD_REPO).exists()
