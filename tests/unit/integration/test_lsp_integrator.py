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
