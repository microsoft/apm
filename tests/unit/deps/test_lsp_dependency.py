"""Unit tests for ``apm_cli.models.dependency.lsp.LSPDependency``."""

from __future__ import annotations

import pytest

from apm_cli.models.dependency.lsp import LSPDependency

# ===========================================================================
# LSPDependency.from_string
# ===========================================================================


class TestFromString:
    """Construct LSPDependency from a plain server name string."""

    def test_simple_name(self):
        dep = LSPDependency.from_string("pyright")
        assert dep.name == "pyright"
        assert dep.command is None
        assert dep.extension_to_language is None

    def test_scoped_name(self):
        dep = LSPDependency.from_string("@scope/lsp-server")
        assert dep.name == "@scope/lsp-server"

    def test_empty_name_raises(self):
        with pytest.raises(ValueError, match="must not be empty"):
            LSPDependency.from_string("")

    def test_invalid_chars_raises(self):
        with pytest.raises(ValueError, match="Invalid LSP dependency name"):
            LSPDependency.from_string("bad name!")

    def test_traversal_in_name_raises(self):
        with pytest.raises(ValueError, match="Invalid LSP dependency name"):
            LSPDependency.from_string("../escape")


# ===========================================================================
# LSPDependency.from_dict
# ===========================================================================


class TestFromDict:
    """Construct LSPDependency from a dict (strict validation)."""

    def test_minimal_valid(self):
        dep = LSPDependency.from_dict(
            {
                "name": "pyright",
                "command": "pyright-langserver",
                "extensionToLanguage": {".py": "python"},
            }
        )
        assert dep.name == "pyright"
        assert dep.command == "pyright-langserver"
        assert dep.extension_to_language == {".py": "python"}

    def test_all_optional_fields(self):
        dep = LSPDependency.from_dict(
            {
                "name": "ruff-lsp",
                "command": "ruff",
                "args": ["server"],
                "extensionToLanguage": {".py": "python"},
                "transport": "stdio",
                "env": {"RUFF_LOG": "debug"},
                "initializationOptions": {"lint": True},
                "settings": {"format": True},
                "workspaceFolder": "/projects",
                "startupTimeout": 5000,
                "shutdownTimeout": 3000,
                "restartOnCrash": True,
                "maxRestarts": 3,
            }
        )
        assert dep.transport == "stdio"
        assert dep.args == ["server"]
        assert dep.env == {"RUFF_LOG": "debug"}
        assert dep.initialization_options == {"lint": True}
        assert dep.settings == {"format": True}
        assert dep.workspace_folder == "/projects"
        assert dep.startup_timeout == 5000
        assert dep.shutdown_timeout == 3000
        assert dep.restart_on_crash is True
        assert dep.max_restarts == 3

    def test_snake_case_keys_accepted(self):
        dep = LSPDependency.from_dict(
            {
                "name": "ts-lsp",
                "command": "typescript-language-server",
                "extension_to_language": {".ts": "typescript"},
                "workspace_folder": "/home",
                "startup_timeout": 1000,
            }
        )
        assert dep.extension_to_language == {".ts": "typescript"}
        assert dep.workspace_folder == "/home"
        assert dep.startup_timeout == 1000

    def test_camel_case_zero_values_preserved(self):
        dep = LSPDependency.from_dict(
            {
                "name": "zero-lsp",
                "command": "zero-langserver",
                "extensionToLanguage": {".z": "zero"},
                "workspaceFolder": "",
                "startupTimeout": 0,
                "shutdownTimeout": 0,
                "maxRestarts": 0,
            }
        )
        assert dep.workspace_folder == ""
        assert dep.startup_timeout == 0
        assert dep.shutdown_timeout == 0
        assert dep.max_restarts == 0

    def test_missing_name_raises(self):
        with pytest.raises(ValueError, match="must contain 'name'"):
            LSPDependency.from_dict({"command": "x", "extensionToLanguage": {".py": "python"}})

    def test_missing_command_strict_raises(self):
        with pytest.raises(ValueError, match="requires 'command'"):
            LSPDependency.from_dict({"name": "no-cmd", "extensionToLanguage": {".py": "python"}})

    def test_missing_extension_to_language_strict_raises(self):
        with pytest.raises(ValueError, match="requires 'extensionToLanguage'"):
            LSPDependency.from_dict({"name": "no-ext", "command": "x"})

    def test_invalid_transport_raises(self):
        with pytest.raises(ValueError, match="unsupported transport"):
            LSPDependency.from_dict(
                {
                    "name": "bad-transport",
                    "command": "x",
                    "extensionToLanguage": {".py": "python"},
                    "transport": "grpc",
                }
            )

    def test_traversal_in_command_raises(self):
        with pytest.raises(ValueError, match="must not contain"):
            LSPDependency.from_dict(
                {
                    "name": "bad-cmd",
                    "command": "../../../bin/evil",
                    "extensionToLanguage": {".py": "python"},
                }
            )

    def test_unknown_keys_ignored(self):
        dep = LSPDependency.from_dict(
            {
                "name": "forward-compat",
                "command": "lsp",
                "extensionToLanguage": {".rs": "rust"},
                "futureField": True,
            }
        )
        assert dep.name == "forward-compat"


# ===========================================================================
# LSPDependency.to_dict / to_lsp_json_entry
# ===========================================================================


class TestSerialisation:
    """Round-trip serialisation behaviour."""

    def test_minimal_to_dict(self):
        dep = LSPDependency(name="pyright")
        d = dep.to_dict()
        assert d == {"name": "pyright"}

    def test_full_round_trip(self):
        original = {
            "name": "ruff-lsp",
            "command": "ruff",
            "extensionToLanguage": {".py": "python"},
            "transport": "stdio",
        }
        dep = LSPDependency.from_dict(original)
        d = dep.to_dict()
        assert d["name"] == "ruff-lsp"
        assert d["command"] == "ruff"
        assert d["extensionToLanguage"] == {".py": "python"}
        assert d["transport"] == "stdio"

    def test_to_lsp_json_entry_excludes_name(self):
        dep = LSPDependency.from_dict(
            {
                "name": "pyright",
                "command": "pyright-langserver",
                "extensionToLanguage": {".py": "python"},
            }
        )
        entry = dep.to_lsp_json_entry()
        assert "name" not in entry
        assert entry["command"] == "pyright-langserver"

    def test_none_fields_omitted(self):
        dep = LSPDependency(name="minimal", command="x", extension_to_language={".py": "python"})
        d = dep.to_dict()
        assert "args" not in d
        assert "env" not in d
        assert "transport" not in d


# ===========================================================================
# LSPDependency.__str__ / __repr__
# ===========================================================================


class TestStringRepresentation:
    def test_str_without_transport(self):
        dep = LSPDependency(name="pyright")
        assert str(dep) == "pyright"

    def test_str_with_transport(self):
        dep = LSPDependency(name="pyright", transport="stdio")
        assert str(dep) == "pyright (stdio)"

    def test_repr_redacts_env(self):
        dep = LSPDependency(name="s", command="cmd", env={"SECRET": "token123"})
        r = repr(dep)
        assert "token123" not in r
        assert "***" in r


# ===========================================================================
# LSPDependency.validate edge cases
# ===========================================================================


class TestValidateEdgeCases:
    def test_name_max_length_accepted(self):
        name = "a" * 128
        dep = LSPDependency(name=name)
        dep.validate(strict=False)

    def test_name_too_long_rejected(self):
        name = "a" * 129
        with pytest.raises(ValueError, match="Invalid LSP dependency name"):
            LSPDependency(name=name).validate(strict=False)

    def test_socket_transport_accepted(self):
        dep = LSPDependency(
            name="socket-lsp",
            command="lsp",
            extension_to_language={".py": "python"},
            transport="socket",
        )
        dep.validate(strict=True)

    def test_non_strict_skips_command_check(self):
        dep = LSPDependency(name="ref-only")
        dep.validate(strict=False)

    def test_extension_to_language_non_dict_raises(self):
        dep = LSPDependency(
            name="bad-ext",
            command="x",
            extension_to_language="not-a-dict",  # type: ignore[arg-type]
        )
        with pytest.raises(ValueError, match="must be a dict"):
            dep.validate(strict=True)

    def test_extension_to_language_values_must_be_strings(self):
        dep = LSPDependency(
            name="bad-ext-value",
            command="x",
            extension_to_language={".py": 123},  # type: ignore[dict-item]
        )
        with pytest.raises(ValueError, match="string extensions to string language IDs"):
            dep.validate(strict=True)

    def test_workspace_folder_traversal_raises(self):
        with pytest.raises(ValueError, match="Invalid LSP workspaceFolder"):
            LSPDependency.from_dict(
                {
                    "name": "bad-workspace",
                    "command": "x",
                    "extensionToLanguage": {".py": "python"},
                    "workspaceFolder": "../outside",
                }
            )
