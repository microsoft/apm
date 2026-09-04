"""Lifecycle invariant: APM only touches what it owns."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import pytest

from apm_cli.core.target_catalog import manifest_target_names
from apm_cli.deps.lockfile import LockFile
from apm_cli.integration.targets import KNOWN_TARGETS, PrimitiveMapping, TargetProfile
from apm_cli.utils.yaml_io import dump_yaml
from tests.integration.test_required_lifecycle_state_machine import (
    _OWNER,
    _assert_same_state,
    _audit,
    _hook,
    _instruction,
    _new_scenario,
    _run_success,
    _skill,
)
from tests.utils.lifecycle_state import LifecycleStateRoot, LifecycleStateSnapshot
from tests.utils.local_package import LocalPackage

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
]

_INSTALL_ARGS = ("install", "--no-policy", "--parallel-downloads", "0")
_ROOT_CONTEXT_BY_FAMILY = {
    "agents": "AGENTS.md",
    "claude": "CLAUDE.md",
    "gemini": "GEMINI.md",
    "vscode": "AGENTS.md",
}
_SENTINEL_PREFIX = b"APM-OWNERSHIP-SENTINEL\n"


@dataclass(frozen=True)
class _Sentinel:
    """A byte-exact user-owned file that APM must never mutate."""

    root_id: str
    root: Path
    relative_path: PurePosixPath
    content: bytes

    @property
    def path(self) -> Path:
        """Return the concrete sentinel path."""
        return self.root.joinpath(*self.relative_path.parts)


@dataclass(frozen=True)
class _ExternalRootSpec:
    """One bounded user-scope root for lifecycle snapshots."""

    root_id: str
    target: str
    path: Path
    config_paths: tuple[PurePosixPath, ...]


@dataclass(frozen=True)
class _InvariantCase:
    """One ownership-invariant lifecycle route."""

    id: str
    targets: tuple[str, ...]
    user_scope: bool = False
    include_catalog_primitives: bool = True
    include_hooks: bool = False
    include_mcp: bool = False
    mcp_unowned_target: str | None = None


@dataclass(frozen=True)
class _PublishedInvariant:
    """A published local Git package and its install-time dependency entry."""

    remote_url: str
    dependency: dict[str, object]
    environment: dict[str, str]


def _project_lifecycle_targets() -> tuple[str, ...]:
    """Derive the project-scope lifecycle target set from the live catalog."""
    accepted_manifest_targets = manifest_target_names()
    return tuple(
        name
        for name, profile in KNOWN_TARGETS.items()
        if name in accepted_manifest_targets and not profile.capability.mcp_only
    )


def _user_copilot_hooks_case() -> _InvariantCase:
    """Return the user-scope Copilot hooks route from the live catalog."""
    profile = KNOWN_TARGETS["copilot"]
    assert profile.user_supported and "hooks" in profile.primitives
    return _InvariantCase(
        "user-copilot-hooks",
        (profile.name,),
        user_scope=True,
        include_catalog_primitives=False,
        include_hooks=True,
    )


def _mcp_boundary_case() -> _InvariantCase:
    """Return a two-target MCP ownership boundary from the live catalog."""
    owner = KNOWN_TARGETS["claude"]
    unowned = KNOWN_TARGETS["cursor"]
    assert owner.hooks_config_display and unowned.hooks_config_display
    return _InvariantCase(
        "project-mcp-target-scope",
        (owner.name,),
        include_catalog_primitives=False,
        include_mcp=True,
        mcp_unowned_target=unowned.name,
    )


_OWNERSHIP_CASES = (
    _InvariantCase("project-catalog", _project_lifecycle_targets()),
    _mcp_boundary_case(),
    _user_copilot_hooks_case(),
)


def _publish_invariant_package(scenario, case: _InvariantCase, package_name: str):
    """Publish one package carrying every primitive needed by the invariant."""
    mcp_dependencies: tuple[dict[str, object], ...] = ()
    if case.include_mcp:
        mcp_dependencies = (
            {
                "name": "fixture-mcp",
                "registry": False,
                "transport": "stdio",
                "command": "printf",
                "args": ["fixture"],
            },
        )
    package = scenario.sources.create(
        package_name,
        mcp_dependencies=mcp_dependencies,
    )
    if case.include_catalog_primitives:
        scenario.sources.add_skill(package, "owned-skill", _skill("owned-skill"))
        scenario.sources.add_instruction(
            package,
            "owned-instruction",
            _instruction("owned-instruction"),
        )
        scenario.sources.add_agent(
            package,
            "owned-agent",
            "---\ndescription: Ownership agent\n---\n# Ownership agent\n",
        )
        scenario.sources.add_command(
            package,
            "owned-command",
            "---\ndescription: Ownership command\n---\nRun ownership command\n",
        )
        scenario.sources.add_prompt(
            package,
            "owned-prompt",
            "---\ndescription: Ownership prompt\n---\nRun ownership prompt\n",
        )
    if case.include_hooks:
        scenario.sources.add_hook(package, "owned-hook", _hook(f"echo {package_name}"))
        if case.user_scope:
            scenario.sources.add_instruction(
                package,
                "owned-user-instruction",
                _instruction("owned-user-instruction"),
            )
    repository = scenario.repositories.create(package_name, source_tree=package.root)
    commit = scenario.repositories.commit(
        repository,
        message=f"seed invariant primitives for {package_name}",
    )
    remote_url = f"https://github.com/{_OWNER}/{package_name}"
    environment = scenario.repositories.url_rewrite_subprocess_env(repository, remote_url)
    dependency = {
        "git": remote_url,
        "ref": commit.sha,
        "alias": package_name,
    }
    return _PublishedInvariant(
        remote_url=remote_url,
        dependency=dependency,
        environment=environment,
    )


def _create_consumer(
    scenario, case: _InvariantCase, dependency: Mapping[str, object]
) -> LocalPackage:
    """Create a project or user manifest for one invariant case."""
    if case.user_scope:
        project = scenario.consumers.create(f"consumer-{case.id}")
        dump_yaml(
            {
                "name": f"consumer-{case.id}",
                "version": "0.1.0",
                "description": "Ownership invariant user-scope consumer",
                "targets": list(case.targets),
                "dependencies": {"apm": [dict(dependency)]},
            },
            scenario.isolated.config_root / "apm.yml",
        )
        return project
    return scenario.consumers.create(
        f"consumer-{case.id}",
        dependencies=(dict(dependency),),
        targets=case.targets,
    )


def _compile_targets(case: _InvariantCase) -> tuple[str, ...]:
    """Return compile-capable targets for this lifecycle case."""
    return tuple(
        target
        for target in case.targets
        if KNOWN_TARGETS[target].compile_family in _ROOT_CONTEXT_BY_FAMILY
    )


def _all_project_sentinels(project_root: Path) -> tuple[_Sentinel, ...]:
    """Plant user-owned project files across every catalog deployment shape."""
    relative_paths: dict[PurePosixPath, bytes] = {}
    for profile in KNOWN_TARGETS.values():
        family = profile.compile_family
        if family in _ROOT_CONTEXT_BY_FAMILY:
            relative = PurePosixPath(_ROOT_CONTEXT_BY_FAMILY[family])
            relative_paths.setdefault(relative, _sentinel_content("project-root", relative))
        for generated in profile.generated_files:
            relative = PurePosixPath(profile.root_dir) / generated
            relative_paths.setdefault(relative, _sentinel_content("project-generated", relative))
        for primitive, mapping in profile.primitives.items():
            relative = _mapping_sentinel_path(profile, primitive, mapping)
            relative_paths.setdefault(relative, _sentinel_content("project", relative))
    return tuple(
        _Sentinel("workspace", project_root, relative, content)
        for relative, content in sorted(relative_paths.items(), key=lambda item: item[0].as_posix())
    )


def _all_user_sentinels(
    home: Path,
    *,
    targets: Sequence[str] | None = None,
) -> tuple[_Sentinel, ...]:
    """Plant user-owned files under every static user-scope catalog root."""
    selected_targets = set(targets) if targets is not None else None
    sentinels: dict[tuple[str, PurePosixPath], _Sentinel] = {}
    for profile in KNOWN_TARGETS.values():
        if selected_targets is not None and profile.name not in selected_targets:
            continue
        if not profile.user_supported or profile.user_root_resolver is not None:
            continue
        root_dir = profile.user_root_dir or profile.root_dir
        root = home.joinpath(*PurePosixPath(root_dir).parts)
        family = profile.compile_family
        if family in _ROOT_CONTEXT_BY_FAMILY:
            relative = PurePosixPath(_ROOT_CONTEXT_BY_FAMILY[family])
            sentinels.setdefault(
                (root_dir, relative),
                _Sentinel(
                    _root_id(root_dir),
                    root,
                    relative,
                    _sentinel_content(f"user-root-{profile.name}", relative),
                ),
            )
        for primitive, mapping in _user_primitive_mappings(profile).items():
            relative = _mapping_sentinel_path(profile, primitive, mapping, include_root=False)
            sentinels.setdefault(
                (root_dir, relative),
                _Sentinel(
                    _root_id(root_dir),
                    root,
                    relative,
                    _sentinel_content(f"user-{profile.name}", relative),
                ),
            )
    return tuple(
        sentinel
        for _key, sentinel in sorted(
            sentinels.items(),
            key=lambda item: (item[0][0], item[0][1].as_posix()),
        )
    )


def _user_primitive_mappings(profile: TargetProfile) -> dict[str, PrimitiveMapping]:
    """Return static user-scope primitive mappings for a profile."""
    mappings = {
        primitive: mapping
        for primitive, mapping in profile.primitives.items()
        if primitive not in profile.unsupported_user_primitives
    }
    if profile.user_primitive_overrides:
        mappings.update(profile.user_primitive_overrides)
    return mappings


def _mapping_sentinel_path(
    profile: TargetProfile,
    primitive: str,
    mapping: PrimitiveMapping,
    *,
    include_root: bool = True,
) -> PurePosixPath:
    """Return a realistic user-authored path for one primitive mapping."""
    root_parts = (
        () if not include_root else PurePosixPath(mapping.deploy_root or profile.root_dir).parts
    )
    subdir_parts = PurePosixPath(mapping.subdir).parts
    stem = f"user-owned-{profile.name}-{primitive}"
    if mapping.extension == "/SKILL.md":
        tail = (*subdir_parts, stem, "SKILL.md")
    elif mapping.extension.startswith("."):
        tail = (*subdir_parts, f"{stem}{mapping.extension}")
    elif mapping.extension:
        tail = (*subdir_parts, f"{stem}-{mapping.extension}")
    else:
        tail = (*subdir_parts, stem, "sentinel.txt")
    return PurePosixPath(*root_parts, *tail)


def _sentinel_content(label: str, relative_path: PurePosixPath) -> bytes:
    return _SENTINEL_PREFIX + f"{label}:{relative_path.as_posix()}\n".encode("ascii")


def _root_id(root_dir: str) -> str:
    return "user-" + root_dir.replace("/", "-").replace(".", "dot")


def _plant_sentinels(sentinels: Iterable[_Sentinel]) -> None:
    for sentinel in sentinels:
        sentinel.path.parent.mkdir(parents=True, exist_ok=True)
        sentinel.path.write_bytes(sentinel.content)


def _assert_sentinels_unchanged(sentinels: Iterable[_Sentinel], phase: str) -> None:
    for sentinel in sentinels:
        assert sentinel.path.read_bytes() == sentinel.content, (
            f"{phase}: APM mutated unowned sentinel {sentinel.path}"
        )


def _external_roots_for_sentinels(
    sentinels: Sequence[_Sentinel],
    home: Path,
    *,
    collapse_to_home: bool = True,
) -> tuple[_ExternalRootSpec, ...]:
    """Group user sentinels into bounded snapshot roots."""
    if not collapse_to_home:
        grouped: dict[str, tuple[Path, list[PurePosixPath]]] = {}
        for sentinel in sentinels:
            if sentinel.root_id == "workspace":
                continue
            root, paths = grouped.setdefault(sentinel.root_id, (sentinel.root, []))
            assert root == sentinel.root
            paths.append(sentinel.relative_path)
        return tuple(
            _ExternalRootSpec(
                root_id=root_id,
                target=KNOWN_TARGETS["copilot"].name,
                path=root,
                config_paths=tuple(sorted(set(paths), key=lambda path: path.as_posix())),
            )
            for root_id, (root, paths) in sorted(grouped.items())
        )

    relative_paths = []
    for sentinel in sentinels:
        if sentinel.root_id == "workspace":
            continue
        relative_paths.append(PurePosixPath(*sentinel.path.relative_to(home).parts))
    return (
        _ExternalRootSpec(
            root_id="user-home",
            target=KNOWN_TARGETS["copilot"].name,
            path=home,
            config_paths=tuple(sorted(set(relative_paths), key=lambda path: path.as_posix())),
        ),
    )


def _capture_invariant_state(
    root: Path,
    *,
    targets: Sequence[str],
    project_sentinels: Sequence[_Sentinel],
    external_roots: Sequence[_ExternalRootSpec],
) -> LifecycleStateSnapshot:
    return LifecycleStateSnapshot.capture(
        root,
        targets=targets,
        config_paths=tuple(s.relative_path for s in project_sentinels),
        external_roots=tuple(
            LifecycleStateRoot(
                root_id=spec.root_id,
                target=spec.target,
                scope="user",
                path=spec.path,
                config_paths=spec.config_paths,
            )
            for spec in external_roots
        ),
    )


def _owned_file_paths(snapshot: LifecycleStateSnapshot) -> tuple[Path, ...]:
    return tuple(
        file.root_path.joinpath(*PurePosixPath(file.relative_path).parts)
        for file in snapshot.files
        if "deployment" in file.roles and file.kind == "file"
    )


def _lock_deployed_file_paths(lock_root: Path, deployment_root: Path) -> tuple[Path, ...]:
    """Return file paths from the lockfile's package deployment records."""
    lockfile = LockFile.read(lock_root / "apm.lock.yaml")
    assert lockfile is not None
    paths = [
        deployment_root.joinpath(*PurePosixPath(path).parts)
        for dependency in lockfile.get_package_dependencies()
        for path in dependency.deployed_files
    ]
    return tuple(sorted(paths))


def _assert_owned_files_removed(paths: Sequence[Path]) -> None:
    remaining = [path for path in paths if path.exists()]
    assert remaining == [], f"APM-owned deployed files survived uninstall: {remaining!r}"


def _seed_unowned_mcp_config(project_root: Path, target: str) -> Path:
    """Seed a same-named unowned MCP server in a target APM will not own."""
    assert target == "cursor"
    config = project_root / ".cursor" / "mcp.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "fixture-mcp": {
                        "command": "user-owned-fixture-mcp",
                    }
                }
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return config


@pytest.mark.parametrize("case", _OWNERSHIP_CASES, ids=lambda case: case.id)
def test_apm_only_touches_owned_state_across_lifecycle(
    tmp_path: Path,
    apm_binary_path: Path,
    case: _InvariantCase,
) -> None:
    """Run install, compile, audit, and uninstall without mutating sentinels."""
    scenario = _new_scenario(tmp_path / case.id, apm_binary_path)
    package_name = f"ownership-{case.id}"
    published = _publish_invariant_package(scenario, case, package_name)
    project = _create_consumer(scenario, case, published.dependency)
    project_sentinels = (
        _all_project_sentinels(project.root) if case.include_catalog_primitives else ()
    )
    user_sentinels = _all_user_sentinels(
        scenario.isolated.home,
        targets=case.targets if case.user_scope else None,
    )
    _plant_sentinels((*project_sentinels, *user_sentinels))
    external_roots = _external_roots_for_sentinels(
        user_sentinels,
        scenario.isolated.home,
        collapse_to_home=not case.user_scope,
    )
    mcp_config = (
        _seed_unowned_mcp_config(project.root, case.mcp_unowned_target)
        if case.mcp_unowned_target is not None
        else None
    )
    mcp_config_bytes = mcp_config.read_bytes() if mcp_config is not None else None

    state_root = scenario.isolated.config_root if case.user_scope else project.root
    lifecycle_targets = case.targets
    before = _capture_invariant_state(
        state_root,
        targets=lifecycle_targets,
        project_sentinels=() if case.user_scope else project_sentinels,
        external_roots=external_roots,
    )
    install_args = (
        (
            "install",
            "--global",
            "--target",
            case.targets[0],
            "--no-policy",
            "--parallel-downloads",
            "0",
        )
        if case.user_scope
        else _INSTALL_ARGS
    )

    _run_success(
        scenario,
        project,
        install_args,
        environment=published.environment,
        scenario_id=f"{case.id}-install",
    )
    after_install = _capture_invariant_state(
        state_root,
        targets=lifecycle_targets,
        project_sentinels=() if case.user_scope else project_sentinels,
        external_roots=external_roots,
    )
    _assert_sentinels_unchanged((*project_sentinels, *user_sentinels), "install")
    assert after_install.deployment_records != before.deployment_records
    owned_paths = (
        _lock_deployed_file_paths(state_root, scenario.isolated.home)
        if case.user_scope
        else _owned_file_paths(after_install)
    )
    if case.include_mcp:
        assert b"fixture-mcp" in after_install.mcp_state_bytes
    else:
        assert owned_paths, f"{case.id}: install produced no owned deployment files"
    if case.user_scope and case.include_hooks:
        hook_path = (
            scenario.isolated.home / ".copilot" / "hooks" / f"{package_name}-owned-hook.json"
        )
        assert hook_path in owned_paths
        assert hook_path.is_file()

    compile_targets = _compile_targets(case)
    if compile_targets:
        compile_args = (
            ("compile", "--global")
            if case.user_scope
            else ("compile", "--target", ",".join(compile_targets), "--force-instructions")
        )
        _run_success(
            scenario,
            project,
            compile_args,
            environment=published.environment,
            scenario_id=f"{case.id}-compile",
        )
        _assert_sentinels_unchanged((*project_sentinels, *user_sentinels), "compile")

    _audit(
        scenario,
        project,
        environment=published.environment,
        scenario_id=f"{case.id}-audit",
    )
    _assert_sentinels_unchanged((*project_sentinels, *user_sentinels), "audit")

    uninstall_args = (
        ("uninstall", "--global", published.remote_url)
        if case.user_scope
        else ("uninstall", f"{_OWNER}/{package_name}")
    )
    _run_success(
        scenario,
        project,
        uninstall_args,
        environment=published.environment,
        scenario_id=f"{case.id}-uninstall",
    )
    after_uninstall = _capture_invariant_state(
        state_root,
        targets=lifecycle_targets,
        project_sentinels=() if case.user_scope else project_sentinels,
        external_roots=external_roots,
    )

    _assert_sentinels_unchanged((*project_sentinels, *user_sentinels), "uninstall")
    _assert_owned_files_removed(owned_paths)
    assert after_uninstall.lockfile_bytes is None
    assert after_uninstall.deployment_records == ()
    assert b"fixture-mcp" not in after_uninstall.mcp_state_bytes
    if mcp_config is not None:
        assert mcp_config.read_bytes() == mcp_config_bytes


def test_apm_install_dry_run_preserves_complete_unowned_state(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Preview install must leave project, deployment, and user roots byte-identical."""
    case = _InvariantCase("dry-run-preview", _project_lifecycle_targets())
    scenario = _new_scenario(tmp_path / case.id, apm_binary_path)
    published = _publish_invariant_package(scenario, case, "ownership-dry-run")
    project = _create_consumer(scenario, case, published.dependency)
    project_sentinels = _all_project_sentinels(project.root)
    user_sentinels = _all_user_sentinels(scenario.isolated.home)
    _plant_sentinels((*project_sentinels, *user_sentinels))
    external_roots = _external_roots_for_sentinels(user_sentinels, scenario.isolated.home)
    before = _capture_invariant_state(
        project.root,
        targets=case.targets,
        project_sentinels=project_sentinels,
        external_roots=external_roots,
    )

    _run_success(
        scenario,
        project,
        (*_INSTALL_ARGS, "--dry-run"),
        environment=published.environment,
        scenario_id="ownership-dry-run-install",
    )
    after = _capture_invariant_state(
        project.root,
        targets=case.targets,
        project_sentinels=project_sentinels,
        external_roots=external_roots,
    )

    _assert_sentinels_unchanged((*project_sentinels, *user_sentinels), "dry-run")
    assert (scenario.isolated.config_root / "apm.yml").exists() is False
    assert (scenario.isolated.config_root / "config.json").exists() is False
    assert (scenario.isolated.config_root / "apm_modules").exists() is False
    _assert_same_state(before, after)
