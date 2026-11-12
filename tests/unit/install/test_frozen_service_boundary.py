"""Cross-platform lifetime and recorded-only native-service eligibility."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from apm_cli.constants import InstallMode
from apm_cli.core.scope import InstallScope
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.install.context import InstallContext
from apm_cli.install.errors import FrozenInstallError
from apm_cli.install.mcp.integration import prepare_mcp_integration
from apm_cli.install.outcome import result_from_install_context
from apm_cli.install.service import InstallService
from apm_cli.install.service_integration import run_service_integrations
from apm_cli.integration.mcp_config_view import CurrentMcpConfigView, McpSourceProblem
from apm_cli.models.apm_package import APMPackage
from apm_cli.models.dependency.mcp import MCPDependency
from apm_cli.models.results import InstallResult
from apm_cli.utils.yaml_io import dump_yaml

pytestmark = pytest.mark.component


@pytest.mark.parametrize("admitted", [False, True], ids=["omitted", "admitted"])
def test_skills_only_preparation_retains_omissions_without_selected_comparison(
    tmp_path: Path, admitted: bool
) -> None:
    """Omission intent is captured before writes without comparing unselected MCP."""
    source = tmp_path / "dependency"
    source.mkdir()
    declaration = MCPDependency(
        name="service", registry=False, transport="http", url="https://example.invalid/service"
    )
    dump_yaml(
        {
            "name": "dependency",
            "version": "1.0.0",
            "dependencies": {"mcp": [declaration.to_dict()]},
        },
        source / "apm.yml",
    )
    dependency = LockedDependency(
        repo_url="_local/dependency", source="local", local_path=str(source), depth=1
    )
    lock = LockFile(
        dependencies={dependency.get_unique_key(): dependency},
        mcp_servers=["service"] if admitted else [],
        mcp_configs={"service": declaration.to_dict()} if admitted else {},
    )
    lock_path = tmp_path / "apm.lock.yaml"
    lock.write(lock_path)
    before = lock_path.read_bytes()
    prepared = prepare_mcp_integration(
        apm_package=APMPackage(name="root", version="1.0.0", package_path=tmp_path),
        mcp_deps=[],
        apm_modules_path=tmp_path / "apm_modules",
        lock_path=lock_path,
        should_install=False,
        trust_transitive_mcp=False,
        logger=Mock(),
        allow_pending=False,
    )
    assert prepared.current_view is None
    assert prepared.dependencies == []
    assert prepared.omissions is not None
    assert set(prepared.omissions) == (set() if admitted else {"service"})
    if not admitted:
        assert prepared.omissions["service"]["reason"] == "only-apm"
    assert lock_path.read_bytes() == before


def test_result_retains_complete_graph_instead_of_selected_packages(tmp_path: Path) -> None:
    """Late services receive the same pre-selection graph as the hydrated gate."""
    context = InstallContext(project_root=tmp_path, apm_dir=tmp_path)
    context.update_plan_complete_dep_keys = {"owner/selected", "owner/transitive", "owner/other"}
    context.intended_dep_keys = {"owner/selected"}
    result = result_from_install_context(context)
    assert result.active_package_keys == frozenset(
        {"owner/selected", "owner/transitive", "owner/other"}
    )
    context.update_plan_complete_dep_keys.clear()
    assert len(result.active_package_keys) == 3


def test_result_distinguishes_unknown_membership_from_resolved_empty_graph(tmp_path: Path) -> None:
    """Skipping package resolution must not claim that no active packages exist."""
    assert InstallResult().active_package_keys is None
    context = InstallContext(project_root=tmp_path, apm_dir=tmp_path)
    assert result_from_install_context(context).active_package_keys == frozenset()


@pytest.mark.parametrize(
    ("active_keys", "selected", "expected_problems"),
    [
        (None, True, {"owner/active", "owner/orphan"}),
        (frozenset(), True, set()),
        (frozenset({"owner/active"}), True, {"owner/active"}),
        (None, False, None),
    ],
    ids=["unknown", "known-empty", "active-unavailable", "standalone-mcp-unselected"],
)
def test_prepared_frozen_view_preserves_membership_and_actual_source_requirements(
    tmp_path: Path,
    active_keys: frozenset[str] | None,
    selected: bool,
    expected_problems: set[str] | None,
) -> None:
    """Inactive rows may be ignored; unavailable active sources are never empty."""
    root = APMPackage(
        name="root",
        version="1.0.0",
        package_path=tmp_path,
        dependencies={
            "mcp": [
                MCPDependency(
                    name="root-service",
                    registry=False,
                    transport="http",
                    url="https://example.invalid/root",
                )
            ]
        },
    )
    dependencies = {
        key: LockedDependency(repo_url=key, package_type="apm_package", depth=1)
        for key in ("owner/active", "owner/orphan")
    }
    lock_path = tmp_path / "apm.lock.yaml"
    LockFile(dependencies=dependencies).write(lock_path)
    prepared = prepare_mcp_integration(
        apm_package=root,
        mcp_deps=root.get_all_mcp_dependencies(),
        apm_modules_path=tmp_path / "apm_modules",
        lock_path=lock_path,
        should_install=selected,
        trust_transitive_mcp=False,
        logger=Mock(),
        allow_pending=False,
        active_package_keys=active_keys,
    )
    if expected_problems is None:
        assert prepared.current_view is None
    else:
        assert prepared.current_view is not None
        assert {
            problem.package_key for problem in prepared.current_view.problems
        } == expected_problems
        assert set(prepared.current_view.configs) == {"root-service"}
        assert not prepared.current_view.pending_packages


@pytest.mark.parametrize("mode", [InstallMode.ALL, InstallMode.APM], ids=["selected", "mcp-off"])
def test_late_service_boundary_requires_actual_selected_source_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: InstallMode
) -> None:
    """Strict late verification is selected independently from package integration."""
    monkeypatch.chdir(tmp_path)
    root = APMPackage(name="root", version="1.0.0", package_path=tmp_path)
    dependency = LockedDependency(repo_url="owner/missing", package_type="apm_package", depth=1)
    lock = LockFile(dependencies={dependency.get_unique_key(): dependency})
    lock_path = tmp_path / "apm.lock.yaml"
    lock.write(lock_path)
    mcp_writer = Mock(return_value=(0, {}))
    lsp_writer = Mock(return_value=0)
    monkeypatch.setattr("apm_cli.install.mcp.run_mcp_integration", mcp_writer)
    monkeypatch.setattr("apm_cli.install.lsp.run_lsp_integration", lsp_writer)
    context = SimpleNamespace(
        project_root=tmp_path,
        scope=InstallScope.PROJECT,
        install_mode=mode,
        logger=Mock(),
        frozen=True,
        trust_transitive_mcp=False,
        exec_allow_map=None,
        exec_allow_resolved=False,
        no_policy=True,
        runtime=None,
        exclude=None,
        verbose=False,
        force=False,
    )
    arguments = {
        "apm_package": root,
        "mcp_deps": [],
        "lock_path": lock_path,
        "existing_lock": lock,
        "old_mcp_servers": set(),
        "old_mcp_configs": {},
        "old_mcp_provenance": {},
        "old_mcp_target_servers": {},
        "old_mcp_target_servers_present": True,
        "diagnostics": None,
        "explicit_target": None,
        "target_decision": None,
        "active_package_keys": frozenset({dependency.get_unique_key()}),
    }
    if mode is InstallMode.ALL:
        with pytest.raises(FrozenInstallError, match="cannot replay") as failure:
            run_service_integrations(context, **arguments)
        assert any("owner/missing" in reason for reason in failure.value.reasons)
        mcp_writer.assert_not_called()
        lsp_writer.assert_not_called()
    else:
        run_service_integrations(context, **arguments)
        assert mcp_writer.call_args.kwargs["prepared"].current_view is None
        assert mcp_writer.call_args.kwargs["should_install"] is False


@pytest.mark.windows_compat
@pytest.mark.parametrize("fail", [False, True], ids=["success", "failure"])
def test_command_suppression_preserves_exact_bytes_and_restores_writer(
    tmp_path: Path, fail: bool
) -> None:
    lock = tmp_path / "space root" / "apm.lock.yaml"
    lock.parent.mkdir()
    original = b"# retained\r\nlockfile_version: '1'\r\ngenerated_at: '2020-01-01'\r\n"
    lock.write_bytes(original)

    def execute() -> None:
        with InstallService.frozen_writes(True) as writes:
            LockFile(mcp_servers=["late-service"]).write(lock)
            assert writes and "late-service" in writes[0]
            if fail:
                raise ValueError("native integration failed")

    if fail:
        with pytest.raises(ValueError, match="native integration failed"):
            execute()
    else:
        execute()
    assert lock.read_bytes() == original
    with InstallService.frozen_writes(False):
        LockFile(mcp_servers=["ordinary-install"]).write(lock)
    restored = LockFile.read(lock)
    assert restored is not None and restored.mcp_servers == ["ordinary-install"]


@pytest.mark.parametrize("legacy", [False, True], ids=["explicit-empty", "legacy-absent"])
def test_native_guard_does_not_adopt_unrecorded_native_ownership(legacy: bool) -> None:
    lock = LockFile(mcp_servers=["managed"], mcp_target_servers={})
    lock._mcp_target_servers_present = not legacy
    with pytest.raises(FrozenInstallError, match="cannot replay") as failure:
        InstallService.enforce_frozen_services(
            lock,
            mcp_servers={"managed"},
            mcp_targets=["intellij"],
            lsp_servers=set(),
            lsp_targets=[],
            current_mcp=None,
            user_scope=False,
        )
    assert failure.value.reasons == [
        "  - MCP server 'managed' target 'intellij' ownership is not recorded in apm.lock.yaml"
    ]
    assert lock.mcp_target_servers == {}


def test_native_guard_requires_actual_source_evidence_even_without_claims() -> None:
    with pytest.raises(FrozenInstallError, match="cannot replay") as failure:
        InstallService.enforce_frozen_services(
            LockFile(),
            mcp_servers=set(),
            mcp_targets=[],
            lsp_servers=set(),
            lsp_targets=[],
            current_mcp=CurrentMcpConfigView(
                dependencies=(),
                configs={},
                provenance={},
                problems=(McpSourceProblem("package", Path("apm.yml"), "source unavailable"),),
            ),
            user_scope=False,
        )
    assert failure.value.reasons == ["  - package: source unavailable"]


@pytest.mark.windows_compat
def test_native_ownership_diagnostic_escapes_non_ascii_server_names() -> None:
    with pytest.raises(FrozenInstallError, match="cannot replay") as failure:
        InstallService.enforce_frozen_services(
            LockFile(),
            mcp_servers={"snowman-\u2603"},
            mcp_targets=["intellij"],
            lsp_servers=set(),
            lsp_targets=[],
            current_mcp=None,
            user_scope=False,
        )
    assert failure.value.reasons == [
        "  - MCP server 'snowman-\\u2603' target 'intellij' "
        "ownership is not recorded in apm.lock.yaml"
    ]
