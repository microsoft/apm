"""Regression tests for MCP lockfile determinism."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from apm_cli.core.scope import InstallScope
from apm_cli.deps.installed_package import InstalledPackage
from apm_cli.deps.lockfile import LockFile, get_lockfile_path
from apm_cli.install.context import InstallContext
from apm_cli.install.phases import post_deps_local
from apm_cli.install.phases.lockfile import LockfileBuilder
from apm_cli.integration.mcp_integrator import MCPIntegrator
from apm_cli.models.apm_package import APMPackage, DependencyReference, clear_apm_yml_cache


class _FixedDatetime:
    instant = datetime(2026, 1, 1, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz: timezone | None = None) -> datetime:
        if tz is None:
            return cls.instant.replace(tzinfo=None)
        return cls.instant.astimezone(tz)


def _write_manifest_with_mcp(
    project_root: Path,
    *,
    server_name: str = "atlassian",
    server_url: str = "https://mcp.atlassian.com/v1/mcp",
) -> APMPackage:
    (project_root / "packages" / "dep" / ".apm" / "instructions").mkdir(
        parents=True,
        exist_ok=True,
    )
    (project_root / "packages" / "dep" / "apm.yml").write_text(
        'name: dep\nversion: "1.0.0"\n',
        encoding="utf-8",
    )
    deployed_file = project_root / ".github" / "instructions" / "dep.instructions.md"
    deployed_file.parent.mkdir(parents=True, exist_ok=True)
    deployed_file.write_text(
        '---\napplyTo: "**"\n---\n# Dep\n',
        encoding="utf-8",
    )
    (project_root / "apm.yml").write_text(
        f"""
name: repro
version: "1.0.0"
dependencies:
  apm:
    - ./packages/dep
  mcp:
    - name: {server_name}
      registry: false
      transport: http
      url: {server_url}
""".lstrip(),
        encoding="utf-8",
    )
    clear_apm_yml_cache()
    return APMPackage.from_apm_yml(project_root / "apm.yml")


def _run_lockfile_phase_and_mcp_persist(
    project_root: Path,
    package: APMPackage,
    instant: datetime,
) -> None:
    lock_path = get_lockfile_path(project_root)
    dep_ref = package.get_apm_dependencies()[0]
    ctx = InstallContext(
        project_root=project_root,
        apm_dir=project_root,
        apm_package=package,
        existing_lockfile=LockFile.read(lock_path),
        logger=MagicMock(),
        diagnostics=MagicMock(),
    )
    ctx.installed_packages = [
        InstalledPackage(dep_ref=dep_ref, resolved_commit=None, depth=1, resolved_by=None)
    ]
    dep_key = dep_ref.get_unique_key()
    ctx.package_deployed_files = {dep_key: [".github/instructions/dep.instructions.md"]}
    ctx.package_types = {dep_key: "apm_package"}

    _FixedDatetime.instant = instant
    with (
        patch("apm_cli.deps.lockfile.datetime", _FixedDatetime),
        patch("apm_cli.integration.mcp_integrator.datetime", _FixedDatetime),
    ):
        LockfileBuilder(ctx).build_and_save()
        mcp_deps = package.get_mcp_dependencies()
        MCPIntegrator.update_lockfile(
            MCPIntegrator.get_server_names(mcp_deps),
            lock_path,
            mcp_configs=MCPIntegrator.get_server_configs(mcp_deps),
        )


def _write_local_instruction(project_root: Path) -> list[str]:
    (project_root / ".apm" / "instructions").mkdir(parents=True, exist_ok=True)
    (project_root / ".apm" / "instructions" / "local.instructions.md").write_text(
        "# Local instructions\n",
        encoding="utf-8",
    )
    deployed_file = project_root / ".github" / "instructions" / "local.instructions.md"
    deployed_file.parent.mkdir(parents=True, exist_ok=True)
    deployed_file.write_text("# Local instructions\n", encoding="utf-8")
    return [".github/instructions/local.instructions.md"]


def _run_lockfile_phase_and_local_persist(project_root: Path, instant: datetime) -> None:
    lock_path = get_lockfile_path(project_root)
    existing_lockfile = LockFile.read(lock_path)
    local_deployed_files = _write_local_instruction(project_root)
    dep_ref = DependencyReference(
        repo_url="AllySummers/apm-generatedat-repro-dep",
        reference="1111111111111111111111111111111111111111",
    )
    ctx = InstallContext(
        project_root=project_root,
        apm_dir=project_root,
        existing_lockfile=existing_lockfile,
        logger=MagicMock(),
        diagnostics=MagicMock(error_count=0),
        scope=InstallScope.PROJECT,
    )
    ctx.installed_packages = [
        InstalledPackage(
            dep_ref=dep_ref,
            resolved_commit="a" * 40,
            depth=1,
            resolved_by=None,
        )
    ]
    ctx.local_deployed_files = local_deployed_files
    if existing_lockfile is not None:
        ctx.old_local_deployed = list(existing_lockfile.local_deployed_files)

    _FixedDatetime.instant = instant
    with patch("apm_cli.deps.lockfile.datetime", _FixedDatetime):
        LockfileBuilder(ctx).build_and_save()
        post_deps_local.run(ctx)


def test_unchanged_local_instructions_do_not_rewrite_lockfile(tmp_path: Path) -> None:
    """The lockfile stays byte-stable when local instruction hashes are unchanged."""
    first_instant = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    second_instant = datetime(2026, 1, 1, 0, 1, 0, tzinfo=timezone.utc)

    _run_lockfile_phase_and_local_persist(tmp_path, first_instant)
    lock_path = get_lockfile_path(tmp_path)
    first_bytes = lock_path.read_bytes()
    first_lock = LockFile.read(lock_path)
    assert first_lock is not None
    assert first_lock.generated_at == first_instant.isoformat()
    assert first_lock.local_deployed_files == [".github/instructions/local.instructions.md"]

    _run_lockfile_phase_and_local_persist(tmp_path, second_instant)
    second_bytes = lock_path.read_bytes()
    second_lock = LockFile.read(lock_path)
    assert second_lock is not None

    assert second_lock.generated_at == first_lock.generated_at
    assert second_lock.local_deployed_file_hashes == first_lock.local_deployed_file_hashes
    assert second_bytes == first_bytes


def test_unchanged_mcp_dependencies_do_not_rewrite_lockfile(tmp_path: Path) -> None:
    """The real lockfile phase stays byte-stable when MCP inputs are unchanged."""
    package = _write_manifest_with_mcp(tmp_path)
    first_instant = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    second_instant = datetime(2026, 1, 1, 0, 1, 0, tzinfo=timezone.utc)

    _run_lockfile_phase_and_mcp_persist(tmp_path, package, first_instant)
    lock_path = get_lockfile_path(tmp_path)
    first_bytes = lock_path.read_bytes()
    first_lock = LockFile.read(lock_path)
    assert first_lock is not None
    assert first_lock.generated_at == first_instant.isoformat()

    _run_lockfile_phase_and_mcp_persist(tmp_path, package, second_instant)
    second_bytes = lock_path.read_bytes()
    second_lock = LockFile.read(lock_path)
    assert second_lock is not None

    assert second_lock.generated_at == first_lock.generated_at
    assert second_bytes == first_bytes


def test_changed_mcp_dependencies_update_lockfile(tmp_path: Path) -> None:
    """The no-write optimization still persists changed MCP inputs."""
    package = _write_manifest_with_mcp(tmp_path)
    first_instant = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    second_instant = datetime(2026, 1, 1, 0, 1, 0, tzinfo=timezone.utc)

    _run_lockfile_phase_and_mcp_persist(tmp_path, package, first_instant)
    lock_path = get_lockfile_path(tmp_path)
    first_bytes = lock_path.read_bytes()
    first_lock = LockFile.read(lock_path)
    assert first_lock is not None
    assert first_lock.mcp_servers == ["atlassian"]

    changed_package = _write_manifest_with_mcp(
        tmp_path,
        server_name="github",
        server_url="https://api.githubcopilot.com/mcp/",
    )
    _run_lockfile_phase_and_mcp_persist(tmp_path, changed_package, second_instant)
    second_bytes = lock_path.read_bytes()
    second_lock = LockFile.read(lock_path)
    assert second_lock is not None

    assert second_lock.generated_at == second_instant.isoformat()
    assert second_lock.mcp_servers == ["github"]
    assert second_lock.mcp_configs == {
        "github": {
            "name": "github",
            "registry": False,
            "transport": "http",
            "url": "https://api.githubcopilot.com/mcp/",
        }
    }
    assert second_bytes != first_bytes
