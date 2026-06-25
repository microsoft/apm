"""Unit tests for ``apm_cli.security.executables``.

Covers:
- ``ExecutableDeclaration`` data model properties
- ``is_package_approved`` and ``is_any_type_approved`` checking logic
- ``build_approval_key`` construction
- ``scan_package_executables`` filesystem scanning
- ``parse_allow_executables`` validation
- ``write_allow_executables`` round-trip
- ``prompt_executable_approval`` interactive / CI / trust-all / no-executables paths
- ``_is_fully_approved`` helper
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from apm_cli.security.executables import (
    EXEC_TYPE_BIN,
    EXEC_TYPE_HOOKS,
    EXEC_TYPE_MCP,
    ExecutableDeclaration,
    _is_fully_approved,
    build_approval_key,
    is_any_type_approved,
    is_package_approved,
    parse_allow_executables,
    prompt_executable_approval,
    scan_package_executables,
    write_allow_executables,
)

# ---------------------------------------------------------------------------
# ExecutableDeclaration
# ---------------------------------------------------------------------------


class TestExecutableDeclaration:
    """Tests for the ExecutableDeclaration data model."""

    def test_has_executables_false_when_all_zero(self) -> None:
        decl = ExecutableDeclaration(package_key="a#1.0", package_name="a")
        assert not decl.has_executables

    def test_has_executables_true_with_hooks(self) -> None:
        decl = ExecutableDeclaration(package_key="a#1.0", package_name="a", hook_count=2)
        assert decl.has_executables

    def test_has_executables_true_with_mcp_only(self) -> None:
        """MCP-only packages are now flagged (MCP enforcement is active)."""
        decl = ExecutableDeclaration(package_key="a#1.0", package_name="a", mcp_count=1)
        assert decl.has_executables

    def test_has_executables_true_with_bin(self) -> None:
        decl = ExecutableDeclaration(package_key="a#1.0", package_name="a", bin_count=3)
        assert decl.has_executables

    def test_exec_types_empty(self) -> None:
        decl = ExecutableDeclaration(package_key="a#1.0", package_name="a")
        assert decl.exec_types == []

    def test_exec_types_all(self) -> None:
        """exec_types includes all enforced types (hooks, mcp, bin, canvas)."""
        decl = ExecutableDeclaration(
            package_key="a#1.0",
            package_name="a",
            hook_count=1,
            mcp_count=1,
            bin_count=1,
        )
        assert EXEC_TYPE_HOOKS in decl.exec_types
        assert EXEC_TYPE_BIN in decl.exec_types
        assert EXEC_TYPE_MCP in decl.exec_types

    def test_exec_types_partial(self) -> None:
        decl = ExecutableDeclaration(
            package_key="a#1.0", package_name="a", hook_count=1, bin_count=2
        )
        assert decl.exec_types == [EXEC_TYPE_HOOKS, EXEC_TYPE_BIN]

    def test_summary_line(self) -> None:
        """summary_line shows all enforced types (hooks, mcp, bin, canvas)."""
        decl = ExecutableDeclaration(
            package_key="a#1.0",
            package_name="a",
            hook_count=2,
            mcp_count=1,
            bin_count=3,
        )
        summary = decl.summary_line()
        assert "2 hook(s)" in summary
        assert "1 MCP server(s)" in summary
        assert "3 bin executable(s)" in summary

    def test_summary_line_hooks_only(self) -> None:
        decl = ExecutableDeclaration(package_key="a#1.0", package_name="a", hook_count=1)
        assert decl.summary_line() == "1 hook(s)"

    def test_is_frozen(self) -> None:
        decl = ExecutableDeclaration(package_key="a#1.0", package_name="a")
        try:
            decl.hook_count = 5  # type: ignore[misc]
            raise AssertionError("Expected FrozenInstanceError")
        except AttributeError:
            pass


# ---------------------------------------------------------------------------
# is_package_approved
# ---------------------------------------------------------------------------


class TestIsPackageApproved:
    """Tests for is_package_approved."""

    def test_none_allow_executables_returns_false(self) -> None:
        assert not is_package_approved(None, "a#1.0", EXEC_TYPE_HOOKS)

    def test_empty_allow_executables_returns_false(self) -> None:
        assert not is_package_approved({}, "a#1.0", EXEC_TYPE_HOOKS)

    def test_missing_key_returns_false(self) -> None:
        allow = {"b#1.0": {"hooks": True}}
        assert not is_package_approved(allow, "a#1.0", EXEC_TYPE_HOOKS)

    def test_wrong_exec_type_returns_false(self) -> None:
        allow = {"a#1.0": {"hooks": True}}
        assert not is_package_approved(allow, "a#1.0", EXEC_TYPE_BIN)

    def test_approved_returns_true(self) -> None:
        allow = {"a#1.0": {"hooks": True, "bin": True}}
        assert is_package_approved(allow, "a#1.0", EXEC_TYPE_HOOKS)
        assert is_package_approved(allow, "a#1.0", EXEC_TYPE_BIN)

    def test_false_value_returns_false(self) -> None:
        allow = {"a#1.0": {"hooks": False}}
        assert not is_package_approved(allow, "a#1.0", EXEC_TYPE_HOOKS)

    def test_non_dict_entry_returns_false(self) -> None:
        allow = {"a#1.0": True}  # type: ignore[dict-item]
        assert not is_package_approved(allow, "a#1.0", EXEC_TYPE_HOOKS)


# ---------------------------------------------------------------------------
# is_any_type_approved
# ---------------------------------------------------------------------------


class TestIsAnyTypeApproved:
    """Tests for is_any_type_approved."""

    def test_none_returns_false(self) -> None:
        assert not is_any_type_approved(None, "a#1.0")

    def test_empty_returns_false(self) -> None:
        assert not is_any_type_approved({}, "a#1.0")

    def test_any_true_returns_true(self) -> None:
        allow = {"a#1.0": {"mcp": True}}
        assert is_any_type_approved(allow, "a#1.0")

    def test_all_false_returns_false(self) -> None:
        allow = {"a#1.0": {"hooks": False, "mcp": False, "bin": False}}
        assert not is_any_type_approved(allow, "a#1.0")


# ---------------------------------------------------------------------------
# build_approval_key
# ---------------------------------------------------------------------------


class TestBuildApprovalKey:
    """Tests for build_approval_key."""

    def test_with_version(self) -> None:
        assert build_approval_key("owner/repo", "v1.0") == "owner/repo#v1.0"

    def test_empty_version(self) -> None:
        assert build_approval_key("owner/repo", "") == "owner/repo"

    def test_marketplace_format(self) -> None:
        assert build_approval_key("ci-hooks@acme", "1.2.0") == "ci-hooks@acme#1.2.0"


# ---------------------------------------------------------------------------
# scan_package_executables
# ---------------------------------------------------------------------------


class TestScanPackageExecutables:
    """Tests for scan_package_executables."""

    def test_empty_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            decl = scan_package_executables(Path(tmpdir), "test-pkg", "1.0")
            assert not decl.has_executables
            assert decl.package_key == "test-pkg#1.0"
            assert decl.hook_count == 0
            assert decl.mcp_count == 0
            assert decl.bin_count == 0

    def test_detects_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            hook_dir = Path(tmpdir) / ".apm" / "hooks"
            hook_dir.mkdir(parents=True)
            (hook_dir / "pre-tool-use.json").write_text("{}")
            (hook_dir / "post-tool-use.json").write_text("{}")

            decl = scan_package_executables(Path(tmpdir), "hooks-pkg", "2.0")
            assert decl.hook_count == 2
            assert decl.has_executables
            assert EXEC_TYPE_HOOKS in decl.exec_types
            assert "pre-tool-use.json" in decl.hook_details
            assert "post-tool-use.json" in decl.hook_details

    def test_detects_bin(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            bin_dir = Path(tmpdir) / "bin"
            bin_dir.mkdir()
            (bin_dir / "tool1").write_text("#!/bin/sh\necho hi")
            (bin_dir / "tool2").write_text("#!/bin/sh\necho bye")
            # Hidden files should be ignored
            (bin_dir / ".hidden").write_text("ignored")

            decl = scan_package_executables(Path(tmpdir), "bin-pkg", "3.0")
            assert decl.bin_count == 2
            assert EXEC_TYPE_BIN in decl.exec_types
            assert "tool1" in decl.bin_details
            assert "tool2" in decl.bin_details

    def test_detects_mcp_from_apm_yml(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            apm_yml = Path(tmpdir) / "apm.yml"
            apm_yml.write_text(
                yaml.dump(
                    {
                        "name": "mcp-pkg",
                        "version": "1.0",
                        "dependencies": {
                            "mcp": [
                                "server-a",
                                {"name": "server-b", "command": "node"},
                            ]
                        },
                    }
                )
            )
            decl = scan_package_executables(Path(tmpdir), "mcp-pkg", "1.0")
            assert decl.mcp_count == 2
            # MCP is now enforced so it appears in exec_types.
            assert EXEC_TYPE_MCP in decl.exec_types
            assert "server-a" in decl.mcp_details

    def test_transitive_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            decl = scan_package_executables(
                Path(tmpdir),
                "trans-pkg",
                "1.0",
                is_transitive=True,
                parent_name="parent-pkg",
            )
            assert decl.is_transitive
            assert decl.parent_name == "parent-pkg"

    def test_combined_hooks_and_bin(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            hook_dir = Path(tmpdir) / ".apm" / "hooks"
            hook_dir.mkdir(parents=True)
            (hook_dir / "validate.json").write_text("{}")

            bin_dir = Path(tmpdir) / "bin"
            bin_dir.mkdir()
            (bin_dir / "cli-tool").write_text("#!/bin/sh")

            decl = scan_package_executables(Path(tmpdir), "combined", "1.0")
            assert decl.hook_count == 1
            assert decl.bin_count == 1
            assert decl.exec_types == [EXEC_TYPE_HOOKS, EXEC_TYPE_BIN]


# ---------------------------------------------------------------------------
# parse_allow_executables
# ---------------------------------------------------------------------------


class TestParseAllowExecutables:
    """Tests for parse_allow_executables validation."""

    def test_absent_returns_none(self) -> None:
        assert parse_allow_executables({}) is None

    def test_valid_block(self) -> None:
        data = {
            "allowExecutables": {
                "ci-hooks@acme#1.2.0": {"hooks": True, "bin": False},
                "mcp-tools@corp#2.0": {"mcp": True},
            }
        }
        result = parse_allow_executables(data)
        assert result is not None
        assert result["ci-hooks@acme#1.2.0"]["hooks"] is True
        assert result["ci-hooks@acme#1.2.0"]["bin"] is False
        assert result["mcp-tools@corp#2.0"]["mcp"] is True

    def test_empty_block(self) -> None:
        data = {"allowExecutables": {}}
        result = parse_allow_executables(data)
        assert result == {}

    def test_non_dict_top_level_raises(self) -> None:
        data = {"allowExecutables": "invalid"}
        try:
            parse_allow_executables(data)
            raise AssertionError("Expected ValueError")
        except ValueError as e:
            assert "must be a mapping" in str(e)

    def test_non_dict_entry_raises(self) -> None:
        data = {"allowExecutables": {"pkg#1.0": True}}
        try:
            parse_allow_executables(data)
            raise AssertionError("Expected ValueError")
        except ValueError as e:
            assert "must be a mapping" in str(e)

    def test_non_bool_value_raises(self) -> None:
        data = {"allowExecutables": {"pkg#1.0": {"hooks": "yes"}}}
        try:
            parse_allow_executables(data)
            raise AssertionError("Expected ValueError")
        except ValueError as e:
            assert "must be a boolean" in str(e)

    def test_unknown_exec_type_raises(self) -> None:
        data = {"allowExecutables": {"pkg#1.0": {"hokks": True}}}
        try:
            parse_allow_executables(data)
            raise AssertionError("Expected ValueError")
        except ValueError as e:
            assert "unknown exec type" in str(e)
            assert "hokks" in str(e)


# ---------------------------------------------------------------------------
# write_allow_executables
# ---------------------------------------------------------------------------


class TestWriteAllowExecutables:
    """Tests for write_allow_executables round-trip."""

    def test_writes_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = Path(tmpdir) / "apm.yml"
            manifest.write_text(yaml.dump({"name": "my-project", "version": "1.0"}))

            allow = {"pkg#1.0": {"hooks": True}}
            write_allow_executables(manifest, allow)

            from apm_cli.utils.yaml_io import load_yaml

            data = load_yaml(manifest)
            assert data["allowExecutables"] == {"pkg#1.0": {"hooks": True}}

    def test_removes_block_when_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = Path(tmpdir) / "apm.yml"
            manifest.write_text(
                yaml.dump(
                    {
                        "name": "my-project",
                        "allowExecutables": {"old#1.0": {"hooks": True}},
                    }
                )
            )

            write_allow_executables(manifest, {})

            from apm_cli.utils.yaml_io import load_yaml

            data = load_yaml(manifest)
            assert "allowExecutables" not in data

    def test_preserves_other_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = Path(tmpdir) / "apm.yml"
            manifest.write_text(
                yaml.dump({"name": "my-project", "version": "1.0", "description": "test"})
            )

            allow = {"pkg#1.0": {"bin": True}}
            write_allow_executables(manifest, allow)

            from apm_cli.utils.yaml_io import load_yaml

            data = load_yaml(manifest)
            assert data["name"] == "my-project"
            assert data["version"] == "1.0"
            assert data["description"] == "test"
            assert data["allowExecutables"]["pkg#1.0"]["bin"] is True


# ---------------------------------------------------------------------------
# _is_fully_approved
# ---------------------------------------------------------------------------


class TestIsFullyApproved:
    """Tests for _is_fully_approved helper."""

    def test_all_types_approved(self) -> None:
        allow = {"a#1.0": {"hooks": True, "bin": True}}
        decl = ExecutableDeclaration(
            package_key="a#1.0", package_name="a", hook_count=1, bin_count=1
        )
        assert _is_fully_approved(allow, decl)

    def test_partial_approval(self) -> None:
        allow = {"a#1.0": {"hooks": True}}
        decl = ExecutableDeclaration(
            package_key="a#1.0", package_name="a", hook_count=1, bin_count=1
        )
        assert not _is_fully_approved(allow, decl)

    def test_no_entry(self) -> None:
        decl = ExecutableDeclaration(package_key="a#1.0", package_name="a", hook_count=1)
        assert not _is_fully_approved({}, decl)


# ---------------------------------------------------------------------------
# prompt_executable_approval
# ---------------------------------------------------------------------------


class TestPromptExecutableApproval:
    """Tests for prompt_executable_approval flow."""

    def _make_decl(
        self,
        key: str = "pkg#1.0",
        name: str = "pkg",
        hooks: int = 1,
        bins: int = 0,
    ) -> ExecutableDeclaration:
        return ExecutableDeclaration(
            package_key=key,
            package_name=name,
            hook_count=hooks,
            bin_count=bins,
        )

    def test_trust_all_approves_everything(self) -> None:
        decl = self._make_decl()
        result = prompt_executable_approval([decl], trust_all=True)
        assert "pkg#1.0" in result
        assert result["pkg#1.0"]["hooks"] is True

    def test_no_executables_denies_everything(self) -> None:
        decl = self._make_decl()
        result = prompt_executable_approval([decl], no_executables=True)
        assert "pkg#1.0" not in result

    def test_already_approved_skipped(self) -> None:
        decl = self._make_decl()
        existing = {"pkg#1.0": {"hooks": True}}
        result = prompt_executable_approval([decl], allow_executables=existing, trust_all=True)
        # Should preserve existing
        assert result["pkg#1.0"]["hooks"] is True

    def test_no_pending_returns_existing(self) -> None:
        decl = self._make_decl()
        existing = {"pkg#1.0": {"hooks": True}}
        result = prompt_executable_approval([decl], allow_executables=existing)
        assert result == existing

    def test_non_interactive_exits(self) -> None:
        decl = self._make_decl()
        with patch("apm_cli.security.executables._is_interactive", return_value=False):
            try:
                prompt_executable_approval([decl])
                raise AssertionError("Expected SystemExit")
            except SystemExit as e:
                assert e.code == 1

    def test_empty_declarations_returns_existing(self) -> None:
        existing = {"old#1.0": {"hooks": True}}
        result = prompt_executable_approval([], allow_executables=existing)
        assert result == existing

    def test_no_executable_declarations_returns_existing(self) -> None:
        # Declaration with no actual executables
        decl = ExecutableDeclaration(package_key="a#1.0", package_name="a")
        result = prompt_executable_approval([decl], trust_all=True)
        assert "a#1.0" not in result

    def test_trust_all_with_multiple_types(self) -> None:
        decl = self._make_decl(hooks=2, bins=3)
        result = prompt_executable_approval([decl], trust_all=True)
        assert result["pkg#1.0"]["hooks"] is True
        assert result["pkg#1.0"]["bin"] is True

    @patch("apm_cli.security.executables._is_interactive", return_value=True)
    @patch("click.confirm", return_value=True)
    def test_interactive_approve(self, mock_confirm, mock_interactive) -> None:
        decl = self._make_decl()
        result = prompt_executable_approval([decl])
        assert "pkg#1.0" in result
        assert result["pkg#1.0"]["hooks"] is True

    @patch("apm_cli.security.executables._is_interactive", return_value=True)
    @patch("click.confirm", return_value=False)
    def test_interactive_deny(self, mock_confirm, mock_interactive) -> None:
        decl = self._make_decl()
        result = prompt_executable_approval([decl])
        assert "pkg#1.0" not in result

    @patch("apm_cli.security.executables._is_interactive", return_value=False)
    def test_non_interactive_message_uses_unified_vocab(self, _mock, capsys) -> None:
        # M4 (#1873): the live install-gate message must drop the retired
        # ``allowExecutables`` noun, lead with the bulk one-liner, and offer
        # the introspection hatch.
        decl = self._make_decl()
        with pytest.raises(SystemExit):
            prompt_executable_approval([decl])
        out = capsys.readouterr().out
        assert "allowExecutables" not in out
        assert "apm approve --recommended" in out
        assert "apm policy explain" in out


class TestAliasDeprecationWarning:
    """S1 (#1873): the allowExecutables alias warns once, not per-package."""

    def test_warns_once(self, capsys) -> None:
        import apm_cli.security.executables as ex

        ex._ALIAS_DEPRECATION_WARNED = False
        ex.warn_allow_executables_alias_once()
        ex.warn_allow_executables_alias_once()
        out = capsys.readouterr().out
        assert out.count("allowExecutables") == 1
        assert "deprecated" in out
        assert "executables.allow" in out

    def test_uses_logger_when_given(self) -> None:
        import apm_cli.security.executables as ex

        ex._ALIAS_DEPRECATION_WARNED = False
        calls = []

        class _Logger:
            def warning(self, msg, symbol=None):
                calls.append(msg)

        ex.warn_allow_executables_alias_once(_Logger())
        assert len(calls) == 1
        assert "deprecated" in calls[0]


class TestSaveUserExecutablesAtomic:
    """S3 (#1873): personal consent writes are atomic and owner-only."""

    def test_atomic_write_sets_owner_only_mode(self, tmp_path, monkeypatch) -> None:
        import json

        import apm_cli.security.executables as ex

        cfg = tmp_path / "config.json"
        monkeypatch.setattr(ex, "_user_config_file", lambda: cfg)
        ex.save_user_executables({"pkg#1.0": {"hooks": True}}, {})
        assert cfg.is_file()
        # Owner-only perms on the freshly-created file.
        assert (cfg.stat().st_mode & 0o777) == 0o600
        # Content round-trips and preserves co-resident keys on rewrite.
        cfg.write_text(json.dumps({"default_client": "vscode"}), encoding="utf-8")
        ex.save_user_executables({"pkg#1.0": {"hooks": True}}, {})
        data = json.loads(cfg.read_text(encoding="utf-8"))
        assert data["default_client"] == "vscode"
        assert data["executables"]["allow"]["pkg#1.0"]["hooks"] is True
        # No leftover temp files in the dir.
        assert [p.name for p in tmp_path.iterdir()] == ["config.json"]
