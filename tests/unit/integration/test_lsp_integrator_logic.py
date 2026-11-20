"""Unit tests for ``apm_cli.integration.lsp_integrator.LSPIntegrator`` (pure logic cases)."""

from __future__ import annotations

import pytest

from apm_cli.integration.lsp_integrator import LSPIntegrator
from apm_cli.models.dependency.lsp import LSPDependency

pytestmark = pytest.mark.unit


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


# ===========================================================================
# LSPIntegrator.install -- user scope
# ===========================================================================


# ===========================================================================
# LSPIntegrator.remove_stale
# ===========================================================================


# ===========================================================================
# LSPIntegrator.update_lockfile
# ===========================================================================


# ===========================================================================
# LSPIntegrator.collect_transitive
# ===========================================================================


# ===========================================================================
# OpenCode target
# ===========================================================================


class TestOpenCodeTarget:
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

    def test_target_choice_hint(self):
        assert (
            LSPIntegrator.target_choice_hint()
            == "--target copilot, --target claude or --target opencode"
        )
