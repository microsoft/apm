"""Tests for the unified executables vocabulary layer (issue #1873).

Covers:
- ``parse_project_executables`` (new ``executables:{allow,deny}`` block +
  the deprecated ``allowExecutables`` alias).
- The user-local consent store on ``~/.apm/config.json`` plus the one-time
  migration that folds and DELETES the legacy ``~/.apm/approvals.yml``.
- ``build_exec_trust_context`` assembling org / project / user inputs.
- Filesystem cases of ``apm_cli.security.executables``: package scanning,
  ``write_allow_executables``, local bundle hashing and atomic user saves.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

import apm_cli.security.executables as ex
from apm_cli.policy.schema import ApmPolicy, BinDeployPolicy, ExecutablesPolicy
from apm_cli.security.executables import (
    EXEC_TYPE_BIN,
    EXEC_TYPE_CANVAS,
    EXEC_TYPE_HOOKS,
    EXEC_TYPE_LSP,
    EXEC_TYPE_MCP,
    build_exec_trust_context,
    effective_exec_map_for_project,
    load_user_executables,
    local_bundle_approval_key,
    parse_project_executables,
    save_user_executables,
    scan_package_executables,
    write_allow_executables,
)

pytestmark = pytest.mark.component


class TestParseProjectExecutables:
    def test_absent_returns_empty_no_deprecation(self):
        allow, deny, deprecated = parse_project_executables({})
        assert allow == {}
        assert deny == {}
        assert deprecated is False

    def test_new_block_allow_and_deny(self):
        data = {
            "executables": {
                "allow": {"owner/repo": {"hooks": True}},
                "deny": {"bad/pkg": {"bin": True}},
            }
        }
        allow, deny, deprecated = parse_project_executables(data)
        assert allow == {"owner/repo": {"hooks": True}}
        assert deny == {"bad/pkg": {"bin": True}}
        assert deprecated is False

    def test_allow_executables_alias_sets_deprecation_flag(self):
        data = {"allowExecutables": {"owner/repo": {"hooks": True}}}
        allow, _deny, deprecated = parse_project_executables(data)
        assert allow == {"owner/repo": {"hooks": True}}
        assert deprecated is True

    def test_new_block_wins_over_alias_on_conflict(self):
        data = {
            "allowExecutables": {"owner/repo": {"hooks": False}},
            "executables": {"allow": {"owner/repo": {"hooks": True}}},
        }
        allow, _deny, deprecated = parse_project_executables(data)
        assert allow["owner/repo"]["hooks"] is True
        assert deprecated is True


class TestUserExecutablesStore:
    def test_roundtrip_via_config(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.json"
        monkeypatch.setattr(ex, "_user_config_file", lambda: cfg)
        monkeypatch.setattr(ex, "_legacy_approvals_path", lambda: tmp_path / "approvals.yml")

        save_user_executables({"owner/repo": {"hooks": True}}, {"bad/pkg": {"bin": True}})
        allow, deny = load_user_executables()
        assert allow == {"owner/repo": {"hooks": True}}
        assert deny == {"bad/pkg": {"bin": True}}
        stored = json.loads(cfg.read_text(encoding="utf-8"))["executables"]
        assert stored == {"allow": allow, "deny": deny}

    def test_migrates_and_deletes_legacy_approvals_yml(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.json"
        legacy = tmp_path / "approvals.yml"
        monkeypatch.setattr(ex, "_user_config_file", lambda: cfg)
        monkeypatch.setattr(ex, "_legacy_approvals_path", lambda: legacy)

        from apm_cli.utils.yaml_io import dump_yaml

        dump_yaml({"owner/repo": {"hooks": True}}, legacy)
        assert legacy.exists()

        allow, _deny = load_user_executables()
        # Legacy approvals folded into the allow set.
        assert allow.get("owner/repo", {}).get("hooks") is True
        # net-new control-surface files = 0: the legacy file is removed.
        assert not legacy.exists()


class TestReadBundleAllowExecutables:
    def test_canonical_empty_allow_enables_bundle_gate(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ex, "_user_config_file", lambda: tmp_path / "config.json")
        monkeypatch.setattr(ex, "_legacy_approvals_path", lambda: tmp_path / "approvals.yml")
        manifest = tmp_path / "apm.yml"
        manifest.write_text("executables:\n  allow: {}\n", encoding="utf-8")

        assert effective_exec_map_for_project(tmp_path, policy=None) == {}

    def test_canonical_allow_is_accepted_for_bundle_gate(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ex, "_user_config_file", lambda: tmp_path / "config.json")
        monkeypatch.setattr(ex, "_legacy_approvals_path", lambda: tmp_path / "approvals.yml")
        manifest = tmp_path / "apm.yml"
        manifest.write_text(
            "executables:\n  allow:\n    test-plugin:\n      canvas: true\n",
            encoding="utf-8",
        )

        assert effective_exec_map_for_project(tmp_path, policy=None) == {
            "test-plugin": {"canvas": True}
        }

    def test_dry_run_bundle_read_does_not_migrate_legacy_user_approvals(
        self, tmp_path, monkeypatch
    ):
        config = tmp_path / "config.json"
        legacy = tmp_path / "approvals.yml"
        monkeypatch.setattr(ex, "_user_config_file", lambda: config)
        monkeypatch.setattr(ex, "_legacy_approvals_path", lambda: legacy)
        manifest = tmp_path / "apm.yml"
        manifest.write_text("executables:\n  allow: {}\n", encoding="utf-8")
        legacy.write_text("owner/repo:\n  hooks: true\n", encoding="utf-8")

        result = effective_exec_map_for_project(
            tmp_path,
            policy=None,
            migrate_user_legacy=False,
        )

        assert result == {"owner/repo": {"hooks": True}}
        assert legacy.exists()
        assert not config.exists()


class TestBuildExecTrustContext:
    def test_org_executables_block_enables_gate_fleetwide(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ex, "_user_config_file", lambda: tmp_path / "c.json")
        monkeypatch.setattr(ex, "_legacy_approvals_path", lambda: tmp_path / "a.yml")
        policy = ApmPolicy(executables=ExecutablesPolicy(deny=("bad/pkg",)))
        ctx = build_exec_trust_context(policy=policy, project_data={})
        assert ctx.gate_enabled is True
        assert "bad/pkg" in ctx.org_deny

    def test_legacy_bin_deploy_maps_to_bin_deny(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ex, "_user_config_file", lambda: tmp_path / "c.json")
        monkeypatch.setattr(ex, "_legacy_approvals_path", lambda: tmp_path / "a.yml")
        policy = ApmPolicy(bin_deploy=BinDeployPolicy(deny=("https://github.com/BAD/PKG.git",)))
        ctx = build_exec_trust_context(policy=policy, project_data={})
        assert "bad/pkg" in ctx.org_bin_deny

    def test_project_block_enables_gate(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ex, "_user_config_file", lambda: tmp_path / "c.json")
        monkeypatch.setattr(ex, "_legacy_approvals_path", lambda: tmp_path / "a.yml")
        data = {"executables": {"allow": {"owner/repo": {"hooks": True}}}}
        ctx = build_exec_trust_context(policy=None, project_data=data)
        assert ctx.gate_enabled is True
        assert ctx.project_allow == {"owner/repo": {"hooks": True}}

    def test_no_signals_gate_disabled(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ex, "_user_config_file", lambda: tmp_path / "c.json")
        monkeypatch.setattr(ex, "_legacy_approvals_path", lambda: tmp_path / "a.yml")
        ctx = build_exec_trust_context(policy=None, project_data={})
        assert ctx.gate_enabled is False

    def test_user_deny_alone_enables_gate(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ex, "_user_config_file", lambda: tmp_path / "c.json")
        monkeypatch.setattr(ex, "_legacy_approvals_path", lambda: tmp_path / "a.yml")
        save_user_executables({}, {"owner/repo": {EXEC_TYPE_HOOKS: True}})
        ctx = build_exec_trust_context(policy=None, project_data={})
        assert ctx.gate_enabled is True
        assert (tmp_path / "c.json").is_file()

    def test_user_allow_alone_does_not_enable_gate(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ex, "_user_config_file", lambda: tmp_path / "c.json")
        monkeypatch.setattr(ex, "_legacy_approvals_path", lambda: tmp_path / "a.yml")
        save_user_executables({"owner/repo": {EXEC_TYPE_HOOKS: True}}, {})
        ctx = build_exec_trust_context(policy=None, project_data={})
        assert ctx.gate_enabled is False
        assert (tmp_path / "c.json").is_file()

    def test_user_consent_flows_into_context(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ex, "_user_config_file", lambda: tmp_path / "c.json")
        monkeypatch.setattr(ex, "_legacy_approvals_path", lambda: tmp_path / "a.yml")
        save_user_executables({"owner/repo": {EXEC_TYPE_HOOKS: True}}, {})
        data = {"executables": {"allow": {}}}
        ctx = build_exec_trust_context(policy=None, project_data=data)
        assert ctx.user_allow.get("owner/repo", {}).get(EXEC_TYPE_HOOKS) is True
        assert EXEC_TYPE_BIN not in ctx.user_allow.get("owner/repo", {})
        assert (tmp_path / "c.json").is_file()


class TestLocalBundleApprovalKey:
    def test_hash_includes_normally_excluded_directories(self, tmp_path: Path) -> None:
        payload = tmp_path / "__pycache__" / "server.py"
        payload.parent.mkdir()
        payload.write_text("first", encoding="utf-8")
        first = local_bundle_approval_key("demo", "", tmp_path)

        payload.write_text("second", encoding="utf-8")

        assert local_bundle_approval_key("demo", "", tmp_path) != first

    def test_hash_rejects_symlinks(self, tmp_path: Path) -> None:
        target = tmp_path / "target"
        target.write_text("payload", encoding="utf-8")
        link = tmp_path / "link"
        try:
            link.symlink_to(target)
        except OSError as exc:
            pytest.skip(f"file symlinks unavailable: {exc}")

        with pytest.raises(ValueError, match="contains a symlink"):
            local_bundle_approval_key("demo", "", tmp_path)

    def test_locked_hash_uses_manifest_without_reading_payloads(self, tmp_path: Path) -> None:
        lockfile = {"pack": {"bundle_files": {"extension.mjs": "a" * 64}}}

        with patch.object(Path, "read_bytes") as read_bytes:
            key = local_bundle_approval_key("demo", "1", tmp_path, lockfile)

        read_bytes.assert_not_called()
        assert key.startswith("demo#1@sha256:")


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

    def test_detects_lsp_from_apm_yml(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "apm.yml").write_text(
                yaml.dump(
                    {
                        "name": "lsp-pkg",
                        "dependencies": {
                            "lsp": [
                                {"name": "pyright", "command": "pyright-langserver"},
                                "typescript-language-server",
                            ]
                        },
                    }
                ),
                encoding="utf-8",
            )

            decl = scan_package_executables(Path(tmpdir), "lsp-pkg", "1.0")
            assert decl.lsp_count == 2
            assert EXEC_TYPE_LSP in decl.exec_types
            assert decl.lsp_details == ["pyright", "typescript-language-server"]

    def test_preparsed_manifest_avoids_a_second_yaml_read(self, tmp_path) -> None:
        manifest_data = {
            "dependencies": {"lsp": [{"name": "pyright", "command": "pyright-langserver"}]}
        }
        with patch("apm_cli.utils.yaml_io.load_yaml") as load_yaml:
            decl = scan_package_executables(
                tmp_path,
                "lsp-pkg",
                "1.0",
                manifest_data=manifest_data,
            )

        load_yaml.assert_not_called()
        assert decl.lsp_count == 1

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

    def test_detects_canvas_extensions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ext_dir = Path(tmpdir) / ".apm" / "extensions" / "widget"
            ext_dir.mkdir(parents=True)
            (ext_dir / "extension.mjs").write_text("export default {};")
            # A sibling directory without the marker is ignored.
            (Path(tmpdir) / ".apm" / "extensions" / "nomarker").mkdir()

            decl = scan_package_executables(Path(tmpdir), "canvas-pkg", "1.0")
            assert decl.canvas_count == 1
            assert "widget" in decl.canvas_details
            assert EXEC_TYPE_CANVAS in decl.exec_types


class TestWriteAllowExecutables:
    """Tests for write_allow_executables round-trip."""

    def test_writes_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = Path(tmpdir) / "apm.yml"
            manifest.write_text(yaml.dump({"name": "my-project", "version": "1.0"}))

            allow = {"pkg#1.0": {"hooks": True}}
            write_allow_executables(manifest, allow)

            data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
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

            data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
            assert "allowExecutables" not in data

    def test_preserves_other_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = Path(tmpdir) / "apm.yml"
            manifest.write_text(
                yaml.dump({"name": "my-project", "version": "1.0", "description": "test"})
            )

            allow = {"pkg#1.0": {"bin": True}}
            write_allow_executables(manifest, allow)

            data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
            assert data["name"] == "my-project"
            assert data["version"] == "1.0"
            assert data["description"] == "test"
            assert data["allowExecutables"]["pkg#1.0"]["bin"] is True


class TestSaveUserExecutablesAtomic:
    """S3 (#1873): personal consent writes are atomic and owner-only."""

    def test_atomic_write_sets_owner_only_mode(self, tmp_path, monkeypatch) -> None:
        import json

        import apm_cli.security.executables as ex

        cfg = tmp_path / "config.json"
        monkeypatch.setattr(ex, "_user_config_file", lambda: cfg)
        ex.save_user_executables({"pkg#1.0": {"hooks": True}}, {})
        assert cfg.is_file()
        # Owner-only perms on the freshly-created file. POSIX mode bits are not
        # enforced on Windows, where atomic_write_text documents that the mode
        # hint is silently ignored, so only assert the permission contract on
        # platforms that honour it.
        if os.name != "nt":
            assert (cfg.stat().st_mode & 0o777) == 0o600
        # Content round-trips and preserves co-resident keys on rewrite.
        cfg.write_text(json.dumps({"default_client": "vscode"}), encoding="utf-8")
        ex.save_user_executables({"pkg#1.0": {"hooks": True}}, {})
        data = json.loads(cfg.read_text(encoding="utf-8"))
        assert data["default_client"] == "vscode"
        assert data["executables"]["allow"]["pkg#1.0"]["hooks"] is True
        # No leftover temp files in the dir.
        assert [p.name for p in tmp_path.iterdir()] == ["config.json"]
