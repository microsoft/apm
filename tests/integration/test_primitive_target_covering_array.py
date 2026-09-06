"""Real CLI routing cells and constrained source/ref/cache interaction witnesses."""

from __future__ import annotations

import json
import os
import shutil
import time
from collections.abc import Callable
from dataclasses import asdict, replace
from pathlib import Path, PurePosixPath

import pytest

from apm_cli.cache.url_normalize import cache_shard_key
from apm_cli.deps.lockfile import LockFile
from apm_cli.integration.targets import KNOWN_TARGETS
from apm_cli.models.dependency import DependencyReference
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.artifact_snapshot import (
    ArtifactSnapshotSet,
    assert_snapshot_changes_within,
    assert_snapshot_set_unchanged,
)
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.lifecycle_interaction_oracle import (
    InteractionOracle,
    RoutingExpectation,
    SourceFixture,
    expected_routing,
)
from tests.utils.lifecycle_interactions import (
    INTERACTION_ROWS,
    ROUTING_ROWS,
    CaseExecution,
    RoutingRow,
    feature_flags,
    known_gap_for,
    required_transitions,
    row_profiles,
    validate_routing_rows,
)
from tests.utils.local_git_repository import LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackage, LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_e2e_mode,
    pytest.mark.requires_apm_binary,
]

_REMOTE_PREFIX = "https://gitlab.example.invalid/apm-lifecycle"
_TAG = "lifecycle-v1"
_ROWS = (*ROUTING_ROWS, *INTERACTION_ROWS)


def _assert_covering_array() -> None:
    """Retain the old callable while keeping metadata in its pure owner."""
    validate_routing_rows(ROUTING_ROWS)


def _author(
    factory: LocalPackageFactory, package: LocalPackage, row: RoutingRow, *, suffix: str = ""
) -> SourceFixture:
    """Author one source primitive with a stable distinct package-level marker."""
    kind = row.primitives[0]
    name = f"{kind}-{row.id}{suffix}"
    marker = f"covering-array-{row.id}{suffix}-version-a"
    document = (
        f"---\nname: {name}\ndescription: Lifecycle fixture\napplyTo: '**'\n---\n# {marker}\n"
    )
    before = set(package.root.rglob("*"))
    if kind == "hooks":
        factory.add_hook(
            package,
            name,
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Bash",
                            "hooks": [{"type": "command", "command": f"echo {marker}"}],
                        }
                    ]
                }
            },
        )
    elif kind == "canvas":
        factory.add_canvas(
            package,
            name,
            f"// {marker}\nexport default {{ activate() {{}} }};\n",
            assets={PurePosixPath("assets/info.txt"): f"{marker}\n".encode()},
        )
    else:
        author = {
            "skills": factory.add_skill,
            "agents": factory.add_agent,
            "instructions": factory.add_instruction,
            "prompts": factory.add_prompt,
            "commands": factory.add_command,
        }[kind]
        author(package, name, document)
    paths = tuple(
        path.relative_to(package.root).as_posix()
        for path in sorted(set(package.root.rglob("*")) - before)
        if path.is_file()
    )
    return SourceFixture(package.name, kind, name, marker, paths)


def _result(result: CommandResult, operation: str, *, success: bool = True) -> None:
    assert (result.returncode == 0) is success, (
        f"{operation}: exit={result.returncode}\n{result.stdout}\n{result.stderr}"
    )


def _seed_unowned(oracle: InteractionOracle, lifetime: RoutingExpectation) -> None:
    """Protect neighbors and shared members inside otherwise writable target roots."""
    project, home = oracle.roots["project"], oracle.roots["user"]
    deploy_root = oracle.roots[oracle.deployment_root_id]
    sentinels = {
        project / ".outside-row": b"unowned project\n",
        project / ".github/workflows/unrelated.yml": b"name: unrelated\n",
        home / ".config/unrelated-app/settings.json": b'{"keep":true}\n',
        home / ".apm/unrelated.txt": b"unowned apm\n",
        home / ".local/unrelated.txt": b"unowned local\n",
    }
    for path in lifetime.files:
        sentinels[deploy_root / PurePosixPath(path).parts[0] / "unrelated.txt"] = (
            b"unowned neighbor\n"
        )
    shared = {
        deploy_root / name: {"lifecycleUnowned": {"keep": ["foreign-package", "user-entry"]}}
        for name in lifetime.shared
        if name.endswith(".json") and not name.endswith("apm-hooks.json")
    }
    seed_paths = {
        name: {
            path.relative_to(root).as_posix()
            for path in (*sentinels, *shared)
            if path.is_relative_to(root)
        }
        for name, root in oracle.roots.items()
    }
    with oracle.transition("seed-unowned", exact=seed_paths):
        for path, content in sentinels.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        for path, payload in shared.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload) + "\n", encoding="ascii")
    oracle.evaluated()
    oracle.protected_json.update(shared)


def _assert_provenance(oracle: InteractionOracle, expected_commits: dict[str, str]) -> None:
    """Cross-check the materialized dependency graph against authored source inputs."""
    lock = LockFile.read(oracle.lock_root / "apm.lock.yaml")
    assert lock is not None
    entries = lock.get_package_dependencies()
    assert len(entries) == len(oracle.sources), "Dependency shape was not materialized"
    if oracle.row.source_kind == "git":
        assert {entry.name: entry.resolved_commit for entry in entries} == expected_commits
        if oracle.row.ref_state == "tag":
            assert all(entry.resolved_ref == _TAG for entry in entries)
    else:
        assert all(entry.resolved_commit is None for entry in entries)
    if oracle.row.id.startswith("interaction-"):
        oracle.evaluations.append(("provenance", ("source.ref_cache_coherent",)))


def _assert_reinstall(
    oracle: InteractionOracle,
    before: ArtifactSnapshotSet,
    observations: list[tuple[ArtifactSnapshotSet, ArtifactSnapshotSet]] | None,
) -> None:
    """Keep the known second-pass exception narrower than ordinary install writes."""
    after = oracle.capture()
    if observations is not None:
        observations.append((before, after))
    if known_gap_for(oracle.row) is None:
        assert_snapshot_set_unchanged(before, after)
        oracle.evaluations.append(("stable-reinstall", ("idempotency.byte_stable",)))
    else:
        assert_snapshot_changes_within(
            before,
            after,
            exact_paths={
                "user": {
                    ".apm/apm.lock.yaml",
                    *expected_routing(oracle.row, oracle.sources).files,
                }
            },
            tree_prefixes={},
        )


def execute_row(
    tmp_path: Path,
    apm_binary_path: Path,
    row: RoutingRow,
    *,
    idempotency_snapshots: list[tuple[ArtifactSnapshotSet, ArtifactSnapshotSet]] | None = None,
    record_execution: Callable[[CaseExecution], None] | None = None,
    relative_local_children: bool | None = None,
) -> CaseExecution:
    """Run one entire isolated row, returning only actually evaluated evidence."""
    started = time.monotonic()
    runner = ApmLifecycleRunner((str(apm_binary_path),), scenario_timeout_seconds=300)
    observed: list[InteractionOracle] = []
    try:
        with runner.scenario(scenario_id=row.id):
            evidence = _execute_row(
                tmp_path,
                row,
                runner,
                started,
                idempotency_snapshots,
                observed,
                relative_local_children,
            )
    except Exception as error:
        if observed:
            evidence = _evidence(
                tmp_path,
                row,
                observed[0],
                started,
                str(error),
                status="failed" if "install" in observed[0].operations else "setup_failed",
            )
        else:
            evidence = CaseExecution(
                row.id,
                "setup_failed",
                (),
                (),
                time.monotonic() - started,
                str(error),
            )
            _write_evidence(tmp_path, evidence)
        if record_execution is not None:
            record_execution(evidence)
        raise
    if record_execution is not None:
        record_execution(evidence)
    return evidence


def _execute_row(
    tmp_path: Path,
    row: RoutingRow,
    runner: ApmLifecycleRunner,
    started: float,
    idempotency_snapshots: list[tuple[ArtifactSnapshotSet, ArtifactSnapshotSet]] | None,
    observed: list[InteractionOracle],
    relative_local_children: bool | None,
) -> CaseExecution:
    isolated = IsolatedApmEnvironment.create(tmp_path / row.id, base_env=dict(os.environ))
    environment = isolated.subprocess_env()
    # Gated target names cannot yet appear in a fixture manifest before enable.
    declared = (
        ()
        if row.dynamic_refusal or any(KNOWN_TARGETS[target].requires_flag for target in row.targets)
        else row.widen_targets or row.targets
    )
    packages: list[LocalPackage] = []
    fixtures: list[SourceFixture] = []
    rewrites = []
    git_sources = []
    expected_commits = {}
    dependencies = []
    names = (
        (f"fixture-{row.id}-child", f"fixture-{row.id}")
        if (row.dependency_shape == "transitive")
        else (f"fixture-{row.id}",)
    )
    project_root = isolated.work_root / f"consumer-{row.id}"
    lock_root = isolated.config_root if row.user_scope else project_root
    route_row = (
        replace(row, targets=("copilot",), dynamic_refusal=False) if row.dynamic_refusal else row
    )
    row_profiles(route_row)
    root_id = "user" if row.user_scope else "project"
    deploy_root = isolated.home if row.user_scope else project_root
    oracle = InteractionOracle(
        {
            "project": project_root,
            "user": isolated.home,
            "cache": isolated.cache_root,
            "sources": isolated.package_root,
            "repositories": isolated.repository_root,
        },
        root_id,
        lock_root,
        (),
        route_row,
    )
    observed.append(oracle)
    with oracle.transition(
        "source-fixture",
        exact={"project": {"apm.yml"}, "user": {".apm/apm.yml"}},
        trees={
            "sources": set(names),
            "repositories": {
                f"{name}{suffix}" for name in names for suffix in (".git", "-worktree")
            },
        },
    ):
        factory = LocalPackageFactory(isolated.package_root)
        repositories = LocalGitRepositoryFactory(isolated.repository_root, env=environment)
        for index, name in enumerate(names):
            local_child = row.source_kind == "local" and bool(packages)
            relative_child = (
                not row.user_scope if relative_local_children is None else relative_local_children
            )
            authored_dependencies = (
                [{"path": packages[0].root.as_posix()}]
                if local_child and not relative_child
                else dependencies
            )
            package = factory.create(name, targets=declared, dependencies=authored_dependencies)
            if local_child and relative_child:
                factory.add_relative_dependency(package, packages[0])
            if local_child:
                child_path = load_yaml(package.manifest_path)["dependencies"]["apm"][0]["path"]
                assert (package.root / child_path).resolve() == packages[0].root
                assert (package.root / child_path / "apm.yml").is_file()
            fixture = _author(
                factory, package, row, suffix="-child" if index == 0 and len(names) == 2 else ""
            )
            packages.append(package)
            fixtures.append(fixture)
            if row.source_kind == "git":
                repository = repositories.create(name, source_tree=package.root)
                commit = repositories.commit(repository, message="seed interaction")
                if row.ref_state == "tag":
                    repositories.tag(repository, _TAG, commit)
                remote = f"{_REMOTE_PREFIX}/{name}.git"
                rewrites.append((repository, remote))
                dependencies = [
                    {
                        "git": remote,
                        "type": "gitlab",
                        "ref": _TAG if row.ref_state == "tag" else commit.sha,
                        "alias": name,
                    }
                ]
                expected_commits[name] = commit.sha
                git_sources.append((repository, fixture))
        if rewrites:
            environment = repositories.url_rewrite_subprocess_env_many(rewrites)
        if row.source_kind == "local":
            dependencies = [{"path": packages[-1].root.as_posix()}]
        project = LocalPackageFactory(isolated.work_root).create(
            f"consumer-{row.id}",
            dependencies=() if row.user_scope else dependencies,
            targets=() if row.user_scope or not declared else row.targets,
        )
        if row.user_scope:
            dump_yaml(
                {
                    "name": f"consumer-{row.id}",
                    "version": "0.1.0",
                    "dependencies": {"apm": dependencies},
                    **({"targets": list(row.targets)} if declared else {}),
                },
                lock_root / "apm.yml",
            )
    oracle.evaluated()
    sources = tuple(fixtures)
    oracle.sources = sources
    all_targets = tuple(
        dict.fromkeys((*route_row.targets, *row.widen_targets, *row.narrow_targets))
    )
    lifetime = expected_routing(route_row, sources, all_targets)
    _seed_unowned(oracle, lifetime)

    control = {"apm.lock.yaml", ".gitignore"}
    native = set(lifetime.files)
    module_files = set()
    for index, (package, source) in enumerate(zip(packages, sources, strict=True)):
        if row.source_kind == "git":
            dependency = DependencyReference.parse_from_dict(
                {"git": rewrites[index][1], "type": "gitlab"}
            )
        else:
            dependency = DependencyReference.parse_from_dict({"path": package.root.as_posix()})
            if index == 0 and len(packages) == 2:
                dependency.declaring_parent = packages[-1].root.as_posix()
                dependency.anchored_local_path = package.root.as_posix()
        materialized = dependency.get_install_path(lock_root / "apm_modules")
        for relative in ("apm.yml", ".apm-pin", *source.source_files):
            module_files.add(f"apm_modules/{package.name}/{relative}")
            module_files.add((materialized / relative).relative_to(lock_root).as_posix())
    exact = {
        "project": set() if row.user_scope else {*control, *native, *module_files},
        "user": {".apm/config.json", ".local/state/gh/device-id"},
        "cache": {"git", "git/db_v1", "git/checkouts_v1"},
    }
    exact[root_id].update(profile.root_dir for profile in row_profiles(route_row))
    if row.user_scope:
        exact["user"].update(native)
        exact["user"].update(f".apm/{name}" for name in (*control, *module_files))
    cache_trees = {
        "cache": {
            f"git/{bucket}/{cache_shard_key(remote)}"
            for _repository, remote in rewrites
            for bucket in ("db_v1", "checkouts_v1")
        }
    }

    def action(
        operation: str,
        arguments: tuple[str, ...],
        *,
        targets: tuple[str, ...] | None = None,
        success: bool = True,
        unchanged: bool = False,
        manifest: bool = False,
        cwd: Path | None = None,
    ) -> CommandResult:
        allowed = {name: set(paths) for name, paths in exact.items()}
        if manifest:
            allowed[root_id].add(".apm/apm.yml" if row.user_scope else "apm.yml")
        result = oracle.observe(
            operation,
            lambda: runner.run(
                arguments,
                scenario_id=f"{row.id}-{operation}",
                cwd=cwd or project.root,
                env=environment,
            ),
            exact=allowed,
            trees=cache_trees,
            unchanged=unchanged,
        )
        _result(result, operation, success=success)
        if operation == "install":
            _write_install_observation(tmp_path, oracle, result)
        if targets is not None:
            try:
                oracle.assert_routing(targets)
            except AssertionError as error:
                error.add_note(f"{operation} output:\n{result.stdout}\n{result.stderr}")
                raise
        laws = ["outcome.status_matches_state"]
        if targets is not None:
            laws.append("routing.authorized_targets_only")
        if not success:
            laws.append("transaction.failed_command_preserves_state")
        oracle.evaluated(*laws)
        return result

    for flag in feature_flags(row):
        action(f"enable-{flag}", ("experimental", "enable", flag))
    if row.command == "audit" and not declared and not row.dynamic_refusal:
        action("configure-target", ("config", "set", "target", ",".join(row.targets)))
    scope = ("--global",) if row.user_scope else ()

    def install(targets: tuple[str, ...], *, force: bool = False) -> tuple[str, ...]:
        return (
            "install",
            *scope,
            "--target",
            ",".join(targets),
            "--no-policy",
            "--parallel-downloads",
            "0",
            *(("--force",) if force else ()),
        )

    cache_before = set(isolated.cache_root.rglob("HEAD"))
    assert not cache_before, "Fresh fixture unexpectedly has a warm cache"
    action("install", install(route_row.targets), targets=route_row.targets)

    _assert_provenance(oracle, expected_commits)
    if row.dynamic_refusal:
        action("refusal", install(row.targets), success=False, unchanged=True)
        oracle.assert_finished(required_transitions(row))
        return _evidence(tmp_path, row, oracle, started)

    if row.cache_state == "warm":
        assert list(isolated.cache_root.rglob("HEAD")), "Warm cache baseline fetched nothing"
        # Re-fetch/materialize with the already populated Git cache, not just a label.
        modules = lock_root / "apm_modules"
        oracle.observe("remove-materialization", lambda: shutil.rmtree(modules), exact=exact)
        oracle.evaluated()
        action("warm-materialize", install(row.targets), targets=row.targets)
        _assert_provenance(oracle, expected_commits)
    elif row.cache_state == "cold" and row.id.startswith("interaction-"):
        # The requested action starts without a fetch cache, even after setup install.
        def clear_cache() -> None:
            for name in cache_trees["cache"]:
                path = isolated.cache_root / name
                if path.exists():
                    shutil.rmtree(path)

        oracle.observe("clear-cache", clear_cache, trees=cache_trees)
        oracle.evaluated()
        assert not list(isolated.cache_root.rglob("HEAD"))
    if row.integrity_state == "tampered":
        candidates = expected_routing(route_row, (sources[0],)).files
        candidate = next(
            deploy_root / name
            for name in sorted(candidates)
            if sources[0].marker.encode() in (deploy_root / name).read_bytes()
        )
        pristine = candidate.read_bytes()
        corrupted = pristine.replace(
            sources[0].marker.encode(),
            sources[0].marker.encode() + b"-TAMPER",
        )
        assert corrupted != pristine, "Tamper action must actually change native content"
        oracle.observe(
            "tamper",
            lambda: candidate.write_bytes(corrupted),
            exact=exact,
        )
        oracle.evaluated()

    gap = known_gap_for(row)
    if row.command == "audit":
        action(
            "audit",
            ("audit", "--ci", "--no-policy", "--format", "json"),
            success=row.integrity_state == "clean",
            unchanged=True,
            cwd=lock_root,
        )
        if row.integrity_state == "tampered":
            oracle.observe("restore-tamper", lambda: candidate.write_bytes(pristine), exact=exact)
            assert candidate.read_bytes() == pristine
            oracle.evaluated()
    else:
        if row.command == "update" and row.ref_state == "tag":
            updated = []
            for repository, source in git_sources:
                new_marker = source.marker.replace("version-a", "version-b")

                def advance(repository=repository, source=source, new_marker=new_marker):
                    for relative in source.source_files:
                        path = repository.worktree / relative
                        path.write_text(
                            path.read_text().replace(source.marker, new_marker), encoding="utf-8"
                        )
                    commit = repositories.commit(repository, message="advance tagged interaction")
                    repositories.advance_tag(repository, _TAG, commit)
                    return commit

                commit = oracle.observe(
                    "advance-tag",
                    advance,
                    trees={"repositories": {repository.origin.name, repository.worktree.name}},
                )
                oracle.evaluated()
                expected_commits[source.package_name] = commit.sha
                updated.append(replace(source, marker=new_marker))
            oracle.sources = tuple(updated)
        before = oracle.capture()
        operation = "update" if row.command == "update" else "reinstall"
        unchanged_update = operation == "update" and row.ref_state != "tag"
        native_before = {
            name: (deploy_root / name).read_bytes()
            for name in expected_routing(route_row, oracle.sources, row.targets).files
        }
        lock_before = (lock_root / "apm.lock.yaml").read_bytes()
        args = (
            (
                "update",
                *scope,
                "--yes",
                "--target",
                ",".join(row.targets),
                "--parallel-downloads",
                "0",
                *(("--force",) if row.integrity_state == "tampered" else ()),
            )
            if operation == "update"
            else install(row.targets, force=row.integrity_state == "tampered")
        )
        action(operation, args, targets=row.targets)
        _assert_provenance(oracle, expected_commits)
        if unchanged_update:
            assert (lock_root / "apm.lock.yaml").read_bytes() == lock_before
            assert all(
                (deploy_root / name).read_bytes() == content
                for name, content in native_before.items()
            ), "A no-change update unexpectedly rewrote native deployments"
            if row.integrity_state == "tampered":
                # Update skips local deps/no-change plans; install owns this repair.
                action("repair-install", install(row.targets, force=True), targets=row.targets)
                assert candidate.read_bytes() == pristine
        if row.integrity_state == "tampered":
            assert b"TAMPER" not in candidate.read_bytes(), (
                f"{row.id}: successful {operation} did not repair the tampered owned artifact"
            )
        if operation == "reinstall" and row.integrity_state == "clean":
            _assert_reinstall(oracle, before, idempotency_snapshots)
            if gap is not None:
                stable = oracle.capture()
                action("converge-known-gap", install(row.targets), targets=row.targets)
                assert_snapshot_set_unchanged(stable, oracle.capture())
        else:
            # Repair/update is a state change; only the following replay is convergence.
            stable = oracle.capture()
            action("converge", install(row.targets), targets=row.targets)
            assert_snapshot_set_unchanged(stable, oracle.capture())
            oracle.evaluations.append(("stable-reinstall", ("idempotency.byte_stable",)))

    for operation, targets in (("widen", row.widen_targets), ("narrow", row.narrow_targets)):
        if not targets:
            continue

        def change_targets(targets=targets):
            manifest = load_yaml(lock_root / "apm.yml")
            manifest["targets"] = list(targets)
            dump_yaml(manifest, lock_root / "apm.yml")

        manifest_path = ".apm/apm.yml" if row.user_scope else "apm.yml"
        oracle.observe(f"declare-{operation}", change_targets, exact={root_id: {manifest_path}})
        oracle.evaluated()
        action(operation, install(targets), targets=targets)
        if operation == "widen":
            before = oracle.capture()
            action("reinstall-widened", install(targets), targets=targets)
            assert_snapshot_set_unchanged(before, oracle.capture())
        elif not row.user_scope:
            action("prune", ("prune",), targets=targets)
    uninstall_name = rewrites[-1][1] if rewrites else packages[-1].root.as_posix()
    action("uninstall", ("uninstall", uninstall_name, *scope), targets=(), manifest=True)
    oracle.assert_finished(required_transitions(row))
    return _evidence(tmp_path, row, oracle, started, gap)


def _write_install_observation(
    tmp_path: Path,
    oracle: InteractionOracle,
    result: CommandResult,
) -> None:
    """Preserve initial controls even when subsequent lifecycle cleanup removes them."""
    root = oracle.roots[oracle.deployment_root_id]
    lock_path = oracle.lock_root / "apm.lock.yaml"
    paths = expected_routing(oracle.row, oracle.sources).files
    payload = {
        "command": result.command,
        "cwd": str(result.cwd),
        "returncode": result.returncode,
        "manifest_path": str(oracle.lock_root / "apm.yml"),
        "manifest": load_yaml(oracle.lock_root / "apm.yml"),
        "source_manifests": {
            str(path): load_yaml(path)
            for source in oracle.sources
            for path in (oracle.roots["sources"] / source.package_name / "apm.yml",)
        },
        "lock_path": str(lock_path),
        "lock": load_yaml(lock_path) if lock_path.exists() else None,
        "deployments": {
            name: (root / name).read_text(encoding="utf-8") if (root / name).is_file() else None
            for name in sorted(paths)
        },
        "stdout": result.stdout,
        "stderr": result.stderr,
    }
    (tmp_path / "install-observation.json").write_text(
        json.dumps(payload, sort_keys=True, indent=2) + "\n",
        encoding="ascii",
    )


def _evidence(
    tmp_path: Path,
    row: RoutingRow,
    oracle: InteractionOracle,
    started: float,
    gap: str | None = None,
    *,
    status: str | None = None,
) -> CaseExecution:
    laws = {law for _operation, evaluated in oracle.evaluations for law in evaluated}
    evidence = CaseExecution(
        row.id,
        status or ("known_gap" if gap else "executed"),
        tuple(sorted(laws)),
        tuple(oracle.operations),
        time.monotonic() - started,
        gap,
    )
    _write_evidence(tmp_path, evidence)
    return evidence


def _write_evidence(tmp_path: Path, evidence: CaseExecution) -> None:
    """Persist a witness without converting a failed action into coverage credit."""
    (tmp_path / "lifecycle-execution.json").write_text(
        json.dumps(asdict(evidence), sort_keys=True) + "\n",
        encoding="ascii",
    )


@pytest.mark.parametrize("row", _ROWS, ids=lambda row: row.id)
def test_primitive_target_covering_array(
    tmp_path: Path,
    apm_binary_path: Path,
    row: RoutingRow,
    record_property,
) -> None:
    """Keep safety checks passing separately from the explicitly unresolved law."""
    _assert_covering_array()
    execute_row(
        tmp_path,
        apm_binary_path,
        row,
        record_execution=lambda evidence: record_property(
            "lifecycle_execution", json.dumps(asdict(evidence), sort_keys=True)
        ),
    )


class _IdempotencyViolation(AssertionError):
    """Only the desired byte-stability assertion may trigger the known xfail."""


@pytest.mark.xfail(
    strict=True,
    raises=_IdempotencyViolation,
    reason="Known gap: Copilot user instruction second-pass provenance changes (#2813)",
)
def test_copilot_user_instructions_desired_idempotency(
    copilot_idempotency_snapshots: tuple[ArtifactSnapshotSet, ArtifactSnapshotSet],
) -> None:
    """Express the desired byte-stable law without hiding blast-radius failures."""
    try:
        assert_snapshot_set_unchanged(*copilot_idempotency_snapshots)
    except AssertionError as error:
        raise _IdempotencyViolation(str(error)) from error


@pytest.fixture
def copilot_idempotency_snapshots(
    tmp_path: Path,
    apm_binary_path: Path,
) -> tuple[ArtifactSnapshotSet, ArtifactSnapshotSet]:
    """Run all safety checks outside the narrowly expected-failing test call."""
    row = next(row for row in ROUTING_ROWS if row.id == "copilot-instructions-user")
    observations: list[tuple[ArtifactSnapshotSet, ArtifactSnapshotSet]] = []
    execute_row(tmp_path, apm_binary_path, row, idempotency_snapshots=observations)
    assert len(observations) == 1
    return observations[0]


@pytest.mark.parametrize(
    ("user_scope", "relative_child"),
    ((False, True), (True, False), (True, True)),
    ids=("project-relative-control", "user-absolute-control", "user-relative-regression"),
)
def test_local_transitive_command_scope_parity(
    tmp_path: Path,
    apm_binary_path: Path,
    user_scope: bool,
    relative_child: bool,
) -> None:
    """A successful install cannot silently omit an existing declared child."""
    row = RoutingRow(
        "local-scope-parity",
        ("commands",),
        ("cursor",),
        user_scope,
        dependency_shape="transitive",
        source_kind="local",
        ref_state="none",
        cache_state="none",
    )
    execute_row(
        tmp_path,
        apm_binary_path,
        row,
        relative_local_children=relative_child,
    )
