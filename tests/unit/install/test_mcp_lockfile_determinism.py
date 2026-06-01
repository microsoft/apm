"""Regression tests for MCP lockfile determinism."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from apm_cli.deps.installed_package import InstalledPackage
from apm_cli.deps.lockfile import LockFile, get_lockfile_path
from apm_cli.install.context import InstallContext
from apm_cli.install.phases.lockfile import LockfileBuilder
from apm_cli.integration.mcp_integrator import MCPIntegrator
from apm_cli.models.apm_package import APMPackage, clear_apm_yml_cache


class _FixedDatetime:
    instant = datetime(2026, 1, 1, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz: timezone | None = None) -> datetime:
        if tz is None:
            return cls.instant.replace(tzinfo=None)
        return cls.instant.astimezone(tz)


def _write_manifest_with_mcp(project_root: Path) -> APMPackage:
    (project_root / "packages" / "dep" / ".apm" / "instructions").mkdir(parents=True)
    (project_root / "packages" / "dep" / "apm.yml").write_text(
        'name: dep\nversion: "1.0.0"\n',
        encoding="utf-8",
    )
    deployed_file = project_root / ".github" / "instructions" / "dep.instructions.md"
    deployed_file.parent.mkdir(parents=True)
    deployed_file.write_text(
        '---\napplyTo: "**"\n---\n# Dep\n',
        encoding="utf-8",
    )
    (project_root / "apm.yml").write_text(
        """
name: repro
version: "1.0.0"
dependencies:
  apm:
    - ./packages/dep
  mcp:
    - name: atlassian
      registry: false
      transport: http
      url: https://mcp.atlassian.com/v1/mcp
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
