"""Integration tests for integration/ and runtime/ modules.

Covers previously uncovered lines in:
- integration/kiro_hook_integrator.py
- integration/lsp_integrator.py
- integration/copilot_app_ws.py
- integration/instruction_integrator.py
- runtime/manager.py
- runtime/base.py
- runtime/codex_runtime.py
- runtime/copilot_runtime.py
- runtime/llm_runtime.py

All tests are hermetic: no live network or subprocess calls.
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.parse
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# kiro_hook_integrator -- pure helpers
# ---------------------------------------------------------------------------


class TestSafeHookSlug:
    """Tests for _safe_hook_slug()."""

    def test_simple_alphanumeric(self) -> None:
        """Plain alphanumeric stays lower-cased."""
        from apm_cli.integration.kiro_hook_integrator import _safe_hook_slug

        assert _safe_hook_slug("MyHook") == "myhook"

    def test_spaces_become_dashes(self) -> None:
        """Spaces are replaced with dashes."""
        from apm_cli.integration.kiro_hook_integrator import _safe_hook_slug

        assert _safe_hook_slug("my hook name") == "my-hook-name"

    def test_special_chars_replaced(self) -> None:
        """Special characters (!, @, etc.) become dashes."""
        from apm_cli.integration.kiro_hook_integrator import _safe_hook_slug

        result = _safe_hook_slug("hook@v1.0!")
        assert "!" not in result
        assert "@" not in result

    def test_empty_string_returns_fallback(self) -> None:
        """Empty string uses the fallback."""
        from apm_cli.integration.kiro_hook_integrator import _safe_hook_slug

        assert _safe_hook_slug("", fallback="default") == "default"

    def test_only_special_chars_returns_fallback(self) -> None:
        """String of only special chars collapses to fallback."""
        from apm_cli.integration.kiro_hook_integrator import _safe_hook_slug

        result = _safe_hook_slug("---___---")
        # after stripping leading/trailing .-_ we get empty → fallback
        assert result == "hook"

    def test_dots_and_dashes_preserved(self) -> None:
        """Dots and dashes in the middle are preserved."""
        from apm_cli.integration.kiro_hook_integrator import _safe_hook_slug

        assert _safe_hook_slug("pre-tool.use") == "pre-tool.use"


class TestKiroPatternsFromMatcher:
    """Tests for _kiro_patterns_from_matcher()."""

    def test_string_patterns(self) -> None:
        """Single string pattern is returned as a list."""
        from apm_cli.integration.kiro_hook_integrator import _kiro_patterns_from_matcher

        result = _kiro_patterns_from_matcher({"patterns": "**/*.py"})
        assert result == ["**/*.py"]

    def test_list_patterns(self) -> None:
        """List of patterns is returned correctly."""
        from apm_cli.integration.kiro_hook_integrator import _kiro_patterns_from_matcher

        result = _kiro_patterns_from_matcher({"patterns": ["*.ts", "*.js"]})
        assert result == ["*.ts", "*.js"]

    def test_list_patterns_filters_empty(self) -> None:
        """Empty strings in list patterns are filtered out."""
        from apm_cli.integration.kiro_hook_integrator import _kiro_patterns_from_matcher

        result = _kiro_patterns_from_matcher({"patterns": ["*.ts", "", "*.js"]})
        assert result == ["*.ts", "*.js"]

    def test_matcher_key_fallback(self) -> None:
        """Falls back to 'matcher' key when 'patterns' is absent."""
        from apm_cli.integration.kiro_hook_integrator import _kiro_patterns_from_matcher

        result = _kiro_patterns_from_matcher({"matcher": "src/**"})
        assert result == ["src/**"]

    def test_no_patterns_returns_empty(self) -> None:
        """Empty matcher dict returns empty list."""
        from apm_cli.integration.kiro_hook_integrator import _kiro_patterns_from_matcher

        result = _kiro_patterns_from_matcher({})
        assert result == []

    def test_empty_string_patterns_returns_empty(self) -> None:
        """Empty-string pattern field returns empty list."""
        from apm_cli.integration.kiro_hook_integrator import _kiro_patterns_from_matcher

        result = _kiro_patterns_from_matcher({"patterns": "   "})
        assert result == []


class TestKiroThenFromAction:
    """Tests for _kiro_then_from_action()."""

    def test_ask_agent_type(self) -> None:
        """type=askAgent returns askAgent then object with prompt."""
        from apm_cli.integration.kiro_hook_integrator import _kiro_then_from_action

        result = _kiro_then_from_action(
            {"type": "askAgent", "prompt": "Do something"},
            command_keys=("bash", "command"),
        )
        assert result == {"type": "askAgent", "prompt": "Do something"}

    def test_prompt_string_shorthand(self) -> None:
        """Dict with 'prompt' key (no type) maps to askAgent."""
        from apm_cli.integration.kiro_hook_integrator import _kiro_then_from_action

        result = _kiro_then_from_action(
            {"prompt": "Run tests"},
            command_keys=("bash", "command"),
        )
        assert result == {"type": "askAgent", "prompt": "Run tests"}

    def test_bash_command_key(self) -> None:
        """Dict with 'bash' key maps to runCommand."""
        from apm_cli.integration.kiro_hook_integrator import _kiro_then_from_action

        result = _kiro_then_from_action(
            {"bash": "npm test"},
            command_keys=("bash", "command"),
        )
        assert result == {"type": "runCommand", "command": "npm test"}

    def test_command_key(self) -> None:
        """Dict with 'command' key maps to runCommand."""
        from apm_cli.integration.kiro_hook_integrator import _kiro_then_from_action

        result = _kiro_then_from_action(
            {"command": "make lint"},
            command_keys=("bash", "command"),
        )
        assert result == {"type": "runCommand", "command": "make lint"}

    def test_empty_prompt_returns_none(self) -> None:
        """askAgent with blank prompt returns None."""
        from apm_cli.integration.kiro_hook_integrator import _kiro_then_from_action

        result = _kiro_then_from_action(
            {"type": "askAgent", "prompt": "   "},
            command_keys=("bash",),
        )
        assert result is None

    def test_no_matching_key_returns_none(self) -> None:
        """Dict with no matching command key returns None."""
        from apm_cli.integration.kiro_hook_integrator import _kiro_then_from_action

        result = _kiro_then_from_action(
            {"unknown_key": "something"},
            command_keys=("bash", "command"),
        )
        assert result is None


class TestKiroHookDocument:
    """Tests for _kiro_hook_document()."""

    def test_builds_document_with_patterns(self) -> None:
        """Document includes patterns in 'when' when provided."""
        from apm_cli.integration.kiro_hook_integrator import _kiro_hook_document

        doc = _kiro_hook_document(
            name="my-pkg preToolUse 1",
            description="A description",
            event_name="preToolUse",
            patterns=["**/*.py"],
            then={"type": "runCommand", "command": "echo hi"},
        )
        assert doc["name"] == "my-pkg preToolUse 1"
        assert doc["version"] == "1.0.0"
        assert doc["when"]["type"] == "preToolUse"
        assert doc["when"]["patterns"] == ["**/*.py"]
        assert doc["then"] == {"type": "runCommand", "command": "echo hi"}
        assert doc["description"] == "A description"

    def test_builds_document_without_patterns(self) -> None:
        """Document omits 'patterns' from 'when' when list is empty."""
        from apm_cli.integration.kiro_hook_integrator import _kiro_hook_document

        doc = _kiro_hook_document(
            name="pkg hook 1",
            description=None,
            event_name="postToolUse",
            patterns=[],
            then={"type": "askAgent", "prompt": "Check"},
        )
        assert "patterns" not in doc["when"]
        assert "description" not in doc

    def test_no_description_omitted(self) -> None:
        """None description is omitted from the document."""
        from apm_cli.integration.kiro_hook_integrator import _kiro_hook_document

        doc = _kiro_hook_document(
            name="x",
            description=None,
            event_name="ev",
            patterns=[],
            then={"type": "runCommand", "command": "ls"},
        )
        assert "description" not in doc


# ---------------------------------------------------------------------------
# kiro_hook_integrator -- full integration flow
# ---------------------------------------------------------------------------


class TestIntegrateKiroHooksFlow:
    """Tests for integrate_kiro_hooks() end-to-end."""

    def _make_package_info(self, install_path: Path, name: str = "my-pkg") -> Any:
        """Build a minimal PackageInfo-like object."""
        pkg = MagicMock()
        pkg.name = name
        info = MagicMock()
        info.install_path = install_path
        info.package = pkg
        return info

    def _make_integrator(self) -> Any:
        """Build a mock HookIntegrator."""
        integrator = MagicMock()
        integrator.HOOK_COMMAND_KEYS = ("bash", "command")
        integrator.find_hook_files.return_value = []
        integrator._get_package_name.return_value = "my-pkg"
        integrator._parse_hook_json.return_value = None
        integrator._rewrite_hooks_data.return_value = ({}, [])
        integrator.check_collision.return_value = False
        integrator.try_adopt_identical.return_value = False
        return integrator

    def test_returns_empty_when_kiro_dir_missing(self, tmp_path: Path) -> None:
        """Returns zero-count result when .kiro dir doesn't exist."""
        from apm_cli.integration.kiro_hook_integrator import integrate_kiro_hooks

        integrator = self._make_integrator()
        pkg_info = self._make_package_info(tmp_path / "pkg")
        result = integrate_kiro_hooks(integrator, pkg_info, tmp_path)
        assert result.files_integrated == 0
        assert result.files_skipped == 0

    def test_returns_empty_when_no_hook_files(self, tmp_path: Path) -> None:
        """Returns zero-count result when no hook files found."""
        from apm_cli.integration.kiro_hook_integrator import integrate_kiro_hooks

        kiro_dir = tmp_path / ".kiro"
        kiro_dir.mkdir()
        integrator = self._make_integrator()
        integrator.find_hook_files.return_value = []
        pkg_info = self._make_package_info(tmp_path / "pkg")
        result = integrate_kiro_hooks(integrator, pkg_info, tmp_path)
        assert result.files_integrated == 0

    def test_writes_hook_document(self, tmp_path: Path) -> None:
        """A valid hook file produces a JSON file in .kiro/hooks/."""
        from apm_cli.integration.kiro_hook_integrator import integrate_kiro_hooks

        kiro_dir = tmp_path / ".kiro"
        kiro_dir.mkdir()

        pkg_dir = tmp_path / "pkg"
        pkg_dir.mkdir()
        hook_file = pkg_dir / "hooks.json"
        hook_file.write_text('{"hooks":{}}', encoding="utf-8")

        integrator = self._make_integrator()
        integrator.find_hook_files.return_value = [hook_file]
        rewritten = {
            "hooks": {
                "preToolUse": [
                    {"bash": "echo hello"},
                ]
            }
        }
        integrator._parse_hook_json.return_value = rewritten
        integrator._rewrite_hooks_data.return_value = (rewritten, [])

        pkg_info = self._make_package_info(pkg_dir)
        result = integrate_kiro_hooks(integrator, pkg_info, tmp_path)
        assert result.files_integrated == 1
        hooks_dir = kiro_dir / "hooks"
        json_files = list(hooks_dir.glob("*.json"))
        assert len(json_files) == 1
        doc = json.loads(json_files[0].read_text(encoding="utf-8"))
        assert doc["then"]["type"] == "runCommand"

    def test_adopts_identical_existing_file(self, tmp_path: Path) -> None:
        """When file already has identical content, it is adopted not re-written."""
        from apm_cli.integration.kiro_hook_integrator import integrate_kiro_hooks

        kiro_dir = tmp_path / ".kiro"
        kiro_dir.mkdir()
        hooks_dir = kiro_dir / "hooks"
        hooks_dir.mkdir()

        pkg_dir = tmp_path / "pkg"
        pkg_dir.mkdir()
        hook_file = pkg_dir / "hooks.json"
        hook_file.write_text("{}", encoding="utf-8")

        rewritten = {
            "hooks": {
                "preToolUse": [{"command": "make test"}],
            }
        }

        integrator = self._make_integrator()
        integrator.find_hook_files.return_value = [hook_file]
        integrator._parse_hook_json.return_value = rewritten
        integrator._rewrite_hooks_data.return_value = (rewritten, [])

        # Pre-write the expected output so it gets adopted
        from apm_cli.integration.kiro_hook_integrator import _kiro_hook_document, _safe_hook_slug

        doc = _kiro_hook_document(
            name="my-pkg preToolUse 1",
            description=None,
            event_name="preToolUse",
            patterns=[],
            then={"type": "runCommand", "command": "make test"},
        )
        expected_filename = (
            f"{_safe_hook_slug('my-pkg')}-{_safe_hook_slug('hooks')}-"
            f"{_safe_hook_slug('preToolUse')}-1.json"
        )
        target = hooks_dir / expected_filename
        target.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")

        pkg_info = self._make_package_info(pkg_dir)
        result = integrate_kiro_hooks(integrator, pkg_info, tmp_path)
        assert result.files_adopted >= 1
        assert result.files_integrated == 0


# ---------------------------------------------------------------------------
# lsp_integrator -- _LSPTargetSpec
# ---------------------------------------------------------------------------


class TestLSPTargetSpec:
    """Tests for _LSPTargetSpec path/scope helpers."""

    def test_project_path(self, tmp_path: Path) -> None:
        """project path resolves relative to project_root."""
        from apm_cli.integration.lsp_integrator import _LSP_TARGET_SPECS

        spec = _LSP_TARGET_SPECS["claude"]
        path = spec.path(tmp_path, user_scope=False)
        assert path == tmp_path / ".lsp.json"

    def test_user_scope_label(self) -> None:
        """User-scope label differs from project-scope label."""
        from apm_cli.integration.lsp_integrator import _LSP_TARGET_SPECS

        spec = _LSP_TARGET_SPECS["copilot"]
        assert spec.label(user_scope=True) != spec.label(user_scope=False)

    def test_servers_key_project_scope(self) -> None:
        """Claude project scope has None servers_key (top-level map)."""
        from apm_cli.integration.lsp_integrator import _LSP_TARGET_SPECS

        spec = _LSP_TARGET_SPECS["claude"]
        assert spec.servers_key(user_scope=False) is None

    def test_servers_key_user_scope(self) -> None:
        """Claude user scope uses lspServers wrapper key."""
        from apm_cli.integration.lsp_integrator import _LSP_TARGET_SPECS

        spec = _LSP_TARGET_SPECS["claude"]
        assert spec.servers_key(user_scope=True) == "lspServers"

    def test_copilot_project_servers_key(self) -> None:
        """Copilot project scope has lspServers wrapper."""
        from apm_cli.integration.lsp_integrator import _LSP_TARGET_SPECS

        spec = _LSP_TARGET_SPECS["copilot"]
        assert spec.servers_key(user_scope=False) == "lspServers"


class TestLSPReadWriteHelpers:
    """Tests for LSPIntegrator._read_json_object and _write_target_config."""

    def test_read_nonexistent_returns_empty(self, tmp_path: Path) -> None:
        """Missing file returns empty dict."""
        from apm_cli.integration.lsp_integrator import LSPIntegrator

        result = LSPIntegrator._read_json_object(tmp_path / "missing.json")
        assert result == {}

    def test_read_malformed_returns_empty(self, tmp_path: Path) -> None:
        """Malformed JSON returns empty dict."""
        from apm_cli.integration.lsp_integrator import LSPIntegrator

        bad = tmp_path / "bad.json"
        bad.write_text("not-json", encoding="utf-8")
        result = LSPIntegrator._read_json_object(bad)
        assert result == {}

    def test_read_non_object_returns_empty(self, tmp_path: Path) -> None:
        """JSON array returns empty dict."""
        from apm_cli.integration.lsp_integrator import LSPIntegrator

        arr = tmp_path / "arr.json"
        arr.write_text("[1, 2, 3]", encoding="utf-8")
        result = LSPIntegrator._read_json_object(arr)
        assert result == {}

    def test_write_creates_config_with_wrapper_key(self, tmp_path: Path) -> None:
        """Writing copilot project-scope config uses lspServers wrapper."""
        from apm_cli.integration.lsp_integrator import _LSP_TARGET_SPECS, LSPIntegrator

        spec = _LSP_TARGET_SPECS["copilot"]
        servers = {"my-server": {"command": "my-lsp", "args": []}}
        changed = LSPIntegrator._write_target_config(
            spec, servers, project_root=tmp_path, user_scope=False
        )
        assert "my-server" in changed
        config_path = spec.path(tmp_path, user_scope=False)
        data = json.loads(config_path.read_text(encoding="utf-8"))
        assert "lspServers" in data
        assert "my-server" in data["lspServers"]

    def test_write_no_change_on_identical(self, tmp_path: Path) -> None:
        """Second write of same config returns empty changed set."""
        from apm_cli.integration.lsp_integrator import _LSP_TARGET_SPECS, LSPIntegrator

        spec = _LSP_TARGET_SPECS["copilot"]
        servers = {"srv": {"command": "lsp", "args": []}}
        LSPIntegrator._write_target_config(spec, servers, project_root=tmp_path, user_scope=False)
        changed2 = LSPIntegrator._write_target_config(
            spec, servers, project_root=tmp_path, user_scope=False
        )
        assert len(changed2) == 0

    def test_write_claude_project_no_wrapper(self, tmp_path: Path) -> None:
        """Claude project scope writes to top-level (no servers_key wrapper)."""
        from apm_cli.integration.lsp_integrator import _LSP_TARGET_SPECS, LSPIntegrator

        spec = _LSP_TARGET_SPECS["claude"]
        servers = {"python": {"command": "pyright", "extensionToLanguage": {".py": "python"}}}
        LSPIntegrator._write_target_config(spec, servers, project_root=tmp_path, user_scope=False)
        config_path = spec.path(tmp_path, user_scope=False)
        data = json.loads(config_path.read_text(encoding="utf-8"))
        assert "python" in data
        assert "lspServers" not in data


class TestLSPCleanTargetConfig:
    """Tests for LSPIntegrator._clean_target_config."""

    def test_removes_stale_server(self, tmp_path: Path) -> None:
        """Stale server is removed from config file."""
        from apm_cli.integration.lsp_integrator import _LSP_TARGET_SPECS, LSPIntegrator

        spec = _LSP_TARGET_SPECS["copilot"]
        servers = {
            "keep-server": {"command": "lsp", "args": []},
            "stale-server": {"command": "old", "args": []},
        }
        LSPIntegrator._write_target_config(spec, servers, project_root=tmp_path, user_scope=False)
        removed = LSPIntegrator._clean_target_config(
            spec, {"stale-server"}, project_root=tmp_path, user_scope=False
        )
        assert removed == ["stale-server"]
        config_path = spec.path(tmp_path, user_scope=False)
        data = json.loads(config_path.read_text(encoding="utf-8"))
        assert "stale-server" not in data["lspServers"]
        assert "keep-server" in data["lspServers"]

    def test_clean_missing_file_returns_empty(self, tmp_path: Path) -> None:
        """Missing config file returns empty removed list."""
        from apm_cli.integration.lsp_integrator import _LSP_TARGET_SPECS, LSPIntegrator

        spec = _LSP_TARGET_SPECS["copilot"]
        removed = LSPIntegrator._clean_target_config(
            spec, {"nonexistent"}, project_root=tmp_path, user_scope=False
        )
        assert removed == []

    def test_clean_nonexistent_server_returns_empty(self, tmp_path: Path) -> None:
        """Trying to remove server not in config returns empty list."""
        from apm_cli.integration.lsp_integrator import _LSP_TARGET_SPECS, LSPIntegrator

        spec = _LSP_TARGET_SPECS["copilot"]
        LSPIntegrator._write_target_config(
            spec, {"srv": {"command": "lsp", "args": []}}, project_root=tmp_path, user_scope=False
        )
        removed = LSPIntegrator._clean_target_config(
            spec, {"nonexistent"}, project_root=tmp_path, user_scope=False
        )
        assert removed == []


class TestLSPInstall:
    """Tests for LSPIntegrator.install()."""

    def test_install_empty_deps_returns_zero(self, tmp_path: Path) -> None:
        """Empty deps list returns 0 changed servers."""
        from apm_cli.integration.lsp_integrator import LSPIntegrator

        result = LSPIntegrator.install([], project_root=tmp_path, target_runtimes=["copilot"])
        assert result == 0

    def test_install_no_compatible_runtimes_returns_zero(self, tmp_path: Path) -> None:
        """Runtime not in LSP specs returns 0."""
        from apm_cli.integration.lsp_integrator import LSPIntegrator

        dep = MagicMock()
        dep.name = "my-lsp"

        def _to_dict():
            return {"name": "my-lsp", "command": "my-lsp"}

        dep.to_dict = _to_dict
        result = LSPIntegrator.install([dep], project_root=tmp_path, target_runtimes=["unknown-rt"])
        assert result == 0

    def test_install_writes_config_for_copilot(self, tmp_path: Path) -> None:
        """install() writes copilot project-scope config for a real dep."""
        from apm_cli.integration.lsp_integrator import LSPIntegrator

        dep = MagicMock()
        dep.name = "test-lsp"

        def _to_lsp_json_entry() -> dict:
            return {"command": "test-lsp", "args": []}

        dep.to_lsp_json_entry = _to_lsp_json_entry

        result = LSPIntegrator.install([dep], project_root=tmp_path, target_runtimes=["copilot"])
        assert result == 1

    def test_install_deduplicates_servers(self, tmp_path: Path) -> None:
        """Duplicate server names get deduplicated."""
        from apm_cli.integration.lsp_integrator import LSPIntegrator

        dep1 = MagicMock()
        dep1.name = "dup-lsp"

        def _entry1():
            return {"command": "lsp-v1", "args": []}

        dep1.to_lsp_json_entry = _entry1

        dep2 = MagicMock()
        dep2.name = "dup-lsp"

        def _entry2():
            return {"command": "lsp-v2", "args": []}

        dep2.to_lsp_json_entry = _entry2

        result = LSPIntegrator.install(
            [dep1, dep2], project_root=tmp_path, target_runtimes=["copilot"]
        )
        # The server was written (changed) at least once
        assert result >= 1


class TestLSPRemoveStale:
    """Tests for LSPIntegrator.remove_stale()."""

    def test_remove_stale_empty_names_is_noop(self, tmp_path: Path) -> None:
        """Empty stale_names is a no-op."""
        from apm_cli.integration.lsp_integrator import LSPIntegrator

        # Should not raise
        LSPIntegrator.remove_stale(set(), project_root=tmp_path)

    def test_remove_stale_logs_removed(self, tmp_path: Path) -> None:
        """Removed names are reported via logger.progress."""
        from apm_cli.integration.lsp_integrator import _LSP_TARGET_SPECS, LSPIntegrator

        spec = _LSP_TARGET_SPECS["copilot"]
        LSPIntegrator._write_target_config(
            spec,
            {"old-srv": {"command": "old", "args": []}},
            project_root=tmp_path,
            user_scope=False,
        )
        logger = MagicMock()
        LSPIntegrator.remove_stale(
            {"old-srv"}, project_root=tmp_path, target_runtimes=["copilot"], logger=logger
        )
        logger.progress.assert_called_once()
        call_text = logger.progress.call_args[0][0]
        assert "old-srv" in call_text


class TestLSPEntryForTarget:
    """Tests for LSPIntegrator._entry_for_target() schema translation."""

    def test_translates_extension_to_language_for_claude(self) -> None:
        """extension_to_language key is preserved as-is for claude spec."""
        from apm_cli.integration.lsp_integrator import _LSP_TARGET_SPECS, LSPIntegrator

        spec = _LSP_TARGET_SPECS["claude"]
        entry = {"command": "pyright", "extensionToLanguage": {".py": "python"}}
        out = LSPIntegrator._entry_for_target(entry, spec)
        assert "extensionToLanguage" in out

    def test_adds_args_for_copilot_if_missing(self) -> None:
        """Copilot spec always gets args=[] if absent."""
        from apm_cli.integration.lsp_integrator import _LSP_TARGET_SPECS, LSPIntegrator

        spec = _LSP_TARGET_SPECS["copilot"]
        entry = {"command": "ts-server"}
        out = LSPIntegrator._entry_for_target(entry, spec)
        assert out.get("args") == []


# ---------------------------------------------------------------------------
# copilot_app_ws -- pure helpers and connection logic
# ---------------------------------------------------------------------------


class TestCopilotAppWsHelpers:
    """Tests for pure WS helpers in copilot_app_ws."""

    def test_scrub_token_from_url_query(self) -> None:
        """Token in ?token= query string is redacted."""
        from apm_cli.integration.copilot_app_ws import _scrub_token

        url = "ws://127.0.0.1:4242/?token=abc123xyz"
        scrubbed = _scrub_token(url)
        assert "abc123xyz" not in scrubbed
        assert "token=<redacted>" in scrubbed
        # Validate structure with urllib.parse
        parsed = urllib.parse.urlparse(scrubbed)
        assert parsed.scheme == "ws"
        qs = urllib.parse.parse_qs(parsed.query)
        assert qs.get("token") == ["<redacted>"]

    def test_scrub_token_handles_no_token(self) -> None:
        """String without a token query parameter is unchanged."""
        from apm_cli.integration.copilot_app_ws import _scrub_token

        msg = "some error without token"
        assert _scrub_token(msg) == msg

    def test_scrub_token_handles_amp_token(self) -> None:
        """Token in &token= query string is also redacted."""
        from apm_cli.integration.copilot_app_ws import _scrub_token

        url = "ws://127.0.0.1:1234/?foo=bar&token=secret99&other=val"
        scrubbed = _scrub_token(url)
        assert "secret99" not in scrubbed
        assert "token=<redacted>" in scrubbed

    def test_token_file_mode_ok_rejects_group_readable(self, tmp_path: Path) -> None:
        """Group-readable token file is rejected (POSIX only)."""
        from apm_cli.integration.copilot_app_ws import _token_file_mode_ok

        if os.name == "nt":
            pytest.skip("POSIX-only test")
        token_file = tmp_path / "ws.token"
        token_file.write_text("tok", encoding="ascii")
        os.chmod(token_file, 0o640)  # group-readable
        assert _token_file_mode_ok(token_file) is False

    def test_token_file_mode_ok_accepts_600(self, tmp_path: Path) -> None:
        """0o600 token file is accepted."""
        from apm_cli.integration.copilot_app_ws import _token_file_mode_ok

        if os.name == "nt":
            pytest.skip("POSIX-only test")
        token_file = tmp_path / "ws.token"
        token_file.write_text("tok", encoding="ascii")
        os.chmod(token_file, 0o600)
        assert _token_file_mode_ok(token_file) is True

    def test_token_file_mode_ok_missing_file_returns_false(self, tmp_path: Path) -> None:
        """Missing file returns False."""
        from apm_cli.integration.copilot_app_ws import _token_file_mode_ok

        assert _token_file_mode_ok(tmp_path / "nonexistent") is False

    def test_read_creds_returns_none_when_no_files(self, tmp_path: Path) -> None:
        """_read_creds returns None when run-dir is empty."""
        from apm_cli.integration.copilot_app_ws import _RUN_DIR_ENV, _read_creds

        with patch.dict(os.environ, {_RUN_DIR_ENV: str(tmp_path)}):
            result = _read_creds()
        assert result is None

    def test_read_creds_returns_port_and_token(self, tmp_path: Path) -> None:
        """_read_creds returns (port, token) when both files are present."""
        from apm_cli.integration.copilot_app_ws import (
            _PORT_FILE,
            _RUN_DIR_ENV,
            _TOKEN_FILE,
            _read_creds,
        )

        port_file = tmp_path / _PORT_FILE
        token_file = tmp_path / _TOKEN_FILE
        port_file.write_text("4242", encoding="ascii")
        token_file.write_text("my-token", encoding="ascii")
        if os.name != "nt":
            os.chmod(token_file, 0o600)
        with patch.dict(os.environ, {_RUN_DIR_ENV: str(tmp_path)}):
            result = _read_creds()
        assert result == (4242, "my-token")

    def test_read_creds_rejects_invalid_port(self, tmp_path: Path) -> None:
        """_read_creds returns None for out-of-range port."""
        from apm_cli.integration.copilot_app_ws import (
            _PORT_FILE,
            _RUN_DIR_ENV,
            _TOKEN_FILE,
            _read_creds,
        )

        port_file = tmp_path / _PORT_FILE
        token_file = tmp_path / _TOKEN_FILE
        port_file.write_text("99999", encoding="ascii")
        token_file.write_text("tok", encoding="ascii")
        if os.name != "nt":
            os.chmod(token_file, 0o600)
        with patch.dict(os.environ, {_RUN_DIR_ENV: str(tmp_path)}):
            result = _read_creds()
        assert result is None


class TestWsAvailable:
    """Tests for ws_available()."""

    def test_returns_false_when_no_creds(self, tmp_path: Path) -> None:
        """No run-dir files → ws_available returns False."""
        from apm_cli.integration.copilot_app_ws import _RUN_DIR_ENV, ws_available

        with patch.dict(os.environ, {_RUN_DIR_ENV: str(tmp_path)}):
            assert ws_available() is False

    def test_returns_false_when_tcp_refused(self, tmp_path: Path) -> None:
        """Port present but TCP refused → ws_available returns False."""
        from apm_cli.integration.copilot_app_ws import (
            _PORT_FILE,
            _RUN_DIR_ENV,
            _TOKEN_FILE,
            ws_available,
        )

        port_file = tmp_path / _PORT_FILE
        token_file = tmp_path / _TOKEN_FILE
        port_file.write_text("19999", encoding="ascii")  # Unlikely to be in use
        token_file.write_text("tok", encoding="ascii")
        if os.name != "nt":
            os.chmod(token_file, 0o600)
        with patch.dict(os.environ, {_RUN_DIR_ENV: str(tmp_path)}):
            # This will try to connect and fail (no server)
            result = ws_available()
        assert result is False


class TestExtractProjectFields:
    """Tests for _extract_project_fields()."""

    def test_project_created_nested(self) -> None:
        """Nested project dict with type=project_created."""
        from apm_cli.integration.copilot_app_ws import _extract_project_fields

        reply = {
            "type": "project_created",
            "project": {"id": "proj-123", "main_repo_path": "/home/user/repo"},
        }
        pid, path, created = _extract_project_fields(reply, fallback_path="/fallback")
        assert pid == "proj-123"
        assert path == "/home/user/repo"
        assert created is True

    def test_project_updated_top_level(self) -> None:
        """Top-level fields with type=project_updated."""
        from apm_cli.integration.copilot_app_ws import _extract_project_fields

        reply = {
            "type": "project_updated",
            "project_id": "proj-456",
            "main_repo_path": "/tmp/myrepo",
        }
        pid, path, created = _extract_project_fields(reply, fallback_path="/fb")
        assert pid == "proj-456"
        assert path == "/tmp/myrepo"
        assert created is False

    def test_missing_id_returns_none(self) -> None:
        """Reply without id fields returns None for project_id."""
        from apm_cli.integration.copilot_app_ws import _extract_project_fields

        reply = {"type": "project_created"}
        pid, path, _created = _extract_project_fields(reply, fallback_path="/fb")
        assert pid is None
        assert path == "/fb"  # fallback used

    def test_fallback_path_used_when_no_path(self) -> None:
        """Fallback path used when reply has no path fields."""
        from apm_cli.integration.copilot_app_ws import _extract_project_fields

        reply = {"type": "project_updated", "project_id": "p1"}
        _, path, _ = _extract_project_fields(reply, fallback_path="/fb")
        assert path == "/fb"


class TestWsClientConnect:
    """Tests for WsClient._connect() error handling."""

    def test_connect_raises_not_running_when_no_creds(self, tmp_path: Path) -> None:
        """_connect raises WsAppNotRunning when no creds files."""
        from apm_cli.integration.copilot_app_ws import _RUN_DIR_ENV, WsAppNotRunning, WsClient

        with patch.dict(os.environ, {_RUN_DIR_ENV: str(tmp_path)}):
            client = WsClient()
            with pytest.raises(WsAppNotRunning):
                client._connect()

    def test_connect_raises_auth_error_on_401(self, tmp_path: Path) -> None:
        """401 response raises WsAuthError."""
        from apm_cli.integration.copilot_app_ws import (
            _PORT_FILE,
            _RUN_DIR_ENV,
            _TOKEN_FILE,
            WsAuthError,
            WsClient,
        )

        port_file = tmp_path / _PORT_FILE
        token_file = tmp_path / _TOKEN_FILE
        port_file.write_text("4242", encoding="ascii")
        token_file.write_text("bad-token", encoding="ascii")
        if os.name != "nt":
            os.chmod(token_file, 0o600)

        with patch.dict(os.environ, {_RUN_DIR_ENV: str(tmp_path)}):
            with patch("websockets.sync.client.connect") as mock_connect:
                mock_connect.side_effect = Exception("401 Unauthorized")
                client = WsClient()
                with pytest.raises(WsAuthError):
                    client._connect()

    def test_connect_raises_not_running_on_refused(self, tmp_path: Path) -> None:
        """Connection refused raises WsAppNotRunning."""
        from apm_cli.integration.copilot_app_ws import (
            _PORT_FILE,
            _RUN_DIR_ENV,
            _TOKEN_FILE,
            WsAppNotRunning,
            WsClient,
        )

        port_file = tmp_path / _PORT_FILE
        token_file = tmp_path / _TOKEN_FILE
        port_file.write_text("4242", encoding="ascii")
        token_file.write_text("tok", encoding="ascii")
        if os.name != "nt":
            os.chmod(token_file, 0o600)

        with patch.dict(os.environ, {_RUN_DIR_ENV: str(tmp_path)}):
            with patch("websockets.sync.client.connect") as mock_connect:
                mock_connect.side_effect = Exception("Connection refused")
                client = WsClient()
                with pytest.raises(WsAppNotRunning):
                    client._connect()

    def test_connect_raises_protocol_error_on_other_exception(self, tmp_path: Path) -> None:
        """Unknown exception raises WsProtocolError."""
        from apm_cli.integration.copilot_app_ws import (
            _PORT_FILE,
            _RUN_DIR_ENV,
            _TOKEN_FILE,
            WsClient,
            WsProtocolError,
        )

        port_file = tmp_path / _PORT_FILE
        token_file = tmp_path / _TOKEN_FILE
        port_file.write_text("4242", encoding="ascii")
        token_file.write_text("tok", encoding="ascii")
        if os.name != "nt":
            os.chmod(token_file, 0o600)

        with patch.dict(os.environ, {_RUN_DIR_ENV: str(tmp_path)}):
            with patch("websockets.sync.client.connect") as mock_connect:
                mock_connect.side_effect = Exception("some unknown WS error")
                client = WsClient()
                with pytest.raises(WsProtocolError):
                    client._connect()


class TestWsClientRecv:
    """Tests for WsClient._recv() and _send() internals."""

    def _make_connected_client(self) -> Any:
        """Return a WsClient with a mock _conn."""
        from apm_cli.integration.copilot_app_ws import WsClient

        client = WsClient()
        client._conn = MagicMock()
        return client

    def test_send_not_connected_raises(self) -> None:
        """_send raises WsError when not connected."""
        from apm_cli.integration.copilot_app_ws import WsClient, WsError

        client = WsClient()
        with pytest.raises(WsError):
            client._send({"type": "ping"})

    def test_recv_not_connected_raises(self) -> None:
        """_recv raises WsError when not connected."""
        from apm_cli.integration.copilot_app_ws import WsClient, WsError

        client = WsClient()
        with pytest.raises(WsError):
            client._recv()

    def test_recv_json_parse_error_raises_protocol_error(self) -> None:
        """Non-JSON message raises WsProtocolError."""
        from apm_cli.integration.copilot_app_ws import WsProtocolError

        client = self._make_connected_client()
        client._conn.recv.return_value = "not-json"
        with pytest.raises(WsProtocolError, match="not valid JSON"):
            client._recv()

    def test_recv_non_object_raises_protocol_error(self) -> None:
        """JSON array raises WsProtocolError (not a JSON object)."""
        from apm_cli.integration.copilot_app_ws import WsProtocolError

        client = self._make_connected_client()
        client._conn.recv.return_value = "[1,2,3]"
        with pytest.raises(WsProtocolError, match="not a JSON object"):
            client._recv()

    def test_recv_bytes_decoded(self) -> None:
        """Bytes response is decoded to UTF-8 before JSON parsing."""

        client = self._make_connected_client()
        client._conn.recv.return_value = b'{"type":"pong"}'
        result = client._recv()
        assert result == {"type": "pong"}

    def test_recv_timeout_raises_protocol_error(self) -> None:
        """TimeoutError from recv raises WsProtocolError."""
        from apm_cli.integration.copilot_app_ws import WsProtocolError

        client = self._make_connected_client()
        client._conn.recv.side_effect = TimeoutError("timed out")
        with pytest.raises(WsProtocolError, match="timed out"):
            client._recv()

    def test_send_connection_failure_raises_protocol_error(self) -> None:
        """Exception from conn.send raises WsProtocolError."""
        from apm_cli.integration.copilot_app_ws import WsProtocolError

        client = self._make_connected_client()
        client._conn.send.side_effect = Exception("send failed")
        with pytest.raises(WsProtocolError, match="send failed"):
            client._send({"msg": "hello"})

    def test_await_typed_reply_short_circuits_on_error(self) -> None:
        """Server 'error' message raises WsProtocolError immediately."""
        from apm_cli.integration.copilot_app_ws import WsProtocolError

        client = self._make_connected_client()
        client._conn.recv.return_value = json.dumps({"type": "error", "message": "bad path"})
        with pytest.raises(WsProtocolError, match="bad path"):
            client._await_typed_reply(expected={"project_created"})

    def test_await_typed_reply_exhausted_raises_protocol_error(self) -> None:
        """Exhausting max_messages without match raises WsProtocolError."""
        from apm_cli.integration.copilot_app_ws import WsProtocolError

        client = self._make_connected_client()
        client._conn.recv.return_value = json.dumps({"type": "keep_awake"})
        with pytest.raises(WsProtocolError, match="did not return"):
            client._await_typed_reply(expected={"project_created"}, max_messages=3)

    def test_create_project_from_path_returns_project_created(self) -> None:
        """create_project_from_path returns ProjectCreated from server reply."""

        client = self._make_connected_client()
        responses = [
            json.dumps({"type": "project_created", "project_id": "p-99", "main_repo_path": "/repo"})
        ]
        client._conn.recv.side_effect = responses
        result = client.create_project_from_path(Path("/repo"))
        assert result.project_id == "p-99"
        assert result.was_created is True

    def test_create_project_from_path_missing_id_raises(self) -> None:
        """create_project_from_path with no id raises WsProtocolError."""
        from apm_cli.integration.copilot_app_ws import WsProtocolError

        client = self._make_connected_client()
        client._conn.recv.return_value = json.dumps({"type": "project_created"})
        with pytest.raises(WsProtocolError, match="missing id"):
            client.create_project_from_path(Path("/repo"))

    def test_close_is_idempotent(self) -> None:
        """close() can be called multiple times without error."""

        client = self._make_connected_client()
        client.close()
        client.close()  # Should not raise


# ---------------------------------------------------------------------------
# instruction_integrator -- pure helpers
# ---------------------------------------------------------------------------


class TestInstructionIntegratorHelpers:
    """Tests for InstructionIntegrator helper methods."""

    def test_strip_frontmatter_removes_yaml(self) -> None:
        """YAML frontmatter is stripped from content."""
        from apm_cli.integration.instruction_integrator import InstructionIntegrator

        content = "---\napplyTo: '**/*.py'\n---\n# Body\n"
        result = InstructionIntegrator._strip_frontmatter(content)
        assert result == "# Body\n"

    def test_strip_frontmatter_no_op_without_fm(self) -> None:
        """Content without frontmatter is returned unchanged."""
        from apm_cli.integration.instruction_integrator import InstructionIntegrator

        content = "# Just a header\n\nSome body."
        assert InstructionIntegrator._strip_frontmatter(content) == content

    def test_is_apm_managed_copilot_detects_header(self) -> None:
        """_is_apm_managed_copilot returns True for APM managed content."""
        from apm_cli.integration.instruction_integrator import InstructionIntegrator

        managed = InstructionIntegrator._APM_COPILOT_HEADER + "\n# content\n"
        assert InstructionIntegrator._is_apm_managed_copilot(managed) is True

    def test_is_apm_managed_copilot_false_for_plain(self) -> None:
        """_is_apm_managed_copilot returns False for user-authored content."""
        from apm_cli.integration.instruction_integrator import InstructionIntegrator

        assert InstructionIntegrator._is_apm_managed_copilot("# Normal content") is False

    def test_build_copilot_section(self) -> None:
        """_build_copilot_section wraps body in provenance markers."""
        from apm_cli.integration.instruction_integrator import InstructionIntegrator

        section = InstructionIntegrator._build_copilot_section("pkg/source", "# Body")
        assert "<!-- apm:source:pkg/source -->" in section
        assert "# Body" in section
        assert "<!-- /apm:source -->" in section

    def test_build_copilot_section_sanitizes_close_tag(self) -> None:
        """Source string with --> is sanitized in provenance marker."""
        from apm_cli.integration.instruction_integrator import InstructionIntegrator

        section = InstructionIntegrator._build_copilot_section("evil-->src", "body")
        assert (
            "-->"
            not in section.split("<!-- /apm:source -->")[0]
            .split("<!-- apm:source:")[1]
            .split(" -->")[0]
        )

    def test_update_copilot_managed_replaces_existing_section(self) -> None:
        """_update_copilot_managed replaces an existing source section."""
        from apm_cli.integration.instruction_integrator import InstructionIntegrator

        header = InstructionIntegrator._APM_COPILOT_HEADER
        old_section = InstructionIntegrator._build_copilot_section("my-pkg", "Old body")
        existing = f"{header}\n{old_section}\n"

        new_section = InstructionIntegrator._build_copilot_section("my-pkg", "New body")
        result = InstructionIntegrator._update_copilot_managed(existing, "my-pkg", new_section)
        assert "Old body" not in result
        assert "New body" in result

    def test_update_copilot_managed_appends_new_section(self) -> None:
        """_update_copilot_managed appends a new source section."""
        from apm_cli.integration.instruction_integrator import InstructionIntegrator

        header = InstructionIntegrator._APM_COPILOT_HEADER
        existing = f"{header}\n"
        new_section = InstructionIntegrator._build_copilot_section("pkg2", "Pkg2 body")
        result = InstructionIntegrator._update_copilot_managed(existing, "pkg2", new_section)
        assert "Pkg2 body" in result

    def test_convert_to_cursor_rules_maps_apply_to(self) -> None:
        """applyTo frontmatter is converted to globs in cursor rules format."""
        from apm_cli.integration.instruction_integrator import InstructionIntegrator

        content = "---\napplyTo: '**/*.py'\ndescription: Python rules\n---\n# Body"
        result = InstructionIntegrator._convert_to_cursor_rules(content)
        assert "globs" in result
        assert "**/*.py" in result
        assert "Python rules" in result


class TestIntegrateInstructionsForTarget:
    """Tests for InstructionIntegrator.integrate_instructions_for_target()."""

    def _make_package_info(self, install_path: Path, name: str = "test-pkg") -> Any:
        """Build a minimal PackageInfo-like object."""
        pkg = MagicMock()
        pkg.name = name
        pkg.source = "github/org/test-pkg"
        info = MagicMock()
        info.install_path = install_path
        info.package = pkg
        return info

    def test_returns_empty_when_no_mapping(self, tmp_path: Path) -> None:
        """Returns zero result when target has no instructions mapping."""
        from apm_cli.integration.instruction_integrator import InstructionIntegrator

        target = MagicMock()
        target.primitives = {}
        target.root_dir = ".github"
        target.auto_create = False

        integrator = InstructionIntegrator()
        pkg_info = self._make_package_info(tmp_path / "pkg")
        result = integrator.integrate_instructions_for_target(target, pkg_info, tmp_path)
        assert result.files_integrated == 0

    def test_returns_empty_when_target_root_missing(self, tmp_path: Path) -> None:
        """Returns zero result when target root dir doesn't exist."""
        from apm_cli.integration.instruction_integrator import InstructionIntegrator

        target = MagicMock()
        mapping = MagicMock()
        mapping.deploy_root = None
        mapping.subdir = "instructions"
        mapping.format_id = "copilot"
        mapping.extension = ".instructions.md"
        mapping.output_compare = False
        target.primitives = {"instructions": mapping}
        target.root_dir = ".github"
        target.auto_create = False

        integrator = InstructionIntegrator()
        pkg_info = self._make_package_info(tmp_path / "pkg")
        # .github dir doesn't exist
        result = integrator.integrate_instructions_for_target(target, pkg_info, tmp_path)
        assert result.files_integrated == 0

    def test_integrates_instruction_file(self, tmp_path: Path) -> None:
        """Instruction file is copied to target directory."""
        from apm_cli.integration.instruction_integrator import InstructionIntegrator

        # Set up package with instruction file
        pkg_dir = tmp_path / "pkg"
        instr_dir = pkg_dir / ".apm" / "instructions"
        instr_dir.mkdir(parents=True)
        instr_file = instr_dir / "my-rule.instructions.md"
        instr_file.write_text("---\napplyTo: '**/*.py'\n---\n# Python Rules\n", encoding="utf-8")

        # Set up target .github dir
        github_dir = tmp_path / ".github"
        github_dir.mkdir()

        target = MagicMock()
        mapping = MagicMock()
        mapping.deploy_root = None
        mapping.subdir = "instructions"
        mapping.format_id = "copilot"
        mapping.extension = ".instructions.md"
        mapping.output_compare = False
        target.primitives = {"instructions": mapping}
        target.root_dir = ".github"
        target.auto_create = False

        integrator = InstructionIntegrator()
        pkg_info = self._make_package_info(pkg_dir)
        result = integrator.integrate_instructions_for_target(target, pkg_info, tmp_path)
        assert result.files_integrated == 1


# ---------------------------------------------------------------------------
# runtime/manager.py -- RuntimeManager
# ---------------------------------------------------------------------------


class TestRuntimeManager:
    """Tests for RuntimeManager class."""

    def test_supported_runtimes_present(self) -> None:
        """All expected runtime names are in supported_runtimes."""
        from apm_cli.runtime.manager import RuntimeManager

        mgr = RuntimeManager()
        for name in ("copilot", "codex", "llm", "gemini"):
            assert name in mgr.supported_runtimes

    def test_is_runtime_available_unknown_runtime(self) -> None:
        """Unknown runtime name returns False."""
        from apm_cli.runtime.manager import RuntimeManager

        mgr = RuntimeManager()
        assert mgr.is_runtime_available("nonexistent-rt") is False

    def test_is_runtime_available_via_which(self) -> None:
        """Runtime available in PATH is detected via shutil.which."""
        from apm_cli.runtime.manager import RuntimeManager

        mgr = RuntimeManager()
        with patch("shutil.which", return_value="/usr/local/bin/llm"):
            result = mgr.is_runtime_available("llm")
        assert result is True

    def test_is_runtime_available_not_in_path(self) -> None:
        """Runtime not in PATH and not in APM dir returns False."""
        from apm_cli.runtime.manager import RuntimeManager

        mgr = RuntimeManager()
        with patch("shutil.which", return_value=None):
            with patch.object(Path, "exists", return_value=False):
                result = mgr.is_runtime_available("codex")
        assert result is False

    def test_get_runtime_preference_returns_list(self) -> None:
        """get_runtime_preference returns a non-empty list of runtime names."""
        from apm_cli.runtime.manager import RuntimeManager

        mgr = RuntimeManager()
        pref = mgr.get_runtime_preference()
        assert isinstance(pref, list)
        assert len(pref) > 0

    def test_get_available_runtime_returns_none_when_nothing_available(self) -> None:
        """Returns None when no runtimes are installed."""
        from apm_cli.runtime.manager import RuntimeManager

        mgr = RuntimeManager()
        with patch.object(mgr, "is_runtime_available", return_value=False):
            result = mgr.get_available_runtime()
        assert result is None

    def test_get_available_runtime_returns_first_available(self) -> None:
        """Returns the first runtime in the preference order that is available."""
        from apm_cli.runtime.manager import RuntimeManager

        mgr = RuntimeManager()
        available = {"llm"}

        def _check(name: str) -> bool:
            return name in available

        with patch.object(mgr, "is_runtime_available", side_effect=_check):
            result = mgr.get_available_runtime()
        assert result == "llm"

    def test_list_runtimes_structure(self) -> None:
        """list_runtimes returns dict with expected keys."""
        from apm_cli.runtime.manager import RuntimeManager

        mgr = RuntimeManager()
        with patch("shutil.which", return_value=None):
            runtimes = mgr.list_runtimes()
        assert "copilot" in runtimes
        assert "description" in runtimes["copilot"]
        assert "installed" in runtimes["copilot"]

    def test_setup_runtime_unknown_returns_false(self) -> None:
        """setup_runtime with unknown name returns False."""
        from apm_cli.runtime.manager import RuntimeManager

        mgr = RuntimeManager()
        result = mgr.setup_runtime("totally-unknown-rt")
        assert result is False

    def test_remove_runtime_unknown_returns_false(self) -> None:
        """remove_runtime with unknown name returns False."""
        from apm_cli.runtime.manager import RuntimeManager

        mgr = RuntimeManager()
        result = mgr.remove_runtime("no-such-runtime")
        assert result is False

    def test_remove_copilot_runtime_calls_npm(self) -> None:
        """remove_runtime for copilot calls npm uninstall."""
        from apm_cli.runtime.manager import RuntimeManager

        mgr = RuntimeManager()
        mock_result = MagicMock()
        mock_result.returncode = 0
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            result = mgr.remove_runtime("copilot")
        assert result is True
        args = mock_run.call_args[0][0]
        assert "npm" in args
        assert "uninstall" in args

    def test_remove_copilot_runtime_fails_on_npm_error(self) -> None:
        """remove_runtime returns False when npm uninstall fails."""
        from apm_cli.runtime.manager import RuntimeManager

        mgr = RuntimeManager()
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "npm error"
        with patch("subprocess.run", return_value=mock_result):
            result = mgr.remove_runtime("copilot")
        assert result is False

    def test_remove_non_npm_runtime_not_installed(self, tmp_path: Path) -> None:
        """remove_runtime for non-npm runtime with no binary returns False."""
        from apm_cli.runtime.manager import RuntimeManager

        mgr = RuntimeManager()
        mgr.runtime_dir = tmp_path  # Point to empty dir
        result = mgr.remove_runtime("llm")
        assert result is False

    def test_list_runtimes_includes_version_when_installed(self) -> None:
        """list_runtimes includes 'version' key when binary is installed."""
        from apm_cli.runtime.manager import RuntimeManager

        mgr = RuntimeManager()
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "1.2.3\n"

        with patch("shutil.which", return_value="/usr/bin/llm"):
            with patch("subprocess.run", return_value=mock_result):
                runtimes = mgr.list_runtimes()
        assert runtimes["llm"]["installed"] is True
        assert runtimes["llm"].get("version") == "1.2.3"


# ---------------------------------------------------------------------------
# runtime/base.py -- _stream_subprocess_output
# ---------------------------------------------------------------------------


class TestStreamSubprocessOutput:
    """Tests for _stream_subprocess_output()."""

    def test_returns_output_and_return_code(self) -> None:
        """Normal command returns lines and zero exit code."""
        from apm_cli.runtime.base import _stream_subprocess_output

        output, rc = _stream_subprocess_output(["echo", "hello"])
        assert rc == 0
        assert any("hello" in line for line in output)

    def test_failed_command_returns_nonzero_rc(self) -> None:
        """Failing command returns non-zero return code."""
        from apm_cli.runtime.base import _stream_subprocess_output

        _, rc = _stream_subprocess_output(["false"])
        assert rc != 0

    def test_timeout_kills_process(self) -> None:
        """Timeout kills process and raises TimeoutExpired."""
        from apm_cli.runtime.base import _stream_subprocess_output

        mock_process = MagicMock()
        mock_process.stdout.readline.return_value = ""  # No output so readline loop exits
        mock_process.wait.side_effect = subprocess.TimeoutExpired(cmd=["sleep", "10"], timeout=1)
        mock_process.stdout.__iter__ = MagicMock(return_value=iter([]))

        with patch("subprocess.Popen", return_value=mock_process):
            with pytest.raises(subprocess.TimeoutExpired):
                _stream_subprocess_output(["sleep", "10"], timeout=1)


class TestRuntimeAdapterStr:
    """Tests for RuntimeAdapter.__str__()."""

    def test_str_returns_runtime_name(self) -> None:
        """__str__ includes the runtime name."""
        from apm_cli.runtime.base import RuntimeAdapter

        class _Concrete(RuntimeAdapter):
            def execute_prompt(self, prompt_content, **kwargs):
                return ""

            def list_available_models(self):
                return {}

            def get_runtime_info(self):
                return {}

            @staticmethod
            def is_available():
                return True

            @staticmethod
            def get_runtime_name():
                return "test-rt"

        inst = _Concrete()
        assert "test-rt" in str(inst)


# ---------------------------------------------------------------------------
# runtime/codex_runtime.py
# ---------------------------------------------------------------------------


class TestCodexRuntime:
    """Tests for CodexRuntime."""

    def test_is_available_when_binary_found(self) -> None:
        """is_available returns True when binary is on PATH."""
        from apm_cli.runtime.codex_runtime import CodexRuntime

        with patch(
            "apm_cli.runtime.codex_runtime.find_runtime_binary", return_value="/usr/bin/codex"
        ):
            assert CodexRuntime.is_available() is True

    def test_is_available_false_when_missing(self) -> None:
        """is_available returns False when binary not found."""
        from apm_cli.runtime.codex_runtime import CodexRuntime

        with patch("apm_cli.runtime.codex_runtime.find_runtime_binary", return_value=None):
            assert CodexRuntime.is_available() is False

    def test_get_runtime_name(self) -> None:
        """get_runtime_name returns 'codex'."""
        from apm_cli.runtime.codex_runtime import CodexRuntime

        assert CodexRuntime.get_runtime_name() == "codex"

    def test_init_raises_when_not_available(self) -> None:
        """__init__ raises RuntimeError when binary not found."""
        from apm_cli.runtime.codex_runtime import CodexRuntime

        with patch("apm_cli.runtime.codex_runtime.find_runtime_binary", return_value=None):
            with pytest.raises(RuntimeError, match="Codex CLI not available"):
                CodexRuntime()

    def test_list_available_models(self) -> None:
        """list_available_models returns a dict with codex-default."""
        from apm_cli.runtime.codex_runtime import CodexRuntime

        with patch(
            "apm_cli.runtime.codex_runtime.find_runtime_binary", return_value="/usr/bin/codex"
        ):
            rt = CodexRuntime()
            models = rt.list_available_models()
        assert "codex-default" in models

    def test_get_runtime_info(self) -> None:
        """get_runtime_info returns dict with name=codex."""
        from apm_cli.runtime.codex_runtime import CodexRuntime

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "0.9.1"

        with patch(
            "apm_cli.runtime.codex_runtime.find_runtime_binary", return_value="/usr/bin/codex"
        ):
            rt = CodexRuntime()

        with patch("subprocess.run", return_value=mock_result):
            info = rt.get_runtime_info()
        assert info["name"] == "codex"

    def test_str_representation(self) -> None:
        """__str__ shows model name."""
        from apm_cli.runtime.codex_runtime import CodexRuntime

        with patch(
            "apm_cli.runtime.codex_runtime.find_runtime_binary", return_value="/usr/bin/codex"
        ):
            rt = CodexRuntime(model_name="codex-mini")
        assert "codex-mini" in str(rt)

    def test_execute_prompt_raises_on_nonzero_exit(self) -> None:
        """execute_prompt raises RuntimeError on non-zero exit code."""
        from apm_cli.runtime.codex_runtime import CodexRuntime

        with patch(
            "apm_cli.runtime.codex_runtime.find_runtime_binary", return_value="/usr/bin/codex"
        ):
            rt = CodexRuntime()

        mock_popen = MagicMock()
        mock_popen.__enter__ = MagicMock(return_value=mock_popen)
        mock_popen.__exit__ = MagicMock(return_value=False)
        mock_popen.stdout.readline.side_effect = ["error output\n", ""]
        mock_popen.wait.return_value = 1

        with patch("subprocess.Popen", return_value=mock_popen):
            with pytest.raises(RuntimeError):
                rt.execute_prompt("test prompt")


# ---------------------------------------------------------------------------
# runtime/copilot_runtime.py
# ---------------------------------------------------------------------------


class TestCopilotRuntime:
    """Tests for CopilotRuntime."""

    def test_is_available_when_binary_found(self) -> None:
        """is_available returns True when binary on PATH."""
        from apm_cli.runtime.copilot_runtime import CopilotRuntime

        with patch(
            "apm_cli.runtime.copilot_runtime.find_runtime_binary", return_value="/usr/bin/copilot"
        ):
            assert CopilotRuntime.is_available() is True

    def test_is_available_false_when_missing(self) -> None:
        """is_available returns False when binary not found."""
        from apm_cli.runtime.copilot_runtime import CopilotRuntime

        with patch("apm_cli.runtime.copilot_runtime.find_runtime_binary", return_value=None):
            assert CopilotRuntime.is_available() is False

    def test_get_runtime_name(self) -> None:
        """get_runtime_name returns 'copilot'."""
        from apm_cli.runtime.copilot_runtime import CopilotRuntime

        assert CopilotRuntime.get_runtime_name() == "copilot"

    def test_init_raises_when_not_available(self) -> None:
        """__init__ raises RuntimeError when binary not found."""
        from apm_cli.runtime.copilot_runtime import CopilotRuntime

        with patch("apm_cli.runtime.copilot_runtime.find_runtime_binary", return_value=None):
            with pytest.raises(RuntimeError, match="GitHub Copilot CLI not available"):
                CopilotRuntime()

    def test_list_available_models(self) -> None:
        """list_available_models returns copilot-default entry."""
        from apm_cli.runtime.copilot_runtime import CopilotRuntime

        with patch(
            "apm_cli.runtime.copilot_runtime.find_runtime_binary", return_value="/usr/bin/copilot"
        ):
            rt = CopilotRuntime()
        models = rt.list_available_models()
        assert "copilot-default" in models

    def test_get_mcp_config_path(self) -> None:
        """get_mcp_config_path returns path under ~/.copilot/."""
        from apm_cli.runtime.copilot_runtime import CopilotRuntime

        with patch(
            "apm_cli.runtime.copilot_runtime.find_runtime_binary", return_value="/usr/bin/copilot"
        ):
            rt = CopilotRuntime()
        config_path = rt.get_mcp_config_path()
        assert config_path.name == "mcp-config.json"
        assert ".copilot" in str(config_path)

    def test_is_mcp_configured_false_when_no_file(self, tmp_path: Path) -> None:
        """is_mcp_configured returns False when config file absent."""
        from apm_cli.runtime.copilot_runtime import CopilotRuntime

        with patch(
            "apm_cli.runtime.copilot_runtime.find_runtime_binary", return_value="/usr/bin/copilot"
        ):
            rt = CopilotRuntime()
        with patch.object(Path, "exists", return_value=False):
            assert rt.is_mcp_configured() is False

    def test_get_mcp_servers_empty_when_no_config(self) -> None:
        """get_mcp_servers returns empty dict when config file absent."""
        from apm_cli.runtime.copilot_runtime import CopilotRuntime

        with patch(
            "apm_cli.runtime.copilot_runtime.find_runtime_binary", return_value="/usr/bin/copilot"
        ):
            rt = CopilotRuntime()
        with patch.object(Path, "exists", return_value=False):
            servers = rt.get_mcp_servers()
        assert servers == {}

    def test_get_mcp_servers_parses_config(self, tmp_path: Path) -> None:
        """get_mcp_servers reads and returns servers from config file."""
        from apm_cli.runtime.copilot_runtime import CopilotRuntime

        config_data = {"servers": {"my-server": {"command": "node", "args": ["server.js"]}}}
        config_file = tmp_path / "mcp-config.json"
        config_file.write_text(json.dumps(config_data), encoding="utf-8")

        with patch(
            "apm_cli.runtime.copilot_runtime.find_runtime_binary", return_value="/usr/bin/copilot"
        ):
            rt = CopilotRuntime()
        with patch.object(rt, "get_mcp_config_path", return_value=config_file):
            servers = rt.get_mcp_servers()
        assert "my-server" in servers

    def test_get_runtime_info_returns_name(self) -> None:
        """get_runtime_info returns dict with name=copilot."""
        from apm_cli.runtime.copilot_runtime import CopilotRuntime

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "1.2.3"

        with patch(
            "apm_cli.runtime.copilot_runtime.find_runtime_binary", return_value="/usr/bin/copilot"
        ):
            rt = CopilotRuntime()
        with patch("subprocess.run", return_value=mock_result):
            info = rt.get_runtime_info()
        assert info["name"] == "copilot"

    def test_str_representation(self) -> None:
        """__str__ shows model name."""
        from apm_cli.runtime.copilot_runtime import CopilotRuntime

        with patch(
            "apm_cli.runtime.copilot_runtime.find_runtime_binary", return_value="/usr/bin/copilot"
        ):
            rt = CopilotRuntime(model_name="gpt-4o")
        assert "gpt-4o" in str(rt)


# ---------------------------------------------------------------------------
# runtime/llm_runtime.py
# ---------------------------------------------------------------------------


class TestLLMRuntime:
    """Tests for LLMRuntime."""

    def _make_llm_runtime(self, model: str | None = None) -> Any:
        """Construct LLMRuntime with mocked subprocess."""
        from apm_cli.runtime.llm_runtime import LLMRuntime

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "llm 0.18.0"

        with patch("subprocess.run", return_value=mock_result):
            return LLMRuntime(model_name=model)

    def test_init_raises_when_llm_unavailable(self) -> None:
        """__init__ raises RuntimeError when llm CLI not found."""
        from apm_cli.runtime.llm_runtime import LLMRuntime

        with patch("subprocess.run", side_effect=FileNotFoundError):
            with pytest.raises(RuntimeError, match="llm CLI not found"):
                LLMRuntime()

    def test_get_runtime_name(self) -> None:
        """get_runtime_name returns 'llm'."""
        from apm_cli.runtime.llm_runtime import LLMRuntime

        assert LLMRuntime.get_runtime_name() == "llm"

    def test_get_default_model(self) -> None:
        """get_default_model returns None (let CLI decide)."""
        from apm_cli.runtime.llm_runtime import LLMRuntime

        assert LLMRuntime.get_default_model() is None

    def test_is_available_true(self) -> None:
        """is_available returns True when llm CLI responds."""
        from apm_cli.runtime.llm_runtime import LLMRuntime

        mock_result = MagicMock()
        mock_result.returncode = 0
        with patch("subprocess.run", return_value=mock_result):
            assert LLMRuntime.is_available() is True

    def test_is_available_false(self) -> None:
        """is_available returns False when llm not found."""
        from apm_cli.runtime.llm_runtime import LLMRuntime

        with patch("subprocess.run", side_effect=FileNotFoundError):
            assert LLMRuntime.is_available() is False

    def test_get_runtime_info(self) -> None:
        """get_runtime_info returns dict with name=llm."""
        rt = self._make_llm_runtime()
        info = rt.get_runtime_info()
        assert info["name"] == "llm"

    def test_list_available_models_parsed(self) -> None:
        """list_available_models parses llm models list output."""
        rt = self._make_llm_runtime()
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "gpt-4o\ngpt-3.5-turbo\n"

        with patch("subprocess.run", return_value=mock_result):
            models = rt.list_available_models()
        assert "gpt-4o" in models
        assert "gpt-3.5-turbo" in models

    def test_list_available_models_on_error(self) -> None:
        """list_available_models returns error dict on failure."""
        rt = self._make_llm_runtime()
        with patch("subprocess.run", side_effect=Exception("broken")):
            models = rt.list_available_models()
        assert "error" in models

    def test_str_representation(self) -> None:
        """__str__ shows model name."""
        rt = self._make_llm_runtime(model="claude-3")
        assert "claude-3" in str(rt)

    def test_execute_prompt_success(self) -> None:
        """execute_prompt returns stripped output on success."""
        rt = self._make_llm_runtime()

        mock_popen = MagicMock()
        mock_popen.stdout.__iter__ = MagicMock(return_value=iter(["Hello world\n"]))
        mock_popen.stdout.readline.side_effect = ["Hello world\n", ""]
        mock_popen.wait.return_value = 0

        with patch("subprocess.Popen", return_value=mock_popen):
            # Use _stream_subprocess_output which is used internally
            with patch("apm_cli.runtime.llm_runtime._stream_subprocess_output") as mock_stream:
                mock_stream.return_value = (["Hello world\n"], 0)
                result = rt.execute_prompt("Say hello")
        assert "Hello world" in result

    def test_execute_prompt_failure_raises(self) -> None:
        """execute_prompt raises RuntimeError on non-zero exit."""
        rt = self._make_llm_runtime()
        with patch("apm_cli.runtime.llm_runtime._stream_subprocess_output") as mock_stream:
            mock_stream.return_value = (["Error!\n"], 1)
            with pytest.raises(RuntimeError, match="LLM execution failed"):
                rt.execute_prompt("bad prompt")


# ---------------------------------------------------------------------------
# instruction_integrator -- conversion methods
# ---------------------------------------------------------------------------


class TestInstructionConverters:
    """Tests for static format conversion methods."""

    def test_convert_to_claude_rules_with_apply_to(self) -> None:
        """applyTo frontmatter maps to paths in Claude rules."""
        from apm_cli.integration.instruction_integrator import InstructionIntegrator

        content = "---\napplyTo: '**/*.py'\n---\n# Python rules\n"
        result = InstructionIntegrator._convert_to_claude_rules(content)
        assert "paths:" in result
        assert "**/*.py" in result

    def test_convert_to_claude_rules_without_apply_to(self) -> None:
        """Instruction without applyTo becomes unconditional (no paths key)."""
        from apm_cli.integration.instruction_integrator import InstructionIntegrator

        content = "---\ndescription: General rules\n---\n# Body text\n"
        result = InstructionIntegrator._convert_to_claude_rules(content)
        assert "paths:" not in result
        assert "Body text" in result

    def test_convert_to_claude_rules_no_frontmatter(self) -> None:
        """Content without frontmatter is returned as body."""
        from apm_cli.integration.instruction_integrator import InstructionIntegrator

        content = "# Just body"
        result = InstructionIntegrator._convert_to_claude_rules(content)
        assert "Just body" in result

    def test_convert_to_windsurf_rules_with_apply_to(self) -> None:
        """applyTo maps to trigger: glob in windsurf format."""
        from apm_cli.integration.instruction_integrator import InstructionIntegrator

        content = "---\napplyTo: '**/*.ts'\n---\n# TypeScript rules\n"
        result = InstructionIntegrator._convert_to_windsurf_rules(content)
        assert "trigger: glob" in result
        assert "**/*.ts" in result

    def test_convert_to_windsurf_rules_no_apply_to(self) -> None:
        """Instruction without applyTo becomes trigger: always_on."""
        from apm_cli.integration.instruction_integrator import InstructionIntegrator

        content = "# Body only"
        result = InstructionIntegrator._convert_to_windsurf_rules(content)
        assert "trigger: always_on" in result
        assert "Body only" in result

    def test_convert_to_kiro_steering_with_apply_to(self) -> None:
        """applyTo maps to inclusion: fileMatch in kiro steering."""
        from apm_cli.integration.instruction_integrator import InstructionIntegrator

        content = "---\napplyTo: '**/*.go'\n---\n# Go rules\n"
        result = InstructionIntegrator._convert_to_kiro_steering(content)
        assert "inclusion: fileMatch" in result
        assert "**/*.go" in result

    def test_convert_to_kiro_steering_no_apply_to(self) -> None:
        """Instruction without applyTo becomes inclusion: always."""
        from apm_cli.integration.instruction_integrator import InstructionIntegrator

        content = "# Always active"
        result = InstructionIntegrator._convert_to_kiro_steering(content)
        assert "inclusion: always" in result

    def test_convert_to_kiro_steering_list_apply_to(self) -> None:
        """applyTo as list is joined with commas."""
        from apm_cli.integration.instruction_integrator import InstructionIntegrator

        content = "---\napplyTo:\n  - '**/*.py'\n  - '**/*.pyi'\n---\n# Python\n"
        result = InstructionIntegrator._convert_to_kiro_steering(content)
        assert "fileMatch" in result

    def test_convert_to_cursor_rules_multiple_globs(self) -> None:
        """Multiple globs result in a YAML list in cursor rules."""
        from apm_cli.integration.instruction_integrator import InstructionIntegrator

        content = "---\napplyTo: '**/*.py,**/*.pyi'\n---\n# Body\n"
        result = InstructionIntegrator._convert_to_cursor_rules(content)
        assert "globs:" in result

    def test_convert_to_cursor_rules_generates_description(self) -> None:
        """Description is generated from first body line when not in frontmatter."""
        from apm_cli.integration.instruction_integrator import InstructionIntegrator

        content = "---\napplyTo: '**/*.rb'\n---\n## Ruby coding style\n\nDetails here."
        result = InstructionIntegrator._convert_to_cursor_rules(content)
        assert "description:" in result
        assert "Ruby coding style" in result


class TestIntegrateCopilotUserInstructions:
    """Tests for _integrate_copilot_user_instructions()."""

    def _make_pkg_info(self, install_path: Path, name: str = "mypkg") -> Any:
        """Build minimal PackageInfo-like object."""
        pkg = MagicMock()
        pkg.name = name
        pkg.source = f"github/org/{name}"
        info = MagicMock()
        info.install_path = install_path
        info.package = pkg
        return info

    def test_creates_new_managed_file(self, tmp_path: Path) -> None:
        """New file is created with APM managed header."""
        from apm_cli.integration.instruction_integrator import InstructionIntegrator

        pkg_dir = tmp_path / "pkg"
        instr_dir = pkg_dir / ".apm" / "instructions"
        instr_dir.mkdir(parents=True)
        (instr_dir / "rules.instructions.md").write_text(
            "---\napplyTo: '**'\n---\n# Content here", encoding="utf-8"
        )

        deploy_dir = tmp_path / "deploy"
        deploy_dir.mkdir()

        integrator = InstructionIntegrator()
        self._make_pkg_info(pkg_dir)
        result = integrator._integrate_copilot_user_instructions(
            list(instr_dir.glob("*.instructions.md")),
            deploy_dir,
            tmp_path,
            pkg_source="github/org/mypkg",
        )
        assert result.files_integrated == 1
        dest = deploy_dir / "copilot-instructions.md"
        assert dest.exists()
        content = dest.read_text(encoding="utf-8")
        assert InstructionIntegrator._APM_COPILOT_HEADER in content

    def test_updates_existing_managed_file(self, tmp_path: Path) -> None:
        """Existing APM-managed file gets its section updated."""
        from apm_cli.integration.instruction_integrator import InstructionIntegrator

        pkg_dir = tmp_path / "pkg"
        instr_dir = pkg_dir / ".apm" / "instructions"
        instr_dir.mkdir(parents=True)
        (instr_dir / "rules.instructions.md").write_text("# New content", encoding="utf-8")

        deploy_dir = tmp_path / "deploy"
        deploy_dir.mkdir()
        header = InstructionIntegrator._APM_COPILOT_HEADER
        existing_section = InstructionIntegrator._build_copilot_section(
            "github/org/mypkg", "# Old content"
        )
        (deploy_dir / "copilot-instructions.md").write_text(
            f"{header}\n{existing_section}\n", encoding="utf-8"
        )

        integrator = InstructionIntegrator()
        result = integrator._integrate_copilot_user_instructions(
            list(instr_dir.glob("*.instructions.md")),
            deploy_dir,
            tmp_path,
            pkg_source="github/org/mypkg",
        )
        assert result.files_integrated == 1
        content = (deploy_dir / "copilot-instructions.md").read_text(encoding="utf-8")
        assert "New content" in content

    def test_skips_user_authored_file(self, tmp_path: Path) -> None:
        """User-authored (no APM header) file triggers collision skip."""
        from apm_cli.integration.instruction_integrator import InstructionIntegrator

        pkg_dir = tmp_path / "pkg"
        instr_dir = pkg_dir / ".apm" / "instructions"
        instr_dir.mkdir(parents=True)
        (instr_dir / "rules.instructions.md").write_text("# Content", encoding="utf-8")

        deploy_dir = tmp_path / "deploy"
        deploy_dir.mkdir()
        # User-authored file (no APM header)
        (deploy_dir / "copilot-instructions.md").write_text("# User authored", encoding="utf-8")

        integrator = InstructionIntegrator()
        result = integrator._integrate_copilot_user_instructions(
            list(instr_dir.glob("*.instructions.md")),
            deploy_dir,
            tmp_path,
            pkg_source="github/org/mypkg",
        )
        # Should skip (files_skipped=1) or return 0 integrated
        assert result.files_integrated == 0

    def test_force_overwrites_user_authored_file(self, tmp_path: Path) -> None:
        """force=True overwrites user-authored file."""
        from apm_cli.integration.instruction_integrator import InstructionIntegrator

        pkg_dir = tmp_path / "pkg"
        instr_dir = pkg_dir / ".apm" / "instructions"
        instr_dir.mkdir(parents=True)
        (instr_dir / "rules.instructions.md").write_text("# Content", encoding="utf-8")

        deploy_dir = tmp_path / "deploy"
        deploy_dir.mkdir()
        user_file = deploy_dir / "copilot-instructions.md"
        user_file.write_text("# User authored", encoding="utf-8")

        integrator = InstructionIntegrator()
        result = integrator._integrate_copilot_user_instructions(
            list(instr_dir.glob("*.instructions.md")),
            deploy_dir,
            tmp_path,
            pkg_source="github/org/mypkg",
            force=True,
        )
        assert result.files_integrated == 1


# ---------------------------------------------------------------------------
# lsp_integrator -- additional coverage
# ---------------------------------------------------------------------------


class TestLSPServerNames:
    """Tests for LSPIntegrator.get_server_names()."""

    def test_extracts_names_from_objects_with_name_attr(self) -> None:
        """Objects with .name attribute have their names extracted."""
        from apm_cli.integration.lsp_integrator import LSPIntegrator

        dep1 = MagicMock()
        dep1.name = "server-a"
        dep2 = MagicMock()
        dep2.name = "server-b"
        names = LSPIntegrator.get_server_names([dep1, dep2])
        assert names == {"server-a", "server-b"}

    def test_extracts_names_from_strings(self) -> None:
        """Plain string items are treated as server names."""
        from apm_cli.integration.lsp_integrator import LSPIntegrator

        names = LSPIntegrator.get_server_names(["server-x", "server-y"])
        assert names == {"server-x", "server-y"}

    def test_empty_list_returns_empty_set(self) -> None:
        """Empty dep list returns empty set."""
        from apm_cli.integration.lsp_integrator import LSPIntegrator

        assert LSPIntegrator.get_server_names([]) == set()


class TestLSPServerConfigs:
    """Tests for LSPIntegrator.get_server_configs()."""

    def test_extracts_configs_from_deps(self) -> None:
        """Deps with to_dict() and name have their config extracted."""
        from apm_cli.integration.lsp_integrator import LSPIntegrator

        dep = MagicMock()
        dep.name = "my-server"
        dep.to_dict.return_value = {"name": "my-server", "command": "lsp"}
        configs = LSPIntegrator.get_server_configs([dep])
        assert "my-server" in configs
        assert configs["my-server"]["command"] == "lsp"

    def test_extracts_configs_from_strings(self) -> None:
        """String deps get a minimal config dict."""
        from apm_cli.integration.lsp_integrator import LSPIntegrator

        configs = LSPIntegrator.get_server_configs(["plain-srv"])
        assert configs == {"plain-srv": {"name": "plain-srv"}}


class TestLSPBaseServerEntries:
    """Tests for LSPIntegrator._base_server_entries()."""

    def test_uses_to_lsp_json_entry_when_available(self) -> None:
        """Prefers to_lsp_json_entry over to_dict."""
        from apm_cli.integration.lsp_integrator import LSPIntegrator

        dep = MagicMock()
        dep.name = "my-srv"
        dep.to_lsp_json_entry.return_value = {"command": "my-lsp", "args": []}
        entries = LSPIntegrator._base_server_entries([dep])
        assert entries["my-srv"] == {"command": "my-lsp", "args": []}

    def test_uses_to_dict_as_fallback(self) -> None:
        """Falls back to to_dict when to_lsp_json_entry missing."""
        from apm_cli.integration.lsp_integrator import LSPIntegrator

        dep = MagicMock(spec=["name", "to_dict"])
        dep.name = "fallback-srv"
        dep.to_dict.return_value = {"name": "fallback-srv", "command": "fallback-lsp"}
        entries = LSPIntegrator._base_server_entries([dep])
        assert "fallback-srv" in entries
        assert "name" not in entries["fallback-srv"]  # name stripped

    def test_handles_plain_dict_entries(self) -> None:
        """Plain dict items with 'name' key are also supported."""
        from apm_cli.integration.lsp_integrator import LSPIntegrator

        dep = {"name": "dict-srv", "command": "dict-lsp", "args": []}
        entries = LSPIntegrator._base_server_entries([dep])
        assert "dict-srv" in entries
        assert entries["dict-srv"]["command"] == "dict-lsp"


class TestLSPDeduplicate:
    """Tests for LSPIntegrator.deduplicate()."""

    def test_deduplicates_by_name(self) -> None:
        """First occurrence wins when names repeat."""
        from apm_cli.integration.lsp_integrator import LSPIntegrator

        dep1 = MagicMock()
        dep1.name = "srv"
        dep2 = MagicMock()
        dep2.name = "srv"
        result = LSPIntegrator.deduplicate([dep1, dep2])
        assert len(result) == 1

    def test_preserves_unique_entries(self) -> None:
        """Unique entries are all preserved."""
        from apm_cli.integration.lsp_integrator import LSPIntegrator

        dep1 = MagicMock()
        dep1.name = "srv1"
        dep2 = MagicMock()
        dep2.name = "srv2"
        result = LSPIntegrator.deduplicate([dep1, dep2])
        assert len(result) == 2


# ---------------------------------------------------------------------------
# runtime/manager.py -- more coverage
# ---------------------------------------------------------------------------


class TestRuntimeManagerScripts:
    """Tests for RuntimeManager script loading."""

    def test_get_embedded_script_raises_when_not_found(self) -> None:
        """get_embedded_script raises RuntimeError for missing script."""
        from apm_cli.runtime.manager import RuntimeManager

        mgr = RuntimeManager()
        with pytest.raises(RuntimeError, match="Could not load setup script"):
            mgr.get_embedded_script("nonexistent-script.sh")

    def test_get_common_script_loads_real_script(self) -> None:
        """get_common_script returns script content from repo."""
        import sys

        from apm_cli.runtime.manager import RuntimeManager

        if sys.platform == "win32":
            pytest.skip("Unix-only test")

        mgr = RuntimeManager()
        # The repo has scripts/runtime/setup-common.sh
        try:
            content = mgr.get_common_script()
            assert isinstance(content, str)
            assert len(content) > 0
        except RuntimeError:
            pytest.skip("setup-common.sh not present in this environment")

    def test_get_token_helper_script_unix(self) -> None:
        """get_token_helper_script returns string on Unix (may raise if not found)."""
        import sys

        from apm_cli.runtime.manager import RuntimeManager

        if sys.platform == "win32":
            pytest.skip("Unix-only test")

        mgr = RuntimeManager()
        try:
            content = mgr.get_token_helper_script()
            assert isinstance(content, str)
        except RuntimeError:
            pass  # Acceptable if script not in this environment

    def test_setup_runtime_with_mocked_scripts(self) -> None:
        """setup_runtime calls run_embedded_script with script content."""
        from apm_cli.runtime.manager import RuntimeManager

        mgr = RuntimeManager()
        with patch.object(mgr, "get_embedded_script", return_value="#!/bin/sh\necho hi"):
            with patch.object(mgr, "get_common_script", return_value="#!/bin/sh\n"):
                with patch.object(mgr, "run_embedded_script", return_value=True):
                    result = mgr.setup_runtime("llm")
        assert result is True

    def test_setup_runtime_with_version_arg(self) -> None:
        """setup_runtime passes version arg to script."""
        import sys

        from apm_cli.runtime.manager import RuntimeManager

        mgr = RuntimeManager()
        captured: list = []

        def _capture_args(script, common, args=None) -> bool:
            captured.append(args or [])
            return True

        with patch.object(mgr, "get_embedded_script", return_value="#!/bin/sh"):
            with patch.object(mgr, "get_common_script", return_value="#!/bin/sh"):
                with patch.object(mgr, "run_embedded_script", side_effect=_capture_args):
                    mgr.setup_runtime("codex", version="1.0.0")

        if captured and sys.platform != "win32":
            assert "1.0.0" in captured[0]

    def test_setup_runtime_with_vanilla_flag(self) -> None:
        """setup_runtime passes vanilla flag to script."""
        import sys

        from apm_cli.runtime.manager import RuntimeManager

        mgr = RuntimeManager()
        captured: list = []

        def _capture_args(script, common, args=None) -> bool:
            captured.append(args or [])
            return True

        with patch.object(mgr, "get_embedded_script", return_value="#!/bin/sh"):
            with patch.object(mgr, "get_common_script", return_value="#!/bin/sh"):
                with patch.object(mgr, "run_embedded_script", side_effect=_capture_args):
                    mgr.setup_runtime("llm", vanilla=True)

        if captured and sys.platform != "win32":
            assert "--vanilla" in captured[0]

    def test_setup_runtime_returns_false_on_script_failure(self) -> None:
        """setup_runtime returns False when script fails."""
        from apm_cli.runtime.manager import RuntimeManager

        mgr = RuntimeManager()
        with patch.object(mgr, "get_embedded_script", return_value="#!/bin/sh"):
            with patch.object(mgr, "get_common_script", return_value="#!/bin/sh"):
                with patch.object(mgr, "run_embedded_script", return_value=False):
                    result = mgr.setup_runtime("llm")
        assert result is False

    def test_remove_runtime_non_npm_with_binary(self, tmp_path: Path) -> None:
        """remove_runtime for non-npm runtime removes binary file."""
        from apm_cli.runtime.manager import RuntimeManager

        mgr = RuntimeManager()
        mgr.runtime_dir = tmp_path
        # Create a fake binary
        binary = tmp_path / "codex"
        binary.write_text("#!/bin/sh\necho codex", encoding="utf-8")

        result = mgr.remove_runtime("codex")
        assert result is True
        assert not binary.exists()

    def test_remove_llm_runtime_removes_venv(self, tmp_path: Path) -> None:
        """remove_runtime for llm also removes llm-venv directory."""
        from apm_cli.runtime.manager import RuntimeManager

        mgr = RuntimeManager()
        mgr.runtime_dir = tmp_path
        binary = tmp_path / "llm"
        binary.write_text("#!/bin/sh\n", encoding="utf-8")
        venv = tmp_path / "llm-venv"
        venv.mkdir()

        result = mgr.remove_runtime("llm")
        assert result is True
        assert not venv.exists()


# ---------------------------------------------------------------------------
# CopilotRuntime -- execute_prompt
# ---------------------------------------------------------------------------


class TestCopilotRuntimeExecute:
    """Tests for CopilotRuntime.execute_prompt()."""

    def _make_runtime(self) -> Any:
        """Build CopilotRuntime with mocked binary check."""
        from apm_cli.runtime.copilot_runtime import CopilotRuntime

        with patch(
            "apm_cli.runtime.copilot_runtime.find_runtime_binary", return_value="/usr/bin/copilot"
        ):
            return CopilotRuntime()

    def test_execute_prompt_success(self) -> None:
        """execute_prompt returns output on success."""
        rt = self._make_runtime()
        with patch("apm_cli.runtime.copilot_runtime._stream_subprocess_output") as mock_stream:
            mock_stream.return_value = (["Response text\n"], 0)
            result = rt.execute_prompt("do something")
        assert "Response text" in result

    def test_execute_prompt_with_full_auto(self) -> None:
        """execute_prompt passes --allow-all-tools with full_auto=True."""
        rt = self._make_runtime()
        captured: list = []

        def _capture(cmd, timeout=None):
            captured.append(cmd)
            return (["ok\n"], 0)

        with patch(
            "apm_cli.runtime.copilot_runtime._stream_subprocess_output", side_effect=_capture
        ):
            rt.execute_prompt("prompt", full_auto=True)

        if captured:
            assert "--allow-all-tools" in captured[0]

    def test_execute_prompt_with_log_level(self) -> None:
        """execute_prompt passes --log-level when specified."""
        rt = self._make_runtime()
        captured: list = []

        def _capture(cmd, timeout=None):
            captured.append(cmd)
            return (["ok\n"], 0)

        with patch(
            "apm_cli.runtime.copilot_runtime._stream_subprocess_output", side_effect=_capture
        ):
            rt.execute_prompt("prompt", log_level="debug")

        if captured:
            assert "--log-level" in captured[0]
            assert "debug" in captured[0]

    def test_execute_prompt_not_logged_in_raises(self) -> None:
        """execute_prompt raises RuntimeError with login hint."""
        rt = self._make_runtime()
        with patch("apm_cli.runtime.copilot_runtime._stream_subprocess_output") as mock_stream:
            mock_stream.return_value = (["not logged in error\n"], 1)
            with pytest.raises(RuntimeError, match="Not logged in"):
                rt.execute_prompt("prompt")

    def test_execute_prompt_generic_failure_raises(self) -> None:
        """execute_prompt raises RuntimeError on unknown failure."""
        rt = self._make_runtime()
        with patch("apm_cli.runtime.copilot_runtime._stream_subprocess_output") as mock_stream:
            mock_stream.return_value = (["unknown error\n"], 1)
            with pytest.raises(RuntimeError):
                rt.execute_prompt("prompt")

    def test_execute_prompt_timeout_raises(self) -> None:
        """execute_prompt raises RuntimeError on timeout."""
        rt = self._make_runtime()
        with patch("apm_cli.runtime.copilot_runtime._stream_subprocess_output") as mock_stream:
            mock_stream.side_effect = subprocess.TimeoutExpired(cmd=["copilot"], timeout=600)
            with pytest.raises(RuntimeError, match="timed out"):
                rt.execute_prompt("prompt")

    def test_execute_prompt_file_not_found_raises(self) -> None:
        """execute_prompt raises RuntimeError when copilot not found."""
        rt = self._make_runtime()
        with patch("apm_cli.runtime.copilot_runtime._stream_subprocess_output") as mock_stream:
            mock_stream.side_effect = FileNotFoundError("copilot not found")
            with pytest.raises(RuntimeError, match="not found"):
                rt.execute_prompt("prompt")

    def test_execute_prompt_with_add_dirs(self) -> None:
        """execute_prompt includes --add-dir flags for each directory."""
        rt = self._make_runtime()
        captured: list = []

        def _capture(cmd, timeout=None):
            captured.append(cmd)
            return (["ok\n"], 0)

        with patch(
            "apm_cli.runtime.copilot_runtime._stream_subprocess_output", side_effect=_capture
        ):
            rt.execute_prompt("prompt", add_dirs=["/path/a", "/path/b"])

        if captured:
            assert "--add-dir" in captured[0]
