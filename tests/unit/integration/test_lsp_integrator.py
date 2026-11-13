"""Unit tests for ``apm_cli.integration.lsp_integrator.LSPIntegrator``.

Tests focus on pure-logic methods: deduplication, name extraction,
config building, stale cleanup, lockfile update, and the install
orchestrator -- without requiring live network calls or runtimes.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.integration.lsp_integrator import LSPIntegrator
from apm_cli.models.dependency.lsp import LSPDependency

_CLAUDE_PROJECT_PLUGIN = Path(".claude") / "skills" / "apm-lsp" / ".claude-plugin" / "plugin.json"

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

    def test_creates_claude_plugin_manifest(self, tmp_path):
        deps = [_make_dep("pyright")]
        count = LSPIntegrator.install(deps, project_root=tmp_path)
        assert count == 1

        plugin_json = tmp_path / _CLAUDE_PROJECT_PLUGIN
        assert plugin_json.exists()
        data = json.loads(plugin_json.read_text())
        assert data["name"] == "apm-lsp"
        assert data["lspServers"]["pyright"]["command"] == "pyright-langserver"
        assert "name" not in data["lspServers"]["pyright"]

    def test_claude_plugin_install_prompts_runtime_reload(self, tmp_path):
        logger = MagicMock()

        LSPIntegrator.install([_make_dep("pyright")], project_root=tmp_path, logger=logger)

        logger.progress.assert_any_call(
            "  |-- run /reload-plugins or restart Claude Code to activate"
        )

    def test_refuses_existing_foreign_claude_plugin_manifest(self, tmp_path):
        plugin_json = tmp_path / _CLAUDE_PROJECT_PLUGIN
        plugin_json.parent.mkdir(parents=True)
        plugin_json.write_text(
            json.dumps(
                {
                    "name": "custom-lsp-plugin",
                    "description": "Preserve this metadata",
                    "lspServers": {"existing-server": {"command": "x"}},
                }
            )
        )

        deps = [_make_dep("new-server")]
        from apm_cli.install.errors import RequiredIntegrationError

        with pytest.raises(RequiredIntegrationError, match="not APM"):
            LSPIntegrator.install(
                deps,
                project_root=tmp_path,
                fail_on_write_error=True,
            )

        assert json.loads(plugin_json.read_text())["name"] == "custom-lsp-plugin"

    def test_refuses_non_object_plugin_without_overwriting(self, tmp_path):
        plugin_json = tmp_path / _CLAUDE_PROJECT_PLUGIN
        plugin_json.parent.mkdir(parents=True)
        original = b"[]\n"
        plugin_json.write_bytes(original)
        from apm_cli.install.errors import RequiredIntegrationError

        with pytest.raises(RequiredIntegrationError, match="must contain a JSON object"):
            LSPIntegrator.install(
                [_make_dep("pyright")],
                project_root=tmp_path,
                fail_on_write_error=True,
            )

        assert plugin_json.read_bytes() == original

    def test_refuses_unnamed_plugin_without_adopting_it(self, tmp_path):
        plugin_json = tmp_path / _CLAUDE_PROJECT_PLUGIN
        plugin_json.parent.mkdir(parents=True)
        original = b'{"lspServers":{"user-owned":{"command":"keep"}}}\n'
        plugin_json.write_bytes(original)
        from apm_cli.install.errors import RequiredIntegrationError

        with pytest.raises(RequiredIntegrationError, match="unnamed"):
            LSPIntegrator.install(
                [_make_dep("pyright")],
                project_root=tmp_path,
                fail_on_write_error=True,
            )

        assert plugin_json.read_bytes() == original

    def test_force_merges_with_existing_foreign_claude_plugin_manifest(self, tmp_path):
        plugin_json = tmp_path / _CLAUDE_PROJECT_PLUGIN
        plugin_json.parent.mkdir(parents=True)
        plugin_json.write_text(
            json.dumps(
                {
                    "name": "custom-lsp-plugin",
                    "description": "Preserve this metadata",
                    "lspServers": {"existing-server": {"command": "x"}},
                }
            )
        )

        LSPIntegrator.install([_make_dep("new-server")], project_root=tmp_path, force=True)

        data = json.loads(plugin_json.read_text())
        assert data["name"] == "apm-lsp"
        assert data["description"] == "Preserve this metadata"
        assert set(data["lspServers"]) == {"existing-server", "new-server"}
        assert (
            LSPIntegrator.install(
                [_make_dep("new-server")],
                project_root=tmp_path,
                managed_server_names={"new-server"},
                fail_on_write_error=True,
            )
            == 0
        )
        LSPIntegrator.remove_stale(
            {"new-server"},
            project_root=tmp_path,
            fail_on_write_error=True,
        )
        assert set(json.loads(plugin_json.read_text())["lspServers"]) == {"existing-server"}

    def test_multitarget_collision_is_validated_before_any_write(self, tmp_path):
        from apm_cli.install.errors import RequiredIntegrationError

        plugin_json = tmp_path / _CLAUDE_PROJECT_PLUGIN
        plugin_json.parent.mkdir(parents=True)
        plugin_json.write_text(json.dumps({"name": "foreign-plugin", "lspServers": {}}))

        with pytest.raises(RequiredIntegrationError, match="target 'claude'"):
            LSPIntegrator.install(
                [_make_dep("new-server")],
                project_root=tmp_path,
                target_runtimes=["copilot", "claude"],
                fail_on_write_error=True,
            )

        assert not (tmp_path / ".github" / "lsp.json").exists()
        assert json.loads(plugin_json.read_text())["name"] == "foreign-plugin"

    def test_update_existing_server_counts_as_change(self, tmp_path):
        plugin_json = tmp_path / _CLAUDE_PROJECT_PLUGIN
        plugin_json.parent.mkdir(parents=True)
        plugin_json.write_text(
            json.dumps(
                {
                    "name": "apm-lsp",
                    "lspServers": {"pyright": {"command": "old-cmd"}},
                }
            )
        )

        deps = [_make_dep("pyright")]
        count = LSPIntegrator.install(
            deps,
            project_root=tmp_path,
            managed_server_names={"pyright"},
        )
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
        data = json.loads((tmp_path / _CLAUDE_PROJECT_PLUGIN).read_text())
        assert "dict-server" in data["lspServers"]

    def test_multiple_servers(self, tmp_path):
        deps = [_make_dep("pyright"), _make_dep("ruff-lsp")]
        count = LSPIntegrator.install(deps, project_root=tmp_path)
        assert count == 2

    def test_rejects_symlink_escape_before_writing_claude_plugin(self, tmp_path):
        project = tmp_path / "project"
        outside = tmp_path / "outside"
        project.mkdir()
        outside.mkdir()
        try:
            (project / ".claude").symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"directory symlinks unavailable: {exc}")

        from apm_cli.install.errors import RequiredIntegrationError

        with pytest.raises(RequiredIntegrationError, match="symlinked path component"):
            LSPIntegrator.install(
                [_make_dep("pyright")],
                project_root=project,
                fail_on_write_error=True,
            )

        assert not (outside / "skills" / "apm-lsp" / ".claude-plugin" / "plugin.json").exists()

    def test_refuses_foreign_server_collision_without_force(self, tmp_path):
        plugin_json = tmp_path / _CLAUDE_PROJECT_PLUGIN
        plugin_json.parent.mkdir(parents=True)
        plugin_json.write_text(
            json.dumps(
                {
                    "name": "apm-lsp",
                    "lspServers": {"pyright": {"command": "user-command"}},
                }
            )
        )
        from apm_cli.install.errors import RequiredIntegrationError

        with pytest.raises(RequiredIntegrationError, match="not managed by APM"):
            LSPIntegrator.install(
                [_make_dep("pyright")],
                project_root=tmp_path,
                fail_on_write_error=True,
            )

        assert json.loads(plugin_json.read_text())["lspServers"]["pyright"] == {
            "command": "user-command"
        }

    def test_other_target_ownership_does_not_authorize_claude_overwrite(self, tmp_path):
        plugin_json = tmp_path / _CLAUDE_PROJECT_PLUGIN
        plugin_json.parent.mkdir(parents=True)
        plugin_json.write_text(
            json.dumps(
                {
                    "name": "apm-lsp",
                    "lspServers": {"pyright": {"command": "user-command"}},
                }
            )
        )
        from apm_cli.install.errors import RequiredIntegrationError

        with pytest.raises(RequiredIntegrationError, match="not managed by APM"):
            LSPIntegrator.install(
                [_make_dep("pyright")],
                project_root=tmp_path,
                target_runtimes=["claude"],
                managed_target_servers={"copilot": {"pyright"}},
                fail_on_write_error=True,
            )

        assert json.loads(plugin_json.read_text())["lspServers"]["pyright"] == {
            "command": "user-command"
        }

    def test_managed_server_does_not_adopt_foreign_skill_content(self, tmp_path):
        plugin_json = tmp_path / _CLAUDE_PROJECT_PLUGIN
        plugin_json.parent.mkdir(parents=True)
        plugin_json.write_text(
            json.dumps(
                {
                    "name": "apm-lsp",
                    "lspServers": {"pyright": {"command": "old-command"}},
                }
            )
        )
        skill_root = plugin_json.parent.parent
        (skill_root / "SKILL.md").write_text("# user-owned\n", encoding="utf-8")
        from apm_cli.install.errors import RequiredIntegrationError

        with pytest.raises(RequiredIntegrationError, match="unowned content"):
            LSPIntegrator.install(
                [_make_dep("pyright")],
                project_root=tmp_path,
                managed_server_names={"pyright"},
                fail_on_write_error=True,
            )

        assert (skill_root / "SKILL.md").is_file()


# ===========================================================================
# LSPIntegrator.install -- user scope
# ===========================================================================


class TestInstallUserScope:
    def test_writes_to_personal_claude_plugin_manifest(self, tmp_path):
        plugin_json = tmp_path / _CLAUDE_PROJECT_PLUGIN
        with patch("apm_cli.integration.lsp_integrator.Path.home", return_value=tmp_path):
            deps = [_make_dep("pyright")]
            count = LSPIntegrator.install(deps, user_scope=True)
            assert count == 1

        assert not (tmp_path / ".claude.json").exists()
        data = json.loads(plugin_json.read_text())
        assert data["name"] == "apm-lsp"
        assert "pyright" in data["lspServers"]
        assert "enabledPlugins" not in data

    def test_user_scope_reinstall_is_idempotent(self, tmp_path):
        plugin_json = tmp_path / _CLAUDE_PROJECT_PLUGIN
        dep = _make_dep("pyright")

        with patch("apm_cli.integration.lsp_integrator.Path.home", return_value=tmp_path):
            assert LSPIntegrator.install([dep], user_scope=True) == 1
            before = plugin_json.read_text(encoding="utf-8")
            assert LSPIntegrator.install([dep], user_scope=True) == 0

        assert plugin_json.read_text(encoding="utf-8") == before

    def test_merges_with_existing_personal_claude_plugin_manifest(self, tmp_path):
        plugin_json = tmp_path / _CLAUDE_PROJECT_PLUGIN
        plugin_json.parent.mkdir(parents=True)
        plugin_json.write_text(json.dumps({"name": "apm-lsp", "description": "keep"}))

        with patch("apm_cli.integration.lsp_integrator.Path.home", return_value=tmp_path):
            LSPIntegrator.install([_make_dep("ruff")], user_scope=True)

        data = json.loads(plugin_json.read_text())
        assert data["name"] == "apm-lsp"
        assert data["description"] == "keep"
        assert "ruff" in data["lspServers"]

    def test_rejects_symlinked_user_plugin_path_before_writing(self, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        claude_dir = tmp_path / ".claude"
        try:
            claude_dir.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"directory symlinks unavailable: {exc}")
        from apm_cli.install.errors import RequiredIntegrationError

        with (
            patch("apm_cli.integration.lsp_integrator.Path.home", return_value=tmp_path),
            pytest.raises(RequiredIntegrationError, match="symlinked path component"),
        ):
            LSPIntegrator.install(
                [_make_dep("pyright")],
                user_scope=True,
                fail_on_write_error=True,
            )

        assert not (outside / "skills" / "apm-lsp" / ".claude-plugin" / "plugin.json").exists()


class TestInstallCopilotTarget:
    def test_writes_project_lsp_json_with_file_extensions(self, tmp_path):
        deps = [_make_dep("pyright", startup_timeout=120000)]

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
                    "warmupTimeoutMs": 120000,
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
        plugin_json = tmp_path / _CLAUDE_PROJECT_PLUGIN
        plugin_json.parent.mkdir(parents=True)
        plugin_json.write_text(
            json.dumps({"name": "apm-lsp", "lspServers": {"keep": {"command": "x"}}})
        )

        LSPIntegrator.remove_stale(set(), project_root=tmp_path)
        data = json.loads(plugin_json.read_text())
        assert "keep" in data["lspServers"]

    def test_removes_stale_from_project_plugin_manifest(self, tmp_path):
        plugin_json = tmp_path / _CLAUDE_PROJECT_PLUGIN
        plugin_json.parent.mkdir(parents=True)
        plugin_json.write_text(
            json.dumps(
                {
                    "name": "apm-lsp",
                    "lspServers": {
                        "keep": {"command": "x"},
                        "stale": {"command": "y"},
                    },
                }
            )
        )

        logger = MagicMock()
        LSPIntegrator.remove_stale({"stale"}, project_root=tmp_path, logger=logger)
        data = json.loads(plugin_json.read_text())
        assert "keep" in data["lspServers"]
        assert "stale" not in data["lspServers"]
        logger.progress.assert_any_call(
            "  |-- run /reload-plugins or restart Claude Code to activate"
        )

    def test_removing_last_server_deletes_owned_project_plugin(self, tmp_path):
        plugin_json = tmp_path / _CLAUDE_PROJECT_PLUGIN
        plugin_json.parent.mkdir(parents=True)
        plugin_json.write_text(
            json.dumps({"name": "apm-lsp", "lspServers": {"stale": {"command": "y"}}})
        )

        LSPIntegrator.remove_stale({"stale"}, project_root=tmp_path)

        assert not plugin_json.exists()
        assert not plugin_json.parent.parent.exists()

    def test_removing_last_user_claude_server_removes_plugin_directory(self, tmp_path):
        plugin_json = tmp_path / _CLAUDE_PROJECT_PLUGIN
        plugin_json.parent.mkdir(parents=True)
        plugin_json.write_text(
            json.dumps({"name": "apm-lsp", "lspServers": {"stale": {"command": "y"}}}),
            encoding="utf-8",
        )

        with patch("apm_cli.integration.lsp_integrator.Path.home", return_value=tmp_path):
            LSPIntegrator.remove_stale({"stale"}, user_scope=True)

        assert tmp_path.exists()
        assert not plugin_json.exists()
        assert not plugin_json.parent.parent.exists()

    def test_removes_stale_from_user_claude_plugin_manifest(self, tmp_path):
        plugin_json = tmp_path / _CLAUDE_PROJECT_PLUGIN
        plugin_json.parent.mkdir(parents=True)
        plugin_json.write_text(
            json.dumps(
                {
                    "name": "apm-lsp",
                    "lspServers": {
                        "keep": {"command": "x"},
                        "stale": {"command": "y"},
                    },
                }
            )
        )

        with patch("apm_cli.integration.lsp_integrator.Path.home", return_value=tmp_path):
            LSPIntegrator.remove_stale({"stale"}, user_scope=True)

        data = json.loads(plugin_json.read_text())
        assert data["name"] == "apm-lsp"
        assert "keep" in data["lspServers"]
        assert "stale" not in data["lspServers"]

    def test_no_plugin_manifest_is_noop(self, tmp_path):
        LSPIntegrator.remove_stale({"nonexistent"}, project_root=tmp_path)

    def test_strict_cleanup_raises_on_unwritable_config_shape(self, tmp_path):
        from apm_cli.install.errors import RequiredIntegrationError

        config_path = tmp_path / _CLAUDE_PROJECT_PLUGIN
        config_path.mkdir(parents=True)

        with pytest.raises(RequiredIntegrationError, match="LSP cleanup failed"):
            LSPIntegrator.remove_stale(
                {"stale"},
                project_root=tmp_path,
                fail_on_write_error=True,
            )

    def test_cleanup_refuses_foreign_plugin_manifest(self, tmp_path):
        from apm_cli.install.errors import RequiredIntegrationError

        plugin_json = tmp_path / _CLAUDE_PROJECT_PLUGIN
        plugin_json.parent.mkdir(parents=True)
        plugin_json.write_text(
            json.dumps(
                {
                    "name": "user-plugin",
                    "lspServers": {"stale": {"command": "keep"}},
                }
            ),
            encoding="utf-8",
        )

        with pytest.raises(RequiredIntegrationError, match="not owned by APM"):
            LSPIntegrator.remove_stale(
                {"stale"},
                project_root=tmp_path,
                fail_on_write_error=True,
            )

        assert "stale" in json.loads(plugin_json.read_text())["lspServers"]

    def test_multiple_stale_removed(self, tmp_path):
        plugin_json = tmp_path / _CLAUDE_PROJECT_PLUGIN
        plugin_json.parent.mkdir(parents=True)
        plugin_json.write_text(
            json.dumps(
                {
                    "name": "apm-lsp",
                    "lspServers": {
                        "keep": {"command": "x"},
                        "stale1": {"command": "y"},
                        "stale2": {"command": "z"},
                    },
                }
            )
        )

        LSPIntegrator.remove_stale({"stale1", "stale2"}, project_root=tmp_path)
        data = json.loads(plugin_json.read_text())
        assert set(data["lspServers"]) == {"keep"}


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

    def test_missing_lockfile_is_created(self, tmp_path):
        lock_path = tmp_path / "apm.lock.yaml"
        LSPIntegrator.update_lockfile({"pyright"}, lock_path)

        from apm_cli.deps.lockfile import LockFile

        lock = LockFile.read(lock_path)
        assert lock is not None
        assert lock.lsp_servers == ["pyright"]


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
        assert result[0].resolved_by == dep.get_unique_key()
        assert dep.get_unique_key() in result[0].approval_keys
        assert "test-pkg#1.0.0" not in result[0].approval_keys
        assert set(result[0].approval_keys) == {
            dep.get_unique_key(),
            f"{dep.get_unique_key()}#1.0.0",
        }

    def test_rejects_symlinked_locked_package_manifest(self, tmp_path):
        modules_dir = tmp_path / "apm_modules"
        package_dir = modules_dir / "owner" / "repo"
        package_dir.mkdir(parents=True)
        outside = tmp_path / "outside-apm.yml"
        outside.write_text("name: escaped\n", encoding="utf-8")
        try:
            (package_dir / "apm.yml").symlink_to(outside)
        except OSError as exc:
            pytest.skip(f"file symlinks unavailable: {exc}")

        from apm_cli.deps.lockfile import LockedDependency, LockFile

        lock_path = tmp_path / "apm.lock.yaml"
        lock = LockFile()
        lock.add_dependency(LockedDependency(repo_url="owner/repo", version="1.0.0"))
        lock.save(lock_path)

        with pytest.raises(ValueError, match="manifest must not be a symlink"):
            LSPIntegrator.collect_transitive(modules_dir, lock_path)

    def test_unlocked_fallback_does_not_invent_approval_keys(self, tmp_path):
        pkg_dir = tmp_path / "apm_modules" / "ghe.example" / "owner" / "repo"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "apm.yml").write_text(
            "name: enterprise-lsp\n"
            "version: 1.0.0\n"
            "dependencies:\n"
            "  lsp:\n"
            "    - name: pyright\n"
            "      command: pyright-langserver\n"
            "      extensionToLanguage:\n"
            "        .py: python\n",
            encoding="utf-8",
        )

        result = LSPIntegrator.collect_transitive(tmp_path / "apm_modules")

        assert result[0].resolved_by == "ghe.example/owner/repo"
        assert result[0].approval_keys == ()


def test_skill_cleanup_preserves_reserved_claude_lsp_plugin(tmp_path: Path) -> None:
    """Legacy skill cleanup must consume the LSP owner's reservation."""
    from apm_cli.integration.skill_support import clean_orphaned_skills

    skills_dir = tmp_path / ".claude" / "skills"
    plugin = skills_dir / "apm-lsp" / ".claude-plugin" / "plugin.json"
    plugin.parent.mkdir(parents=True)
    plugin.write_text('{"name": "apm-lsp", "lspServers": {}}', encoding="utf-8")
    orphan = skills_dir / "old-apm-skill"
    orphan.mkdir()

    result = clean_orphaned_skills(
        skills_dir,
        set(),
        project_root=tmp_path,
        get_lockfile_owned_agent_skills=lambda _root: set(),
    )

    assert plugin.is_file()
    assert not orphan.exists()
    assert result == {"files_removed": 1, "errors": 0}


# ===========================================================================
# OpenCode target
# ===========================================================================

_OPENCODE_SCHEMA = "https://opencode.ai/config.json"


def _gopls_dep(**kwargs) -> LSPDependency:
    return LSPDependency(
        name="gopls",
        command="gopls",
        extension_to_language=kwargs.pop("extension_to_language", {".go": "go"}),
        **kwargs,
    )


class TestOpenCodeTarget:
    def test_writes_project_opencode_json_with_schema(self, tmp_path):
        count = LSPIntegrator.install(
            [_gopls_dep(startup_timeout=60000)],
            project_root=tmp_path,
            target_runtimes=["opencode"],
        )

        assert count == 1
        data = json.loads((tmp_path / "opencode.json").read_text())
        assert data == {
            "$schema": _OPENCODE_SCHEMA,
            "lsp": {"gopls": {"command": ["gopls"], "extensions": [".go"]}},
        }
        assert next(iter(data)) == "$schema"

    def test_translation_command_args_extensions_env_initialization(self):
        from apm_cli.integration.lsp_integrator import _LSP_TARGET_SPECS

        spec = _LSP_TARGET_SPECS["opencode"]
        entry = {
            "command": "srv",
            "args": ["--stdio", "-v"],
            "extensionToLanguage": {"py": "python", ".b": "b", ".a": "a"},
            "env": {"K": "V"},
            "initializationOptions": {"x": 1},
            "startupTimeout": 1,
            "shutdownTimeout": 2,
            "restartOnCrash": True,
            "maxRestarts": 3,
            "workspaceFolder": "/w",
            "settings": {"s": 1},
            "transport": "stdio",
        }
        assert LSPIntegrator._entry_for_target(entry, spec) == {
            "command": ["srv", "--stdio", "-v"],
            "extensions": [".a", ".b", ".py"],
            "env": {"K": "V"},
            "initialization": {"x": 1},
        }

    def test_name_only_server_skipped_with_warning(self, tmp_path):
        logger = MagicMock()
        deps = [LSPDependency.from_string("gopls"), _make_dep("pyright")]

        LSPIntegrator.install(
            deps,
            project_root=tmp_path,
            target_runtimes=["opencode"],
            logger=logger,
        )

        data = json.loads((tmp_path / "opencode.json").read_text())
        assert list(data["lsp"]) == ["pyright"]
        assert any("gopls" in str(c) for c in logger.warning.call_args_list)
        assert LSPIntegrator.expressible_server_names(deps, "opencode") == {"pyright"}

    def test_only_name_only_servers_writes_nothing(self, tmp_path):
        count = LSPIntegrator.install(
            [LSPDependency.from_string("gopls")],
            project_root=tmp_path,
            target_runtimes=["opencode"],
            fail_on_write_error=True,
        )
        assert count == 0
        assert not (tmp_path / "opencode.json").exists()

    def test_socket_transport_skipped_with_warning(self, tmp_path):
        logger = MagicMock()
        deps = [_gopls_dep(transport="socket"), _make_dep("pyright")]
        count = LSPIntegrator.install(
            deps,
            project_root=tmp_path,
            target_runtimes=["opencode", "copilot"],
            logger=logger,
        )

        data = json.loads((tmp_path / "opencode.json").read_text())
        assert list(data["lsp"]) == ["pyright"]
        assert "gopls" in json.loads((tmp_path / ".github" / "lsp.json").read_text())["lspServers"]
        assert count >= 1
        assert any("gopls" in str(c) for c in logger.warning.call_args_list)

    @pytest.mark.parametrize("user_scope", [False, True])
    def test_config_path_matches_mcp_adapter(self, tmp_path, monkeypatch, user_scope):
        from apm_cli.adapters.client.opencode import OpenCodeClientAdapter
        from apm_cli.integration.lsp_integrator import _LSP_TARGET_SPECS

        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        spec = _LSP_TARGET_SPECS["opencode"]
        assert spec.path(tmp_path, user_scope=user_scope) == (
            OpenCodeClientAdapter.config_path_for(tmp_path, user_scope)
        )

    def test_merge_preserves_existing_keys_and_schema(self, tmp_path):
        path = tmp_path / "opencode.json"
        path.write_text(
            json.dumps({"$schema": "custom", "mcp": {"a": {"type": "local"}}, "theme": "dark"})
        )

        LSPIntegrator.install([_gopls_dep()], project_root=tmp_path, target_runtimes=["opencode"])

        data = json.loads(path.read_text())
        assert data["$schema"] == "custom"
        assert data["mcp"] == {"a": {"type": "local"}}
        assert data["theme"] == "dark"
        assert data["lsp"]["gopls"]["command"] == ["gopls"]

    def test_existing_file_without_schema_gets_none(self, tmp_path):
        path = tmp_path / "opencode.json"
        path.write_text(json.dumps({"mcp": {}}))
        LSPIntegrator.install([_gopls_dep()], project_root=tmp_path, target_runtimes=["opencode"])
        assert "$schema" not in json.loads(path.read_text())

    def test_idempotent(self, tmp_path):
        args = {"project_root": tmp_path, "target_runtimes": ["opencode"]}
        assert LSPIntegrator.install([_gopls_dep()], **args) == 1
        assert LSPIntegrator.install([_gopls_dep()], **args) == 0

    def test_user_scope_path(self, tmp_path):
        with patch("apm_cli.integration.lsp_integrator.Path.home", return_value=tmp_path):
            count = LSPIntegrator.install(
                [_gopls_dep()], user_scope=True, target_runtimes=["opencode"]
            )
        assert count == 1
        path = tmp_path / ".config" / "opencode" / "opencode.json"
        assert json.loads(path.read_text())["lsp"]["gopls"]["extensions"] == [".go"]

    def test_remove_stale_keeps_mcp(self, tmp_path):
        path = tmp_path / "opencode.json"
        path.write_text(json.dumps({"mcp": {"m": {}}, "lsp": {"gopls": {}, "other": {}}}))
        LSPIntegrator.remove_stale({"gopls"}, project_root=tmp_path, target_runtimes=["opencode"])
        assert json.loads(path.read_text()) == {"mcp": {"m": {}}, "lsp": {"other": {}}}

    def test_remove_stale_keeps_file_with_mcp_and_schema(self, tmp_path):
        path = tmp_path / "opencode.json"
        path.write_text(
            json.dumps({"$schema": _OPENCODE_SCHEMA, "mcp": {"m": {}}, "lsp": {"a": {}}})
        )
        LSPIntegrator.remove_stale({"a"}, project_root=tmp_path, target_runtimes=["opencode"])
        assert json.loads(path.read_text()) == {
            "$schema": _OPENCODE_SCHEMA,
            "mcp": {"m": {}},
            "lsp": {},
        }

    def test_remove_stale_deletes_file_with_only_schema_and_lsp(self, tmp_path):
        LSPIntegrator.install([_gopls_dep()], project_root=tmp_path, target_runtimes=["opencode"])
        LSPIntegrator.remove_stale({"gopls"}, project_root=tmp_path, target_runtimes=["opencode"])
        assert not (tmp_path / "opencode.json").exists()

    def test_remove_stale_keeps_file_with_custom_schema(self, tmp_path):
        path = tmp_path / "opencode.json"
        path.write_text(json.dumps({"$schema": "custom"}))
        LSPIntegrator.install([_gopls_dep()], project_root=tmp_path, target_runtimes=["opencode"])
        LSPIntegrator.remove_stale({"gopls"}, project_root=tmp_path, target_runtimes=["opencode"])
        assert json.loads(path.read_text()) == {"$schema": "custom", "lsp": {}}

    @pytest.mark.parametrize(
        "content",
        [
            '{\n  // comment\n  "mcp": {"m": {"type": "local"}}\n}\n',
            '{"mcp": {"m": {"type": "local"}},}\n',
            '["not", "an", "object"]\n',
            '{"mcp": {"m": {"type": "local"}}, "lsp": false}\n',
        ],
        ids=["comment", "trailing-comma", "non-object", "lsp-false"],
    )
    @pytest.mark.parametrize("force", [False, True], ids=["default", "force"])
    def test_unsafe_existing_config_is_never_rewritten(self, tmp_path, content, force):
        from apm_cli.install.errors import RequiredIntegrationError

        path = tmp_path / "opencode.json"
        path.write_text(content, encoding="utf-8")
        diagnostics = MagicMock()

        count = LSPIntegrator.install(
            [_gopls_dep()],
            project_root=tmp_path,
            target_runtimes=["opencode"],
            diagnostics=diagnostics,
            force=force,
        )
        assert count == 0
        assert path.read_text(encoding="utf-8") == content
        assert any("opencode.json" in str(c) for c in diagnostics.warn.call_args_list)

        with pytest.raises(RequiredIntegrationError, match=r"opencode\.json"):
            LSPIntegrator.install(
                [_gopls_dep()],
                project_root=tmp_path,
                target_runtimes=["opencode"],
                fail_on_write_error=True,
                force=force,
            )
        assert path.read_text(encoding="utf-8") == content

    def test_blank_existing_config_is_treated_as_empty(self, tmp_path):
        path = tmp_path / "opencode.json"
        path.write_text("  \n", encoding="utf-8")
        count = LSPIntegrator.install(
            [_gopls_dep()],
            project_root=tmp_path,
            target_runtimes=["opencode"],
            fail_on_write_error=True,
        )
        assert count == 1
        assert json.loads(path.read_text())["lsp"]["gopls"]["command"] == ["gopls"]

    def test_user_authored_same_name_server_is_not_overwritten(self, tmp_path):
        from apm_cli.install.errors import RequiredIntegrationError

        path = tmp_path / "opencode.json"
        content = json.dumps(
            {"mcp": {"m": {"type": "local"}}, "lsp": {"gopls": {"command": ["my-gopls"]}}}
        )
        path.write_text(content, encoding="utf-8")
        args = {"project_root": tmp_path, "target_runtimes": ["opencode"]}

        assert LSPIntegrator.install([_gopls_dep()], diagnostics=MagicMock(), **args) == 0
        assert path.read_text(encoding="utf-8") == content
        with pytest.raises(RequiredIntegrationError, match="not managed by APM"):
            LSPIntegrator.install([_gopls_dep()], fail_on_write_error=True, **args)
        assert path.read_text(encoding="utf-8") == content

        assert LSPIntegrator.install([_gopls_dep()], force=True, **args) == 1
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["lsp"]["gopls"]["command"] == ["gopls"]
        assert data["mcp"] == {"m": {"type": "local"}}

    def test_identical_existing_server_is_accepted(self, tmp_path):
        path = tmp_path / "opencode.json"
        path.write_text(
            json.dumps({"lsp": {"gopls": {"command": ["gopls"], "extensions": [".go"]}}}),
            encoding="utf-8",
        )
        assert (
            LSPIntegrator.install(
                [_gopls_dep()],
                project_root=tmp_path,
                target_runtimes=["opencode"],
                fail_on_write_error=True,
            )
            == 0
        )

    def test_managed_server_is_updated_without_force(self, tmp_path):
        args = {"project_root": tmp_path, "target_runtimes": ["opencode"]}
        LSPIntegrator.install([_gopls_dep()], **args)

        count = LSPIntegrator.install(
            [_gopls_dep(args=["serve"])],
            fail_on_write_error=True,
            managed_target_servers={"opencode": {"gopls"}},
            **args,
        )

        assert count == 1
        data = json.loads((tmp_path / "opencode.json").read_text(encoding="utf-8"))
        assert data["lsp"]["gopls"]["command"] == ["gopls", "serve"]

    def test_interrupted_install_write_keeps_existing_config(self, tmp_path):
        from apm_cli.install.errors import RequiredIntegrationError

        path = tmp_path / "opencode.json"
        content = json.dumps({"mcp": {"m": {"type": "local"}}, "theme": "dark"})
        path.write_text(content, encoding="utf-8")

        with (
            patch(
                "apm_cli.utils.atomic_io._replace_atomic_file",
                side_effect=OSError("interrupted"),
            ),
            pytest.raises(RequiredIntegrationError, match="interrupted"),
        ):
            LSPIntegrator.install(
                [_gopls_dep()],
                project_root=tmp_path,
                target_runtimes=["opencode"],
                fail_on_write_error=True,
            )

        assert path.read_text(encoding="utf-8") == content
        assert [p.name for p in tmp_path.iterdir()] == ["opencode.json"]

    def test_interrupted_cleanup_write_keeps_existing_config(self, tmp_path):
        from apm_cli.install.errors import RequiredIntegrationError

        path = tmp_path / "opencode.json"
        content = json.dumps({"mcp": {"m": {}}, "lsp": {"gopls": {}, "other": {}}})
        path.write_text(content, encoding="utf-8")

        with (
            patch(
                "apm_cli.utils.atomic_io._replace_atomic_file",
                side_effect=OSError("interrupted"),
            ),
            pytest.raises(RequiredIntegrationError, match="interrupted"),
        ):
            LSPIntegrator.remove_stale(
                {"gopls"},
                project_root=tmp_path,
                target_runtimes=["opencode"],
                fail_on_write_error=True,
            )

        assert path.read_text(encoding="utf-8") == content
        assert [p.name for p in tmp_path.iterdir()] == ["opencode.json"]

    def test_resolve_explicit_runtime(self, tmp_path):
        assert LSPIntegrator.resolve_target_runtimes(
            project_root=tmp_path, runtime="opencode", explicit_target="opencode"
        ) == ["opencode"]

    def test_resolve_with_target_decision_is_ordered(self, tmp_path):
        from apm_cli.core.target_detection import EffectiveTargetDecision

        decision = EffectiveTargetDecision(["claude", "copilot", "opencode"], "--target flag")
        assert LSPIntegrator.resolve_target_runtimes(
            project_root=tmp_path,
            target_decision=decision,
            explicit_target=["claude", "copilot", "opencode"],
        ) == ["copilot", "claude", "opencode"]

    def test_autodetect_from_opencode_dir(self, tmp_path):
        (tmp_path / ".opencode").mkdir()
        with patch("apm_cli.integration.lsp_integrator.find_runtime_binary", return_value=None):
            targets = LSPIntegrator.resolve_target_runtimes(project_root=tmp_path)
        assert targets == ["opencode"]

    def test_coexists_with_mcp_adapter(self, tmp_path):
        from apm_cli.adapters.client.opencode import OpenCodeClientAdapter

        (tmp_path / ".opencode").mkdir()
        adapter = OpenCodeClientAdapter(project_root=tmp_path)
        adapter.update_config({"srv": {"command": "x", "args": []}})
        LSPIntegrator.install([_gopls_dep()], project_root=tmp_path, target_runtimes=["opencode"])
        data = json.loads((tmp_path / "opencode.json").read_text())
        assert "srv" in data["mcp"] and "gopls" in data["lsp"]

        adapter.update_config({"srv2": {"command": "y", "args": []}})
        data = json.loads((tmp_path / "opencode.json").read_text())
        assert {"srv", "srv2"} <= set(data["mcp"]) and "gopls" in data["lsp"]

    def test_coexists_lsp_first_then_mcp(self, tmp_path):
        from apm_cli.adapters.client.opencode import OpenCodeClientAdapter

        (tmp_path / ".opencode").mkdir()
        LSPIntegrator.install([_gopls_dep()], project_root=tmp_path, target_runtimes=["opencode"])
        OpenCodeClientAdapter(project_root=tmp_path).update_config(
            {"srv": {"command": "x", "args": []}}
        )
        data = json.loads((tmp_path / "opencode.json").read_text())
        assert "srv" in data["mcp"] and "gopls" in data["lsp"]

    def test_target_choice_hint(self):
        assert (
            LSPIntegrator.target_choice_hint()
            == "--target copilot, --target claude or --target opencode"
        )
