"""Unit tests for ``apm_cli.integration.lsp_integrator.LSPIntegrator``.

Tests focus on pure-logic methods: deduplication, name extraction,
config building, stale cleanup, lockfile update, and the install
orchestrator -- without requiring live network calls or runtimes.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from apm_cli.integration.lsp_integrator import LSPIntegrator
from apm_cli.models.dependency.lsp import LSPDependency

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_dep(name: str, **kwargs) -> LSPDependency:
    """Convenience factory for LSPDependency."""
    defaults = {
        "command": kwargs.pop("command", f"{name}-langserver"),
        "extension_to_language": kwargs.pop("extension_to_language", {".py": "python"}),
    }
    defaults.update(kwargs)
    return LSPDependency(name=name, **defaults)


# ===========================================================================
# LSPIntegrator.deduplicate
# ===========================================================================


class TestDeduplicate:
    def test_empty_list(self):
        assert LSPIntegrator.deduplicate([]) == []

    def test_no_duplicates(self):
        deps = [_make_dep("a"), _make_dep("b"), _make_dep("c")]
        result = LSPIntegrator.deduplicate(deps)
        assert [d.name for d in result] == ["a", "b", "c"]

    def test_first_occurrence_wins(self):
        dep1 = _make_dep("pyright", transport="stdio")
        dep2 = _make_dep("pyright", transport="socket")
        result = LSPIntegrator.deduplicate([dep1, dep2])
        assert len(result) == 1
        assert result[0].transport == "stdio"

    def test_dict_entries_dedup_by_name(self):
        deps = [{"name": "foo"}, {"name": "foo"}, {"name": "bar"}]
        result = LSPIntegrator.deduplicate(deps)
        assert len(result) == 2
        assert result[0]["name"] == "foo"
        assert result[1]["name"] == "bar"

    def test_nameless_items_kept_by_value_inequality(self):
        dep1 = {"other": "x"}
        dep2 = {"other": "y"}
        result = LSPIntegrator.deduplicate([dep1, dep2])
        assert len(result) == 2

    def test_nameless_duplicate_reference_skipped(self):
        dep = {"other": "x"}
        result = LSPIntegrator.deduplicate([dep, dep])
        assert len(result) == 1

    def test_mixed_string_and_object(self):
        deps = ["alpha", _make_dep("beta"), "alpha"]
        result = LSPIntegrator.deduplicate(deps)
        assert len(result) == 2

    def test_preserves_order(self):
        names = ["z", "a", "m", "b"]
        deps = [_make_dep(n) for n in names]
        result = LSPIntegrator.deduplicate(deps)
        assert [d.name for d in result] == names


# ===========================================================================
# LSPIntegrator.get_server_names
# ===========================================================================


class TestGetServerNames:
    def test_empty(self):
        assert LSPIntegrator.get_server_names([]) == set()

    def test_dep_objects(self):
        deps = [_make_dep("pyright"), _make_dep("ruff-lsp")]
        assert LSPIntegrator.get_server_names(deps) == {"pyright", "ruff-lsp"}

    def test_plain_strings(self):
        assert LSPIntegrator.get_server_names(["foo", "bar"]) == {"foo", "bar"}

    def test_deduplication(self):
        deps = [_make_dep("x"), _make_dep("x"), "x"]
        assert LSPIntegrator.get_server_names(deps) == {"x"}


# ===========================================================================
# LSPIntegrator.get_server_configs
# ===========================================================================


class TestGetServerConfigs:
    def test_empty(self):
        assert LSPIntegrator.get_server_configs([]) == {}

    def test_dep_object_serialised(self):
        dep = _make_dep("pyright", transport="stdio")
        configs = LSPIntegrator.get_server_configs([dep])
        assert "pyright" in configs
        assert configs["pyright"]["name"] == "pyright"
        assert configs["pyright"]["transport"] == "stdio"

    def test_plain_string_fallback(self):
        configs = LSPIntegrator.get_server_configs(["plain-server"])
        assert configs == {"plain-server": {"name": "plain-server"}}

    def test_multiple_deps(self):
        deps = [_make_dep("a"), _make_dep("b")]
        configs = LSPIntegrator.get_server_configs(deps)
        assert set(configs.keys()) == {"a", "b"}


# ===========================================================================
# LSPIntegrator.install -- project scope
# ===========================================================================


class TestInstallProjectScope:
    def test_empty_deps_returns_zero(self, tmp_path):
        count = LSPIntegrator.install([], project_root=tmp_path)
        assert count == 0

    def test_creates_lsp_json(self, tmp_path):
        deps = [_make_dep("pyright")]
        count = LSPIntegrator.install(deps, project_root=tmp_path)
        assert count == 1

        lsp_json = tmp_path / ".lsp.json"
        assert lsp_json.exists()
        data = json.loads(lsp_json.read_text())
        assert "pyright" in data
        assert data["pyright"]["command"] == "pyright-langserver"
        assert "name" not in data["pyright"]  # name is the key, not in value

    def test_merges_with_existing_lsp_json(self, tmp_path):
        lsp_json = tmp_path / ".lsp.json"
        lsp_json.write_text(json.dumps({"existing-server": {"command": "x"}}))

        deps = [_make_dep("new-server")]
        LSPIntegrator.install(deps, project_root=tmp_path)

        data = json.loads(lsp_json.read_text())
        assert "existing-server" in data
        assert "new-server" in data

    def test_update_existing_server_counts_as_change(self, tmp_path):
        lsp_json = tmp_path / ".lsp.json"
        lsp_json.write_text(json.dumps({"pyright": {"command": "old-cmd"}}))

        deps = [_make_dep("pyright")]
        count = LSPIntegrator.install(deps, project_root=tmp_path)
        assert count == 1  # changed config counts

    def test_no_change_returns_zero(self, tmp_path):
        dep = _make_dep("pyright")
        # First install
        LSPIntegrator.install([dep], project_root=tmp_path)
        # Second install with same config
        count = LSPIntegrator.install([dep], project_root=tmp_path)
        assert count == 0

    def test_dict_deps_handled(self, tmp_path):
        deps = [{"name": "dict-server", "command": "x", "extensionToLanguage": {".py": "python"}}]
        count = LSPIntegrator.install(deps, project_root=tmp_path)
        assert count == 1
        data = json.loads((tmp_path / ".lsp.json").read_text())
        assert "dict-server" in data

    def test_multiple_servers(self, tmp_path):
        deps = [_make_dep("pyright"), _make_dep("ruff-lsp")]
        count = LSPIntegrator.install(deps, project_root=tmp_path)
        assert count == 2


# ===========================================================================
# LSPIntegrator.install -- user scope
# ===========================================================================


class TestInstallUserScope:
    def test_writes_to_claude_json(self, tmp_path):
        claude_json = tmp_path / ".claude.json"
        with patch("apm_cli.integration.lsp_integrator.Path.home", return_value=tmp_path):
            deps = [_make_dep("pyright")]
            count = LSPIntegrator.install(deps, user_scope=True)
            assert count == 1

        data = json.loads(claude_json.read_text())
        assert "lspServers" in data
        assert "pyright" in data["lspServers"]

    def test_merges_with_existing_claude_json(self, tmp_path):
        claude_json = tmp_path / ".claude.json"
        claude_json.write_text(json.dumps({"existingKey": True}))

        with patch("apm_cli.integration.lsp_integrator.Path.home", return_value=tmp_path):
            LSPIntegrator.install([_make_dep("ruff")], user_scope=True)

        data = json.loads(claude_json.read_text())
        assert data["existingKey"] is True
        assert "ruff" in data["lspServers"]


class TestInstallCopilotTarget:
    def test_writes_project_lsp_json_with_file_extensions(self, tmp_path):
        deps = [_make_dep("pyright")]

        count = LSPIntegrator.install(
            deps,
            project_root=tmp_path,
            target_runtimes=["copilot"],
        )

        assert count == 1
        config_path = tmp_path / ".github" / "lsp.json"
        assert config_path.exists()
        data = json.loads(config_path.read_text())
        assert data == {
            "lspServers": {
                "pyright": {
                    "command": "pyright-langserver",
                    "args": [],
                    "fileExtensions": {".py": "python"},
                }
            }
        }

    def test_writes_user_lsp_config_with_file_extensions(self, tmp_path):
        deps = [_make_dep("pyright")]

        with patch("apm_cli.integration.lsp_integrator.Path.home", return_value=tmp_path):
            count = LSPIntegrator.install(
                deps,
                user_scope=True,
                target_runtimes=["copilot"],
            )

        assert count == 1
        config_path = tmp_path / ".copilot" / "lsp-config.json"
        assert config_path.exists()
        data = json.loads(config_path.read_text())
        assert data == {
            "lspServers": {
                "pyright": {
                    "command": "pyright-langserver",
                    "args": [],
                    "fileExtensions": {".py": "python"},
                }
            }
        }


class TestResolveLspTargets:
    def test_targets_copilot_when_binary_present(self, tmp_path):
        with patch(
            "apm_cli.integration.lsp_integrator.find_runtime_binary",
            side_effect=lambda name: f"/bin/{name}" if name == "copilot" else None,
        ):
            targets = LSPIntegrator.resolve_target_runtimes(
                project_root=tmp_path,
                user_scope=True,
            )

        assert targets == ["copilot"]


# ===========================================================================
# LSPIntegrator.remove_stale
# ===========================================================================


class TestRemoveStale:
    def test_empty_stale_set_is_noop(self, tmp_path):
        lsp_json = tmp_path / ".lsp.json"
        lsp_json.write_text(json.dumps({"keep": {"command": "x"}}))

        LSPIntegrator.remove_stale(set(), project_root=tmp_path)
        data = json.loads(lsp_json.read_text())
        assert "keep" in data

    def test_removes_stale_from_project_lsp_json(self, tmp_path):
        lsp_json = tmp_path / ".lsp.json"
        lsp_json.write_text(
            json.dumps(
                {
                    "keep": {"command": "x"},
                    "stale": {"command": "y"},
                }
            )
        )

        LSPIntegrator.remove_stale({"stale"}, project_root=tmp_path)
        data = json.loads(lsp_json.read_text())
        assert "keep" in data
        assert "stale" not in data

    def test_removes_stale_from_user_claude_json(self, tmp_path):
        claude_json = tmp_path / ".claude.json"
        claude_json.write_text(
            json.dumps(
                {
                    "lspServers": {
                        "keep": {"command": "x"},
                        "stale": {"command": "y"},
                    }
                }
            )
        )

        with patch("apm_cli.integration.lsp_integrator.Path.home", return_value=tmp_path):
            LSPIntegrator.remove_stale({"stale"}, user_scope=True)

        data = json.loads(claude_json.read_text())
        assert "keep" in data["lspServers"]
        assert "stale" not in data["lspServers"]

    def test_no_lsp_json_is_noop(self, tmp_path):
        # Should not raise even if .lsp.json does not exist
        LSPIntegrator.remove_stale({"nonexistent"}, project_root=tmp_path)

    def test_multiple_stale_removed(self, tmp_path):
        lsp_json = tmp_path / ".lsp.json"
        lsp_json.write_text(
            json.dumps(
                {
                    "keep": {"command": "x"},
                    "stale1": {"command": "y"},
                    "stale2": {"command": "z"},
                }
            )
        )

        LSPIntegrator.remove_stale({"stale1", "stale2"}, project_root=tmp_path)
        data = json.loads(lsp_json.read_text())
        assert set(data.keys()) == {"keep"}


# ===========================================================================
# LSPIntegrator.update_lockfile
# ===========================================================================


class TestUpdateLockfile:
    def _write_lockfile(self, path: Path) -> None:
        """Write a minimal valid lockfile."""
        from apm_cli.deps.lockfile import LockFile

        lock = LockFile()
        lock.save(path)

    def test_persists_lsp_servers(self, tmp_path):
        lock_path = tmp_path / "apm.lock.yaml"
        self._write_lockfile(lock_path)

        LSPIntegrator.update_lockfile({"pyright", "ruff-lsp"}, lock_path)

        from apm_cli.deps.lockfile import LockFile

        lock = LockFile.read(lock_path)
        assert lock is not None
        assert set(lock.lsp_servers) == {"pyright", "ruff-lsp"}

    def test_persists_lsp_configs(self, tmp_path):
        lock_path = tmp_path / "apm.lock.yaml"
        self._write_lockfile(lock_path)

        configs = {"pyright": {"name": "pyright", "command": "pyright-langserver"}}
        LSPIntegrator.update_lockfile({"pyright"}, lock_path, lsp_configs=configs)

        from apm_cli.deps.lockfile import LockFile

        lock = LockFile.read(lock_path)
        assert lock is not None
        assert lock.lsp_configs == configs

    def test_clears_servers_on_empty_set(self, tmp_path):
        lock_path = tmp_path / "apm.lock.yaml"
        self._write_lockfile(lock_path)

        LSPIntegrator.update_lockfile({"pyright"}, lock_path)
        LSPIntegrator.update_lockfile(set(), lock_path, lsp_configs={})

        from apm_cli.deps.lockfile import LockFile

        lock = LockFile.read(lock_path)
        assert lock is not None
        assert lock.lsp_servers == []

    def test_no_lockfile_is_noop(self, tmp_path):
        lock_path = tmp_path / "apm.lock.yaml"
        # Should not raise if lockfile does not exist
        LSPIntegrator.update_lockfile({"pyright"}, lock_path)
        assert not lock_path.exists()


# ===========================================================================
# LSPIntegrator.collect_transitive
# ===========================================================================


class TestCollectTransitive:
    def test_empty_modules_dir(self, tmp_path):
        result = LSPIntegrator.collect_transitive(tmp_path / "nonexistent")
        assert result == []

    def test_collects_from_apm_yml(self, tmp_path):
        """Set up a mock package with LSP deps and verify collection."""
        pkg_dir = tmp_path / "apm_modules" / "owner/repo"
        pkg_dir.mkdir(parents=True)

        apm_yml = pkg_dir / "apm.yml"
        apm_yml.write_text(
            "name: test-pkg\n"
            "version: 1.0.0\n"
            "dependencies:\n"
            "  lsp:\n"
            "    - name: pyright\n"
            "      command: pyright-langserver\n"
            "      extensionToLanguage:\n"
            "        .py: python\n"
        )

        # Create lockfile referencing this package
        lock_path = tmp_path / "apm.lock.yaml"
        from apm_cli.deps.lockfile import LockedDependency, LockFile

        lock = LockFile()
        dep = LockedDependency(
            repo_url="owner/repo",
            version="1.0.0",
        )
        lock.add_dependency(dep)
        lock.save(lock_path)

        modules_dir = tmp_path / "apm_modules"
        result = LSPIntegrator.collect_transitive(modules_dir, lock_path)
        assert len(result) == 1
        assert result[0].name == "pyright"
