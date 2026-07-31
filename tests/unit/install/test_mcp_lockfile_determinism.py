"""Regression tests for MCP lockfile determinism."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
from apm_cli.core.scope import InstallScope
from apm_cli.deps.installed_package import InstalledPackage
from apm_cli.deps.lockfile import LockFile, LockfileFormatError, get_lockfile_path
from apm_cli.install.context import InstallContext
from apm_cli.install.phases import post_deps_local
from apm_cli.install.phases.lockfile import LockfileBuilder
from apm_cli.integration.lsp_integrator import LSPIntegrator
from apm_cli.integration.mcp_integrator import MCPIntegrator
from apm_cli.models.apm_package import APMPackage, DependencyReference, clear_apm_yml_cache
from apm_cli.utils.yaml_io import dump_yaml, load_yaml


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


def _write_manifest_with_lsp(
    project_root: Path,
    *,
    server_name: str = "pyright",
    command: str = "pyright-langserver",
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
  lsp:
    - name: {server_name}
      command: {command}
      extensionToLanguage:
        .py: python
""".lstrip(),
        encoding="utf-8",
    )
    clear_apm_yml_cache()
    return APMPackage.from_apm_yml(project_root / "apm.yml")


def _run_lockfile_phase_and_mcp_persist(
    project_root: Path,
    package: APMPackage,
    instant: datetime,
    *,
    mcp_target_servers: dict[str, list[str]] | None = None,
    package_type: str = "apm_package",
) -> InstallContext:
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
    ctx.package_types = {dep_key: package_type}

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
            mcp_target_servers=mcp_target_servers,
        )
    return ctx


def _run_lockfile_phase_and_lsp_persist(
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
    with patch("apm_cli.deps.lockfile.datetime", _FixedDatetime):
        LockfileBuilder(ctx).build_and_save()
        lsp_deps = package.get_lsp_dependencies()
        LSPIntegrator.update_lockfile(
            LSPIntegrator.get_server_names(lsp_deps),
            lock_path,
            lsp_configs=LSPIntegrator.get_server_configs(lsp_deps),
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


def test_unchanged_mcp_target_servers_do_not_rewrite_lockfile(tmp_path: Path) -> None:
    """Regression for #2297: a populated mcp_target_servers must not churn generated_at."""
    package = _write_manifest_with_mcp(tmp_path)
    first_instant = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    second_instant = datetime(2026, 1, 1, 0, 1, 0, tzinfo=timezone.utc)
    target_servers = {"claude": ["atlassian"]}

    _run_lockfile_phase_and_mcp_persist(
        tmp_path, package, first_instant, mcp_target_servers=target_servers
    )
    lock_path = get_lockfile_path(tmp_path)
    first_bytes = lock_path.read_bytes()
    first_lock = LockFile.read(lock_path)
    assert first_lock is not None
    assert first_lock.generated_at == first_instant.isoformat()
    assert first_lock.mcp_target_servers == target_servers

    second_context = _run_lockfile_phase_and_mcp_persist(
        tmp_path, package, second_instant, mcp_target_servers=target_servers
    )
    second_bytes = lock_path.read_bytes()
    second_lock = LockFile.read(lock_path)
    assert second_lock is not None

    assert second_lock.generated_at == first_lock.generated_at
    assert second_lock.mcp_target_servers == target_servers
    assert second_bytes == first_bytes
    deployment_rows = {
        (record.locator.target, record.locator.runtime, record.locator.value)
        for record in second_lock.deployment_ledger.records.values()
    }
    assert ("mcp", "claude", "atlassian") in deployment_rows
    assert (
        "copilot",
        None,
        ".github/instructions/dep.instructions.md",
    ) in deployment_rows
    second_context.logger.verbose_detail.assert_any_call(
        "MCP state unchanged -- carrying forward 1 server, 1 config, 1 target mapping"
    )


def test_real_mcp_target_change_writes_once_then_converges(tmp_path: Path) -> None:
    """A meaningful target transition writes once; its next run writes zero times."""
    package = _write_manifest_with_mcp(tmp_path)
    first_instant = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    second_instant = datetime(2026, 1, 1, 0, 1, 0, tzinfo=timezone.utc)
    third_instant = datetime(2026, 1, 1, 0, 2, 0, tzinfo=timezone.utc)
    first_targets = {"claude": ["atlassian"]}
    changed_targets = {"codex": ["atlassian"]}

    _run_lockfile_phase_and_mcp_persist(
        tmp_path,
        package,
        first_instant,
        mcp_target_servers=first_targets,
    )
    lock_path = get_lockfile_path(tmp_path)
    real_save = LockFile.save
    changed_writes: list[Path] = []

    def track_changed_write(lockfile: LockFile, path: Path) -> None:
        changed_writes.append(path)
        real_save(lockfile, path)

    with patch.object(LockFile, "save", track_changed_write):
        _run_lockfile_phase_and_mcp_persist(
            tmp_path,
            package,
            second_instant,
            mcp_target_servers=changed_targets,
        )
    changed_bytes = lock_path.read_bytes()
    changed_lock = LockFile.read(lock_path)
    assert changed_lock is not None
    assert changed_writes == [lock_path]
    assert changed_lock.generated_at == second_instant.isoformat()
    assert changed_lock.mcp_target_servers == changed_targets

    converged_writes: list[Path] = []

    def track_converged_write(lockfile: LockFile, path: Path) -> None:
        converged_writes.append(path)
        real_save(lockfile, path)

    with patch.object(LockFile, "save", track_converged_write):
        _run_lockfile_phase_and_mcp_persist(
            tmp_path,
            package,
            third_instant,
            mcp_target_servers=changed_targets,
        )
    assert converged_writes == []
    assert lock_path.read_bytes() == changed_bytes


@pytest.mark.parametrize(
    "provenance",
    [
        "packages/local-mcp",
        ["packages/local-mcp", "legacy-alias"],
    ],
)
def test_legacy_lock_preserves_scalar_or_list_provenance_without_write(
    tmp_path: Path,
    provenance: str | list[str],
) -> None:
    """Legacy locks without canonical rows remain byte-identical on a no-op."""
    package = _write_manifest_with_mcp(tmp_path)
    first_instant = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    second_instant = datetime(2026, 1, 1, 0, 1, 0, tzinfo=timezone.utc)
    targets = {"claude": ["atlassian"]}
    _run_lockfile_phase_and_mcp_persist(
        tmp_path,
        package,
        first_instant,
        mcp_target_servers=targets,
    )
    lock_path = get_lockfile_path(tmp_path)
    legacy = load_yaml(lock_path)
    legacy.pop("deployments")
    legacy["mcp_config_provenance"] = {"atlassian": provenance}
    dump_yaml(legacy, lock_path)
    legacy_bytes = lock_path.read_bytes()
    writes: list[Path] = []
    real_save = LockFile.save

    def track_write(lockfile: LockFile, path: Path) -> None:
        writes.append(path)
        real_save(lockfile, path)

    with patch.object(LockFile, "save", track_write):
        _run_lockfile_phase_and_mcp_persist(
            tmp_path,
            package,
            second_instant,
            mcp_target_servers=targets,
        )

    preserved = LockFile.read(lock_path)
    assert preserved is not None
    assert writes == []
    assert lock_path.read_bytes() == legacy_bytes
    assert preserved.mcp_config_provenance == {"atlassian": provenance}


def test_stale_partial_provenance_repairs_once_then_converges(tmp_path: Path) -> None:
    """A stale list-valued owner is pruned once without an earlier builder write."""
    package = _write_manifest_with_mcp(tmp_path)
    first_instant = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    second_instant = datetime(2026, 1, 1, 0, 1, 0, tzinfo=timezone.utc)
    third_instant = datetime(2026, 1, 1, 0, 2, 0, tzinfo=timezone.utc)
    targets = {"claude": ["atlassian"]}
    _run_lockfile_phase_and_mcp_persist(
        tmp_path,
        package,
        first_instant,
        mcp_target_servers=targets,
    )
    lock_path = get_lockfile_path(tmp_path)
    stale = load_yaml(lock_path)
    stale["mcp_config_provenance"] = {"departed-server": ["departed-package"]}
    dump_yaml(stale, lock_path)
    real_save = LockFile.save
    repair_writes: list[Path] = []

    def track_repair(lockfile: LockFile, path: Path) -> None:
        repair_writes.append(path)
        real_save(lockfile, path)

    with patch.object(LockFile, "save", track_repair):
        _run_lockfile_phase_and_mcp_persist(
            tmp_path,
            package,
            second_instant,
            mcp_target_servers=targets,
        )
    repaired = LockFile.read(lock_path)
    assert repaired is not None
    assert repair_writes == [lock_path]
    assert repaired.generated_at == second_instant.isoformat()
    assert repaired.mcp_config_provenance == {}
    repaired_bytes = lock_path.read_bytes()

    converged_writes: list[Path] = []

    def track_converged(lockfile: LockFile, path: Path) -> None:
        converged_writes.append(path)
        real_save(lockfile, path)

    with patch.object(LockFile, "save", track_converged):
        _run_lockfile_phase_and_mcp_persist(
            tmp_path,
            package,
            third_instant,
            mcp_target_servers=targets,
        )
    assert converged_writes == []
    assert lock_path.read_bytes() == repaired_bytes


def test_lockfile_write_failure_preserves_original_and_reports_error(tmp_path: Path) -> None:
    """A failed atomic replacement leaves no partial lockfile behind."""
    package = _write_manifest_with_mcp(tmp_path)
    first_instant = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    second_instant = datetime(2026, 1, 1, 0, 1, 0, tzinfo=timezone.utc)
    targets = {"claude": ["atlassian"]}
    _run_lockfile_phase_and_mcp_persist(
        tmp_path,
        package,
        first_instant,
        mcp_target_servers=targets,
    )
    lock_path = get_lockfile_path(tmp_path)
    original_bytes = lock_path.read_bytes()

    with patch("apm_cli.utils.atomic_io.os.replace", side_effect=OSError("replace blocked")):
        context = _run_lockfile_phase_and_mcp_persist(
            tmp_path,
            package,
            second_instant,
            mcp_target_servers=targets,
            package_type="skill_bundle",
        )

    expected_error = "Could not generate apm.lock.yaml: replace blocked"
    context.diagnostics.error.assert_called_once_with(expected_error)
    context.logger.error.assert_called_once_with(expected_error)
    assert lock_path.read_bytes() == original_bytes
    assert list(tmp_path.glob("apm-atomic-*")) == []


def test_mcp_persistence_write_failure_is_not_hidden(tmp_path: Path) -> None:
    """The MCP persistence layer must propagate an atomic-write failure."""
    lock_path = tmp_path / "apm.lock.yaml"
    lockfile = LockFile(generated_at="2026-01-01T00:00:00+00:00")
    lockfile.mcp_servers = ["atlassian"]
    lockfile.mcp_configs = {"atlassian": {"name": "atlassian"}}
    lockfile.save(lock_path)
    original_bytes = lock_path.read_bytes()

    with (
        patch.object(LockFile, "save", side_effect=OSError("replace blocked")),
        pytest.raises(OSError, match="replace blocked"),
    ):
        MCPIntegrator.update_lockfile(
            {"atlassian"},
            lock_path,
            mcp_configs={"atlassian": {"name": "atlassian"}},
            mcp_target_servers={"codex": ["atlassian"]},
        )

    assert lock_path.read_bytes() == original_bytes


@pytest.mark.parametrize(
    "document",
    [
        {
            "lockfile_version": "1",
            "generated_at": "2026-01-01T00:00:00+00:00",
            "dependencies": [],
            "mcp_target_servers": {"codex": "atlassian"},
        },
        {
            "lockfile_version": "1",
            "generated_at": "2026-01-01T00:00:00+00:00",
            "dependencies": [],
            "mcp_target_servers": {"codex": [123]},
        },
        {
            "lockfile_version": "1",
            "generated_at": "2026-01-01T00:00:00+00:00",
            "dependencies": [],
            "mcp_target_servers": {"": ["atlassian"]},
        },
        {
            "lockfile_version": "1",
            "generated_at": "2026-01-01T00:00:00+00:00",
            "dependencies": [],
            "mcp_target_servers": {"codex": [""]},
        },
        {
            "lockfile_version": "1",
            "generated_at": "2026-01-01T00:00:00+00:00",
            "dependencies": [],
            "mcp_config_provenance": {"atlassian": {"owner": "package"}},
        },
        {
            "lockfile_version": "1",
            "generated_at": "2026-01-01T00:00:00+00:00",
            "dependencies": [],
            "mcp_config_provenance": {"atlassian": []},
        },
        {
            "lockfile_version": "1",
            "generated_at": "2026-01-01T00:00:00+00:00",
            "dependencies": [],
            "mcp_config_provenance": {"atlassian": [123]},
        },
    ],
)
def test_malformed_mcp_lock_state_fails_explicitly(
    tmp_path: Path,
    document: dict[str, object],
) -> None:
    """Malformed target or provenance state never becomes an empty fallback."""
    lock_path = tmp_path / "apm.lock.yaml"
    dump_yaml(document, lock_path)

    with pytest.raises(LockfileFormatError):
        LockFile.read(lock_path)


def test_mcp_serialization_is_deterministic_across_input_order() -> None:
    """Target, server, and provenance ordering is platform-independent."""
    first = LockFile(generated_at="2026-01-01T00:00:00+00:00")
    second = LockFile(generated_at=first.generated_at)
    first.mcp_servers = ["zeta", "alpha"]
    second.mcp_servers = ["alpha", "zeta"]
    first.mcp_configs = {
        "zeta": {"name": "zeta"},
        "alpha": {"name": "alpha"},
    }
    second.mcp_configs = dict(reversed(list(first.mcp_configs.items())))
    first.mcp_config_provenance = {
        "zeta": ["packages/zeta", r"packages\zeta"],
        "alpha": "packages/alpha",
    }
    second.mcp_config_provenance = dict(reversed(list(first.mcp_config_provenance.items())))
    second.mcp_config_provenance["zeta"] = list(reversed(second.mcp_config_provenance["zeta"]))
    DeploymentLedgerCodec.replace_mcp_target_servers(
        first,
        {"vscode": ["zeta", "alpha"], "codex": ["zeta", "alpha"]},
    )
    DeploymentLedgerCodec.replace_mcp_target_servers(
        second,
        {"codex": ["alpha", "zeta"], "vscode": ["alpha", "zeta"]},
    )

    assert first.to_yaml() == second.to_yaml()


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


def test_unchanged_lsp_dependencies_do_not_rewrite_lockfile(tmp_path: Path) -> None:
    """The real lockfile phase stays byte-stable when LSP inputs are unchanged."""
    package = _write_manifest_with_lsp(tmp_path)
    first_instant = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    second_instant = datetime(2026, 1, 1, 0, 1, 0, tzinfo=timezone.utc)

    _run_lockfile_phase_and_lsp_persist(tmp_path, package, first_instant)
    lock_path = get_lockfile_path(tmp_path)
    first_bytes = lock_path.read_bytes()
    first_lock = LockFile.read(lock_path)
    assert first_lock is not None
    assert first_lock.generated_at == first_instant.isoformat()

    _run_lockfile_phase_and_lsp_persist(tmp_path, package, second_instant)
    second_bytes = lock_path.read_bytes()
    second_lock = LockFile.read(lock_path)
    assert second_lock is not None

    assert second_lock.generated_at == first_lock.generated_at
    assert second_lock.lsp_servers == first_lock.lsp_servers
    assert second_lock.lsp_configs == first_lock.lsp_configs
    assert second_bytes == first_bytes
