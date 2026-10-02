"""Real local installs qualify the ownership snapshot's pipeline boundary."""

from __future__ import annotations

import shutil
import tempfile
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml

from apm_cli.core.scope import InstallScope
from apm_cli.deps.lockfile import LockFile, get_lockfile_path
from apm_cli.install import pipeline
from apm_cli.install.context import InstallContext
from apm_cli.install.phases import integrate
from apm_cli.install.phases.lockfile import LockfileBuilder
from apm_cli.integration.skill_integrator import SkillIntegrator
from apm_cli.models.apm_package import APMPackage, clear_apm_yml_cache
from apm_cli.models.results import InstallDisposition

pytestmark = pytest.mark.component


@dataclass
class LocalInstall:
    """A filesystem-only dependency and colliding root-local skill."""

    root: Path
    metadata: Path
    source: Path
    package: APMPackage
    scope: InstallScope

    @property
    def deployed(self) -> Path:
        return self.root / ".agents" / "skills" / "shared" / "SKILL.md"

    @property
    def local_skill(self) -> Path:
        return self.root / ".apm" / "skills" / "shared" / "SKILL.md"


@pytest.fixture
def local_install_factory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Callable[..., LocalInstall]:
    """Use real resolver/local-source fixtures; only remote downloading is mocked."""
    home = tmp_path / "home"
    home.mkdir()
    metadata = home / ".apm"
    metadata.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("APM_HOME", str(metadata))
    monkeypatch.setenv("APM_TEMP_DIR", str(tmp_path))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    from apm_cli import config

    monkeypatch.setattr(config, "CONFIG_DIR", str(metadata))
    monkeypatch.setattr(config, "CONFIG_FILE", str(metadata / "config.json"))
    monkeypatch.setattr(config, "_config_cache", None)
    for name in ("CLAUDE_CONFIG_DIR", "HERMES_HOME", "CODEX_HOME", "APM_LEGACY_SKILL_PATHS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("APM_NO_CACHE", "1")
    monkeypatch.setenv("APM_PROGRESS", "never")
    downloader = MagicMock()
    downloader.persistent_git_cache = None
    downloader._tiered_resolver = None
    downloader.download_package.side_effect = AssertionError("Local fixture attempted a download")
    monkeypatch.setattr(
        "apm_cli.deps.github_downloader.GitHubPackageDownloader",
        MagicMock(return_value=downloader),
    )

    def create(scope: InstallScope = InstallScope.PROJECT) -> LocalInstall:
        root = home if scope is InstallScope.USER else tmp_path / "project"
        root.mkdir(exist_ok=True)
        metadata = root / ".apm" if scope is InstallScope.USER else root
        metadata.mkdir(exist_ok=True)
        source = tmp_path / "sources" / "shared"
        source.mkdir(parents=True)
        (source / "SKILL.md").write_bytes(
            b"---\nname: shared\ndescription: Dependency skill\n---\nDependency version one.\n"
        )
        local_skill = root / ".apm" / "skills" / "shared" / "SKILL.md"
        local_skill.parent.mkdir(parents=True, exist_ok=True)
        local_skill.write_bytes(
            b"---\nname: shared\ndescription: Root skill\n---\nRoot version one.\n"
        )
        manifest = metadata / "apm.yml"
        manifest.write_text(
            yaml.safe_dump(
                {
                    "name": "snapshot-consumer",
                    "version": "1.0.0",
                    "description": "Ownership phase fixture",
                    "dependencies": {"apm": [str(source)]},
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.chdir(root)
        package = APMPackage.from_apm_yml(manifest)
        return LocalInstall(root, metadata, source, package, scope)

    return create


@dataclass
class PhaseEvidence:
    """Capture forwarding observations without replacing phase execution."""

    contexts: list[InstallContext] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    builds: Counter[Path] = field(default_factory=Counter)
    root_checks: int = 0
    lock_writes: int = 0


def _set_dependencies(fixture: LocalInstall, dependencies: list[str | dict[str, Any]]) -> None:
    """Change the real manifest before the next command reads it."""
    manifest = fixture.metadata / "apm.yml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data["dependencies"]["apm"] = dependencies
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")
    clear_apm_yml_cache()
    fixture.package = APMPackage.from_apm_yml(manifest)


def _observe_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    fixture: LocalInstall,
    *,
    lockfile_only: bool = False,
    expect_skill_deployment: bool = True,
    failed_dependency: Callable[[], bool] | None = None,
) -> PhaseEvidence:
    """Observe actual phase, root-local deployment, and durable writer boundaries."""
    evidence = PhaseEvidence()
    run_phase = pipeline._run_phase
    root_project = integrate._integrate_root_project
    ownership_build = SkillIntegrator._build_ownership_maps
    build_and_save = LockfileBuilder.build_and_save
    write = LockFile.write
    phase_lock_bytes: bytes | None = None

    def lock_bytes() -> bytes | None:
        path = get_lockfile_path(fixture.metadata)
        return path.read_bytes() if path.exists() else None

    def observed_phase(name: str, phase: ModuleType, ctx: InstallContext) -> Any:
        nonlocal phase_lock_bytes
        evidence.events.append(name)
        if name == "integrate":
            evidence.contexts.append(ctx)
            phase_lock_bytes = lock_bytes()
            if lockfile_only:
                assert ctx.integrators == {}
                assert ctx.targets == []
            else:
                assert ctx.integrators["skill"]._ownership_snapshots == {}
                assert ctx.integrators["skill"]._native_skill_session_owners == {}
            result = run_phase(name, phase, ctx)
            assert lock_bytes() == phase_lock_bytes
            assert ctx.installed_packages
            if lockfile_only:
                assert ctx.package_deployed_files == {}
                assert ctx.local_deployed_files == []
            else:
                assert ctx.package_deployed_files, ctx.diagnostics.by_category()
                assert bool(ctx.local_deployed_files) is expect_skill_deployment
            return result
        if "skill" in ctx.integrators:
            assert ctx.integrators["skill"]._ownership_snapshots is None
        return run_phase(name, phase, ctx)

    def observed_root(ctx: InstallContext) -> dict[str, int] | None:
        if lockfile_only:
            return root_project(ctx)
        assert ctx.integrators["skill"]._ownership_snapshots is not None
        assert lock_bytes() == phase_lock_bytes
        if not expect_skill_deployment:
            result = root_project(ctx)
            assert ctx.integrators["skill"]._ownership_snapshots == {}
            assert lock_bytes() == phase_lock_bytes
            assert not fixture.deployed.exists()
            evidence.root_checks += 1
            return result
        if failed_dependency is not None and failed_dependency():
            dep_key = fixture.package.get_apm_dependencies()[0].get_unique_key()
            assert dep_key not in ctx.package_deployed_files
            assert ctx.diagnostics.error_count == 1
            assert not fixture.deployed.exists()
            result = root_project(ctx)
            assert fixture.deployed.read_bytes() == fixture.local_skill.read_bytes()
            assert lock_bytes() == phase_lock_bytes
            evidence.root_checks += 1
            return result
        assert fixture.deployed.read_bytes() == (fixture.source / "SKILL.md").read_bytes()
        dep_key = fixture.package.get_apm_dependencies()[0].get_unique_key()
        assert ".agents/skills/shared" in ctx.package_deployed_files[dep_key]
        assert ctx.local_deployed_files == []
        result = root_project(ctx)
        assert lock_bytes() == phase_lock_bytes
        assert fixture.deployed.read_bytes() == fixture.local_skill.read_bytes()
        assert ".agents/skills/shared" in ctx.local_deployed_files
        evidence.root_checks += 1
        return result

    def observed_build(root: Path) -> tuple[dict[str, str], dict[str, str]]:
        evidence.builds[root.resolve()] += 1
        return ownership_build(root)

    def observed_save(builder: LockfileBuilder) -> None:
        ctx = builder.ctx
        evidence.events.append("lockfile")
        if not lockfile_only:
            assert ctx.integrators["skill"]._ownership_snapshots is None
        return build_and_save(builder)

    def observed_write(lock: LockFile, path: Path, **kwargs: Any) -> None:
        if path.resolve() == get_lockfile_path(fixture.metadata).resolve():
            assert evidence.contexts
            ctx = evidence.contexts[-1]
            if not lockfile_only:
                assert ctx.integrators["skill"]._ownership_snapshots is None
            evidence.lock_writes += 1
        return write(lock, path, **kwargs)

    monkeypatch.setattr(pipeline, "_run_phase", observed_phase)
    monkeypatch.setattr(integrate, "_integrate_root_project", observed_root)
    monkeypatch.setattr(SkillIntegrator, "_build_ownership_maps", staticmethod(observed_build))
    monkeypatch.setattr(LockfileBuilder, "build_and_save", observed_save)
    monkeypatch.setattr(LockFile, "write", observed_write)
    return evidence


@pytest.mark.parametrize("scope", [InstallScope.PROJECT, InstallScope.USER])
def test_real_reinstall_changes_deployments_but_not_lock_inside_phase(
    monkeypatch: pytest.MonkeyPatch,
    local_install_factory: Callable[..., LocalInstall],
    scope: InstallScope,
) -> None:
    """Dependency/root writes share an epoch; durable ownership writes follow exit."""
    fixture = local_install_factory(scope)
    evidence = _observe_pipeline(monkeypatch, fixture)
    previous_lock: bytes | None = None
    for epoch in range(2):
        if epoch:
            (fixture.source / "SKILL.md").write_bytes(
                b"---\nname: shared\ndescription: Updated dependency\n---\nNew dependency bytes.\n"
            )
            fixture.local_skill.write_bytes(
                b"---\nname: shared\ndescription: Updated root\n---\nNew root bytes.\n"
            )
        start = len(evidence.events)
        result = pipeline.run_install_pipeline(
            fixture.package,
            scope=fixture.scope,
            target="copilot",
            parallel_downloads=0,
            no_policy=True,
            audit_override="off",
        )
        assert result.disposition is InstallDisposition.SUCCESS
        assert result.exit_code == 0
        events = evidence.events[start:]
        assert events.index("download") < events.index("integrate") < events.index("lockfile")
        assert events.index("lockfile") < events.index("post_deps_local") < events.index("audit")
        assert evidence.root_checks == epoch + 1
        assert evidence.builds == {fixture.metadata.resolve(): epoch + 1}
        assert fixture.deployed.read_bytes() == fixture.local_skill.read_bytes()
        current_lock = get_lockfile_path(fixture.metadata).read_bytes()
        assert current_lock != previous_lock
        previous_lock = current_lock
        persisted = LockFile.read(get_lockfile_path(fixture.metadata))
        assert persisted is not None
        if scope is InstallScope.PROJECT:
            assert ".agents/skills/shared" in persisted.local_deployed_files
        else:
            dependency = persisted.get_dependency(
                fixture.package.get_apm_dependencies()[0].get_unique_key()
            )
            assert dependency is not None
            assert ".agents/skills/shared" in dependency.deployed_files
            assert persisted.local_deployed_files == []

    first, second = evidence.contexts
    assert first is not second
    assert first.integrators["skill"] is not second.integrators["skill"]
    assert first.integrators["skill"]._ownership_snapshots is None
    assert second.integrators["skill"]._ownership_snapshots is None
    assert evidence.lock_writes >= 2


def test_exception_after_real_integration_exits_before_any_lock_writer(
    monkeypatch: pytest.MonkeyPatch, local_install_factory: Callable[..., LocalInstall]
) -> None:
    """A phase failure discards populated maps even after files were deployed."""
    fixture = local_install_factory()
    evidence = _observe_pipeline(monkeypatch, fixture)
    run = integrate.run

    def fail_after_deployment(ctx: InstallContext) -> None:
        run(ctx)
        assert fixture.deployed.read_bytes() == fixture.local_skill.read_bytes()
        raise RuntimeError("injected failure after deployment")

    monkeypatch.setattr(integrate, "run", fail_after_deployment)
    with pytest.raises(RuntimeError, match="injected failure after deployment"):
        pipeline.run_install_pipeline(
            fixture.package,
            target="copilot",
            parallel_downloads=0,
            no_policy=True,
            audit_override="off",
        )
    assert len(evidence.contexts) == 1
    assert evidence.contexts[0].integrators["skill"]._ownership_snapshots is None
    assert evidence.builds == {fixture.metadata.resolve(): 1}
    assert evidence.root_checks == 1
    assert evidence.lock_writes == 0
    assert "lockfile" not in evidence.events
    assert "post_deps_local" not in evidence.events
    assert not get_lockfile_path(fixture.metadata).exists()


def test_real_template_catches_package_failure_and_retry_gets_fresh_epoch(
    monkeypatch: pytest.MonkeyPatch, local_install_factory: Callable[..., LocalInstall]
) -> None:
    """A failed copy is caught by the real template; later packages still deploy."""
    fixture = local_install_factory()
    later = fixture.source.parent / "zz-later"
    later.mkdir()
    later_content = b"---\nname: zz-later\ndescription: Later package\n---\nStill deployed.\n"
    (later / "SKILL.md").write_bytes(later_content)
    _set_dependencies(fixture, [str(fixture.source), str(later)])
    failed_this_run = False
    inject_failure = True
    copies: list[str] = []
    copytree = shutil.copytree
    evidence = _observe_pipeline(monkeypatch, fixture, failed_dependency=lambda: failed_this_run)

    def fail_first_skill_copy(src: Any, dst: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal failed_this_run, inject_failure
        if Path(dst).parent == fixture.deployed.parent.parent:
            copies.append(Path(dst).name)
        if inject_failure and Path(dst) == fixture.deployed.parent:
            assert evidence.contexts[-1].integrators["skill"]._ownership_snapshots
            inject_failure = False
            failed_this_run = True
            raise OSError("injected package copy failure")
        return copytree(src, dst, *args, **kwargs)

    monkeypatch.setattr(shutil, "copytree", fail_first_skill_copy)
    failed = pipeline.run_install_pipeline(
        fixture.package,
        target="copilot",
        parallel_downloads=0,
        no_policy=True,
        audit_override="off",
    )
    assert failed.disposition is InstallDisposition.FAILED
    assert failed.exit_code == 1
    assert failed_this_run
    assert copies[:2] == ["shared", "zz-later"]
    first = evidence.contexts[0]
    later_key = fixture.package.get_apm_dependencies()[1].get_unique_key()
    assert ".agents/skills/zz-later" in first.package_deployed_files[later_key]
    assert (
        fixture.root / ".agents" / "skills" / "zz-later" / "SKILL.md"
    ).read_bytes() == later_content
    assert fixture.deployed.read_bytes() == fixture.local_skill.read_bytes()
    assert first.diagnostics.error_count == 1
    assert first.integrators["skill"]._ownership_snapshots is None
    assert evidence.builds == {fixture.metadata.resolve(): 1}
    assert evidence.lock_writes == 0
    assert not get_lockfile_path(fixture.metadata).exists()

    failed_this_run = False
    # The failed run left root-local bytes without a lock; retry needs explicit overwrite.
    retried = pipeline.run_install_pipeline(
        fixture.package,
        target="copilot",
        force=True,
        parallel_downloads=0,
        no_policy=True,
        audit_override="off",
    )
    assert retried.disposition is InstallDisposition.SUCCESS
    second = evidence.contexts[1]
    assert second is not first
    assert second.integrators["skill"] is not first.integrators["skill"]
    assert second.diagnostics.error_count == 0
    assert second.integrators["skill"]._ownership_snapshots is None
    assert evidence.builds == {fixture.metadata.resolve(): 2}
    assert evidence.root_checks == 2
    assert evidence.lock_writes >= 1
    locked = LockFile.read(get_lockfile_path(fixture.metadata))
    assert locked is not None
    assert {dep.get_unique_key() for dep in locked.get_package_dependencies()} == {
        dep.get_unique_key() for dep in fixture.package.get_apm_dependencies()
    }


@pytest.mark.parametrize("mode", ["no-skills", "excluded-target"])
def test_real_pipeline_without_skill_consumers_keeps_snapshot_unused(
    monkeypatch: pytest.MonkeyPatch,
    local_install_factory: Callable[..., LocalInstall],
    mode: str,
) -> None:
    """Empty per-package target selection and prompt-only packages never read owners."""
    fixture = local_install_factory()
    shutil.rmtree(fixture.local_skill.parent.parent)
    if mode == "no-skills":
        (fixture.source / "SKILL.md").unlink()
        (fixture.source / "apm.yml").write_bytes(
            b"name: shared\nversion: 1.0.0\ndescription: Prompt-only package\n"
        )
        prompt = fixture.source / ".apm" / "prompts" / "review.prompt.md"
        prompt.parent.mkdir(parents=True)
        prompt.write_bytes(b"---\ndescription: Review changes\n---\nReview carefully.\n")
    else:
        _set_dependencies(fixture, [{"path": str(fixture.source), "targets": ["claude"]}])
    evidence = _observe_pipeline(monkeypatch, fixture, expect_skill_deployment=False)
    owner_access = MagicMock(wraps=SkillIntegrator._ownership_maps)
    monkeypatch.setattr(
        SkillIntegrator,
        "_ownership_maps",
        lambda self, root: owner_access(self, root),
    )
    result = pipeline.run_install_pipeline(
        fixture.package,
        target="copilot",
        parallel_downloads=0,
        no_policy=True,
        audit_override="off",
    )
    assert result.disposition is InstallDisposition.SUCCESS
    owner_access.assert_not_called()
    assert evidence.builds == {}
    assert evidence.events.count("integrate") == 1
    ctx = evidence.contexts[0]
    assert ctx.integrators["skill"]._ownership_snapshots is None
    assert not fixture.deployed.exists()
    assert ctx.local_deployed_files == []
    dep_key = fixture.package.get_apm_dependencies()[0].get_unique_key()
    if mode == "excluded-target":
        assert ctx.package_deployed_files[dep_key] == []
        assert not (fixture.root / ".claude").exists()
    else:
        assert ctx.total_prompts_integrated == 1
        deployed = ctx.package_deployed_files[dep_key]
        assert len(deployed) == 1
        assert (fixture.root / deployed[0]).is_file()


@pytest.mark.parametrize("scope", [InstallScope.PROJECT, InstallScope.USER])
def test_lockfile_only_runs_real_integrate_without_targets_or_skill_lookup(
    monkeypatch: pytest.MonkeyPatch,
    local_install_factory: Callable[..., LocalInstall],
    scope: InstallScope,
) -> None:
    """Lock-only still materializes dependencies and locks them without deployments."""
    fixture = local_install_factory(scope)
    root_before = fixture.local_skill.read_bytes()
    evidence = _observe_pipeline(monkeypatch, fixture, lockfile_only=True)
    result = pipeline.run_install_pipeline(
        fixture.package,
        scope=scope,
        parallel_downloads=0,
        no_policy=True,
        lockfile_only=True,
    )
    assert result.disposition is InstallDisposition.SUCCESS
    assert result.exit_code == 0
    assert evidence.events.count("integrate") == 1
    assert evidence.events.index("integrate") < evidence.events.index("lockfile")
    assert "targets" not in evidence.events
    assert "post_deps_local" not in evidence.events
    assert "cleanup" not in evidence.events
    assert evidence.builds == {}
    assert evidence.lock_writes >= 1
    ctx = evidence.contexts[0]
    assert ctx.integrators == {}
    assert ctx.targets == []
    assert ctx.package_deployed_files == {}
    assert fixture.local_skill.read_bytes() == root_before
    assert not (fixture.root / ".agents").exists()
    assert not (fixture.root / ".github").exists()
    assert not (fixture.root / ".claude").exists()
    assert not (fixture.root / ".gitignore").exists()
    locked = LockFile.read(get_lockfile_path(fixture.metadata))
    assert locked is not None
    dep_ref = fixture.package.get_apm_dependencies()[0]
    dependency = locked.get_dependency(dep_ref.get_unique_key())
    assert dependency is not None
    assert dependency.source == "local"
    assert dependency.deployed_files == []
    installed = dep_ref.get_install_path(ctx.apm_modules_dir)
    assert (installed / "SKILL.md").read_bytes() == (fixture.source / "SKILL.md").read_bytes()
