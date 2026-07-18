"""Hermetic primitive-to-target lifecycle covering array.

The rows deliberately exercise valid sparse pairs, rather than every primitive
against every target. Compatibility is read from ``KNOWN_TARGETS`` so this
module cannot become a second target-routing authority.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import pytest

from apm_cli.deps.lockfile import LockFile
from apm_cli.integration.targets import KNOWN_TARGETS, TargetProfile
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.artifact_snapshot import ArtifactSnapshot, assert_unchanged
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.lifecycle_state import LifecycleStateRoot, LifecycleStateSnapshot
from tests.utils.local_git_repository import LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackage, LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_e2e_mode,
    pytest.mark.requires_apm_binary,
]

_SCENARIO_TIMEOUT_SECONDS = 300.0
_REMOTE_PREFIX = "https://gitlab.example.invalid/apm-lifecycle"


@dataclass(frozen=True)
class _Row:
    """One valid primitive/target/scope lifecycle or honest refusal boundary."""

    id: str
    primitives: tuple[str, ...]
    targets: tuple[str, ...]
    user_scope: bool
    dynamic_refusal: bool = False
    widen_targets: tuple[str, ...] = ()
    narrow_targets: tuple[str, ...] = ()


_ROWS = (
    _Row("copilot-skill-canvas-project", ("skills", "canvas"), ("copilot",), False),
    _Row("claude-agent-user", ("agents",), ("claude",), True),
    _Row(
        "copilot-prompt-widen-narrow",
        ("prompts",),
        ("copilot",),
        False,
        widen_targets=("copilot", "cursor"),
        narrow_targets=("copilot",),
    ),
    _Row("gemini-command-user", ("commands",), ("gemini",), True),
    _Row("cursor-instruction-project", ("instructions",), ("cursor",), False),
    _Row(
        "claude-codex-hook-narrow",
        ("hooks",),
        ("claude", "codex"),
        False,
        narrow_targets=("claude",),
    ),
    _Row("kiro-hook-user", ("hooks",), ("kiro",), True),
    _Row("opencode-skill-user", ("skills",), ("opencode",), True),
    _Row(
        "copilot-app-unavailable",
        ("prompts",),
        ("copilot-app",),
        True,
        dynamic_refusal=True,
    ),
    _Row(
        "copilot-cowork-unavailable",
        ("skills",),
        ("copilot-cowork",),
        True,
        dynamic_refusal=True,
    ),
)


def _assert_sparse_covering_array() -> None:
    """Make the reviewable rows prove the intended bounded coverage."""
    covered_primitives = {primitive for row in _ROWS for primitive in row.primitives}
    assert covered_primitives == {
        "skills",
        "agents",
        "prompts",
        "commands",
        "instructions",
        "hooks",
        "canvas",
    }
    assert {(row.user_scope, row.dynamic_refusal) for row in _ROWS} == {
        (False, False),
        (True, False),
        (True, True),
    }
    assert {target for row in _ROWS for target in row.targets} == {
        "copilot",
        "claude",
        "cursor",
        "gemini",
        "kiro",
        "codex",
        "opencode",
        "copilot-app",
        "copilot-cowork",
    }
    assert sum(bool(row.widen_targets or row.narrow_targets) for row in _ROWS) == 2


def _row_profiles(row: _Row) -> tuple[TargetProfile, ...]:
    """Resolve valid row targets through the canonical target catalog."""
    profiles = []
    for target_name in row.targets:
        profile = KNOWN_TARGETS[target_name]
        if not row.dynamic_refusal:
            profile = profile.for_scope(user_scope=row.user_scope)
            assert profile is not None, f"{row.id}: {target_name} is unavailable at this scope"
        for primitive in row.primitives:
            assert profile.supports(primitive), (
                f"{row.id}: {target_name} does not support {primitive} at this scope"
            )
        profiles.append(profile)
    return tuple(profiles)


def _feature_flags(row: _Row) -> tuple[str, ...]:
    """Return the feature flags required by one executable row."""
    flags = []
    if "canvas" in row.primitives:
        flags.append("canvas")
    for target in row.targets:
        flag = KNOWN_TARGETS[target].requires_flag
        if flag is not None:
            flags.append(flag)
    return tuple(sorted(set(flags)))


def _expected_ledger_targets(row: _Row, targets: tuple[str, ...] | None = None) -> set[str]:
    """Resolve ledger ownership through each canonical target profile."""
    owners = set()
    for target_name in targets or row.targets:
        profile = KNOWN_TARGETS[target_name].for_scope(user_scope=row.user_scope)
        assert profile is not None
        if any(primitive in profile.primitives for primitive in row.primitives):
            owners.add(profile.name)
    return owners


def _source_content(row: _Row, primitive: str) -> str:
    """Return one intentionally small valid primitive document."""
    marker = f"covering-array-{row.id}-{primitive}"
    if primitive == "skills":
        return f"---\nname: {marker}\ndescription: Lifecycle fixture\n---\n# {marker}\n"
    if primitive == "instructions":
        return f"---\napplyTo: '**'\ndescription: Lifecycle fixture\n---\n# {marker}\n"
    return f"---\ndescription: {marker}\n---\n{marker}\n"


def _add_primitives(factory: LocalPackageFactory, package: LocalPackage, row: _Row) -> None:
    """Author only the row's primitive sources through the shared fixture API."""
    for primitive in row.primitives:
        name = f"{primitive}-{row.id}"
        if primitive == "skills":
            factory.add_skill(package, name, _source_content(row, primitive))
        elif primitive == "agents":
            factory.add_agent(package, name, _source_content(row, primitive))
        elif primitive == "prompts":
            factory.add_prompt(package, name, _source_content(row, primitive))
        elif primitive == "commands":
            factory.add_command(package, name, _source_content(row, primitive))
        elif primitive == "instructions":
            factory.add_instruction(package, name, _source_content(row, primitive))
        elif primitive == "hooks":
            factory.add_hook(
                package,
                name,
                {
                    "hooks": {
                        "PreToolUse": [
                            {
                                "matcher": "Bash",
                                "hooks": [{"type": "command", "command": "echo lifecycle"}],
                            }
                        ]
                    }
                },
            )
        elif primitive == "canvas":
            factory.add_canvas(
                package,
                name,
                "export default { activate() {} };\n",
                assets={PurePosixPath("assets/info.txt"): b"lifecycle fixture\n"},
            )
        else:
            raise AssertionError(f"Unknown primitive {primitive!r}")


def _write_user_manifest(
    isolated: IsolatedApmEnvironment,
    *,
    name: str,
    remote: str,
    revision: str,
    targets: tuple[str, ...],
) -> None:
    """Seed the user manifest at the CLI's canonical user-scope location."""
    manifest = {
        "name": f"{name}-user",
        "version": "0.1.0",
        "description": "Hermetic target lifecycle consumer",
        "dependencies": {
            "apm": [
                {
                    "git": remote,
                    "type": "gitlab",
                    "ref": revision,
                    "alias": name,
                }
            ]
        },
    }
    if targets:
        manifest["targets"] = list(targets)
    dump_yaml(manifest, isolated.config_root / "apm.yml")


def _set_project_targets(project: LocalPackage, targets: tuple[str, ...]) -> None:
    """Change only the consumer target declaration for a transition row."""
    manifest = load_yaml(project.manifest_path)
    assert isinstance(manifest, dict)
    manifest["targets"] = list(targets)
    dump_yaml(manifest, project.manifest_path)


def _run(
    runner: ApmLifecycleRunner,
    args: tuple[str, ...],
    *,
    scenario_id: str,
    cwd: Path,
    environment: dict[str, str],
) -> CommandResult:
    """Run one command and report its captured evidence on failure."""
    result = runner.run(args, scenario_id=scenario_id, cwd=cwd, env=environment)
    assert result.returncode == 0, (
        f"{scenario_id} failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return result


def _enable_required_features(
    runner: ApmLifecycleRunner,
    row: _Row,
    *,
    cwd: Path,
    environment: dict[str, str],
) -> None:
    """Enable feature-gated executable surfaces using the public CLI."""
    for flag in _feature_flags(row):
        _run(
            runner,
            ("experimental", "enable", flag),
            scenario_id=f"{row.id}-enable-{flag}",
            cwd=cwd,
            environment=environment,
        )


def _user_deploy_root(
    isolated: IsolatedApmEnvironment,
    profile: TargetProfile,
    primitive: str,
) -> Path:
    """Derive the external user deployment root from the resolved profile."""
    mapping = profile.primitives[primitive]
    root_dir = mapping.deploy_root or profile.root_dir
    root = isolated.home / root_dir
    root.mkdir(parents=True, exist_ok=True)
    return root


def _capture_state(
    row: _Row,
    *,
    isolated: IsolatedApmEnvironment,
    project_root: Path,
    profiles: tuple[TargetProfile, ...],
) -> LifecycleStateSnapshot:
    """Capture ledger-backed materialization using explicit bounded roots."""
    if not row.user_scope:
        return LifecycleStateSnapshot.capture(project_root, targets=row.targets)

    external_roots = tuple(
        LifecycleStateRoot(
            root_id=f"{profile.name}-{primitive}",
            target=profile.name,
            scope="user",
            path=_user_deploy_root(isolated, profile, primitive),
        )
        for profile in profiles
        for primitive in row.primitives
        if primitive in profile.primitives
    )
    return LifecycleStateSnapshot.capture(
        isolated.config_root,
        targets=(),
        external_roots=external_roots,
    )


def _assert_materialized(
    row: _Row,
    *,
    before: LifecycleStateSnapshot,
    after: LifecycleStateSnapshot,
    lock_root: Path,
    deployment_root: Path,
) -> None:
    """Assert durable deployment bytes, ledger rows, and native hook sidecars."""
    assert before.deployment_records == ()
    records = _deployment_records(after, lock_root)
    deployed_files: list[Path] = []
    if records:
        assert {record.locator.target for record in records} >= _expected_ledger_targets(row)
        deployed_paths = [deployment_root / record.locator.value for record in records]
        assert all(path.exists() for path in deployed_paths), (
            f"{row.id}: ledger includes missing deployment paths: {deployed_paths}"
        )
        deployed_files = [path for path in deployed_paths if path.is_file()]
        assert deployed_files, f"{row.id}: ledger did not resolve to deployed bytes"
        assert all(path.read_bytes() for path in deployed_files)

        if "instructions" in row.primitives:
            source_marker = f"covering-array-{row.id}-instructions".encode()
            assert any(source_marker in path.read_bytes() for path in deployed_files), (
                f"{row.id}: transformed instruction did not retain fixture content"
            )
    else:
        assert row.primitives == ("hooks",), f"{row.id}: install omitted deployment ledger rows"

    if "hooks" in row.primitives:
        hook_configs = [
            deployment_root / KNOWN_TARGETS[target].hooks_config_display
            for target in row.targets
            if KNOWN_TARGETS[target].hooks_config_display
        ]
        if hook_configs:
            assert all(path.is_file() for path in hook_configs)
            marker = f"fixture-{row.id}"
            sidecars = [path.with_name("apm-hooks.json") for path in hook_configs]
            assert all(marker in path.read_text(encoding="utf-8") for path in sidecars)
        else:
            assert records and all(path.suffix == ".json" for path in deployed_files)
    if "canvas" in row.primitives:
        assert any(path.name == "extension.mjs" for path in deployed_files), (
            f"{row.id}: canvas entry point was not deployed"
        )


def _deployment_records(
    snapshot: LifecycleStateSnapshot,
    lock_root: Path,
) -> tuple:
    """Read the canonical ledger from its project or isolated user lock."""
    if snapshot.deployment_records:
        return snapshot.deployment_records
    lock = LockFile.read(lock_root / "apm.lock.yaml")
    if lock is None:
        return ()
    return tuple(record for _key, record in sorted(lock.deployment_ledger.records.items()))


def _assert_removed(
    after_uninstall: LifecycleStateSnapshot,
    *,
    lock_root: Path,
    deployment_root: Path,
    deployed_records: tuple,
) -> None:
    """Require the final lifecycle operation to clear every deployment record."""
    assert _deployment_records(after_uninstall, lock_root) == ()
    assert all(not (deployment_root / record.locator.value).exists() for record in deployed_records)


def _run_dynamic_refusal(
    row: _Row,
    *,
    runner: ApmLifecycleRunner,
    cwd: Path,
    environment: dict[str, str],
    isolated: IsolatedApmEnvironment,
) -> None:
    """Prove unavailable external targets do not alter a last-known-good state."""
    _enable_required_features(runner, row, cwd=cwd, environment=environment)
    _run(
        runner,
        ("install", "--global", "--target", "copilot", "--no-policy", "--parallel-downloads", "0"),
        scenario_id=f"{row.id}-baseline-copilot",
        cwd=cwd,
        environment=environment,
    )
    before = ArtifactSnapshot.capture(isolated.root)
    result = runner.run(
        (
            "install",
            "--global",
            "--target",
            row.targets[0],
            "--no-policy",
            "--parallel-downloads",
            "0",
        ),
        scenario_id=row.id,
        cwd=cwd,
        env=environment,
    )
    assert result.returncode != 0, f"{row.id}: unavailable external target unexpectedly succeeded"
    after = ArtifactSnapshot.capture(isolated.root)
    assert_unchanged(before, after)
    assert (
        "cowork://" not in (isolated.config_root / "apm.lock.yaml").read_text(encoding="utf-8")
        if (isolated.config_root / "apm.lock.yaml").exists()
        else True
    )
    assert (
        "copilot-app-db://"
        not in (isolated.config_root / "apm.lock.yaml").read_text(encoding="utf-8")
        if (isolated.config_root / "apm.lock.yaml").exists()
        else True
    )


@pytest.mark.parametrize("row", _ROWS, ids=lambda row: row.id)
def test_primitive_target_covering_array(
    tmp_path: Path,
    apm_binary_path: Path,
    row: _Row,
) -> None:
    """Drive each sparse pair through install, convergence, transition, and cleanup."""
    _assert_sparse_covering_array()
    profiles = _row_profiles(row)
    isolated = IsolatedApmEnvironment.create(tmp_path / row.id, base_env=dict(os.environ))
    environment = isolated.subprocess_env()
    runner = ApmLifecycleRunner(
        (str(apm_binary_path),),
        scenario_timeout_seconds=_SCENARIO_TIMEOUT_SECONDS,
    )

    source_factory = LocalPackageFactory(isolated.package_root)
    package_name = f"fixture-{row.id}"
    manifest_targets = () if row.dynamic_refusal else row.targets
    source = source_factory.create(package_name, targets=manifest_targets)
    _add_primitives(source_factory, source, row)
    repositories = LocalGitRepositoryFactory(isolated.repository_root, env=environment)
    repository = repositories.create(package_name, source_tree=source.root)
    revision = repositories.commit(repository, message="seed primitive target lifecycle")
    remote = f"{_REMOTE_PREFIX}/{package_name}.git"
    repositories.install_url_rewrite(repository, remote)

    project_factory = LocalPackageFactory(isolated.work_root)
    project = project_factory.create(
        f"consumer-{row.id}",
        dependencies=(
            ()
            if row.user_scope
            else (
                {
                    "git": remote,
                    "type": "gitlab",
                    "ref": revision.sha,
                    "alias": package_name,
                },
            )
        ),
        targets=() if row.user_scope else manifest_targets,
    )
    if row.user_scope:
        _write_user_manifest(
            isolated,
            name=package_name,
            remote=remote,
            revision=revision.sha,
            targets=manifest_targets,
        )

    cwd = project.root
    project_before = ArtifactSnapshot.capture(project.root)
    if row.dynamic_refusal:
        _run_dynamic_refusal(
            row,
            runner=runner,
            cwd=cwd,
            environment=environment,
            isolated=isolated,
        )
        assert_unchanged(project_before, ArtifactSnapshot.capture(project.root))
        return

    _enable_required_features(runner, row, cwd=cwd, environment=environment)
    state_root = isolated.config_root if row.user_scope else project.root
    before = _capture_state(
        row,
        isolated=isolated,
        project_root=state_root,
        profiles=profiles,
    )
    install_args = (
        (
            "install",
            "--global",
            "--target",
            row.targets[0],
            "--no-policy",
            "--parallel-downloads",
            "0",
        )
        if row.user_scope
        else (
            "install",
            "--target",
            ",".join(row.targets),
            "--no-policy",
            "--parallel-downloads",
            "0",
        )
    )
    _run(runner, install_args, scenario_id=f"{row.id}-install", cwd=cwd, environment=environment)
    if row.user_scope:
        assert_unchanged(project_before, ArtifactSnapshot.capture(project.root))
    after_install = _capture_state(
        row,
        isolated=isolated,
        project_root=state_root,
        profiles=profiles,
    )
    deployment_root = isolated.home if row.user_scope else project.root
    _assert_materialized(
        row,
        before=before,
        after=after_install,
        lock_root=state_root,
        deployment_root=deployment_root,
    )
    deployed_records = _deployment_records(after_install, state_root)

    _run(runner, install_args, scenario_id=f"{row.id}-reinstall", cwd=cwd, environment=environment)
    if row.user_scope:
        assert_unchanged(project_before, ArtifactSnapshot.capture(project.root))
    after_reinstall = _capture_state(
        row,
        isolated=isolated,
        project_root=state_root,
        profiles=profiles,
    )
    assert after_reinstall.semantic_bytes == after_install.semantic_bytes

    if row.widen_targets:
        _set_project_targets(project, row.widen_targets)
        _run(
            runner,
            ("install", "--no-policy", "--parallel-downloads", "0"),
            scenario_id=f"{row.id}-widen",
            cwd=cwd,
            environment=environment,
        )
        after_widen = _capture_state(
            row,
            isolated=isolated,
            project_root=state_root,
            profiles=tuple(KNOWN_TARGETS[target] for target in row.widen_targets),
        )
        widen_records = _deployment_records(after_widen, state_root)
        assert {record.locator.target for record in widen_records} >= set(row.widen_targets)
        cursor_paths = [
            deployment_root / record.locator.value
            for record in widen_records
            if record.locator.target == "cursor"
        ]
        assert cursor_paths and all(path.is_file() and path.read_bytes() for path in cursor_paths)
        _run(
            runner,
            ("install", "--no-policy", "--parallel-downloads", "0"),
            scenario_id=f"{row.id}-reinstall-widened",
            cwd=cwd,
            environment=environment,
        )
        after_widen_reinstall = _capture_state(
            row,
            isolated=isolated,
            project_root=state_root,
            profiles=tuple(KNOWN_TARGETS[target] for target in row.widen_targets),
        )
        assert after_widen_reinstall.semantic_bytes == after_widen.semantic_bytes

    if row.narrow_targets:
        _set_project_targets(project, row.narrow_targets)
        _run(
            runner,
            ("install", "--no-policy", "--parallel-downloads", "0"),
            scenario_id=f"{row.id}-narrow",
            cwd=cwd,
            environment=environment,
        )
        _run(
            runner,
            ("prune",),
            scenario_id=f"{row.id}-prune-narrowed-target",
            cwd=cwd,
            environment=environment,
        )
        after_transition = _capture_state(
            row,
            isolated=isolated,
            project_root=state_root,
            profiles=tuple(KNOWN_TARGETS[target] for target in row.narrow_targets),
        )
        transition_records = _deployment_records(after_transition, state_root)
        if transition_records:
            assert {
                record.locator.target for record in transition_records
            } == _expected_ledger_targets(row, row.narrow_targets)
        else:
            assert row.primitives == ("hooks",)
            marker = f"fixture-{row.id}"
            assert marker in (project.root / ".claude/apm-hooks.json").read_text(encoding="utf-8")

    if row.primitives == ("hooks",) and row.narrow_targets:
        _set_project_targets(project, row.targets)

    uninstall_args = (
        ("uninstall", remote, "--global")
        if row.user_scope
        else (
            "uninstall",
            remote,
        )
    )
    _run(
        runner,
        uninstall_args,
        scenario_id=f"{row.id}-uninstall",
        cwd=cwd,
        environment=environment,
    )
    if row.user_scope:
        assert_unchanged(project_before, ArtifactSnapshot.capture(project.root))
    after_uninstall = _capture_state(
        row,
        isolated=isolated,
        project_root=state_root,
        profiles=profiles,
    )
    _assert_removed(
        after_uninstall,
        lock_root=state_root,
        deployment_root=deployment_root,
        deployed_records=deployed_records,
    )
    if "hooks" in row.primitives:
        marker = f"fixture-{row.id}"
        for target in row.targets:
            display = KNOWN_TARGETS[target].hooks_config_display
            if display is None:
                continue
            sidecar = deployment_root / Path(display).with_name("apm-hooks.json")
            assert not sidecar.exists() or marker not in sidecar.read_text(encoding="utf-8")
