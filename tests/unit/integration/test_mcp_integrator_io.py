"""Unit tests for MCPIntegrator (filesystem, process and CLI boundary cases)."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from apm_cli.integration.mcp_integrator import MCPIntegrator, _is_vscode_available

pytestmark = pytest.mark.component

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# ===========================================================================
# _is_vscode_available
# ===========================================================================


class TestIsVscodeAvailable:
    @pytest.mark.parametrize(
        ("code_path", "has_vscode_dir", "expected"),
        [
            ("/usr/bin/code", False, True),
            (None, True, True),
            (None, False, False),
        ],
        ids=["code-on-path", "vscode-dir-exists", "neither-available"],
    )
    def test_availability_from_cwd(self, tmp_path, code_path, has_vscode_dir, expected):
        if has_vscode_dir:
            (tmp_path / ".vscode").mkdir()
        with (
            patch("shutil.which", return_value=code_path),
            patch("apm_cli.integration.mcp_integrator.Path.cwd", return_value=tmp_path),
        ):
            assert _is_vscode_available() is expected

    def test_code_on_path_takes_precedence_over_missing_dir(self, tmp_path):
        missing_root = tmp_path / "no-such-project"
        with (
            patch("shutil.which", return_value="/usr/bin/code") as which,
            patch.object(Path, "is_dir", autospec=True, return_value=False) as is_dir,
        ):
            assert _is_vscode_available(missing_root) is True
        which.assert_called_once_with("code")
        is_dir.assert_not_called()


# ===========================================================================
# MCPIntegrator.deduplicate
# ===========================================================================


# ===========================================================================
# MCPIntegrator.get_server_names
# ===========================================================================


# ===========================================================================
# MCPIntegrator.get_server_configs
# ===========================================================================


# ===========================================================================
# MCPIntegrator.get_server_provenance
# ===========================================================================


# ===========================================================================
# MCPIntegrator._append_drifted_to_install_list
# ===========================================================================


# ===========================================================================
# MCPIntegrator._detect_mcp_config_drift
# ===========================================================================


# ===========================================================================
# MCPIntegrator._detect_runtimes
# ===========================================================================


# ===========================================================================
# MCPIntegrator._build_self_defined_info
# ===========================================================================


# ===========================================================================
# MCPIntegrator._apply_overlay
# ===========================================================================


# ===========================================================================
# MCPIntegrator.update_lockfile
# ===========================================================================


class TestUpdateLockfile:
    def _write_minimal_lockfile(self, path: Path) -> None:
        content = "lockfile_version: '1'\ngenerated_at: '2026-01-01'\ndependencies: []\n"
        path.write_text(content, encoding="utf-8")

    def test_updates_mcp_servers_in_lockfile(self, tmp_path):
        lock_path = tmp_path / "apm.lock.yaml"
        self._write_minimal_lockfile(lock_path)

        MCPIntegrator.update_lockfile({"server-a", "server-b"}, lock_path=lock_path)

        from apm_cli.deps.lockfile import LockFile

        lf = LockFile.read(lock_path)
        assert set(lf.mcp_servers) == {"server-a", "server-b"}

    def test_update_reuses_preloaded_lockfile(self, tmp_path, monkeypatch):
        from apm_cli.utils import yaml_io

        lock_path = tmp_path / "apm.lock.yaml"
        self._write_minimal_lockfile(lock_path)
        real_load_yaml_str = yaml_io.load_yaml_str
        load_calls = 0

        def counting_load_yaml_str(text):
            nonlocal load_calls
            load_calls += 1
            return real_load_yaml_str(text)

        monkeypatch.setattr(yaml_io, "load_yaml_str", counting_load_yaml_str)

        MCPIntegrator.update_lockfile({"server-a"}, lock_path=lock_path)

        assert load_calls == 1

    def test_updates_mcp_configs_when_provided(self, tmp_path):
        lock_path = tmp_path / "apm.lock.yaml"
        self._write_minimal_lockfile(lock_path)
        configs = {"server-a": {"name": "server-a", "transport": "stdio"}}

        MCPIntegrator.update_lockfile({"server-a"}, lock_path=lock_path, mcp_configs=configs)

        from apm_cli.deps.lockfile import LockFile

        lf = LockFile.read(lock_path)
        assert lf.mcp_configs == configs

    def test_creates_lockfile_when_missing(self, tmp_path):
        missing = tmp_path / "no_lock.yaml"
        MCPIntegrator.update_lockfile({"svc"}, lock_path=missing)

        from apm_cli.deps.lockfile import LockFile

        lock = LockFile.read(missing)
        assert lock is not None
        assert lock.mcp_servers == ["svc"]
        assert lock.generated_at is None
        assert "generated_at" not in missing.read_text(encoding="utf-8")

    def test_mcp_servers_sorted_in_lockfile(self, tmp_path):
        lock_path = tmp_path / "apm.lock.yaml"
        self._write_minimal_lockfile(lock_path)

        MCPIntegrator.update_lockfile({"z-svc", "a-svc", "m-svc"}, lock_path=lock_path)

        from apm_cli.deps.lockfile import LockFile

        lf = LockFile.read(lock_path)
        assert lf.mcp_servers == sorted(lf.mcp_servers)

    def test_empty_server_set_clears_mcp_servers(self, tmp_path):
        lock_path = tmp_path / "apm.lock.yaml"
        # Pre-populate
        self._write_minimal_lockfile(lock_path)
        MCPIntegrator.update_lockfile({"existing"}, lock_path=lock_path)

        MCPIntegrator.update_lockfile(set(), lock_path=lock_path)

        from apm_cli.deps.lockfile import LockFile

        lf = LockFile.read(lock_path)
        assert lf.mcp_servers == []

    def test_updates_provenance_when_provided(self, tmp_path):
        lock_path = tmp_path / "apm.lock.yaml"
        self._write_minimal_lockfile(lock_path)
        configs = {"shadcn": {"name": "shadcn"}}
        prov = {"shadcn": "@qado/agent-config"}

        MCPIntegrator.update_lockfile(
            {"shadcn"}, lock_path=lock_path, mcp_configs=configs, mcp_config_provenance=prov
        )

        from apm_cli.deps.lockfile import LockFile

        lf = LockFile.read(lock_path)
        assert lf.mcp_config_provenance == prov

    def test_prunes_dangling_provenance_when_config_removed(self, tmp_path):
        # Seed a lockfile that carries provenance for 'shadcn'. A later update
        # rewrites mcp_configs WITHOUT 'shadcn' and without an explicit
        # provenance arg (the single-add path). The dangling 'shadcn'
        # provenance must be pruned so it cannot exempt an orphan (#2081).
        lock_path = tmp_path / "apm.lock.yaml"
        lock_path.write_text(
            "lockfile_version: '1'\n"
            "generated_at: '2026-01-01'\n"
            "dependencies: []\n"
            "mcp_configs:\n"
            "  shadcn:\n"
            "    name: shadcn\n"
            "mcp_config_provenance:\n"
            "  shadcn: '@qado/agent-config'\n",
            encoding="utf-8",
        )

        MCPIntegrator.update_lockfile(
            {"other"}, lock_path=lock_path, mcp_configs={"other": {"name": "other"}}
        )

        from apm_cli.deps.lockfile import LockFile

        lf = LockFile.read(lock_path)
        assert lf.mcp_config_provenance == {}


# ===========================================================================
# MCPIntegrator.remove_stale - vscode
# ===========================================================================


class TestRemoveStaleVscode:
    def _write_vscode_mcp(self, path: Path, servers: dict) -> None:
        path.mkdir(parents=True, exist_ok=True)
        mcp_json = path / "mcp.json"
        mcp_json.write_text(json.dumps({"servers": servers}), encoding="utf-8")

    def test_removes_stale_server_from_vscode(self, tmp_path):
        vscode_dir = tmp_path / ".vscode"
        self._write_vscode_mcp(vscode_dir, {"old-server": {}, "keep-server": {}})

        with (
            patch("apm_cli.integration.mcp_integrator.Path.cwd", return_value=tmp_path),
            patch("pathlib.Path.home", return_value=tmp_path),
        ):
            MCPIntegrator.remove_stale({"old-server"}, runtime="vscode")

        remaining = json.loads((vscode_dir / "mcp.json").read_text())
        assert "old-server" not in remaining["servers"]
        assert "keep-server" in remaining["servers"]

    def test_short_name_matched_for_path_reference(self, tmp_path):
        vscode_dir = tmp_path / ".vscode"
        self._write_vscode_mcp(vscode_dir, {"github-mcp-server": {}, "keep": {}})

        with (
            patch("apm_cli.integration.mcp_integrator.Path.cwd", return_value=tmp_path),
            patch("pathlib.Path.home", return_value=tmp_path),
        ):
            MCPIntegrator.remove_stale({"io.github.github/github-mcp-server"}, runtime="vscode")

        remaining = json.loads((vscode_dir / "mcp.json").read_text())
        assert "github-mcp-server" not in remaining["servers"]

    def test_empty_stale_set_is_noop(self, tmp_path):
        vscode_dir = tmp_path / ".vscode"
        self._write_vscode_mcp(vscode_dir, {"server": {}})
        original_mtime = (vscode_dir / "mcp.json").stat().st_mtime

        with (
            patch("apm_cli.integration.mcp_integrator.Path.cwd", return_value=tmp_path),
            patch("pathlib.Path.home", return_value=tmp_path),
        ):
            MCPIntegrator.remove_stale(set(), runtime="vscode")

        # File unchanged when stale set is empty (early return)
        assert (vscode_dir / "mcp.json").stat().st_mtime == original_mtime

    def test_missing_vscode_mcp_json_is_noop(self, tmp_path):
        # No .vscode/mcp.json at all - should not raise
        with (
            patch("apm_cli.integration.mcp_integrator.Path.cwd", return_value=tmp_path),
            patch("pathlib.Path.home", return_value=tmp_path),
        ):
            MCPIntegrator.remove_stale({"ghost"}, runtime="vscode")

    def test_strict_cleanup_raises_on_unwritable_config_shape(self, tmp_path):
        from apm_cli.install.errors import RequiredIntegrationError

        config_path = tmp_path / ".vscode" / "mcp.json"
        config_path.mkdir(parents=True)

        with pytest.raises(RequiredIntegrationError, match="MCP cleanup failed"):
            MCPIntegrator.remove_stale(
                {"stale"},
                runtime="vscode",
                project_root=tmp_path,
                fail_on_write_error=True,
            )

    def test_target_restricted_to_requested_runtime(self, tmp_path):
        vscode_dir = tmp_path / ".vscode"
        self._write_vscode_mcp(vscode_dir, {"stale": {}})
        copilot_dir = tmp_path / ".copilot"
        copilot_dir.mkdir()
        copilot_mcp = copilot_dir / "mcp-config.json"
        copilot_mcp.write_text(json.dumps({"mcpServers": {"stale": {}}}), encoding="utf-8")

        with (
            patch("apm_cli.integration.mcp_integrator.Path.cwd", return_value=tmp_path),
            patch("pathlib.Path.home", return_value=tmp_path),
        ):
            MCPIntegrator.remove_stale({"stale"}, runtime="vscode")

        # vscode cleaned; copilot untouched
        vscode_remaining = json.loads((vscode_dir / "mcp.json").read_text())
        assert "stale" not in vscode_remaining["servers"]
        copilot_remaining = json.loads(copilot_mcp.read_text())
        assert "stale" in copilot_remaining["mcpServers"]

    def test_removes_stale_server_from_vscode_with_explicit_project_root(self, tmp_path):
        nested = tmp_path / "nested-project"
        vscode_dir = nested / ".vscode"
        self._write_vscode_mcp(vscode_dir, {"old-server": {}, "keep-server": {}})

        with (
            patch("apm_cli.integration.mcp_integrator.Path.cwd", return_value=tmp_path),
            patch("pathlib.Path.home", return_value=tmp_path),
        ):
            MCPIntegrator.remove_stale(
                {"old-server"},
                runtime="vscode",
                project_root=nested,
            )

        remaining = json.loads((vscode_dir / "mcp.json").read_text())
        assert "old-server" not in remaining["servers"]
        assert "keep-server" in remaining["servers"]


class TestInstallProjectRootDetection:
    @patch("apm_cli.registry.operations.MCPServerOperations")
    @patch("apm_cli.integration.mcp_integrator.MCPIntegrator._install_for_runtime")
    @patch("apm_cli.runtime.manager.RuntimeManager")
    @patch("shutil.which", return_value=None)
    def test_install_uses_explicit_project_root_for_workspace_runtime_detection(
        self, _which, mock_mgr_cls, mock_install_rt, mock_ops_cls, tmp_path
    ):
        # A single, unambiguous directory signal in `nested` (none in
        # `tmp_path`/cwd) isolates what this test asserts: auto-detection
        # (and the active-targets gate it feeds) probes the explicit
        # `project_root`, not `Path.cwd()`. apm.yml declares no `targets:`
        # here -- a manifest-declared value now resolves MCP ownership
        # deterministically from the declaration itself (issue #2298) and
        # would bypass the auto-detection this test exercises; see
        # `test_declared_targets_override_local_runtime_detection` below for
        # that path.
        nested = tmp_path / "nested-project"
        (nested / ".cursor").mkdir(parents=True)

        mock_mgr = mock_mgr_cls.return_value
        mock_mgr.is_runtime_available.return_value = False
        mock_install_rt.return_value = True

        mock_ops = mock_ops_cls.return_value
        mock_ops.validate_servers_exist.return_value = (["test/server"], [])
        mock_ops.check_servers_needing_installation.return_value = ["test/server"]
        mock_ops.batch_fetch_server_info.return_value = {"test/server": {}}
        mock_ops.collect_environment_variables.return_value = {}
        mock_ops.collect_runtime_variables.return_value = {}

        with patch("apm_cli.integration.mcp_integrator.Path.cwd", return_value=tmp_path):
            MCPIntegrator.install(
                mcp_deps=["test/server"],
                project_root=nested,
            )

        called_runtimes = {call.args[0] for call in mock_install_rt.call_args_list}
        assert called_runtimes == {"cursor"}

    @patch("apm_cli.registry.operations.MCPServerOperations")
    @patch("apm_cli.integration.mcp_integrator.MCPIntegrator._install_for_runtime")
    @patch("apm_cli.runtime.manager.RuntimeManager")
    @patch("shutil.which", return_value=None)
    def test_declared_targets_override_local_runtime_detection(
        self, _which, mock_mgr_cls, mock_install_rt, mock_ops_cls, tmp_path
    ):
        """Regression for #2298: declared `targets:` must not gain undeclared runtimes.

        `.vscode/` exists on disk (as it would on one developer's machine
        but not another's). The declared Copilot target intentionally projects
        to the Copilot CLI project runtime; no additional runtime may be inferred.
        """
        nested = tmp_path / "nested-project"
        (nested / ".vscode").mkdir(parents=True)
        (nested / ".github").mkdir()

        mock_mgr = mock_mgr_cls.return_value
        mock_mgr.is_runtime_available.return_value = False
        mock_install_rt.return_value = True

        mock_ops = mock_ops_cls.return_value
        mock_ops.validate_servers_exist.return_value = (["test/server"], [])
        mock_ops.check_servers_needing_installation.return_value = ["test/server"]
        mock_ops.batch_fetch_server_info.return_value = {"test/server": {}}
        mock_ops.collect_environment_variables.return_value = {}
        mock_ops.collect_runtime_variables.return_value = {}

        with patch("apm_cli.integration.mcp_integrator.Path.cwd", return_value=tmp_path):
            MCPIntegrator.install(
                mcp_deps=["test/server"],
                project_root=nested,
                apm_config={"targets": ["copilot", "cursor", "opencode"]},
            )

        called_runtimes = {call.args[0] for call in mock_install_rt.call_args_list}
        assert called_runtimes == {"copilot", "cursor", "opencode"}

    @patch("apm_cli.registry.operations.MCPServerOperations")
    @patch("apm_cli.integration.mcp_integrator.MCPIntegrator._install_for_runtime")
    @patch("apm_cli.integration.mcp_integrator_install._discover_installed_runtimes")
    def test_explicit_runtime_overrides_manifest_and_machine(
        self, mock_discover, mock_install_rt, mock_ops_cls, tmp_path
    ):
        """The legacy explicit runtime flag outranks manifest and discovery."""
        nested = tmp_path / "nested-project"
        nested.mkdir()
        mock_discover.return_value = ["codex"]
        mock_install_rt.return_value = True

        mock_ops = mock_ops_cls.return_value
        mock_ops.validate_servers_exist.return_value = (["test/server"], [])
        mock_ops.check_servers_needing_installation.return_value = ["test/server"]
        mock_ops.batch_fetch_server_info.return_value = {"test/server": {}}
        mock_ops.collect_environment_variables.return_value = {}
        mock_ops.collect_runtime_variables.return_value = {}

        MCPIntegrator.install(
            mcp_deps=["test/server"],
            runtime="cursor",
            project_root=nested,
            apm_config={"targets": ["claude"]},
        )

        assert {call.args[0] for call in mock_install_rt.call_args_list} == {"cursor"}
        mock_discover.assert_not_called()

    @patch("apm_cli.registry.operations.MCPServerOperations")
    @patch("apm_cli.integration.mcp_integrator.MCPIntegrator._install_for_runtime")
    @patch("apm_cli.runtime.manager.RuntimeManager")
    @patch("shutil.which", return_value=None)
    def test_exclude_applies_to_declared_targets(
        self, _which, mock_mgr_cls, mock_install_rt, mock_ops_cls, tmp_path
    ):
        """Regression: --exclude must still drop a runtime from a declared `targets:` list."""
        nested = tmp_path / "nested-project"
        nested.mkdir()

        mock_mgr = mock_mgr_cls.return_value
        mock_mgr.is_runtime_available.return_value = False
        mock_install_rt.return_value = True

        mock_ops = mock_ops_cls.return_value
        mock_ops.validate_servers_exist.return_value = (["test/server"], [])
        mock_ops.check_servers_needing_installation.return_value = ["test/server"]
        mock_ops.batch_fetch_server_info.return_value = {"test/server": {}}
        mock_ops.collect_environment_variables.return_value = {}
        mock_ops.collect_runtime_variables.return_value = {}
        logger = MagicMock()

        with patch("apm_cli.integration.mcp_integrator.Path.cwd", return_value=tmp_path):
            MCPIntegrator.install(
                mcp_deps=["test/server"],
                exclude="cursor",
                project_root=nested,
                apm_config={"targets": ["copilot", "cursor"]},
                logger=logger,
            )

        called_runtimes = {call.args[0] for call in mock_install_rt.call_args_list}
        assert called_runtimes == {"copilot"}
        logger.progress.assert_any_call("Targeting declared target from apm.yml: copilot")

    @pytest.mark.parametrize(
        "apm_config",
        (
            {"target": "claude", "targets": ["copilot"]},
            {"targets": []},
            {"targets": ["definitely-unknown"]},
        ),
        ids=("conflicting-fields", "empty-list", "unknown-target"),
    )
    @patch("apm_cli.integration.mcp_integrator_install._discover_installed_runtimes")
    @patch("apm_cli.integration.mcp_integrator.MCPIntegrator._install_for_runtime")
    def test_invalid_manifest_does_not_fall_through_to_machine_discovery(
        self,
        mock_install_rt,
        mock_discover,
        tmp_path,
        apm_config,
    ):
        """Malformed target declarations fail closed before machine discovery."""
        result = MCPIntegrator.install(
            mcp_deps=["test/server"],
            project_root=tmp_path,
            apm_config=apm_config,
        )

        assert result == 0
        mock_discover.assert_not_called()
        mock_install_rt.assert_not_called()

    @pytest.mark.parametrize(
        "load_error",
        (yaml.YAMLError("malformed"), OSError("permission denied")),
        ids=("malformed-yaml", "read-error"),
    )
    @patch("apm_cli.integration.mcp_integrator_install._discover_installed_runtimes")
    @patch("apm_cli.integration.mcp_integrator.MCPIntegrator._install_for_runtime")
    def test_manifest_load_failure_is_explicit_before_discovery_or_writes(
        self,
        mock_install_rt,
        mock_discover,
        tmp_path,
        load_error,
    ):
        """A present but unreadable manifest cannot become unrestricted discovery."""
        (tmp_path / "apm.yml").write_text("targets: [copilot]\n", encoding="utf-8")

        with (
            patch(
                "apm_cli.integration.mcp_integrator_install.load_yaml",
                side_effect=load_error,
            ),
            pytest.raises(RuntimeError, match="Unable to load MCP targets"),
        ):
            MCPIntegrator.install(
                mcp_deps=["test/server"],
                project_root=tmp_path,
                apm_config=None,
            )

        mock_discover.assert_not_called()
        mock_install_rt.assert_not_called()
        assert tuple(path.name for path in tmp_path.iterdir()) == ("apm.yml",)


# ===========================================================================
# MCPIntegrator.remove_stale - copilot
# ===========================================================================


class TestRemoveStaleCopilot:
    def _write_copilot_mcp(self, home: Path, servers: dict) -> Path:
        copilot_dir = home / ".copilot"
        copilot_dir.mkdir(parents=True, exist_ok=True)
        cfg = copilot_dir / "mcp-config.json"
        cfg.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
        return cfg

    def test_removes_stale_from_copilot(self, tmp_path):
        cfg = self._write_copilot_mcp(tmp_path, {"old": {}, "keep": {}})

        with (
            patch("apm_cli.integration.mcp_integrator.Path.cwd", return_value=tmp_path),
            patch("pathlib.Path.home", return_value=tmp_path),
        ):
            MCPIntegrator.remove_stale({"old"}, runtime="copilot")

        remaining = json.loads(cfg.read_text())
        assert "old" not in remaining["mcpServers"]
        assert "keep" in remaining["mcpServers"]


# ===========================================================================
# MCPIntegrator.collect_transitive - edge cases
# ===========================================================================


class TestCollectTransitive:
    def test_returns_empty_when_dir_missing(self, tmp_path):
        missing = tmp_path / "nonexistent"
        result = MCPIntegrator.collect_transitive(missing)
        assert result == []

    def test_returns_empty_when_dir_empty(self, tmp_path):
        result = MCPIntegrator.collect_transitive(tmp_path)
        assert result == []


# ===========================================================================
# _gate_project_scoped_runtimes -- issue #1335
# ===========================================================================


def _make_signal_dir(root: Path, target: str) -> None:
    """Create a directory marker that detect_signals() recognises.

    Lets us exercise the gate's third-priority signals path without
    mocking resolve_targets. Maps the canonical target name to the
    minimal on-disk artifact detect_signals scans for; mirrors the
    fixture-style setup in tests/integration/conftest.py:make_copilot_project
    rather than duplicating private detection rules in test code.
    """
    if target == "copilot":
        gh = root / ".github"
        gh.mkdir(parents=True, exist_ok=True)
        (gh / "copilot-instructions.md").write_text("placeholder\n")
    elif target == "claude":
        (root / ".claude").mkdir(parents=True, exist_ok=True)
        (root / "CLAUDE.md").write_text("placeholder\n")
    elif target == "codex":
        (root / ".codex").mkdir(parents=True, exist_ok=True)
    elif target == "cursor":
        (root / ".cursor").mkdir(parents=True, exist_ok=True)
    elif target == "gemini":
        (root / ".gemini").mkdir(parents=True, exist_ok=True)
    else:
        raise AssertionError(f"unknown target signal in test: {target}")


class TestGateProjectScopedRuntimes:
    """Tests for MCPIntegrator._gate_project_scoped_runtimes (issue #1335).

    Behavior model: the gate delegates to
    :func:`apm_cli.core.target_detection.resolve_targets`, which enforces
    the strict flag > yaml > signals chain (no permissive
    "fallback to copilot" greenfield default). Tests that exercise the
    signals path use real on-disk markers via :func:`_make_signal_dir`
    rather than mocking the resolver -- it keeps test failures honest if
    detect_signals' rules change.
    """

    _gate = staticmethod(MCPIntegrator._gate_project_scoped_runtimes)

    # -- user_scope: declared targets are respected -------------------------

    def test_user_scope_filters_by_declared_targets(self, tmp_path):
        result = self._gate(
            ["claude", "copilot", "vscode", "codex"],
            user_scope=True,
            project_root=tmp_path,
            apm_config={"targets": ["claude"]},
            explicit_target=None,
        )
        assert result == ["claude"]

    def test_user_scope_no_targets_passes_all_runtimes(self, tmp_path):
        result = self._gate(
            ["claude", "copilot", "vscode", "codex"],
            user_scope=True,
            project_root=tmp_path,
            apm_config={},
            explicit_target=None,
        )
        assert result == ["claude", "copilot", "vscode", "codex"]

    def test_user_scope_explicit_target_filters(self, tmp_path):
        result = self._gate(
            ["claude", "copilot", "vscode", "codex"],
            user_scope=True,
            project_root=tmp_path,
            apm_config={},
            explicit_target="copilot",
        )
        assert result == ["copilot"]

    def test_user_scope_explicit_target_overrides_config(self, tmp_path):
        result = self._gate(
            ["claude", "copilot", "vscode", "codex"],
            user_scope=True,
            project_root=tmp_path,
            apm_config={"targets": ["claude"]},
            explicit_target="copilot",
        )
        assert result == ["copilot"]

    def test_user_scope_apm_config_none_passes_all_runtimes(self, tmp_path):
        result = self._gate(
            ["claude", "copilot", "vscode", "codex"],
            user_scope=True,
            project_root=tmp_path,
            apm_config=None,
            explicit_target=None,
        )
        assert result == ["claude", "copilot", "vscode", "codex"]

    def test_user_scope_malformed_targets_fails_closed(self, tmp_path):
        result = self._gate(
            ["claude", "copilot"],
            user_scope=True,
            project_root=tmp_path,
            apm_config={"target": "claude", "targets": ["copilot"]},
            explicit_target=None,
        )
        assert result == []

    def test_user_scope_explicit_target_all_passes_all_runtimes(self, tmp_path):
        result = self._gate(
            ["claude", "copilot", "vscode", "codex"],
            user_scope=True,
            project_root=tmp_path,
            apm_config={"targets": ["claude"]},
            explicit_target="all",
        )
        assert result == ["claude", "copilot", "vscode", "codex"]

    def test_user_scope_explicit_target_all_in_list_passes_all_runtimes(self, tmp_path):
        result = self._gate(
            ["claude", "copilot", "vscode"],
            user_scope=True,
            project_root=tmp_path,
            apm_config={},
            explicit_target=["all"],
        )
        assert result == ["claude", "copilot", "vscode"]

    # -- explicit targets: (plural) gates all runtimes ---------------------

    def test_targets_plural_filters_unlisted_runtimes(self, tmp_path):
        result = self._gate(
            ["claude", "copilot", "vscode", "codex"],
            user_scope=False,
            project_root=tmp_path,
            apm_config={"targets": ["claude"]},
            explicit_target=None,
        )
        assert result == ["claude"]

    def test_target_singular_filters_unlisted_runtimes(self, tmp_path):
        result = self._gate(
            ["claude", "copilot", "vscode"],
            user_scope=False,
            project_root=tmp_path,
            apm_config={"target": "claude"},
            explicit_target=None,
        )
        assert result == ["claude"]

    def test_targets_multiple_values_keeps_all_listed(self, tmp_path):
        # `vscode` is no longer an MCP runtime (#3866): copilot owns the
        # project destination, so a stray `vscode` runtime never passes.
        result = self._gate(
            ["claude", "copilot", "vscode", "codex", "cursor"],
            user_scope=False,
            project_root=tmp_path,
            apm_config={"targets": ["claude", "copilot"]},
            explicit_target=None,
        )
        assert result == ["claude", "copilot"]

    # -- no targets field: directory-detection acts as the whitelist ------

    def test_no_targets_uses_directory_detection_for_all_runtimes(self, tmp_path):
        # Single signal: .github/copilot-instructions.md -> copilot only.
        # Every other runtime gates, mirroring `apm install`.
        _make_signal_dir(tmp_path, "copilot")
        result = self._gate(
            ["copilot", "vscode", "codex", "claude", "cursor"],
            user_scope=False,
            project_root=tmp_path,
            apm_config={},
            explicit_target=None,
        )
        assert result == ["copilot"]

    def test_no_targets_with_ambiguous_signals_fails_closed(self, tmp_path):
        # >=2 signals + no flag + no targets: -> AmbiguousHarnessError.
        # Strict v2 contract: gate writes nothing, surfaces red [x].
        _make_signal_dir(tmp_path, "copilot")
        _make_signal_dir(tmp_path, "claude")
        result = self._gate(
            ["copilot", "claude"],
            user_scope=False,
            project_root=tmp_path,
            apm_config={},
            explicit_target=None,
        )
        assert result == []

    # -- explicit_target CLI flag overrides config -------------------------

    def test_explicit_target_overrides_config(self, tmp_path):
        # The deprecated vscode flag selects copilot; only the copilot
        # runtime writes (#3866).
        result = self._gate(
            ["claude", "copilot", "vscode", "codex"],
            user_scope=False,
            project_root=tmp_path,
            apm_config={"targets": ["claude", "copilot"]},
            explicit_target="vscode",
        )
        assert result == ["copilot"]

    def test_explicit_target_without_config(self, tmp_path):
        result = self._gate(
            ["claude", "copilot", "cursor", "codex"],
            user_scope=False,
            project_root=tmp_path,
            apm_config={},
            explicit_target="cursor",
        )
        assert result == ["cursor"]

    # -- edge cases --------------------------------------------------------

    def test_empty_target_runtimes_returns_empty(self, tmp_path):
        result = self._gate(
            [],
            user_scope=False,
            project_root=tmp_path,
            apm_config={"targets": ["claude"]},
            explicit_target=None,
        )
        assert result == []

    def test_apm_config_none_with_signal_falls_through_to_auto_detect(self, tmp_path):
        _make_signal_dir(tmp_path, "copilot")
        result = self._gate(
            ["copilot", "codex"],
            user_scope=False,
            project_root=tmp_path,
            apm_config=None,
            explicit_target=None,
        )
        assert "copilot" in result
        assert "codex" not in result

    # -- strict-mode greenfield: no flag/yaml/signal -> NoHarnessError ----

    def test_no_signal_no_targets_no_flag_fails_closed(self, tmp_path, capsys):
        """Greenfield (no flag, no targets:, no harness dir) writes nothing.

        Closes the asymmetry the panel surfaced: canonical `apm install`
        raises NoHarnessError on this state (install/phases/targets.py),
        but the MCP gate previously fell back to permissive [copilot]
        via active_targets -- the same class of silent-write bug as
        #1335, just gated by greenfield rather than explicit-target
        mismatch.
        """
        result = self._gate(
            ["copilot", "claude", "codex", "cursor"],
            user_scope=False,
            project_root=tmp_path,
            apm_config={},
            explicit_target=None,
        )
        assert result == []
        out = capsys.readouterr().out
        # Canonical lead-with-outcome voice + structured error body.
        assert "Skipping all MCP config writes" in out
        assert "could not resolve active targets" in out

    # -- malformed targets field: fail-closed (issue #1335 follow-up) ------

    def test_conflicting_targets_field_fails_closed(self, tmp_path):
        # Both `target` and `targets` set -> ConflictingTargetsError.
        result = self._gate(
            ["claude", "copilot", "vscode", "codex"],
            user_scope=False,
            project_root=tmp_path,
            apm_config={"target": "claude", "targets": ["copilot"]},
            explicit_target=None,
        )
        assert result == []

    def test_empty_targets_list_fails_closed(self, tmp_path):
        # `targets: []` -> EmptyTargetsListError.
        result = self._gate(
            ["claude", "copilot", "codex"],
            user_scope=False,
            project_root=tmp_path,
            apm_config={"targets": []},
            explicit_target=None,
        )
        assert result == []

    def test_unknown_target_in_yaml_fails_closed(self, tmp_path, capsys):
        """Non-canonical token in `targets:` -> UnknownTargetError.

        scsec R2: the previous catch only handled ConflictingTargets and
        EmptyTargets; UnknownTargetError leaked uncaught past the gate
        on entry paths that bypass the upstream manifest validator
        (mcp_integrator_install.py, _wire_legacy_bundle_mcp_servers).
        """
        result = self._gate(
            ["copilot", "claude"],
            user_scope=False,
            project_root=tmp_path,
            apm_config={"targets": ["copilot", "bogus"]},
            explicit_target=None,
        )
        assert result == []
        out = capsys.readouterr().out
        assert "Skipping all MCP config writes" in out
        assert "apm.yml 'targets' field is invalid" in out

    def test_explicit_target_csv_string_normalized(self, tmp_path):
        """Legacy `_wire_legacy_bundle_mcp_servers` CSV input must normalize first.

        The canonical-name validator inside resolve_targets would reject
        the whole CSV "claude,copilot" as one unknown token -- the gate
        normalizes to a list before the resolver sees it.
        """
        result = self._gate(
            ["claude", "copilot", "codex"],
            user_scope=False,
            project_root=tmp_path,
            apm_config=None,
            explicit_target="claude,copilot",
        )
        assert "claude" in result
        assert "copilot" in result
        assert "codex" not in result

    # -- call-site forwarding regression (PR #1336 audit, devx-ux B1) ------

    def test_apm_package_targets_plural_forwards_through_call_site(self, tmp_path):
        """Regression: `targets:` plural in apm.yml must reach the gate.

        Before the audit fix, ``commands/install.py`` built
        ``mcp_apm_config = {"target": apm_package.target, ...}`` -- which is
        always None for users on the modern ``targets:`` form. The gate then
        saw an empty config dict and fell back to permissive directory
        detection, silently bypassing the whitelist the user explicitly set.

        This test exercises the full call-site path: load real apm.yml ->
        APMPackage -> build mcp_apm_config the way commands/install.py does
        -> assert the gate sees the plural list.
        """
        from apm_cli.core.apm_yml import parse_targets_field
        from apm_cli.models.apm_package import APMPackage

        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text(
            "name: gate-regression\nversion: 0.0.1\ntargets:\n  - copilot\n  - claude\n"
        )
        pkg = APMPackage.from_apm_yml(apm_yml)
        assert pkg.targets == ["copilot", "claude"], (
            "APMPackage must expose 'targets:' plural; otherwise the call site "
            "cannot forward it to the gate."
        )

        # Mirror the exact dict construction in commands/install.py:1795.
        mcp_apm_config: dict = {"scripts": pkg.scripts or {}}
        if pkg.targets is not None:
            mcp_apm_config["targets"] = pkg.targets
        elif pkg.target is not None:
            mcp_apm_config["target"] = pkg.target

        # The gate's parse_targets_field must now see the plural list.
        assert parse_targets_field(mcp_apm_config) == ["copilot", "claude"]

    def test_dropped_runtime_message_includes_active_targets(self, tmp_path, capsys):
        """Negative-case message must name the active set (cli-logging B3).

        Without "(active targets: ...)" the user has to grep apm.yml to
        confirm what the gate did. Mirrors the canonical provenance line
        shape ``Targets: X  (source: Y)`` -- including the double-space
        separator before the parenthetical (tc N1 contract lock).
        """
        import re

        self._gate(
            ["copilot", "claude", "codex"],
            user_scope=False,
            project_root=tmp_path,
            apm_config={"targets": ["copilot"]},
            explicit_target=None,
        )
        out = capsys.readouterr().out
        first_line = next(line for line in out.splitlines() if line.strip())
        assert first_line.lstrip().startswith("[i] Skipped MCP config")
        assert "Skipped MCP config for claude, codex" in out
        assert "active targets: copilot" in out
        # Symbol prefix asserts the gate honors the [+]/[!]/[i]/[x] contract.
        assert "[i]" in out
        # Lock the double-space provenance separator that mirrors
        # canonical `Targets: X  (source: Y)`. Without this, a future
        # contributor collapsing to a single space would silently break
        # provenance-line parity with the install phase.
        assert re.search(r"Skipped MCP config for [^(]+  \(active targets:", out)
