"""Real skill consumers exercise the bounded durable-ownership epoch."""

from __future__ import annotations

import tempfile
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

import pytest

from apm_cli.core.scope import InstallScope
from apm_cli.deps.lockfile import LockedDependency, LockFile, get_lockfile_path
from apm_cli.integration.skill_integrator import SkillIntegrationResult, SkillIntegrator
from apm_cli.integration.targets import KNOWN_TARGETS, apply_legacy_skill_paths
from apm_cli.models.apm_package import (
    APMPackage,
    DependencyReference,
    PackageContentType,
    PackageInfo,
    PackageType,
)
from apm_cli.utils.diagnostics import CATEGORY_OVERWRITE, DiagnosticCollector

pytestmark = pytest.mark.component


@pytest.fixture(autouse=True)
def _fence_fixture_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep inherited home, target, config, and scratch locations out of the test."""
    from apm_cli import config

    home = tmp_path / "home"
    metadata = home / ".apm"
    metadata.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("APM_HOME", str(metadata))
    monkeypatch.setenv("APM_TEMP_DIR", str(tmp_path))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(config, "CONFIG_DIR", str(metadata))
    monkeypatch.setattr(config, "CONFIG_FILE", str(metadata / "config.json"))
    monkeypatch.setattr(config, "_config_cache", None)
    for name in ("CLAUDE_CONFIG_DIR", "HERMES_HOME", "CODEX_HOME", "APM_LEGACY_SKILL_PATHS"):
        monkeypatch.delenv(name, raising=False)


@dataclass
class OwnershipWork:
    """Count actual canonical reads and yielded records, not elapsed time."""

    builds: Counter[Path] = field(default_factory=Counter)
    reads: Counter[Path] = field(default_factory=Counter)
    dependencies: int = 0
    paths: int = 0
    building: bool = False


class _CountedPaths:
    """Forward the existing path iterable without copying or pre-traversing it."""

    def __init__(self, paths: list[str], work: OwnershipWork) -> None:
        self.paths = paths
        self.work = work

    def __iter__(self) -> Iterator[str]:
        for path in self.paths:
            self.work.paths += 1
            yield path


@pytest.fixture
def ownership_work(monkeypatch: pytest.MonkeyPatch) -> OwnershipWork:
    """Instrument only work inside the real canonical ownership builder."""
    work = OwnershipWork()
    build = SkillIntegrator._build_ownership_maps
    read = LockFile.read
    dependencies = LockFile.get_package_dependencies

    def counted_build(root: Path) -> tuple[dict[str, str], dict[str, str]]:
        work.builds[root.resolve()] += 1
        work.building = True
        try:
            return build(root)
        finally:
            work.building = False

    def counted_read(cls: type[LockFile], path: Path) -> LockFile | None:
        if work.building:
            work.reads[path.resolve()] += 1
        return read(path)

    def counted_dependencies(lock: LockFile) -> Iterable[LockedDependency]:
        records = dependencies(lock)
        if not work.building:
            return records

        def visit() -> Iterator[LockedDependency]:
            for dep in records:
                work.dependencies += 1
                dep.deployed_files = _CountedPaths(dep.deployed_files, work)
                yield dep

        return visit()

    monkeypatch.setattr(SkillIntegrator, "_build_ownership_maps", staticmethod(counted_build))
    monkeypatch.setattr(LockFile, "read", classmethod(counted_read))
    monkeypatch.setattr(LockFile, "get_package_dependencies", counted_dependencies)
    return work


def _skill_file(directory: Path, name: str, body: str) -> bytes:
    """Write a valid skill with byte-stable content."""
    directory.mkdir(parents=True, exist_ok=True)
    content = f"---\nname: {name}\ndescription: Ownership fixture\n---\n{body}\n".encode()
    (directory / "SKILL.md").write_bytes(content)
    return content


def _package(base: Path, name: str, owner: str, layout: str) -> tuple[PackageInfo, Path]:
    """Materialize native, bundle, or non-skill APM package source trees."""
    install_path = base / owner / name
    skill_dir = {
        "native": install_path,
        "bundle": install_path / "skills" / name,
        "standalone": install_path / ".apm" / "skills" / name,
    }[layout]
    _skill_file(skill_dir, name, f"new {owner}/{name}")
    package_type = {
        "native": PackageType.CLAUDE_SKILL,
        "bundle": PackageType.SKILL_BUNDLE,
        "standalone": PackageType.APM_PACKAGE,
    }[layout]
    package = APMPackage(
        name=name,
        version="1.0.0",
        type=PackageContentType.INSTRUCTIONS
        if layout == "standalone"
        else PackageContentType.SKILL,
    )
    return (
        PackageInfo(
            package=package,
            install_path=install_path,
            package_type=package_type,
            dependency_ref=DependencyReference(repo_url=f"{owner}/{name}"),
        ),
        skill_dir,
    )


def _write_owners(root: Path, owners: dict[str, str]) -> bytes:
    """Persist genuine lock records, including a non-skill path per owner."""
    root.mkdir(parents=True, exist_ok=True)
    lock = LockFile()
    for name, owner in owners.items():
        lock.add_dependency(
            LockedDependency(
                repo_url=owner,
                resolved_commit="a" * 40,
                deployed_files=[
                    f".agents/skills/{name}/",
                    f".github/prompts/{name}-prompt",
                ],
            )
        )
    content = lock.to_yaml().encode()
    get_lockfile_path(root).write_bytes(content)
    return content


def _deploy(
    integrator: SkillIntegrator,
    package: PackageInfo,
    root: Path,
    diagnostics: DiagnosticCollector,
    scope: InstallScope = InstallScope.PROJECT,
    *,
    force: bool = False,
) -> SkillIntegrationResult:
    """Use public routing and actual target deployment, not a forwarding stub."""
    return integrator.integrate_package_skill(
        package,
        root,
        targets=[KNOWN_TARGETS["copilot"]],
        managed_files=None,
        force=force,
        diagnostics=diagnostics,
        scope=scope,
    )


@pytest.mark.parametrize("layout", ["native", "bundle", "standalone"])
@pytest.mark.parametrize("scope", [InstallScope.PROJECT, InstallScope.USER])
def test_consumers_build_once_with_linear_record_visits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ownership_work: OwnershipWork,
    layout: str,
    scope: InstallScope,
) -> None:
    """N-to-10N real deployments retain exact ownership and update old bytes."""
    visits = []
    for count in (5, 50):
        root = tmp_path / str(count)
        root.mkdir()
        monkeypatch.setattr(Path, "home", classmethod(lambda cls, root=root: root))
        monkeypatch.setenv("HOME", str(root))
        monkeypatch.setenv("APM_HOME", str(root / ".apm"))
        metadata = root / ".apm" if scope is InstallScope.USER else root
        owners = {f"skill-{index}": f"owner-{index}/skill-{index}" for index in range(count)}
        before = _write_owners(metadata, owners)
        packages = [
            _package(root / "sources", name, f"owner-{index}", layout)
            for index, name in enumerate(owners)
        ]
        for name in owners:
            _skill_file(root / ".agents" / "skills" / name, name, "previous deployment")
        integrator = SkillIntegrator()
        diagnostics = DiagnosticCollector()
        initial_visits = ownership_work.dependencies + ownership_work.paths

        with integrator.ownership_snapshot():
            for package, source in packages:
                result = _deploy(integrator, package, root, diagnostics, scope)
                target = root / ".agents" / "skills" / source.name
                assert result.target_paths == [target]
                assert (target / "SKILL.md").read_bytes() == (source / "SKILL.md").read_bytes()
            owned, native = integrator._ownership_maps(metadata)
            assert native == {f".agents/skills/{name}": owner for name, owner in owners.items()}
            assert owned == owners | {f"{name}-prompt": owner for name, owner in owners.items()}
            assert isinstance(owned, MappingProxyType)
            assert isinstance(native, MappingProxyType)
            with pytest.raises(TypeError):
                owned["injected"] = "foreign/owner"
            with pytest.raises(TypeError):
                native["injected"] = "foreign/owner"
            assert get_lockfile_path(metadata).read_bytes() == before

        assert not diagnostics.has_diagnostics
        assert ownership_work.builds[metadata.resolve()] == 1
        assert ownership_work.reads[get_lockfile_path(metadata).resolve()] == 1
        visits.append(ownership_work.dependencies + ownership_work.paths - initial_visits)
        assert visits[-1] == count * 3

    assert visits[1] <= 15 * visits[0]


@pytest.mark.parametrize("lifetime", ["same-integrator", "fresh-integrator", "uncached"])
def test_replace_delete_create_and_reinstall_start_fresh_epochs(
    tmp_path: Path, ownership_work: OwnershipWork, lifetime: str
) -> None:
    """Each valid boundary observes changed durable owners and deployed content."""
    from contextlib import nullcontext

    package, source = _package(tmp_path / "sources", "shared", "incoming", "native")
    target = tmp_path / ".agents" / "skills" / "shared"
    _skill_file(target, "shared", "initial")
    integrator = SkillIntegrator()
    owners = ("old/shared", "replacement/shared", None, "created/shared", "created/shared")
    for epoch, owner in enumerate(owners):
        if owner is None:
            get_lockfile_path(tmp_path).unlink()
        else:
            _write_owners(tmp_path, {"shared": owner})
        if lifetime == "fresh-integrator":
            integrator = SkillIntegrator()
            assert integrator._native_skill_session_owners == {}
        content = _skill_file(source, "shared", f"epoch {epoch}")
        diagnostics = DiagnosticCollector()
        context = nullcontext() if lifetime == "uncached" else integrator.ownership_snapshot()
        with context:
            result = _deploy(integrator, package, tmp_path, diagnostics, force=owner is None)
            assert result.skill_updated
            assert (target / "SKILL.md").read_bytes() == content
            if lifetime != "uncached":
                _, native = integrator._ownership_maps(tmp_path)
                assert native == ({".agents/skills/shared": owner} if owner else {})
        collisions = diagnostics.by_category().get(CATEGORY_OVERWRITE, [])
        assert [entry.package for entry in collisions] == (["incoming/shared"] if owner else [])
        if owner:
            assert collisions[0].detail == (
                f"Skill 'shared' from 'incoming/shared' replaced "
                f"'{owner}' -- remove one package to avoid this"
            )
        assert integrator._ownership_snapshots is None

    assert ownership_work.builds[tmp_path.resolve()] == len(owners)
    assert ownership_work.reads[get_lockfile_path(tmp_path).resolve()] == len(owners)


def test_roots_are_separate_but_resolved_aliases_share_maps(
    tmp_path: Path, ownership_work: OwnershipWork
) -> None:
    """Only physical metadata-root identity determines snapshot reuse."""
    project = tmp_path / "project"
    user = tmp_path / "home" / ".apm"
    _write_owners(project, {"shared": "project/shared"})
    _write_owners(user, {"shared": "user/shared"})
    (project / "nested").mkdir()
    alias = project / "nested" / ".."
    integrator = SkillIntegrator()
    with integrator.ownership_snapshot():
        project_maps = integrator._ownership_maps(project)
        user_maps = integrator._ownership_maps(user)
        assert integrator._ownership_maps(alias) is project_maps
        assert project_maps[1] == {".agents/skills/shared": "project/shared"}
        assert user_maps[1] == {".agents/skills/shared": "user/shared"}
        assert user_maps is not project_maps
    assert ownership_work.builds == {project.resolve(): 1, user.resolve(): 1}


def test_symlink_metadata_alias_shares_only_its_physical_root(
    tmp_path: Path, ownership_work: OwnershipWork
) -> None:
    """A filesystem alias shares one build, never another root's same-named owner."""
    physical = tmp_path / "physical"
    separate = tmp_path / "separate"
    _write_owners(physical, {"shared": "first/shared"})
    _write_owners(separate, {"shared": "second/shared"})
    alias = tmp_path / "metadata-alias"
    alias.symlink_to(physical, target_is_directory=True)
    integrator = SkillIntegrator()
    with integrator.ownership_snapshot():
        aliased = integrator._ownership_maps(alias)
        assert integrator._ownership_maps(physical) is aliased
        assert aliased[1] == {".agents/skills/shared": "first/shared"}
        distinct = integrator._ownership_maps(separate)
        assert distinct is not aliased
        assert distinct[1] == {".agents/skills/shared": "second/shared"}
        assert integrator._ownership_maps(alias) is aliased
    assert ownership_work.builds == {physical.resolve(): 1, separate.resolve(): 1}
    assert ownership_work.reads == {
        get_lockfile_path(physical).resolve(): 1,
        get_lockfile_path(separate).resolve(): 1,
    }


def test_unused_snapshot_and_package_without_skills_do_not_read(
    tmp_path: Path, ownership_work: OwnershipWork
) -> None:
    """Entering an epoch is lazy even when a real non-skill package is routed."""
    _write_owners(tmp_path, {"unrelated": "owner/unrelated"})
    package = PackageInfo(
        package=APMPackage(name="empty", version="1.0.0", type=PackageContentType.INSTRUCTIONS),
        install_path=tmp_path / "empty",
        package_type=PackageType.APM_PACKAGE,
    )
    package.install_path.mkdir()
    integrator = SkillIntegrator()
    with integrator.ownership_snapshot():
        pass
    with integrator.ownership_snapshot():
        result = _deploy(integrator, package, tmp_path, DiagnosticCollector())
    assert result.skill_skipped
    assert result.target_paths == []
    assert ownership_work.builds == {}
    assert ownership_work.reads == {}
    assert not (tmp_path / ".agents").exists()


def test_nested_refusal_and_caught_package_failure_keep_outer_epoch(
    tmp_path: Path, ownership_work: OwnershipWork
) -> None:
    """A real filesystem deployment failure may be caught without losing the batch."""
    before = _write_owners(tmp_path, {"shared": "incoming/shared"})
    package, source = _package(tmp_path / "sources", "shared", "incoming", "native")
    target = tmp_path / ".agents" / "skills" / "shared"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"not a directory")
    integrator = SkillIntegrator()
    with integrator.ownership_snapshot():
        with pytest.raises(OSError):
            _deploy(integrator, package, tmp_path, DiagnosticCollector())
        original = integrator._ownership_maps(tmp_path)
        with pytest.raises(RuntimeError, match="already active"):
            with integrator.ownership_snapshot():
                pytest.fail("Nested ownership epoch was accepted")
        assert integrator._ownership_maps(tmp_path) is original
        target.unlink()
        result = _deploy(integrator, package, tmp_path, DiagnosticCollector())
        assert result.skill_created
        assert (target / "SKILL.md").read_bytes() == (source / "SKILL.md").read_bytes()
        assert get_lockfile_path(tmp_path).read_bytes() == before
    assert ownership_work.builds[tmp_path.resolve()] == 1
    assert integrator._ownership_snapshots is None


def test_exception_drops_maps_before_retry(tmp_path: Path, ownership_work: OwnershipWork) -> None:
    """Exceptional exit discards a populated snapshot before a repaired retry."""
    _write_owners(tmp_path, {"shared": "first/shared"})
    package, source = _package(tmp_path / "sources", "shared", "incoming", "native")
    target = tmp_path / ".agents" / "skills" / "shared"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"invalid destination")
    integrator = SkillIntegrator()
    with pytest.raises(OSError):
        with integrator.ownership_snapshot():
            _deploy(integrator, package, tmp_path, DiagnosticCollector())
    assert integrator._ownership_snapshots is None
    _write_owners(tmp_path, {"shared": "second/shared"})
    target.unlink()
    _skill_file(target, "shared", "repaired destination")
    diagnostics = DiagnosticCollector()
    with integrator.ownership_snapshot():
        _deploy(integrator, package, tmp_path, diagnostics)
    assert (target / "SKILL.md").read_bytes() == (source / "SKILL.md").read_bytes()
    assert diagnostics.by_category()[CATEGORY_OVERWRITE][0].detail == (
        "Skill 'shared' from 'incoming/shared' replaced "
        "'second/shared' -- remove one package to avoid this"
    )
    assert ownership_work.builds[tmp_path.resolve()] == 2


@pytest.mark.parametrize("durable_owner", [None, "durable/shared"])
def test_session_overlay_stays_live_with_durable_owner_precedence(
    tmp_path: Path, ownership_work: OwnershipWork, durable_owner: str | None
) -> None:
    """Unchanged durable maps must not freeze current-run collision ownership."""
    before = _write_owners(tmp_path, {"shared": durable_owner}) if durable_owner else None
    first, _ = _package(tmp_path / "sources", "shared", "first", "native")
    second, source = _package(tmp_path / "sources", "shared", "second", "native")
    integrator = SkillIntegrator()
    diagnostics = DiagnosticCollector()
    destination = (tmp_path / ".agents" / "skills" / "shared").resolve()
    with integrator.ownership_snapshot():
        _deploy(integrator, first, tmp_path, DiagnosticCollector())
        assert integrator._native_skill_session_owners == {destination: "first/shared"}
        _deploy(integrator, second, tmp_path, diagnostics)
        assert integrator._native_skill_session_owners == {destination: "second/shared"}
        _, native = integrator._ownership_maps(tmp_path)
        assert native == ({".agents/skills/shared": durable_owner} if durable_owner else {})
    previous = durable_owner or "first/shared"
    assert diagnostics.by_category()[CATEGORY_OVERWRITE][0].detail == (
        f"Skill 'shared' from 'second/shared' replaced "
        f"'{previous}' -- remove one package to avoid this"
    )
    assert (tmp_path / ".agents" / "skills" / "shared" / "SKILL.md").read_bytes() == (
        source / "SKILL.md"
    ).read_bytes()
    lock_path = get_lockfile_path(tmp_path)
    assert (lock_path.read_bytes() if lock_path.exists() else None) == before
    assert ownership_work.builds[tmp_path.resolve()] == 1


@pytest.mark.parametrize("managed", [None, set()])
def test_snapshot_preserves_destination_ownership_and_explicit_managed_set(
    tmp_path: Path, ownership_work: OwnershipWork, managed: set[str] | None
) -> None:
    """A durable first-target owner cannot authorize the second target's same name."""
    lock_bytes = _write_owners(tmp_path, {"shared": "incoming/shared"})
    package, source = _package(tmp_path / "sources", "shared", "incoming", "native")
    owned_target = tmp_path / ".agents" / "skills" / "shared"
    unowned_target = tmp_path / ".claude" / "skills" / "shared"
    owned_before = _skill_file(owned_target, "shared", "previous managed bytes")
    unowned_before = _skill_file(unowned_target, "shared", "user-owned bytes")
    sentinel = unowned_target / "user.bin"
    sentinel.write_bytes(b"\xff\x00preserve")
    targets = [
        KNOWN_TARGETS["copilot"],
        apply_legacy_skill_paths([KNOWN_TARGETS["claude"]])[0],
    ]
    integrator = SkillIntegrator()
    with integrator.ownership_snapshot():
        result = integrator.integrate_package_skill(
            package,
            tmp_path,
            targets=targets,
            managed_files=managed,
            diagnostics=DiagnosticCollector(),
        )
        assert integrator._ownership_maps(tmp_path)[1] == {
            ".agents/skills/shared": "incoming/shared"
        }
    assert result.target_paths == ([owned_target] if managed is None else [])
    assert (owned_target / "SKILL.md").read_bytes() == (
        (source / "SKILL.md").read_bytes() if managed is None else owned_before
    )
    assert (unowned_target / "SKILL.md").read_bytes() == unowned_before
    assert sentinel.read_bytes() == b"\xff\x00preserve"
    assert integrator._native_skill_session_owners == (
        {owned_target.resolve(): "incoming/shared"} if managed is None else {}
    )
    assert get_lockfile_path(tmp_path).read_bytes() == lock_bytes
    assert ownership_work.builds == {tmp_path.resolve(): 1}


def test_standalone_drift_replay_uses_fresh_uncached_integrators(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ownership_work: OwnershipWork
) -> None:
    """Consecutive real scratch replays see replaced lock owners, not install epochs."""
    from apm_cli.install import drift

    project = tmp_path / "project"
    project.mkdir()
    (project / "apm.yml").write_bytes(b"name: replay-project\nversion: 1.0.0\ntarget: copilot\n")
    live_skill = project / ".agents" / "skills" / "shared"
    live_content = _skill_file(live_skill, "shared", "untouched live deployment")
    instruction = project / ".github" / "instructions" / "local.instructions.md"
    instruction.parent.mkdir(parents=True)
    instruction.write_bytes(b"---\napplyTo: '**'\n---\nUntouched local instruction.\n")
    instruction_before = instruction.read_bytes()
    make_integrators = drift._make_integrators
    observed_integrators: list[SkillIntegrator] = []
    maps = SkillIntegrator._ownership_maps
    accesses: list[tuple[SkillIntegrator, Path]] = []

    def observe_integrators() -> dict[str, object]:
        integrators = make_integrators()
        skill = integrators["skill"]
        assert skill._ownership_snapshots is None
        assert skill._native_skill_session_owners == {}
        observed_integrators.append(skill)
        return integrators

    def observe_maps(self: SkillIntegrator, root: Path) -> object:
        assert self._ownership_snapshots is None
        accesses.append((self, root.resolve()))
        return maps(self, root)

    monkeypatch.setattr(drift, "_make_integrators", observe_integrators)
    monkeypatch.setattr(SkillIntegrator, "_ownership_maps", observe_maps)
    outputs: list[bytes] = []
    for epoch, owner in enumerate(("first", "replacement")):
        package, source = _package(tmp_path / "sources", "shared", owner, "native")
        dep = LockedDependency(
            repo_url=f"{owner}/shared",
            source="local",
            local_path=str(package.install_path),
            package_type=PackageType.CLAUDE_SKILL.value,
            deployed_files=[".agents/skills/shared"],
        )
        lock = LockFile()
        lock.add_dependency(dep)
        lock_path = get_lockfile_path(project)
        lock_path.write_bytes(lock.to_yaml().encode())
        lock_before = lock_path.read_bytes()
        scratch = tmp_path / f"replay-{epoch}"
        result = drift.run_replay(
            drift.ReplayConfig(
                project_root=project,
                lockfile_path=lock_path,
                scratch_root=scratch,
                cache_only=True,
            ),
            drift.CheckLogger(verbose=False),
        )
        assert result == scratch.resolve()
        replayed = scratch / ".agents" / "skills" / "shared" / "SKILL.md"
        outputs.append(replayed.read_bytes())
        assert outputs[-1] == (source / "SKILL.md").read_bytes()
        assert lock_path.read_bytes() == lock_before
        assert (live_skill / "SKILL.md").read_bytes() == live_content
        assert instruction.read_bytes() == instruction_before
        assert not get_lockfile_path(scratch).exists()
        assert observed_integrators[-1]._native_skill_session_owners == {
            replayed.parent.resolve(): dep.get_unique_key()
        }
        assert ownership_work.builds[scratch.resolve()] == 1
        assert ownership_work.reads[get_lockfile_path(scratch).resolve()] == 1

    assert len(observed_integrators) == 2
    assert observed_integrators[0] is not observed_integrators[1]
    assert accesses == [
        (observed_integrators[0], (tmp_path / "replay-0").resolve()),
        (observed_integrators[1], (tmp_path / "replay-1").resolve()),
    ]
    assert outputs[0] != outputs[1]
    assert ownership_work.dependencies == 0
    assert ownership_work.paths == 0
