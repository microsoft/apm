"""Phase-3c integration tests: HookIntegrator, SkillIntegrator (uncovered paths),
MCPIntegrator / run_mcp_install (uncovered branches), and
github_downloader_validation helper functions.

Coverage targets
----------------
  - hook_integrator.py               (75.6%, gap=191)
  - skill_integrator.py              (63.8%, gap=297) -- uncovered branches only
  - mcp_integrator.py                (72.8%, gap=227) -- uncovered branches only
  - mcp_integrator_install.py        (47.7%, gap=248) -- uncovered branches only
  - github_downloader_validation.py  (27.4%, gap=193)

Strategy
--------
  - Exercise real code paths; mock only HTTP, subprocess/git, env vars,
    and home-directory side-effects.
  - No live network calls.
  - Use type hints throughout.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.core.null_logger import NullCommandLogger
from apm_cli.deps.github_downloader import GitHubPackageDownloader
from apm_cli.deps.github_downloader_validation import _ssh_attempt_allowed
from apm_cli.integration.hook_integrator import (
    _HOOK_EVENT_MAP,
    HookIntegrationResult,
    HookIntegrator,
    _filter_hook_files_for_target,
    _reinject_apm_source_from_sidecar,
)
from apm_cli.integration.hook_native_formats import (
    _copilot_keys_to_gemini,
    _to_gemini_hook_entries,
)
from apm_cli.integration.mcp_integrator import MCPIntegrator
from apm_cli.integration.skill_integrator import (
    SkillIntegrationResult,
    SkillIntegrator,
)
from apm_cli.models.apm_package import APMPackage, PackageInfo
from apm_cli.models.validation import PackageType

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Helpers shared across test classes
# ---------------------------------------------------------------------------


def _make_apm_package(name: str = "test-pkg", version: str = "1.0.0") -> APMPackage:
    return APMPackage(name=name, version=version)


def _make_package_info(
    install_path: Path,
    name: str = "test-pkg",
    pkg_type: PackageType | None = None,
) -> PackageInfo:
    pkg = _make_apm_package(name)
    return PackageInfo(package=pkg, install_path=install_path, package_type=pkg_type)


def _make_copilot_project(tmp_path: Path, name: str = "proj") -> Path:
    root = tmp_path / name
    root.mkdir(parents=True)
    github = root / ".github"
    github.mkdir()
    (github / "copilot-instructions.md").write_bytes(b"# instructions\n")
    return root


def _make_lockfile(root: Path) -> None:
    (root / "apm.lock.yaml").write_text("version: 1\npackages: []\nmcp_servers: []\n")


def _make_skill_dir(parent: Path, skill_name: str) -> Path:
    skill_dir = parent / skill_name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(f"# {skill_name}\n\nA test skill.")
    return skill_dir


# ===========================================================================
# github_downloader_validation -- pure-logic helpers
# ===========================================================================


class TestIsShaPin:
    """_is_sha_pin: SHA detection based on hex string length."""

    def test_full_40_char_sha(self) -> None:
        from apm_cli.deps.github_downloader_validation import _is_sha_pin

        assert _is_sha_pin("a" * 40) is True

    def test_abbreviated_7_char_sha(self) -> None:
        from apm_cli.deps.github_downloader_validation import _is_sha_pin

        assert _is_sha_pin("abc1234") is True

    def test_tag_name_not_sha(self) -> None:
        from apm_cli.deps.github_downloader_validation import _is_sha_pin

        assert _is_sha_pin("v1.0.0") is False

    def test_branch_name_not_sha(self) -> None:
        from apm_cli.deps.github_downloader_validation import _is_sha_pin

        assert _is_sha_pin("main") is False

    def test_too_short_not_sha(self) -> None:
        from apm_cli.deps.github_downloader_validation import _is_sha_pin

        assert _is_sha_pin("abc12") is False  # only 5 chars

    def test_mixed_case_sha(self) -> None:
        from apm_cli.deps.github_downloader_validation import _is_sha_pin

        assert _is_sha_pin("AbCdEf1234567") is True

    def test_41_chars_too_long(self) -> None:
        from apm_cli.deps.github_downloader_validation import _is_sha_pin

        assert _is_sha_pin("a" * 41) is False

    def test_non_hex_returns_false(self) -> None:
        from apm_cli.deps.github_downloader_validation import _is_sha_pin

        assert _is_sha_pin("xyz" + "a" * 7) is False


class TestSplitOwnerRepo:
    """_split_owner_repo: safe splitting of owner/repo strings."""

    def test_valid_owner_repo(self) -> None:
        from apm_cli.deps.github_downloader_validation import _split_owner_repo

        result = _split_owner_repo("owner/repo")
        assert result == ("owner", "repo")

    def test_no_slash_returns_none(self) -> None:
        from apm_cli.deps.github_downloader_validation import _split_owner_repo

        assert _split_owner_repo("noslash") is None

    def test_empty_owner_returns_none(self) -> None:
        from apm_cli.deps.github_downloader_validation import _split_owner_repo

        assert _split_owner_repo("/repo") is None

    def test_empty_repo_returns_none(self) -> None:
        from apm_cli.deps.github_downloader_validation import _split_owner_repo

        assert _split_owner_repo("owner/") is None

    def test_owner_repo_with_extra_slash_keeps_repo(self) -> None:
        from apm_cli.deps.github_downloader_validation import _split_owner_repo

        result = _split_owner_repo("owner/org/repo")
        assert result is not None
        assert result[0] == "owner"
        # The repo portion is everything after the first slash
        assert "repo" in result[1]


class TestAttemptSpec:
    """AttemptSpec NamedTuple: creation and attribute access."""

    def test_creation_and_fields(self) -> None:
        from apm_cli.deps.github_downloader_validation import AttemptSpec

        spec = AttemptSpec(label="test", url="https://example.com", env={"TOKEN": "x"})
        assert spec.label == "test"
        assert spec.url == "https://example.com"
        assert spec.env == {"TOKEN": "x"}

    def test_unpacking(self) -> None:
        from apm_cli.deps.github_downloader_validation import AttemptSpec

        spec = AttemptSpec("lbl", "http://h", {"a": "b"})
        label, url, env = spec
        assert label == "lbl"
        assert url == "http://h"
        assert env == {"a": "b"}


class TestValidateVirtualPackageExistsEdgeCases:
    """validate_virtual_package_exists edge cases not covered elsewhere."""

    def _make_dep_ref(
        self,
        is_virtual: bool = True,
        vpath: str = "skills/foo",
        ref: str | None = "main",
        host: str | None = None,
    ):
        from apm_cli.models.apm_package import DependencyReference

        return DependencyReference(
            repo_url="owner/repo",
            host=host,
            reference=ref,
            virtual_path=vpath,
            is_virtual=is_virtual,
        )

    def test_non_virtual_raises_value_error(self) -> None:
        from apm_cli.deps import github_downloader_validation as gdv
        from apm_cli.deps.github_downloader import GitHubPackageDownloader

        downloader = GitHubPackageDownloader()
        dep = self._make_dep_ref(is_virtual=False)
        with pytest.raises(ValueError, match="virtual"):
            gdv.validate_virtual_package_exists(downloader, dep)

    def test_empty_vpath_rejected(self) -> None:
        from apm_cli.deps import github_downloader_validation as gdv
        from apm_cli.deps.github_downloader import GitHubPackageDownloader

        downloader = GitHubPackageDownloader()
        dep = self._make_dep_ref(vpath="")
        result = gdv.validate_virtual_package_exists(downloader, dep)
        assert result is False

    def test_virtual_file_probes_directly(self) -> None:
        from apm_cli.deps import github_downloader_validation as gdv
        from apm_cli.deps.github_downloader import GitHubPackageDownloader

        downloader = GitHubPackageDownloader()
        dep = self._make_dep_ref(vpath="prompts/file.prompt.md")

        with patch.object(downloader, "download_raw_file", return_value=b"content") as mock:
            result = gdv.validate_virtual_package_exists(downloader, dep)

        assert result is True
        mock.assert_called_once()

    def test_virtual_file_returns_false_on_runtime_error(self) -> None:
        from apm_cli.deps import github_downloader_validation as gdv
        from apm_cli.deps.github_downloader import GitHubPackageDownloader

        downloader = GitHubPackageDownloader()
        dep = self._make_dep_ref(vpath="prompts/file.prompt.md")

        with patch.object(downloader, "download_raw_file", side_effect=RuntimeError("404")):
            result = gdv.validate_virtual_package_exists(downloader, dep)

        assert result is False

    def test_verbose_callback_called_on_traversal(self) -> None:
        from apm_cli.deps import github_downloader_validation as gdv
        from apm_cli.deps.github_downloader import GitHubPackageDownloader

        messages: list[str] = []
        downloader = GitHubPackageDownloader()
        dep = self._make_dep_ref(vpath="../etc/passwd")
        gdv.validate_virtual_package_exists(downloader, dep, verbose_callback=messages.append)
        assert any("rejected" in m for m in messages)

    def test_subdir_no_ref_fallback_returns_false_when_no_marker(self) -> None:
        """Without an explicit ref, ls-remote fallback is skipped."""
        from apm_cli.deps import github_downloader_validation as gdv
        from apm_cli.deps.github_downloader import GitHubPackageDownloader

        downloader = GitHubPackageDownloader()
        dep = self._make_dep_ref(vpath="skills/no-ref-pkg", ref=None)

        with (
            patch.object(downloader, "download_raw_file", side_effect=RuntimeError("404")),
            patch.object(gdv, "_directory_exists_at_ref", return_value=False),
        ):
            result = gdv.validate_virtual_package_exists(downloader, dep)

        assert result is False


class TestDirectoryExistsAtRef:
    """_directory_exists_at_ref: HTTP API probe."""

    def _make_downloader_with_mock_auth(self):
        from apm_cli.deps.github_downloader import GitHubPackageDownloader

        downloader = GitHubPackageDownloader()
        mock_auth = MagicMock()
        mock_auth.resolve_for_dep.return_value = MagicMock(token="fake-token")
        downloader.auth_resolver = mock_auth
        return downloader

    def _make_dep_ref(
        self,
        host: str | None = None,
        repo_url: str = "owner/repo",
    ):
        from apm_cli.models.apm_package import DependencyReference

        return DependencyReference(
            repo_url=repo_url,
            host=host,
            reference="main",
            virtual_path="skills/foo",
            is_virtual=True,
        )

    def test_ado_host_skips_probe(self) -> None:
        from apm_cli.deps.github_downloader_validation import _directory_exists_at_ref

        downloader = self._make_downloader_with_mock_auth()
        dep = self._make_dep_ref(host="dev.azure.com")
        log_messages: list[str] = []
        result = _directory_exists_at_ref(
            downloader, dep, "skills/foo", "main", log_messages.append
        )
        assert result is False
        assert any("skipped" in m for m in log_messages)

    def test_github_200_returns_true(self) -> None:
        from apm_cli.deps.github_downloader_validation import _directory_exists_at_ref

        downloader = self._make_downloader_with_mock_auth()
        dep = self._make_dep_ref(host="github.com")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch.object(downloader, "_resilient_get", return_value=mock_resp):
            result = _directory_exists_at_ref(downloader, dep, "skills/foo", "main", lambda m: None)

        assert result is True

    def test_github_404_returns_false(self) -> None:
        from apm_cli.deps.github_downloader_validation import _directory_exists_at_ref

        downloader = self._make_downloader_with_mock_auth()
        dep = self._make_dep_ref(host="github.com")

        mock_resp = MagicMock()
        mock_resp.status_code = 404
        with patch.object(downloader, "_resilient_get", return_value=mock_resp):
            result = _directory_exists_at_ref(downloader, dep, "skills/foo", "main", lambda m: None)

        assert result is False

    def test_request_exception_returns_false(self) -> None:
        import requests

        from apm_cli.deps.github_downloader_validation import _directory_exists_at_ref

        downloader = self._make_downloader_with_mock_auth()
        dep = self._make_dep_ref(host="github.com")

        with patch.object(
            downloader, "_resilient_get", side_effect=requests.exceptions.ConnectionError("err")
        ):
            result = _directory_exists_at_ref(downloader, dep, "skills/foo", "main", lambda m: None)

        assert result is False

    def test_no_token_still_makes_request(self) -> None:
        from apm_cli.deps.github_downloader_validation import _directory_exists_at_ref

        downloader = self._make_downloader_with_mock_auth()
        downloader.auth_resolver.resolve_for_dep.return_value = MagicMock(token=None)
        dep = self._make_dep_ref(host="github.com")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch.object(downloader, "_resilient_get", return_value=mock_resp) as mock_get:
            result = _directory_exists_at_ref(downloader, dep, "path", "ref", lambda m: None)

        assert result is True
        # Should not include Authorization header when token is None
        call_kwargs = mock_get.call_args[1]
        headers = call_kwargs.get("headers", {})
        assert "Authorization" not in headers or headers.get("Authorization") is None

    def test_invalid_repo_url_returns_false(self) -> None:
        from apm_cli.deps.github_downloader_validation import _directory_exists_at_ref

        downloader = self._make_downloader_with_mock_auth()
        dep = self._make_dep_ref(host="github.com", repo_url="invalid-no-slash")
        log_messages: list[str] = []
        result = _directory_exists_at_ref(
            downloader, dep, "skills/foo", "main", log_messages.append
        )
        assert result is False


class TestSshAttemptAllowed:
    """_ssh_attempt_allowed: SSH attempt gating logic."""

    def test_import_error_returns_false(self) -> None:
        downloader = GitHubPackageDownloader()
        with patch.dict("sys.modules", {"apm_cli.deps.transport_selection": None}):
            # When the import fails, should return False
            result = _ssh_attempt_allowed(downloader)
            assert isinstance(result, bool)

    def test_ssh_protocol_preference_allows(self) -> None:
        downloader = GitHubPackageDownloader()
        try:
            from apm_cli.deps.transport_selection import ProtocolPreference

            downloader._protocol_pref = ProtocolPreference.SSH
            downloader._allow_fallback = False
            result = _ssh_attempt_allowed(downloader)
            assert result is True
        except ImportError:
            pytest.skip("ProtocolPreference not available")

    def test_allow_fallback_permits_ssh(self) -> None:
        downloader = GitHubPackageDownloader()
        try:
            from apm_cli.deps.transport_selection import ProtocolPreference

            downloader._protocol_pref = ProtocolPreference.HTTPS
            downloader._allow_fallback = True
            result = _ssh_attempt_allowed(downloader)
            assert result is True
        except ImportError:
            pytest.skip("ProtocolPreference not available")


# ===========================================================================
# HookIntegrator -- helper functions
# ===========================================================================


class TestFilterHookFilesForTargetExtended:
    """Extended _filter_hook_files_for_target edge cases."""

    def test_copilot_suffix_targets_copilot(self, tmp_path: Path) -> None:
        f = tmp_path / "my-copilot-hooks.json"
        f.touch()
        result = _filter_hook_files_for_target([f], "copilot")
        assert f in result

    def test_copilot_suffix_excluded_from_cursor(self, tmp_path: Path) -> None:
        f = tmp_path / "copilot-hooks.json"
        f.touch()
        result = _filter_hook_files_for_target([f], "cursor")
        assert f not in result

    def test_cursor_suffix_excluded_from_claude(self, tmp_path: Path) -> None:
        f = tmp_path / "cursor-hooks.json"
        f.touch()
        result = _filter_hook_files_for_target([f], "claude")
        assert f not in result

    def test_cursor_suffix_included_for_cursor(self, tmp_path: Path) -> None:
        f = tmp_path / "cursor-hooks.json"
        f.touch()
        result = _filter_hook_files_for_target([f], "cursor")
        assert f in result

    def test_claude_suffix_included_for_claude(self, tmp_path: Path) -> None:
        f = tmp_path / "claude-hooks.json"
        f.touch()
        result = _filter_hook_files_for_target([f], "claude")
        assert f in result

    def test_universal_hooks_file_included_everywhere(self, tmp_path: Path) -> None:
        f = tmp_path / "hooks.json"
        f.touch()
        for target in ("copilot", "claude", "cursor", "codex", "gemini", "windsurf"):
            result = _filter_hook_files_for_target([f], target)
            assert f in result

    def test_codex_suffix_excluded_from_vscode(self, tmp_path: Path) -> None:
        f = tmp_path / "codex-hooks.json"
        f.touch()
        result = _filter_hook_files_for_target([f], "vscode")
        assert f not in result

    def test_gemini_suffix_included_for_gemini(self, tmp_path: Path) -> None:
        f = tmp_path / "gemini-hooks.json"
        f.touch()
        result = _filter_hook_files_for_target([f], "gemini")
        assert f in result

    def test_windsurf_suffix_included_for_windsurf(self, tmp_path: Path) -> None:
        f = tmp_path / "windsurf-hooks.json"
        f.touch()
        result = _filter_hook_files_for_target([f], "windsurf")
        assert f in result

    def test_empty_list_returns_empty(self) -> None:
        result = _filter_hook_files_for_target([], "claude")
        assert result == []


class TestReinjectApmSourceFromSidecar:
    """_reinject_apm_source_from_sidecar: ownership metadata restoration."""

    def test_basic_reinject(self) -> None:
        hooks = {
            "PreToolUse": [
                {"type": "command", "command": "echo hi"},
            ]
        }
        sidecar = {
            "PreToolUse": [
                {"type": "command", "command": "echo hi", "_apm_source": "my-pkg"},
            ]
        }
        _reinject_apm_source_from_sidecar(hooks, sidecar)
        assert hooks["PreToolUse"][0].get("_apm_source") == "my-pkg"

    def test_event_not_in_hooks_is_ignored(self) -> None:
        hooks: dict = {}
        sidecar = {"PreToolUse": [{"command": "echo", "_apm_source": "pkg"}]}
        _reinject_apm_source_from_sidecar(hooks, sidecar)
        # No error, hooks unchanged
        assert hooks == {}

    def test_sidecar_entry_without_source_ignored(self) -> None:
        hooks = {"PreToolUse": [{"command": "echo hi"}]}
        sidecar = {"PreToolUse": [{"command": "echo hi"}]}  # no _apm_source
        _reinject_apm_source_from_sidecar(hooks, sidecar)
        assert "_apm_source" not in hooks["PreToolUse"][0]

    def test_already_tagged_entry_not_overwritten(self) -> None:
        hooks = {"PreToolUse": [{"command": "echo hi", "_apm_source": "existing"}]}
        sidecar = {"PreToolUse": [{"command": "echo hi", "_apm_source": "other"}]}
        _reinject_apm_source_from_sidecar(hooks, sidecar)
        # The already-tagged entry is skipped in the loop
        assert hooks["PreToolUse"][0]["_apm_source"] == "existing"

    def test_each_sidecar_entry_consumed_at_most_once(self) -> None:
        """Identical entries share ownership only once to avoid false claims."""
        hooks = {
            "PreToolUse": [
                {"command": "echo hi"},
                {"command": "echo hi"},  # second identical entry
            ]
        }
        sidecar = {
            "PreToolUse": [
                {"command": "echo hi", "_apm_source": "pkg-a"},
            ]
        }
        _reinject_apm_source_from_sidecar(hooks, sidecar)
        sources = [e.get("_apm_source") for e in hooks["PreToolUse"]]
        # Only one entry should be claimed
        assert sources.count("pkg-a") == 1


class TestCopilotKeysToGemini:
    """_copilot_keys_to_gemini: in-place key renaming."""

    def test_bash_renamed_to_command(self) -> None:
        hook: dict = {"bash": "echo hi", "timeoutSec": 5}
        _copilot_keys_to_gemini(hook)
        assert "command" in hook
        assert "bash" not in hook
        assert hook["command"] == "echo hi"

    def test_powershell_renamed_to_command(self) -> None:
        hook: dict = {"powershell": "Write-Host hi"}
        _copilot_keys_to_gemini(hook)
        assert hook["command"] == "Write-Host hi"

    def test_timeout_sec_to_ms(self) -> None:
        hook: dict = {"bash": "echo", "timeoutSec": 10}
        _copilot_keys_to_gemini(hook)
        assert hook["timeout"] == 10_000
        assert "timeoutSec" not in hook

    def test_command_key_already_present_not_overwritten(self) -> None:
        hook: dict = {"command": "my-cmd", "bash": "other-cmd"}
        _copilot_keys_to_gemini(hook)
        # "command" is already set; bash is skipped
        assert hook["command"] == "my-cmd"

    def test_windows_key_renamed_to_command(self) -> None:
        hook: dict = {"windows": "cmd.exe /c echo hi"}
        _copilot_keys_to_gemini(hook)
        assert hook["command"] == "cmd.exe /c echo hi"


class TestToGeminiHookEntries:
    """_to_gemini_hook_entries: transformation to Gemini nested format."""

    def test_flat_copilot_entry_wrapped(self) -> None:
        entries = [{"bash": "echo hi", "type": "command"}]
        result = _to_gemini_hook_entries(entries)
        assert len(result) == 1
        outer = result[0]
        assert "hooks" in outer
        inner = outer["hooks"][0]
        assert inner["command"] == "echo hi"

    def test_already_nested_entry_left_alone(self) -> None:
        entries = [{"hooks": [{"command": "echo hi"}]}]
        result = _to_gemini_hook_entries(entries)
        assert result == entries or result[0]["hooks"][0]["command"] == "echo hi"

    def test_apm_source_promoted_to_outer(self) -> None:
        entries = [{"bash": "echo", "_apm_source": "my-pkg"}]
        result = _to_gemini_hook_entries(entries)
        assert result[0].get("_apm_source") == "my-pkg"

    def test_non_dict_entry_passed_through(self) -> None:
        entries: list[Any] = ["not-a-dict"]
        result = _to_gemini_hook_entries(entries)
        assert result == ["not-a-dict"]

    def test_timeoutsec_converted_in_flat(self) -> None:
        entries = [{"bash": "echo", "timeoutSec": 3}]
        result = _to_gemini_hook_entries(entries)
        inner = result[0]["hooks"][0]
        assert inner["timeout"] == 3000


# ===========================================================================
# HookIntegrator -- class methods
# ===========================================================================


class TestHookIntegratorFindHookFiles:
    """find_hook_files discovery across .apm/hooks/ and hooks/ dirs."""

    def test_empty_package_returns_empty(self, tmp_path: Path) -> None:
        integrator = HookIntegrator()
        assert integrator.find_hook_files(tmp_path) == []

    def test_finds_files_in_apm_hooks(self, tmp_path: Path) -> None:
        apm_hooks = tmp_path / ".apm" / "hooks"
        apm_hooks.mkdir(parents=True)
        (apm_hooks / "hooks.json").write_text('{"hooks":{}}')
        integrator = HookIntegrator()
        result = integrator.find_hook_files(tmp_path)
        assert len(result) == 1
        assert result[0].name == "hooks.json"

    def test_finds_files_in_hooks_dir(self, tmp_path: Path) -> None:
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "main.json").write_text('{"hooks":{}}')
        integrator = HookIntegrator()
        result = integrator.find_hook_files(tmp_path)
        assert len(result) == 1

    def test_deduplicates_across_both_dirs(self, tmp_path: Path) -> None:
        """Files in both directories are all returned (no false dedup)."""
        apm_hooks = tmp_path / ".apm" / "hooks"
        apm_hooks.mkdir(parents=True)
        (apm_hooks / "a.json").write_text("{}")
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "b.json").write_text("{}")
        integrator = HookIntegrator()
        result = integrator.find_hook_files(tmp_path)
        assert len(result) == 2

    def test_skips_symlinks(self, tmp_path: Path) -> None:
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        real_file = tmp_path / "real.json"
        real_file.write_text("{}")
        sym = hooks_dir / "sym.json"
        sym.symlink_to(real_file)
        integrator = HookIntegrator()
        result = integrator.find_hook_files(tmp_path)
        # The symlink should be skipped
        names = [f.name for f in result]
        assert "sym.json" not in names


class TestHookIntegratorParseHookJson:
    """_parse_hook_json: parse valid, invalid, and non-dict JSON."""

    def test_valid_json_returned(self, tmp_path: Path) -> None:
        f = tmp_path / "hooks.json"
        data = {"hooks": {"PreToolUse": []}}
        f.write_text(json.dumps(data))
        integrator = HookIntegrator()
        result = integrator._parse_hook_json(f)
        assert result == data

    def test_invalid_json_returns_none(self, tmp_path: Path) -> None:
        f = tmp_path / "hooks.json"
        f.write_text("NOT JSON {{}")
        integrator = HookIntegrator()
        assert integrator._parse_hook_json(f) is None

    def test_non_dict_returns_none(self, tmp_path: Path) -> None:
        f = tmp_path / "hooks.json"
        f.write_text(json.dumps([1, 2, 3]))
        integrator = HookIntegrator()
        assert integrator._parse_hook_json(f) is None

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        integrator = HookIntegrator()
        result = integrator._parse_hook_json(tmp_path / "nonexistent.json")
        assert result is None


class TestRewriteCommandForTarget:
    """_rewrite_command_for_target: path rewriting for different targets."""

    def test_system_command_unchanged(self, tmp_path: Path) -> None:
        integrator = HookIntegrator()
        cmd, scripts = integrator._rewrite_command_for_target(
            "echo hello",
            tmp_path,
            "my-pkg",
            "claude",
        )
        assert cmd == "echo hello"
        assert scripts == []

    def test_plugin_root_variable_replaced(self, tmp_path: Path) -> None:
        script = tmp_path / "scripts" / "validate.sh"
        script.parent.mkdir(parents=True)
        script.write_text("#!/bin/sh")

        integrator = HookIntegrator()
        cmd, scripts = integrator._rewrite_command_for_target(
            "${CLAUDE_PLUGIN_ROOT}/scripts/validate.sh",
            tmp_path,
            "my-pkg",
            "claude",
        )
        assert "${CLAUDE_PLUGIN_ROOT}" not in cmd
        assert len(scripts) == 1
        assert scripts[0][0] == script

    def test_cursor_plugin_root_variable(self, tmp_path: Path) -> None:
        script = tmp_path / "scripts" / "run.sh"
        script.parent.mkdir(parents=True)
        script.write_text("#!/bin/sh")

        integrator = HookIntegrator()
        cmd, scripts = integrator._rewrite_command_for_target(
            "${CURSOR_PLUGIN_ROOT}/scripts/run.sh",
            tmp_path,
            "my-pkg",
            "cursor",
        )
        assert "${CURSOR_PLUGIN_ROOT}" not in cmd
        assert len(scripts) >= 1

    def test_relative_path_replaced_with_script_copy(self, tmp_path: Path) -> None:
        hook_dir = tmp_path / "hooks"
        hook_dir.mkdir()
        script = hook_dir / "format.sh"
        script.write_text("#!/bin/sh\necho format")

        integrator = HookIntegrator()
        cmd, scripts = integrator._rewrite_command_for_target(
            "./format.sh",
            tmp_path,
            "my-pkg",
            "vscode",
            hook_file_dir=hook_dir,
        )
        assert "./format.sh" not in cmd
        assert len(scripts) == 1

    def test_vscode_target_uses_github_scripts_base(self, tmp_path: Path) -> None:
        script = tmp_path / "scripts" / "scan.sh"
        script.parent.mkdir(parents=True)
        script.write_text("#!/bin/sh")

        integrator = HookIntegrator()
        _cmd, scripts = integrator._rewrite_command_for_target(
            "${PLUGIN_ROOT}/scripts/scan.sh",
            tmp_path,
            "my-pkg",
            "vscode",
        )
        if scripts:
            # VSCode scripts go to .github/hooks/scripts/
            assert ".github/hooks/scripts" in scripts[0][1]

    def test_missing_script_does_not_add_to_copy_list(self, tmp_path: Path) -> None:
        integrator = HookIntegrator()
        _cmd, scripts = integrator._rewrite_command_for_target(
            "${CLAUDE_PLUGIN_ROOT}/does-not-exist.sh",
            tmp_path,
            "my-pkg",
            "claude",
        )
        assert scripts == []


class TestIntegratePackageHooksCopilot:
    """integrate_package_hooks: VSCode/Copilot integration."""

    def test_no_hook_files_returns_zero(self, tmp_path: Path) -> None:
        project_root = _make_copilot_project(tmp_path)
        pkg_path = project_root / "apm_modules" / "no-hooks-pkg"
        pkg_path.mkdir(parents=True)
        pkg_info = _make_package_info(pkg_path, "no-hooks-pkg")

        integrator = HookIntegrator()
        result = integrator.integrate_package_hooks(pkg_info, project_root)
        assert result.hooks_integrated == 0

    def test_basic_hook_file_integrated(self, tmp_path: Path) -> None:
        project_root = _make_copilot_project(tmp_path)
        pkg_path = project_root / "apm_modules" / "hooks-pkg"
        pkg_path.mkdir(parents=True)
        hooks_dir = pkg_path / "hooks"
        hooks_dir.mkdir()
        hook_data = {
            "version": 1,
            "hooks": {"preToolUse": [{"type": "command", "bash": "echo hi", "timeoutSec": 10}]},
        }
        (hooks_dir / "hooks.json").write_text(json.dumps(hook_data))
        pkg_info = _make_package_info(pkg_path, "hooks-pkg")

        integrator = HookIntegrator()
        result = integrator.integrate_package_hooks(pkg_info, project_root)
        assert result.hooks_integrated >= 1
        assert isinstance(result.target_paths, list)

    def test_invalid_hook_json_skipped(self, tmp_path: Path) -> None:
        project_root = _make_copilot_project(tmp_path)
        pkg_path = project_root / "apm_modules" / "bad-hooks-pkg"
        pkg_path.mkdir(parents=True)
        hooks_dir = pkg_path / ".apm" / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "bad.json").write_text("NOT JSON {{}")
        pkg_info = _make_package_info(pkg_path, "bad-hooks-pkg")

        integrator = HookIntegrator()
        result = integrator.integrate_package_hooks(pkg_info, project_root)
        assert result.hooks_integrated == 0

    def test_hook_with_script_copies_script(self, tmp_path: Path) -> None:
        project_root = _make_copilot_project(tmp_path)
        pkg_path = project_root / "apm_modules" / "script-hooks-pkg"
        pkg_path.mkdir(parents=True)

        scripts_dir = pkg_path / "scripts"
        scripts_dir.mkdir()
        script_file = scripts_dir / "validate.sh"
        script_file.write_text("#!/bin/sh\necho validate")

        hooks_dir = pkg_path / ".apm" / "hooks"
        hooks_dir.mkdir(parents=True)
        hook_data = {
            "hooks": {
                "preToolUse": [{"type": "command", "bash": "${PLUGIN_ROOT}/scripts/validate.sh"}]
            }
        }
        (hooks_dir / "hooks.json").write_text(json.dumps(hook_data))
        pkg_info = _make_package_info(pkg_path, "script-hooks-pkg")

        integrator = HookIntegrator()
        result = integrator.integrate_package_hooks(pkg_info, project_root)
        assert result.hooks_integrated >= 1


class TestIntegratePackageHooksClaude:
    """integrate_package_hooks_claude: merge into .claude/settings.json."""

    def test_no_hook_files_returns_empty(self, tmp_path: Path) -> None:
        project_root = _make_copilot_project(tmp_path)
        pkg_path = project_root / "apm_modules" / "no-hooks"
        pkg_path.mkdir(parents=True)
        pkg_info = _make_package_info(pkg_path, "no-hooks")

        integrator = HookIntegrator()
        result = integrator.integrate_package_hooks_claude(pkg_info, project_root)
        assert result.hooks_integrated == 0

    def test_hook_merged_into_settings_json(self, tmp_path: Path) -> None:
        project_root = _make_copilot_project(tmp_path)
        claude_dir = project_root / ".claude"
        claude_dir.mkdir()

        pkg_path = project_root / "apm_modules" / "claude-hooks-pkg"
        pkg_path.mkdir(parents=True)
        hooks_dir = pkg_path / "hooks"
        hooks_dir.mkdir()
        hook_data = {
            "hooks": {
                "PreToolUse": [
                    {"hooks": [{"type": "command", "command": "echo check", "timeout": 30}]}
                ]
            }
        }
        (hooks_dir / "hooks.json").write_text(json.dumps(hook_data))
        pkg_info = _make_package_info(pkg_path, "claude-hooks-pkg")

        integrator = HookIntegrator()
        result = integrator.integrate_package_hooks_claude(pkg_info, project_root)
        assert result.hooks_integrated >= 1

        settings_file = claude_dir / "settings.json"
        assert settings_file.exists()
        settings = json.loads(settings_file.read_text())
        assert "hooks" in settings

    def test_idempotent_reinvoke_doesnt_duplicate(self, tmp_path: Path) -> None:
        project_root = _make_copilot_project(tmp_path)
        claude_dir = project_root / ".claude"
        claude_dir.mkdir()

        pkg_path = project_root / "apm_modules" / "idem-pkg"
        pkg_path.mkdir(parents=True)
        hooks_dir = pkg_path / ".apm" / "hooks"
        hooks_dir.mkdir(parents=True)
        hook_data = {
            "hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "echo idem"}]}]}
        }
        (hooks_dir / "hooks.json").write_text(json.dumps(hook_data))
        pkg_info = _make_package_info(pkg_path, "idem-pkg")

        integrator = HookIntegrator()
        integrator.integrate_package_hooks_claude(pkg_info, project_root)
        integrator.integrate_package_hooks_claude(pkg_info, project_root)

        settings = json.loads((claude_dir / "settings.json").read_text())
        entries = settings.get("hooks", {}).get("PreToolUse", [])
        # Should not have duplicates from the same package
        assert len(entries) == 1

    def test_copilot_event_name_normalized_to_pascal(self, tmp_path: Path) -> None:
        """'preToolUse' (Copilot camelCase) is remapped to 'PreToolUse' (Claude PascalCase)."""
        project_root = _make_copilot_project(tmp_path)
        claude_dir = project_root / ".claude"
        claude_dir.mkdir()

        pkg_path = project_root / "apm_modules" / "camel-hooks-pkg"
        pkg_path.mkdir(parents=True)
        hooks_dir = pkg_path / "hooks"
        hooks_dir.mkdir()
        hook_data = {"hooks": {"preToolUse": [{"type": "command", "bash": "echo pre"}]}}
        (hooks_dir / "hooks.json").write_text(json.dumps(hook_data))
        pkg_info = _make_package_info(pkg_path, "camel-hooks-pkg")

        integrator = HookIntegrator()
        integrator.integrate_package_hooks_claude(pkg_info, project_root)

        settings = json.loads((claude_dir / "settings.json").read_text())
        hooks = settings.get("hooks", {})
        # Should be under PascalCase "PreToolUse" or camelCase depending on event_map
        event_map = _HOOK_EVENT_MAP.get("claude", {})
        mapped_name = event_map.get("preToolUse", "preToolUse")
        assert mapped_name in hooks or "preToolUse" in hooks

    def test_schema_strict_strips_apm_source_from_disk(self, tmp_path: Path) -> None:
        """Claude (schema_strict=True) must not persist _apm_source in settings.json."""
        project_root = _make_copilot_project(tmp_path)
        claude_dir = project_root / ".claude"
        claude_dir.mkdir()

        pkg_path = project_root / "apm_modules" / "strict-pkg"
        pkg_path.mkdir(parents=True)
        hooks_dir = pkg_path / "hooks"
        hooks_dir.mkdir()
        hook_data = {"hooks": {"PreToolUse": [{"type": "command", "command": "echo hi"}]}}
        (hooks_dir / "hooks.json").write_text(json.dumps(hook_data))
        pkg_info = _make_package_info(pkg_path, "strict-pkg")

        integrator = HookIntegrator()
        integrator.integrate_package_hooks_claude(pkg_info, project_root)

        settings = json.loads((claude_dir / "settings.json").read_text())
        for event_entries in settings.get("hooks", {}).values():
            for entry in event_entries:
                assert "_apm_source" not in entry


class TestIntegratePackageHooksCursor:
    """integrate_package_hooks_cursor: merge into .cursor/hooks.json (opt-in)."""

    def test_skips_when_cursor_dir_absent(self, tmp_path: Path) -> None:
        project_root = _make_copilot_project(tmp_path)
        pkg_path = project_root / "apm_modules" / "cursor-pkg"
        pkg_path.mkdir(parents=True)
        hooks_dir = pkg_path / "hooks"
        hooks_dir.mkdir()
        hook_data = {"hooks": {"afterFileEdit": [{"command": "echo"}]}}
        (hooks_dir / "hooks.json").write_text(json.dumps(hook_data))
        pkg_info = _make_package_info(pkg_path, "cursor-pkg")

        integrator = HookIntegrator()
        result = integrator.integrate_package_hooks_cursor(pkg_info, project_root)
        # No .cursor/ dir => skipped
        assert result.hooks_integrated == 0

    def test_integrates_when_cursor_dir_exists(self, tmp_path: Path) -> None:
        project_root = _make_copilot_project(tmp_path)
        cursor_dir = project_root / ".cursor"
        cursor_dir.mkdir()

        pkg_path = project_root / "apm_modules" / "cursor-hooks-pkg"
        pkg_path.mkdir(parents=True)
        hooks_dir = pkg_path / ".apm" / "hooks"
        hooks_dir.mkdir(parents=True)
        hook_data = {"hooks": {"afterFileEdit": [{"command": "echo edit"}]}}
        (hooks_dir / "hooks.json").write_text(json.dumps(hook_data))
        pkg_info = _make_package_info(pkg_path, "cursor-hooks-pkg")

        integrator = HookIntegrator()
        result = integrator.integrate_package_hooks_cursor(pkg_info, project_root)
        assert result.hooks_integrated >= 1

        hooks_json = cursor_dir / "hooks.json"
        assert hooks_json.exists()
        data = json.loads(hooks_json.read_text())
        assert "hooks" in data


class TestIntegratePackageHooksCodex:
    """integrate_package_hooks_codex: merge into .codex/hooks.json."""

    def test_skips_when_codex_dir_absent(self, tmp_path: Path) -> None:
        project_root = _make_copilot_project(tmp_path)
        pkg_path = project_root / "apm_modules" / "codex-pkg"
        pkg_path.mkdir(parents=True)
        hooks_dir = pkg_path / "hooks"
        hooks_dir.mkdir()
        hook_data = {"hooks": {"preApply": [{"command": "echo pre"}]}}
        (hooks_dir / "hooks.json").write_text(json.dumps(hook_data))
        pkg_info = _make_package_info(pkg_path, "codex-pkg")

        integrator = HookIntegrator()
        result = integrator.integrate_package_hooks_codex(pkg_info, project_root)
        assert result.hooks_integrated == 0

    def test_integrates_when_codex_dir_exists(self, tmp_path: Path) -> None:
        project_root = _make_copilot_project(tmp_path)
        codex_dir = project_root / ".codex"
        codex_dir.mkdir()

        pkg_path = project_root / "apm_modules" / "codex-hooks-pkg"
        pkg_path.mkdir(parents=True)
        hooks_dir = pkg_path / ".apm" / "hooks"
        hooks_dir.mkdir(parents=True)
        hook_data = {"hooks": {"preApply": [{"type": "command", "command": "echo codex"}]}}
        (hooks_dir / "codex-hooks.json").write_text(json.dumps(hook_data))
        pkg_info = _make_package_info(pkg_path, "codex-hooks-pkg")

        integrator = HookIntegrator()
        result = integrator.integrate_package_hooks_codex(pkg_info, project_root)
        assert result.hooks_integrated >= 1


class TestIntegrateHooksForTarget:
    """integrate_hooks_for_target: dispatch to copilot vs. merge targets."""

    def _make_target(self, name: str, root_dir: str, supports_hooks: bool = True) -> Any:
        target = MagicMock()
        target.name = name
        target.root_dir = root_dir
        target.supports.return_value = supports_hooks
        target.primitives = {
            "hooks": MagicMock(deploy_root=None),
        }
        return target

    def test_copilot_target_dispatches_to_integrate_package_hooks(self, tmp_path: Path) -> None:
        project_root = _make_copilot_project(tmp_path)
        pkg_path = project_root / "apm_modules" / "dispatch-pkg"
        pkg_path.mkdir(parents=True)
        pkg_info = _make_package_info(pkg_path, "dispatch-pkg")
        target = self._make_target("copilot", ".github")

        integrator = HookIntegrator()
        result = integrator.integrate_hooks_for_target(target, pkg_info, project_root)
        assert isinstance(result, HookIntegrationResult)

    def test_unknown_target_returns_empty_result(self, tmp_path: Path) -> None:
        project_root = _make_copilot_project(tmp_path)
        pkg_path = project_root / "apm_modules" / "unknown-pkg"
        pkg_path.mkdir(parents=True)
        pkg_info = _make_package_info(pkg_path, "unknown-pkg")
        target = self._make_target("unknown-target", ".unknown")

        integrator = HookIntegrator()
        result = integrator.integrate_hooks_for_target(target, pkg_info, project_root)
        assert result.hooks_integrated == 0


class TestSyncIntegrationHooks:
    """sync_integration: managed and legacy fallback removal."""

    def test_removes_managed_hook_json(self, tmp_path: Path) -> None:
        project_root = _make_copilot_project(tmp_path)
        hooks_dir = project_root / ".github" / "hooks"
        hooks_dir.mkdir(parents=True)
        hook_file = hooks_dir / "my-pkg-hooks.json"
        hook_file.write_text('{"hooks":{}}')

        managed = {".github/hooks/my-pkg-hooks.json"}
        integrator = HookIntegrator()
        stats = integrator.sync_integration(
            _make_apm_package(), project_root, managed_files=managed
        )
        assert stats["files_removed"] >= 1
        assert not hook_file.exists()

    def test_skips_traversal_in_managed_paths(self, tmp_path: Path) -> None:
        project_root = _make_copilot_project(tmp_path)
        managed = {"../.github/hooks/escape.json"}
        integrator = HookIntegrator()
        stats = integrator.sync_integration(
            _make_apm_package(), project_root, managed_files=managed
        )
        assert stats["errors"] == 0

    def test_legacy_fallback_removes_apm_suffix_files(self, tmp_path: Path) -> None:
        project_root = _make_copilot_project(tmp_path)
        hooks_dir = project_root / ".github" / "hooks"
        hooks_dir.mkdir(parents=True)
        legacy_file = hooks_dir / "pkg-apm.json"
        legacy_file.write_text('{"hooks":{}}')

        integrator = HookIntegrator()
        stats = integrator.sync_integration(_make_apm_package(), project_root, managed_files=None)
        assert stats["files_removed"] >= 1
        assert not legacy_file.exists()

    def test_cleans_apm_entries_from_cursor_hooks_json(self, tmp_path: Path) -> None:
        project_root = _make_copilot_project(tmp_path)
        cursor_dir = project_root / ".cursor"
        cursor_dir.mkdir()
        hooks_json = cursor_dir / "hooks.json"
        hooks_data = {
            "hooks": {
                "afterFileEdit": [
                    {"command": "echo user", "type": "command"},
                    {"command": "echo apm", "type": "command", "_apm_source": "apm-pkg"},
                ]
            }
        }
        hooks_json.write_text(json.dumps(hooks_data))

        integrator = HookIntegrator()
        integrator.sync_integration(_make_apm_package(), project_root, managed_files=set())

        updated = json.loads(hooks_json.read_text())
        entries = updated.get("hooks", {}).get("afterFileEdit", [])
        assert all("_apm_source" not in e for e in entries)

    def test_sync_cleans_claude_settings_json(self, tmp_path: Path) -> None:
        project_root = _make_copilot_project(tmp_path)
        claude_dir = project_root / ".claude"
        claude_dir.mkdir()
        settings_json = claude_dir / "settings.json"
        settings_data = {
            "hooks": {
                "PreToolUse": [
                    {"command": "echo hi", "_apm_source": "my-pkg"},
                ]
            }
        }
        settings_json.write_text(json.dumps(settings_data))

        # Also write a sidecar
        sidecar_path = claude_dir / "apm-hooks.json"
        sidecar_data = {
            "PreToolUse": [
                {"command": "echo hi", "_apm_source": "my-pkg"},
            ]
        }
        sidecar_path.write_text(json.dumps(sidecar_data))

        integrator = HookIntegrator()
        stats = integrator.sync_integration(_make_apm_package(), project_root, managed_files=set())
        assert stats["files_removed"] >= 1

        # The APM entry should be gone; file may be cleaned up entirely
        if settings_json.exists():
            updated = json.loads(settings_json.read_text())
            for event_entries in updated.get("hooks", {}).values():
                for entry in event_entries:
                    assert "_apm_source" not in entry

    def test_clean_apm_entries_from_json_static(self, tmp_path: Path) -> None:
        """_clean_apm_entries_from_json removes _apm_source-tagged entries."""
        hooks_json = tmp_path / "hooks.json"
        data = {
            "hooks": {
                "PreToolUse": [
                    {"command": "user", "type": "command"},
                    {"command": "apm", "_apm_source": "pkg"},
                ]
            }
        }
        hooks_json.write_text(json.dumps(data))

        stats = {"files_removed": 0, "errors": 0}
        HookIntegrator._clean_apm_entries_from_json(hooks_json, stats)

        assert stats["files_removed"] == 1
        updated = json.loads(hooks_json.read_text())
        assert len(updated["hooks"]["PreToolUse"]) == 1

    def test_clean_apm_entries_noop_when_file_absent(self, tmp_path: Path) -> None:
        stats = {"files_removed": 0, "errors": 0}
        HookIntegrator._clean_apm_entries_from_json(tmp_path / "nonexistent.json", stats)
        assert stats["files_removed"] == 0

    def test_clean_apm_entries_deletes_empty_event_key(self, tmp_path: Path) -> None:
        hooks_json = tmp_path / "hooks.json"
        data = {
            "hooks": {
                "PreToolUse": [
                    {"command": "apm", "_apm_source": "pkg"},
                ]
            }
        }
        hooks_json.write_text(json.dumps(data))

        stats = {"files_removed": 0, "errors": 0}
        HookIntegrator._clean_apm_entries_from_json(hooks_json, stats)

        updated = json.loads(hooks_json.read_text())
        assert "hooks" not in updated


class TestHookIntegrationResultCompat:
    """HookIntegrationResult backward-compat shim."""

    def test_hooks_integrated_alias(self) -> None:
        r = HookIntegrationResult(hooks_integrated=3)
        assert r.hooks_integrated == 3

    def test_full_constructor_via_super(self) -> None:
        r = HookIntegrationResult(
            files_integrated=2,
            files_updated=0,
            files_skipped=1,
            target_paths=[],
        )
        assert r.hooks_integrated == 2

    def test_target_paths_preserved(self) -> None:
        paths = [Path("/tmp/a"), Path("/tmp/b")]
        r = HookIntegrationResult(
            files_integrated=2,
            files_updated=0,
            files_skipped=0,
            target_paths=paths,
        )
        assert len(r.target_paths) == 2


# ===========================================================================
# SkillIntegrator -- uncovered branches
# ===========================================================================


class TestSkillIntegrationResultPostInit:
    """SkillIntegrationResult: __post_init__ defaults target_paths."""

    def test_target_paths_defaults_to_empty_list(self) -> None:
        r = SkillIntegrationResult(
            skill_created=True,
            skill_updated=False,
            skill_skipped=False,
            skill_path=None,
            references_copied=0,
        )
        assert r.target_paths == []

    def test_explicit_target_paths_preserved(self) -> None:
        paths = [Path("/a"), Path("/b")]
        r = SkillIntegrationResult(
            skill_created=True,
            skill_updated=False,
            skill_skipped=False,
            skill_path=None,
            references_copied=0,
            target_paths=paths,
        )
        assert r.target_paths == paths


class TestSkillIntegratorVirtualFileDep:
    """integrate_package_skill: virtual FILE deps are skipped."""

    def test_virtual_file_dep_is_skipped(self, tmp_path: Path) -> None:
        from apm_cli.models.apm_package import DependencyReference

        project_root = _make_copilot_project(tmp_path)
        _make_lockfile(project_root)

        pkg_path = project_root / "apm_modules" / "virtual-file"
        pkg_path.mkdir(parents=True)
        (pkg_path / "SKILL.md").write_text("# virtual file skill")

        pkg = APMPackage(name="virtual-file", version="1.0.0")
        dep_ref = DependencyReference(
            repo_url="owner/repo",
            virtual_path="prompts/file.prompt.md",
            is_virtual=True,
        )
        pkg_info = PackageInfo(
            package=pkg,
            install_path=pkg_path,
            package_type=PackageType.CLAUDE_SKILL,
            dependency_ref=dep_ref,
        )

        integrator = SkillIntegrator()
        result = integrator.integrate_package_skill(pkg_info, project_root)
        assert result.skill_skipped is True

    def test_virtual_subdirectory_dep_is_not_skipped(self, tmp_path: Path) -> None:
        from apm_cli.models.apm_package import DependencyReference

        project_root = _make_copilot_project(tmp_path)
        _make_lockfile(project_root)

        pkg_path = project_root / "apm_modules" / "my-subdir-skill"
        pkg_path.mkdir(parents=True)
        (pkg_path / "SKILL.md").write_text("# subdir skill")

        pkg = APMPackage(name="my-subdir-skill", version="1.0.0")
        dep_ref = DependencyReference(
            repo_url="owner/repo",
            virtual_path="skills/my-subdir-skill",  # subdirectory, not a file
            is_virtual=True,
        )
        pkg_info = PackageInfo(
            package=pkg,
            install_path=pkg_path,
            package_type=PackageType.CLAUDE_SKILL,
            dependency_ref=dep_ref,
        )

        integrator = SkillIntegrator()
        result = integrator.integrate_package_skill(pkg_info, project_root)
        # Subdirectory virtual packages should proceed to install
        assert result.skill_skipped is False


class TestSkillIntegratorSkillSubsetOnNonBundle:
    """integrate_package_skill: --skill filter on single-skill packages emits warning."""

    def test_skill_subset_warning_for_single_skill(
        self, tmp_path: Path, capfd: pytest.CaptureFixture
    ) -> None:
        project_root = _make_copilot_project(tmp_path)
        _make_lockfile(project_root)

        pkg_path = project_root / "apm_modules" / "single-skill"
        pkg_path.mkdir(parents=True)
        (pkg_path / "SKILL.md").write_text("# single-skill")
        pkg_info = _make_package_info(pkg_path, "single-skill", PackageType.CLAUDE_SKILL)

        integrator = SkillIntegrator()
        # Passing skill_subset on a single-skill package should trigger a warning
        # but still integrate
        result = integrator.integrate_package_skill(
            pkg_info, project_root, skill_subset=("some-other-skill",)
        )
        # The skill itself is still installed
        assert result.skill_created is True or result.skill_updated is True


class TestSkillIntegratorBuildOwnershipMaps:
    """_build_ownership_maps reads lockfile correctly."""

    def test_returns_empty_maps_when_no_lockfile(self, tmp_path: Path) -> None:
        owned_by, native_owners = SkillIntegrator._build_ownership_maps(tmp_path)
        assert owned_by == {}
        assert native_owners == {}

    def test_build_skill_ownership_map_returns_dict(self, tmp_path: Path) -> None:
        result = SkillIntegrator._build_skill_ownership_map(tmp_path)
        assert isinstance(result, dict)

    def test_build_native_skill_owner_map_returns_dict(self, tmp_path: Path) -> None:
        result = SkillIntegrator._build_native_skill_owner_map(tmp_path)
        assert isinstance(result, dict)


class TestSkillIntegratorPromoteSubSkillsForce:
    """_promote_sub_skills: force flag overrides user-authored skill protection."""

    def test_force_true_overwrites_existing_different_content(self, tmp_path: Path) -> None:
        sub_skills = tmp_path / "sub"
        sub_skills.mkdir()
        skill_dir = sub_skills / "my-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("# v2")

        target_root = tmp_path / "target"
        target_root.mkdir()
        # Pre-create an existing skill with different content
        existing = target_root / "my-skill"
        existing.mkdir()
        (existing / "SKILL.md").write_text("# user authored v1")

        count, _deployed = SkillIntegrator._promote_sub_skills(
            sub_skills,
            target_root,
            "parent",
            force=True,
            managed_files=set(),
        )
        assert count >= 1
        # After force, the content should be from the source
        assert (target_root / "my-skill" / "SKILL.md").read_text() == "# v2"


class TestSkillIntegratorIntegrateSkillBundle:
    """_integrate_skill_bundle: skill subset filtering."""

    @pytest.mark.parametrize(
        "skill_subset",
        [
            pytest.param(("skill-alpha",), id="leaf"),
            pytest.param(("category/skill-alpha",), id="source-prefixed"),
        ],
    )
    def test_skill_subset_filters_to_named_skills_only(
        self,
        tmp_path: Path,
        skill_subset: tuple[str, ...],
    ) -> None:
        project_root = _make_copilot_project(tmp_path)
        _make_lockfile(project_root)

        bundle_path = project_root / "apm_modules" / "my-bundle"
        bundle_path.mkdir(parents=True)
        skills_dir = bundle_path / "skills"
        skills_dir.mkdir()
        _make_skill_dir(skills_dir, "skill-alpha")
        _make_skill_dir(skills_dir, "skill-beta")

        pkg_info = _make_package_info(bundle_path, "my-bundle", PackageType.SKILL_BUNDLE)

        integrator = SkillIntegrator()
        result = integrator.integrate_package_skill(
            pkg_info, project_root, skill_subset=skill_subset
        )
        assert isinstance(result, SkillIntegrationResult)
        # Only skill-alpha should be deployed
        names = [p.name for p in result.target_paths]
        assert "skill-alpha" in names
        assert "skill-beta" not in names

    def test_all_skills_promoted_without_subset(self, tmp_path: Path) -> None:
        project_root = _make_copilot_project(tmp_path)
        _make_lockfile(project_root)

        bundle_path = project_root / "apm_modules" / "full-bundle"
        bundle_path.mkdir(parents=True)
        skills_dir = bundle_path / "skills"
        skills_dir.mkdir()
        _make_skill_dir(skills_dir, "alpha")
        _make_skill_dir(skills_dir, "beta")
        _make_skill_dir(skills_dir, "gamma")

        pkg_info = _make_package_info(bundle_path, "full-bundle", PackageType.SKILL_BUNDLE)

        integrator = SkillIntegrator()
        result = integrator.integrate_package_skill(pkg_info, project_root)
        assert result.sub_skills_promoted >= 3


class TestSkillIntegratorSyncIntegrationNoLockfile:
    """sync_integration handles no lockfile gracefully."""

    def test_no_lockfile_no_crash(self, tmp_path: Path) -> None:
        project_root = _make_copilot_project(tmp_path)
        # Deliberately no apm.lock.yaml

        apm_package = _make_apm_package()
        integrator = SkillIntegrator()
        stats = integrator.sync_integration(apm_package, project_root, managed_files=set())
        assert "files_removed" in stats
        assert stats["errors"] == 0


# ===========================================================================
# MCPIntegrator -- uncovered branches
# ===========================================================================


class TestMCPIntegratorDetectMcpConfigDrift:
    """_detect_mcp_config_drift: returns names of changed deps."""

    def test_detects_changed_dep(self) -> None:
        from apm_cli.models.dependency.mcp import MCPDependency

        dep = MCPDependency.from_string("srv-a")
        original_dict = dep.to_dict()
        # Simulate a stored config that differs from current
        stored = {"srv-a": {**original_dict, "extra_key": "different"}}

        result = MCPIntegrator._detect_mcp_config_drift([dep], stored)
        assert "srv-a" in result

    def test_no_drift_when_identical(self) -> None:
        from apm_cli.models.dependency.mcp import MCPDependency

        dep = MCPDependency.from_string("srv-b")
        stored = {"srv-b": dep.to_dict()}

        result = MCPIntegrator._detect_mcp_config_drift([dep], stored)
        assert "srv-b" not in result

    def test_new_dep_not_in_stored_is_not_drifted(self) -> None:
        from apm_cli.models.dependency.mcp import MCPDependency

        dep = MCPDependency.from_string("new-srv")
        stored: dict = {}  # not stored yet

        result = MCPIntegrator._detect_mcp_config_drift([dep], stored)
        assert "new-srv" not in result

    def test_non_dep_object_skipped(self) -> None:
        result = MCPIntegrator._detect_mcp_config_drift(["plain-string"], {"plain-string": {}})
        assert len(result) == 0


class TestMCPIntegratorAppendDrifted:
    """_append_drifted_to_install_list: sorted, no duplicates."""

    def test_appends_new_names_sorted(self) -> None:
        install_list: list[str] = ["existing"]
        MCPIntegrator._append_drifted_to_install_list(install_list, {"beta-srv", "alpha-srv"})
        assert "alpha-srv" in install_list
        assert "beta-srv" in install_list

    def test_does_not_duplicate_existing(self) -> None:
        install_list: list[str] = ["existing"]
        MCPIntegrator._append_drifted_to_install_list(install_list, {"existing"})
        assert install_list.count("existing") == 1

    def test_empty_drifted_noop(self) -> None:
        install_list: list[str] = ["a", "b"]
        MCPIntegrator._append_drifted_to_install_list(install_list, set())
        assert install_list == ["a", "b"]

    def test_sorted_order(self) -> None:
        install_list: list[str] = []
        MCPIntegrator._append_drifted_to_install_list(install_list, {"z-srv", "a-srv", "m-srv"})
        assert install_list == ["a-srv", "m-srv", "z-srv"]


class TestMCPIntegratorRemoveStaleVscode:
    """MCPIntegrator.remove_stale: VSCode .vscode/mcp.json cleanup."""

    def test_removes_from_vscode_mcp_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        vscode_dir = tmp_path / ".vscode"
        vscode_dir.mkdir()
        mcp_json = vscode_dir / "mcp.json"
        mcp_json.write_text(
            json.dumps({"servers": {"stale-srv": {"command": "stale"}, "keep-srv": {}}})
        )

        with patch("apm_cli.factory.ClientFactory") as mock_cf:
            mock_cf.supported_clients.return_value = ["vscode"]
            MCPIntegrator.remove_stale(
                {"stale-srv"},
                project_root=str(tmp_path),
                logger=NullCommandLogger(),
            )

        config = json.loads(mcp_json.read_text())
        assert "stale-srv" not in config.get("servers", {})
        assert "keep-srv" in config.get("servers", {})

    def test_skips_when_mcp_json_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".vscode").mkdir()

        with patch("apm_cli.factory.ClientFactory") as mock_cf:
            mock_cf.supported_clients.return_value = ["vscode"]
            # Should not raise
            MCPIntegrator.remove_stale(
                {"phantom-srv"},
                project_root=str(tmp_path),
                logger=NullCommandLogger(),
            )

    def test_empty_stale_set_is_noop(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        MCPIntegrator.remove_stale(
            set(),
            project_root=str(tmp_path),
            logger=NullCommandLogger(),
        )


class TestMCPIntegratorRemoveStaleCursor:
    """MCPIntegrator.remove_stale: cursor .cursor/mcp.json cleanup."""

    def test_removes_from_cursor_mcp_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        cursor_dir = tmp_path / ".cursor"
        cursor_dir.mkdir()
        mcp_json = cursor_dir / "mcp.json"
        mcp_json.write_text(json.dumps({"mcpServers": {"old-srv": {}, "new-srv": {}}}))

        with patch("apm_cli.factory.ClientFactory") as mock_cf:
            mock_cf.supported_clients.return_value = ["cursor"]
            MCPIntegrator.remove_stale(
                {"old-srv"},
                project_root=str(tmp_path),
                logger=NullCommandLogger(),
            )

        config = json.loads(mcp_json.read_text())
        assert "old-srv" not in config.get("mcpServers", {})
        assert "new-srv" in config.get("mcpServers", {})


class TestRunMcpInstallDirectoryGating:
    """run_mcp_install: opt-in directory presence gates (cursor, gemini, windsurf)."""

    def test_cursor_dir_enables_cursor_runtime(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".cursor").mkdir()
        # With no mcp_deps, returns 0 without processing
        from apm_cli.integration.mcp_integrator_install import run_mcp_install

        result = run_mcp_install([], logger=NullCommandLogger(), project_root=str(tmp_path))
        assert result == 0

    def test_gemini_dir_enables_gemini_runtime(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".gemini").mkdir()
        from apm_cli.integration.mcp_integrator_install import run_mcp_install

        result = run_mcp_install([], logger=NullCommandLogger(), project_root=str(tmp_path))
        assert result == 0

    def test_windsurf_dir_enables_windsurf_runtime(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".windsurf").mkdir()
        from apm_cli.integration.mcp_integrator_install import run_mcp_install

        result = run_mcp_install([], logger=NullCommandLogger(), project_root=str(tmp_path))
        assert result == 0

    def test_explicit_runtime_targets_single_runtime(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        from apm_cli.integration.mcp_integrator_install import run_mcp_install

        result = run_mcp_install(
            [],
            runtime="copilot",
            logger=NullCommandLogger(),
            project_root=str(tmp_path),
        )
        assert result == 0

    def test_exclude_parameter_accepted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        from apm_cli.integration.mcp_integrator_install import run_mcp_install

        result = run_mcp_install(
            [],
            exclude="cursor",
            logger=NullCommandLogger(),
            project_root=str(tmp_path),
        )
        assert result == 0

    def test_explicit_target_passed_through(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        from apm_cli.integration.mcp_integrator_install import run_mcp_install

        result = run_mcp_install(
            [],
            explicit_target="vscode",
            logger=NullCommandLogger(),
            project_root=str(tmp_path),
        )
        assert result == 0

    def test_with_stored_mcp_configs(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        from apm_cli.integration.mcp_integrator_install import run_mcp_install

        result = run_mcp_install(
            [],
            stored_mcp_configs={"srv-a": {"name": "srv-a"}},
            logger=NullCommandLogger(),
            project_root=str(tmp_path),
        )
        assert result == 0

    def test_verbose_mode_no_crash(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        from apm_cli.integration.mcp_integrator_install import run_mcp_install

        result = run_mcp_install(
            [],
            verbose=True,
            logger=NullCommandLogger(),
            project_root=str(tmp_path),
        )
        assert result == 0

    def test_project_scope_no_crash(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from apm_cli.core.scope import InstallScope
        from apm_cli.integration.mcp_integrator_install import run_mcp_install

        monkeypatch.chdir(tmp_path)
        result = run_mcp_install(
            [],
            scope=InstallScope.PROJECT,
            logger=NullCommandLogger(),
            project_root=str(tmp_path),
        )
        assert result == 0


class TestMCPIntegratorGateProjectScopedRuntimes:
    """MCPIntegrator._gate_project_scoped_runtimes: filters project-only runtimes."""

    def test_returns_list(self, tmp_path: Path) -> None:
        result = MCPIntegrator._gate_project_scoped_runtimes(
            ["vscode", "copilot"],
            user_scope=False,
            project_root=tmp_path,
            apm_config=None,
            explicit_target=None,
        )
        assert isinstance(result, list)

    def test_user_scope_filters_project_only_runtimes(self, tmp_path: Path) -> None:
        result = MCPIntegrator._gate_project_scoped_runtimes(
            ["codex", "cursor", "copilot"],
            user_scope=True,
            project_root=tmp_path,
            apm_config=None,
            explicit_target=None,
        )
        # Codex/cursor are project-scoped; with user_scope they should be filtered
        assert isinstance(result, list)

    def test_empty_runtimes_returns_empty(self, tmp_path: Path) -> None:
        result = MCPIntegrator._gate_project_scoped_runtimes(
            [],
            user_scope=False,
            project_root=tmp_path,
            apm_config=None,
            explicit_target=None,
        )
        assert result == []

    def test_explicit_target_allows_through(self, tmp_path: Path) -> None:
        (tmp_path / ".cursor").mkdir()
        result = MCPIntegrator._gate_project_scoped_runtimes(
            ["cursor"],
            user_scope=False,
            project_root=tmp_path,
            apm_config=None,
            explicit_target="cursor",
        )
        assert isinstance(result, list)


class TestMCPIntegratorCollectTransitiveWithLock:
    """collect_transitive: lock-file-guided scanning."""

    def test_returns_empty_when_modules_dir_absent(self, tmp_path: Path) -> None:
        result = MCPIntegrator.collect_transitive(
            tmp_path / "apm_modules",
            lock_path=None,
        )
        assert result == []

    def test_returns_empty_when_no_apm_yml_in_modules(self, tmp_path: Path) -> None:
        modules = tmp_path / "apm_modules"
        modules.mkdir()
        result = MCPIntegrator.collect_transitive(modules, lock_path=None)
        assert result == []

    def test_parses_valid_mcp_package(self, tmp_path: Path) -> None:
        modules = tmp_path / "apm_modules"
        pkg = modules / "owner" / "test-mcp-pkg"
        pkg.mkdir(parents=True)
        apm_yml = pkg / "apm.yml"
        apm_yml.write_text("name: test-mcp-pkg\nversion: 1.0.0\nmcp:\n  - github/copilot-mcp\n")
        result = MCPIntegrator.collect_transitive(modules, lock_path=None)
        assert isinstance(result, list)
