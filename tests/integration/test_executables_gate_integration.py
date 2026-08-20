"""Integration tests for the executable approval gate.

Exercises real filesystem scanning, approval checking, manifest
read/write, the exec_gate pipeline helper, and the approve/deny CLI
commands -- raising integration coverage for security/executables.py,
install/exec_gate.py, and commands/approve.py.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.install.exec_gate import (
    check_executable_approval,
    log_bin_status,
    resolve_package_key,
)
from apm_cli.install.phases.integrate import _run_executable_approval_prompt
from apm_cli.install.phases.lockfile import LockfileBuilder
from apm_cli.security.executables import (
    ALL_EXEC_TYPES,
    ENFORCED_EXEC_TYPES,
    EXEC_TYPE_BIN,
    EXEC_TYPE_CANVAS,
    EXEC_TYPE_HOOKS,
    EXEC_TYPE_MCP,
    ExecutableDeclaration,
    build_approval_key,
    is_any_type_approved,
    is_package_approved,
    parse_allow_executables,
    scan_package_executables,
    write_allow_executables,
)

# -------------------------------------------------------------------
# scan_package_executables -- real filesystem
# -------------------------------------------------------------------


class TestScanPackageExecutablesIntegration:
    """Integration: scan a package directory with real files."""

    def test_scan_hooks_in_apm_hooks_dir(self, tmp_path: Path) -> None:
        hooks_dir = tmp_path / ".apm" / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "pre-install.json").write_text("{}")
        (hooks_dir / "post-install.json").write_text("{}")
        # Non-JSON files should be ignored
        (hooks_dir / "README.md").write_text("docs")

        decl = scan_package_executables(tmp_path, "test-pkg", "1.0.0")

        assert decl.hook_count == 2
        assert decl.bin_count == 0
        assert decl.has_executables is True
        assert EXEC_TYPE_HOOKS in decl.exec_types
        assert EXEC_TYPE_BIN not in decl.exec_types
        assert "pre-install.json" in decl.hook_details
        assert "post-install.json" in decl.hook_details

    def test_scan_hooks_in_toplevel_hooks_dir(self, tmp_path: Path) -> None:
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "startup.json").write_text("{}")

        decl = scan_package_executables(tmp_path, "my-pkg", "2.0")

        assert decl.hook_count == 1
        assert decl.hook_details == ["startup.json"]

    def test_scan_bin_top_level(self, tmp_path: Path) -> None:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        (bin_dir / "my-tool").write_text("#!/bin/sh")
        (bin_dir / "helper").write_text("#!/bin/sh")
        # Hidden files ignored
        (bin_dir / ".hidden").write_text("skip")

        decl = scan_package_executables(tmp_path, "bin-pkg", "0.5")

        assert decl.bin_count == 2
        assert decl.has_executables is True
        assert EXEC_TYPE_BIN in decl.exec_types
        assert "my-tool" in decl.bin_details
        assert "helper" in decl.bin_details

    def test_scan_bin_in_skill_subdirs(self, tmp_path: Path) -> None:
        skill_bin = tmp_path / ".apm" / "skills" / "my-skill" / "bin"
        skill_bin.mkdir(parents=True)
        (skill_bin / "tool-a").write_text("exec")

        decl = scan_package_executables(tmp_path, "skill-pkg", "1.0")

        assert decl.bin_count == 1
        assert "tool-a" in decl.bin_details

    def test_scan_combined_hooks_and_bin(self, tmp_path: Path) -> None:
        (tmp_path / "hooks").mkdir()
        (tmp_path / "hooks" / "on-start.json").write_text("{}")
        (tmp_path / "bin").mkdir()
        (tmp_path / "bin" / "cli-tool").write_text("exec")

        decl = scan_package_executables(
            tmp_path, "combo", "3.0", is_transitive=True, parent_name="parent-pkg"
        )

        assert decl.hook_count == 1
        assert decl.bin_count == 1
        assert decl.has_executables is True
        assert decl.is_transitive is True
        assert decl.parent_name == "parent-pkg"
        assert set(decl.exec_types) == {EXEC_TYPE_HOOKS, EXEC_TYPE_BIN}

    def test_scan_empty_package(self, tmp_path: Path) -> None:
        decl = scan_package_executables(tmp_path, "empty", "0.1")

        assert decl.hook_count == 0
        assert decl.bin_count == 0
        assert decl.mcp_count == 0
        assert decl.has_executables is False
        assert decl.exec_types == []

    def test_scan_mcp_from_apm_yml(self, tmp_path: Path) -> None:
        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text(
            "name: mcp-pkg\ndependencies:\n  mcp:\n    - name: server-a\n    - server-b\n"
        )

        decl = scan_package_executables(tmp_path, "mcp-pkg", "1.0")

        assert decl.mcp_count == 2
        # MCP is an enforced exec type (#1865 expanded the gate to MCP+canvas).
        assert decl.has_executables is True
        assert EXEC_TYPE_MCP in decl.exec_types

    def test_scan_package_key_format(self, tmp_path: Path) -> None:
        decl = scan_package_executables(tmp_path, "owner/repo", "v2.1.0")
        assert decl.package_key == "owner/repo#v2.1.0"

    def test_scan_package_key_no_version(self, tmp_path: Path) -> None:
        decl = scan_package_executables(tmp_path, "local-pkg", "")
        assert decl.package_key == "local-pkg"

    def test_symlinks_excluded_from_hooks(self, tmp_path: Path) -> None:
        hooks_dir = tmp_path / ".apm" / "hooks"
        hooks_dir.mkdir(parents=True)
        real = hooks_dir / "real.json"
        real.write_text("{}")
        link = hooks_dir / "link.json"
        link.symlink_to(real)

        decl = scan_package_executables(tmp_path, "pkg", "1.0")
        # Only the real file is counted
        assert decl.hook_count == 1
        assert decl.hook_details == ["real.json"]


class TestLockfileExecStatusIntegration:
    """Integration: install-phase trust state is serialized into the lockfile."""

    def test_attach_exec_status_serializes_to_lockfile_yaml(self) -> None:
        lockfile = LockFile()
        lockfile.add_dependency(LockedDependency(repo_url="owner/repo"))
        ctx = SimpleNamespace(package_exec_status={"owner/repo": "gated_pending_approval"})

        LockfileBuilder(ctx)._attach_exec_status(lockfile)

        assert lockfile.dependencies["owner/repo"].exec_status == "gated_pending_approval"
        assert "exec_status: gated_pending_approval" in lockfile.to_yaml()


class TestNonInteractiveExecutablePromptIntegration:
    """Integration: CI installs park executables instead of aborting."""

    def test_noninteractive_blocked_executables_warn_without_exit(self, monkeypatch) -> None:
        decl = ExecutableDeclaration(
            package_key="owner/repo#1.0", package_name="owner/repo", hook_count=1
        )
        logger = MagicMock()
        ctx = SimpleNamespace(blocked_executables=[decl], logger=logger)

        monkeypatch.setenv("APM_NON_INTERACTIVE", "1")

        _run_executable_approval_prompt(ctx)

        logger.warning.assert_called_once()
        logger.info.assert_called_once()
        remedy = logger.info.call_args.args[0]
        assert "apm policy explain owner/repo" in remedy
        # M4 (#1873): non-interactive remedy leads with the bulk one-liner.
        assert "apm approve --recommended" in remedy

    def test_interactive_decline_surfaces_parked_remedy(self, monkeypatch) -> None:
        # M3 (#1873): declining at the interactive prompt must still print a
        # remedy -- present-but-parked guidance is not approve-only.
        import sys

        import apm_cli.install.phases.integrate as integ
        import apm_cli.security.executables as ex

        decl = ExecutableDeclaration(
            package_key="owner/repo#1.0", package_name="owner/repo", hook_count=1
        )
        logger = MagicMock()
        ctx = SimpleNamespace(blocked_executables=[decl], logger=logger)

        monkeypatch.delenv("APM_NON_INTERACTIVE", raising=False)
        monkeypatch.delenv("CI", raising=False)
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
        monkeypatch.setattr(ex, "load_user_executables", lambda: ({}, {}))
        # Simulate a full decline: prompt returns the seed unchanged.
        monkeypatch.setattr(ex, "prompt_executable_approval", lambda *a, **k: {})

        integ._run_executable_approval_prompt(ctx)

        info_msgs = " ".join(str(c.args[0]) for c in logger.info.call_args_list)
        assert "left parked" in info_msgs
        assert "apm approve owner/repo" in info_msgs
        assert "apm policy explain owner/repo" in info_msgs


# -------------------------------------------------------------------
# Approval checking integration
# -------------------------------------------------------------------


class TestApprovalCheckingIntegration:
    """Integration: approval logic with realistic data shapes."""

    def test_approve_hooks_only(self) -> None:
        allow = {"my-pkg#1.0": {"hooks": True, "bin": False}}
        assert is_package_approved(allow, "my-pkg#1.0", EXEC_TYPE_HOOKS) is True
        assert is_package_approved(allow, "my-pkg#1.0", EXEC_TYPE_BIN) is False

    def test_approve_all_types(self) -> None:
        allow = {"pkg#2.0": {"hooks": True, "bin": True, "mcp": True}}
        assert is_package_approved(allow, "pkg#2.0", EXEC_TYPE_HOOKS) is True
        assert is_package_approved(allow, "pkg#2.0", EXEC_TYPE_BIN) is True
        assert is_package_approved(allow, "pkg#2.0", EXEC_TYPE_MCP) is True

    def test_missing_package_key(self) -> None:
        allow = {"other#1.0": {"hooks": True}}
        assert is_package_approved(allow, "missing#1.0", EXEC_TYPE_HOOKS) is False

    def test_none_allow_executables(self) -> None:
        assert is_package_approved(None, "any#1.0", EXEC_TYPE_HOOKS) is False

    def test_empty_allow_executables(self) -> None:
        assert is_package_approved({}, "any#1.0", EXEC_TYPE_BIN) is False

    def test_is_any_type_approved_with_hooks_only(self) -> None:
        allow = {"pkg#1.0": {"hooks": True}}
        assert is_any_type_approved(allow, "pkg#1.0") is True

    def test_is_any_type_approved_all_false(self) -> None:
        allow = {"pkg#1.0": {"hooks": False, "bin": False}}
        assert is_any_type_approved(allow, "pkg#1.0") is False

    def test_is_any_type_approved_none(self) -> None:
        assert is_any_type_approved(None, "pkg#1.0") is False


# -------------------------------------------------------------------
# parse_allow_executables integration
# -------------------------------------------------------------------


class TestParseAllowExecutablesIntegration:
    """Integration: parse from realistic YAML-like data."""

    def test_absent_block_returns_none(self) -> None:
        assert parse_allow_executables({"name": "proj"}) is None

    def test_valid_block(self) -> None:
        data = {
            "allowExecutables": {
                "owner/repo#v1.0": {"hooks": True, "bin": False},
                "other#2.0": {"mcp": True},
            }
        }
        result = parse_allow_executables(data)
        assert result == {
            "owner/repo#v1.0": {"hooks": True, "bin": False},
            "other#2.0": {"mcp": True},
        }

    def test_empty_block(self) -> None:
        assert parse_allow_executables({"allowExecutables": {}}) == {}

    def test_invalid_block_type_raises(self) -> None:
        with pytest.raises(ValueError, match="must be a mapping"):
            parse_allow_executables({"allowExecutables": "invalid"})

    def test_invalid_entry_type_raises(self) -> None:
        with pytest.raises(ValueError, match="must be a mapping"):
            parse_allow_executables({"allowExecutables": {"pkg#1": "invalid"}})

    def test_unknown_exec_type_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown exec type"):
            parse_allow_executables({"allowExecutables": {"pkg#1": {"scripts": True}}})

    def test_non_bool_value_raises(self) -> None:
        with pytest.raises(ValueError, match="must be a boolean"):
            parse_allow_executables({"allowExecutables": {"pkg#1": {"hooks": "yes"}}})


# -------------------------------------------------------------------
# write_allow_executables integration (real filesystem)
# -------------------------------------------------------------------


class TestWriteAllowExecutablesIntegration:
    """Integration: write to real YAML file and verify roundtrip."""

    def test_write_and_read_back(self, tmp_path: Path) -> None:
        manifest = tmp_path / "apm.yml"
        manifest.write_text("name: my-project\nversion: 1.0.0\n")

        allow = {"risky-pkg#1.0": {"hooks": True, "bin": True}}
        write_allow_executables(manifest, allow)

        from apm_cli.utils.yaml_io import load_yaml

        data = load_yaml(manifest)
        assert data["allowExecutables"] == allow
        assert data["name"] == "my-project"

    def test_write_empty_removes_block(self, tmp_path: Path) -> None:
        manifest = tmp_path / "apm.yml"
        manifest.write_text("name: proj\nallowExecutables:\n  pkg#1:\n    hooks: true\n")

        write_allow_executables(manifest, {})

        from apm_cli.utils.yaml_io import load_yaml

        data = load_yaml(manifest)
        assert "allowExecutables" not in data

    def test_write_overwrites_existing_block(self, tmp_path: Path) -> None:
        manifest = tmp_path / "apm.yml"
        manifest.write_text("name: proj\nallowExecutables:\n  old#1:\n    hooks: true\n")

        new_allow = {"new#2.0": {"bin": True}}
        write_allow_executables(manifest, new_allow)

        from apm_cli.utils.yaml_io import load_yaml

        data = load_yaml(manifest)
        assert data["allowExecutables"] == new_allow
        assert "old#1" not in data.get("allowExecutables", {})


# -------------------------------------------------------------------
# exec_gate.check_executable_approval integration
# -------------------------------------------------------------------


class TestCheckExecutableApprovalIntegration:
    """Integration: full approval check with mocked package_info."""

    def _make_pkg_info(self, tmp_path: Path, name: str, version: str) -> MagicMock:
        pkg_info = MagicMock()
        pkg_info.install_path = str(tmp_path)
        pkg_info.package = MagicMock()
        pkg_info.package.name = name
        pkg_info.package.version = version
        pkg_info.dependency_ref = None
        return pkg_info

    def test_local_always_approved(self, tmp_path: Path) -> None:
        pkg_info = self._make_pkg_info(tmp_path, "_local", "")
        hooks_ok, bin_ok, _mcp_ok, _canvas_ok = check_executable_approval(
            "_local", pkg_info, {"deny-all": {}}
        )
        assert hooks_ok is True
        assert bin_ok is True

    def test_none_allow_executables_means_all_approved(self, tmp_path: Path) -> None:
        pkg_info = self._make_pkg_info(tmp_path, "any-pkg", "1.0")
        hooks_ok, bin_ok, _mcp_ok, _canvas_ok = check_executable_approval("any-pkg", pkg_info, None)
        assert hooks_ok is True
        assert bin_ok is True

    def test_empty_allow_executables_blocks_all(self, tmp_path: Path) -> None:
        pkg_info = self._make_pkg_info(tmp_path, "pkg", "1.0")
        hooks_ok, bin_ok, _mcp_ok, _canvas_ok = check_executable_approval("pkg", pkg_info, {})
        assert hooks_ok is False
        assert bin_ok is False

    def test_approved_package_passes(self, tmp_path: Path) -> None:
        pkg_info = self._make_pkg_info(tmp_path, "pkg", "1.0")
        allow = {"pkg#1.0": {"hooks": True, "bin": True}}
        hooks_ok, bin_ok, _mcp_ok, _canvas_ok = check_executable_approval("pkg", pkg_info, allow)
        assert hooks_ok is True
        assert bin_ok is True

    def test_partial_approval(self, tmp_path: Path) -> None:
        pkg_info = self._make_pkg_info(tmp_path, "pkg", "1.0")
        allow = {"pkg#1.0": {"hooks": True, "bin": False}}
        hooks_ok, bin_ok, _mcp_ok, _canvas_ok = check_executable_approval("pkg", pkg_info, allow)
        assert hooks_ok is True
        assert bin_ok is False

    def test_dep_ref_key_takes_priority(self, tmp_path: Path) -> None:
        pkg_info = self._make_pkg_info(tmp_path, "pkg", "1.0")
        dep_ref = MagicMock()
        dep_ref.canonical_string.return_value = "github:owner/repo#v1.0"
        pkg_info.dependency_ref = dep_ref

        allow = {"github:owner/repo#v1.0": {"hooks": True, "bin": True}}
        hooks_ok, bin_ok, _mcp_ok, _canvas_ok = check_executable_approval("pkg", pkg_info, allow)
        assert hooks_ok is True
        assert bin_ok is True

    def test_fallback_to_name_version_key(self, tmp_path: Path) -> None:
        pkg_info = self._make_pkg_info(tmp_path, "pkg", "1.0")
        dep_ref = MagicMock()
        dep_ref.canonical_string.return_value = "./packages/local-ref"
        pkg_info.dependency_ref = dep_ref

        # Approved under name#version, not dep-ref
        allow = {"pkg#1.0": {"hooks": True, "bin": True}}
        hooks_ok, bin_ok, _mcp_ok, _canvas_ok = check_executable_approval("pkg", pkg_info, allow)
        assert hooks_ok is True
        assert bin_ok is True

    def test_blocked_packages_tracked_on_context(self, tmp_path: Path) -> None:
        # Create hooks so scan finds them
        hooks_dir = tmp_path / ".apm" / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "hook.json").write_text("{}")

        pkg_info = self._make_pkg_info(tmp_path, "risky", "1.0")
        ctx = MagicMock()
        ctx.blocked_executables = []

        check_executable_approval("risky", pkg_info, {}, ctx=ctx)

        assert len(ctx.blocked_executables) == 1
        assert ctx.blocked_executables[0].package_name == "risky"


# -------------------------------------------------------------------
# resolve_package_key integration
# -------------------------------------------------------------------


class TestResolvePackageKeyIntegration:
    """Integration: key resolution with different package_info shapes."""

    def test_dep_ref_canonical_string(self) -> None:
        pkg_info = MagicMock()
        dep_ref = MagicMock()
        dep_ref.canonical_string.return_value = "github:owner/repo@main"
        pkg_info.dependency_ref = dep_ref

        assert resolve_package_key(pkg_info, "fallback") == "github:owner/repo@main"

    def test_dep_ref_str_fallback(self) -> None:
        pkg_info = MagicMock()
        dep_ref = MagicMock()
        dep_ref.canonical_string.return_value = ""
        dep_ref.__str__ = lambda self: "./local/path"
        pkg_info.dependency_ref = dep_ref

        assert resolve_package_key(pkg_info, "fallback") == "./local/path"

    def test_package_metadata_fallback(self) -> None:
        pkg_info = MagicMock()
        pkg_info.dependency_ref = None
        pkg_info.package = MagicMock()
        pkg_info.package.name = "my-pkg"
        pkg_info.package.version = "2.0.0"

        assert resolve_package_key(pkg_info, "unused") == "my-pkg#2.0.0"

    def test_bare_name_fallback(self) -> None:
        pkg_info = MagicMock()
        pkg_info.dependency_ref = None
        pkg_info.package = None

        assert resolve_package_key(pkg_info, "bare-name") == "bare-name"


# -------------------------------------------------------------------
# log_bin_status integration
# -------------------------------------------------------------------


class TestLogBinStatusIntegration:
    """Integration: log_bin_status emits correct messages."""

    def test_deployed_message(self) -> None:
        result = SimpleNamespace(bin_deployed=3, bin_skipped_reason=None)
        lines: list[str] = []
        log_bin_status(result, "/path/to/bin", "pkg", MagicMock(), lines.append)
        assert any("3 executable(s) deployed" in line for line in lines)
        assert any("reload-plugins" in line for line in lines)

    def test_skipped_project_scope(self) -> None:
        result = SimpleNamespace(bin_deployed=0, bin_skipped_reason="project_scope")
        lines: list[str] = []
        log_bin_status(result, "", "pkg", MagicMock(), lines.append)
        assert any("re-run with -g" in line for line in lines)

    def test_skipped_no_claude_target(self) -> None:
        result = SimpleNamespace(bin_deployed=0, bin_skipped_reason="no_claude_target")
        lines: list[str] = []
        log_bin_status(result, "", "pkg", MagicMock(), lines.append)
        assert any("no active Claude Code" in line for line in lines)

    def test_skipped_not_approved(self) -> None:
        result = SimpleNamespace(bin_deployed=0, bin_skipped_reason="not_approved")
        lines: list[str] = []
        log_bin_status(result, "", "risky-pkg", MagicMock(), lines.append)
        assert any("not approved" in line for line in lines)
        assert any("apm approve" in line for line in lines)


# -------------------------------------------------------------------
# ExecutableDeclaration summary and properties
# -------------------------------------------------------------------


class TestExecutableDeclarationIntegration:
    """Integration: full dataclass behaviour."""

    def test_summary_line_hooks_only(self) -> None:
        decl = ExecutableDeclaration(package_key="p#1", package_name="p", hook_count=3)
        assert "3 hook(s)" in decl.summary_line()
        assert "bin" not in decl.summary_line()

    def test_summary_line_bin_only(self) -> None:
        decl = ExecutableDeclaration(package_key="p#1", package_name="p", bin_count=2)
        assert "2 bin executable(s)" in decl.summary_line()

    def test_summary_line_combined(self) -> None:
        decl = ExecutableDeclaration(package_key="p#1", package_name="p", hook_count=1, bin_count=4)
        line = decl.summary_line()
        assert "1 hook(s)" in line
        assert "4 bin executable(s)" in line


# -------------------------------------------------------------------
# build_approval_key integration
# -------------------------------------------------------------------


class TestBuildApprovalKeyIntegration:
    """Integration: key construction edge cases."""

    def test_standard_format(self) -> None:
        assert build_approval_key("owner/repo", "v1.0.0") == "owner/repo#v1.0.0"

    def test_empty_version(self) -> None:
        assert build_approval_key("local-pkg", "") == "local-pkg"

    def test_marketplace_format(self) -> None:
        assert build_approval_key("name@marketplace", "2.0") == "name@marketplace#2.0"


# -------------------------------------------------------------------
# Constants validation
# -------------------------------------------------------------------


class TestConstantsIntegration:
    """Integration: ensure constants are consistent."""

    def test_enforced_types_subset_of_all(self) -> None:
        for t in ENFORCED_EXEC_TYPES:
            assert t in ALL_EXEC_TYPES

    def test_mcp_and_canvas_in_enforced(self) -> None:
        # #1865 expanded the executable gate to cover MCP and canvas.
        assert EXEC_TYPE_MCP in ENFORCED_EXEC_TYPES
        assert EXEC_TYPE_CANVAS in ENFORCED_EXEC_TYPES

    def test_hooks_and_bin_in_enforced(self) -> None:
        assert EXEC_TYPE_HOOKS in ENFORCED_EXEC_TYPES
        assert EXEC_TYPE_BIN in ENFORCED_EXEC_TYPES


# -------------------------------------------------------------------
# Non-interactive prompt (CI mode) integration
# -------------------------------------------------------------------


class TestPromptNonInteractiveIntegration:
    """Integration: CI mode raises SystemExit for unapproved packages."""

    def test_ci_mode_exits_on_unapproved(self, tmp_path: Path, monkeypatch) -> None:
        from apm_cli.security.executables import prompt_executable_approval

        monkeypatch.setenv("CI", "true")
        monkeypatch.delenv("APM_NON_INTERACTIVE", raising=False)

        decl = ExecutableDeclaration(
            package_key="risky#1.0",
            package_name="risky",
            hook_count=2,
        )

        with pytest.raises(SystemExit):
            prompt_executable_approval([decl])

    def test_trust_all_approves_without_prompt(self, monkeypatch) -> None:
        from apm_cli.security.executables import prompt_executable_approval

        monkeypatch.setenv("CI", "true")

        decl = ExecutableDeclaration(
            package_key="pkg#1.0",
            package_name="pkg",
            hook_count=1,
            bin_count=2,
        )

        result = prompt_executable_approval([decl], trust_all=True)
        assert result["pkg#1.0"] == {"hooks": True, "bin": True}

    def test_no_executables_denies_without_prompt(self, monkeypatch) -> None:
        from apm_cli.security.executables import prompt_executable_approval

        monkeypatch.setenv("CI", "true")

        decl = ExecutableDeclaration(
            package_key="pkg#1.0",
            package_name="pkg",
            hook_count=1,
        )

        result = prompt_executable_approval([decl], no_executables=True)
        assert "pkg#1.0" not in result

    def test_already_approved_skipped(self) -> None:
        from apm_cli.security.executables import prompt_executable_approval

        decl = ExecutableDeclaration(
            package_key="pkg#1.0",
            package_name="pkg",
            hook_count=1,
        )

        # Already approved -- should return immediately
        existing = {"pkg#1.0": {"hooks": True}}
        result = prompt_executable_approval([decl], allow_executables=existing)
        assert result == existing

    def test_no_pending_returns_early(self) -> None:
        from apm_cli.security.executables import prompt_executable_approval

        # Empty declarations -- nothing to prompt
        result = prompt_executable_approval([])
        assert result == {}

    def test_no_executables_in_declaration(self) -> None:
        from apm_cli.security.executables import prompt_executable_approval

        # Declaration with zero counts
        decl = ExecutableDeclaration(package_key="pkg#1.0", package_name="pkg")
        result = prompt_executable_approval([decl])
        assert result == {}


# -------------------------------------------------------------------
# approve/deny CLI commands integration (real filesystem)
# -------------------------------------------------------------------


class TestApproveDenyCommandsIntegration:
    """Integration: approve/deny commands with real apm.yml + apm_modules."""

    def _setup_project(self, tmp_path: Path) -> Path:
        """Create a minimal project with apm.yml and apm_modules."""
        project = tmp_path / "project"
        project.mkdir()
        manifest = project / "apm.yml"
        manifest.write_text("name: test-project\nversion: 1.0.0\n")

        # Create apm_modules with a package that has hooks
        pkg_dir = project / "apm_modules" / "risky-pkg"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "apm.yml").write_text("name: risky-pkg\nversion: 2.0.0\n")
        hooks_dir = pkg_dir / ".apm" / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "pre-tool.json").write_text("{}")

        # Create a package with bin
        bin_pkg = project / "apm_modules" / "tool-pkg"
        bin_pkg.mkdir(parents=True)
        (bin_pkg / "apm.yml").write_text("name: tool-pkg\nversion: 1.0.0\n")
        bin_dir = bin_pkg / "bin"
        bin_dir.mkdir()
        (bin_dir / "my-tool").write_text("#!/bin/sh")

        # Create a text-only package (no executables)
        safe_pkg = project / "apm_modules" / "safe-pkg"
        safe_pkg.mkdir(parents=True)
        (safe_pkg / "apm.yml").write_text("name: safe-pkg\nversion: 1.0.0\n")

        return project

    def test_approve_pending_shows_unapproved(self, tmp_path: Path, monkeypatch) -> None:
        from click.testing import CliRunner

        from apm_cli.commands.approve import approve_cmd

        project = self._setup_project(tmp_path)
        monkeypatch.chdir(project)

        runner = CliRunner()
        result = runner.invoke(approve_cmd, ["--pending"])

        assert result.exit_code == 0
        assert "risky-pkg" in result.output
        assert "tool-pkg" in result.output

    def test_approve_pending_all_approved(self, tmp_path: Path, monkeypatch) -> None:
        from click.testing import CliRunner

        from apm_cli.commands.approve import approve_cmd

        project = self._setup_project(tmp_path)
        manifest = project / "apm.yml"
        manifest.write_text(
            "name: test\nallowExecutables:\n"
            "  risky-pkg#2.0.0:\n    hooks: true\n"
            "  tool-pkg#1.0.0:\n    bin: true\n"
        )
        monkeypatch.chdir(project)

        runner = CliRunner()
        result = runner.invoke(approve_cmd, ["--pending"])

        assert result.exit_code == 0
        assert "All packages with executables are approved" in result.output

    def test_approve_specific_package(self, tmp_path: Path, monkeypatch) -> None:
        from click.testing import CliRunner

        from apm_cli.commands.approve import approve_cmd

        project = self._setup_project(tmp_path)
        monkeypatch.chdir(project)

        runner = CliRunner()
        result = runner.invoke(approve_cmd, ["risky-pkg"])

        assert result.exit_code == 0
        assert "Approved" in result.output
        assert "risky-pkg" in result.output

        # Verify manifest was updated (unified executables.allow noun)
        from apm_cli.utils.yaml_io import load_yaml

        data = load_yaml(project / "apm.yml")
        assert "executables" in data
        assert "risky-pkg#2.0.0" in data["executables"]["allow"]

    def test_approve_all(self, tmp_path: Path, monkeypatch) -> None:
        from click.testing import CliRunner

        from apm_cli.commands.approve import approve_cmd

        project = self._setup_project(tmp_path)
        monkeypatch.chdir(project)

        runner = CliRunner()
        result = runner.invoke(approve_cmd, ["--all"])

        assert result.exit_code == 0
        assert "Approved" in result.output

        from apm_cli.utils.yaml_io import load_yaml

        data = load_yaml(project / "apm.yml")
        allow = data["executables"]["allow"]
        assert "risky-pkg#2.0.0" in allow
        assert "tool-pkg#1.0.0" in allow

    def test_approve_nonexistent_package(self, tmp_path: Path, monkeypatch) -> None:
        from click.testing import CliRunner

        from apm_cli.commands.approve import approve_cmd

        project = self._setup_project(tmp_path)
        monkeypatch.chdir(project)

        runner = CliRunner()
        result = runner.invoke(approve_cmd, ["nonexistent"])

        assert result.exit_code == 0
        assert "not found" in result.output

    def test_approve_safe_package_no_executables(self, tmp_path: Path, monkeypatch) -> None:
        from click.testing import CliRunner

        from apm_cli.commands.approve import approve_cmd

        project = self._setup_project(tmp_path)
        monkeypatch.chdir(project)

        runner = CliRunner()
        result = runner.invoke(approve_cmd, ["safe-pkg"])

        assert result.exit_code == 0
        # safe-pkg has no executables so _scan_installed_packages skips it
        assert "not found" in result.output

    def test_approve_no_args_errors(self, tmp_path: Path, monkeypatch) -> None:
        from click.testing import CliRunner

        from apm_cli.commands.approve import approve_cmd

        project = self._setup_project(tmp_path)
        monkeypatch.chdir(project)

        runner = CliRunner()
        result = runner.invoke(approve_cmd, [])

        assert result.exit_code == 1

    def test_deny_package(self, tmp_path: Path, monkeypatch) -> None:
        from click.testing import CliRunner

        from apm_cli.commands.approve import deny_cmd

        project = self._setup_project(tmp_path)
        manifest = project / "apm.yml"
        manifest.write_text(
            "name: test\nallowExecutables:\n"
            "  risky-pkg#2.0.0:\n    hooks: true\n"
            "  tool-pkg#1.0.0:\n    bin: true\n"
        )
        monkeypatch.chdir(project)

        runner = CliRunner()
        result = runner.invoke(deny_cmd, ["risky-pkg"])

        assert result.exit_code == 0
        assert "Denied" in result.output

        from apm_cli.utils.yaml_io import load_yaml

        data = load_yaml(manifest)
        # deny migrates the legacy alias onto the unified noun
        deny = data["executables"]["deny"]
        assert "risky-pkg#2.0.0" in deny
        # tool-pkg should remain allowed, not denied
        assert "tool-pkg#1.0.0" in data["executables"]["allow"]
        assert "tool-pkg#1.0.0" not in deny

    def test_deny_nonexistent_package(self, tmp_path: Path, monkeypatch) -> None:
        from click.testing import CliRunner

        from apm_cli.commands.approve import deny_cmd

        project = self._setup_project(tmp_path)
        manifest = project / "apm.yml"
        manifest.write_text("name: test\nallowExecutables:\n  pkg#1:\n    hooks: true\n")
        monkeypatch.chdir(project)

        runner = CliRunner()
        result = runner.invoke(deny_cmd, ["missing"])

        assert result.exit_code == 0
        # deny may target a not-yet-installed package (a pre-emptive block)
        assert "Denied missing" in result.output

        from apm_cli.utils.yaml_io import load_yaml

        data = load_yaml(manifest)
        assert "missing" in data["executables"]["deny"]

    def test_approve_no_manifest_errors(self, tmp_path: Path, monkeypatch) -> None:
        from click.testing import CliRunner

        from apm_cli.commands.approve import approve_cmd

        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        result = runner.invoke(approve_cmd, ["--pending"])

        assert result.exit_code == 1
        assert "No apm.yml" in result.output

    def test_deny_no_manifest_errors(self, tmp_path: Path, monkeypatch) -> None:
        from click.testing import CliRunner

        from apm_cli.commands.approve import deny_cmd

        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        result = runner.invoke(deny_cmd, ["pkg"])

        assert result.exit_code == 1
        assert "No apm.yml" in result.output

    def test_approve_all_already_approved(self, tmp_path: Path, monkeypatch) -> None:
        from click.testing import CliRunner

        from apm_cli.commands.approve import approve_cmd

        project = self._setup_project(tmp_path)
        manifest = project / "apm.yml"
        manifest.write_text(
            "name: test\nallowExecutables:\n"
            "  risky-pkg#2.0.0:\n    hooks: true\n"
            "  tool-pkg#1.0.0:\n    bin: true\n"
        )
        monkeypatch.chdir(project)

        runner = CliRunner()
        result = runner.invoke(approve_cmd, ["--all"])

        assert result.exit_code == 0
        assert "already approved" in result.output
