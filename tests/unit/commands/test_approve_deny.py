"""Unit tests for ``apm_cli.commands.approve`` (apm approve / deny / explain).

Issue #1873 vocabulary unification: ``apm approve`` writes to the project
``apm.yml`` ``executables.allow`` block by DEFAULT (committed, admin UX), and
to ``~/.apm/config.json`` only with ``--user`` (personal, lowest authority).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import yaml
from click.testing import CliRunner

from apm_cli.commands.approve import (
    _find_matching_key,
    approve_cmd,
    deny_cmd,
    explain_cmd,
)
from apm_cli.policy.schema import ApmPolicy, ExecutablesPolicy

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_manifest(tmpdir: str, extra: dict | None = None) -> Path:
    """Write a minimal apm.yml and return its path."""
    data = {"name": "test-project", "version": "1.0"}
    if extra:
        data.update(extra)
    manifest = Path(tmpdir) / "apm.yml"
    manifest.write_text(yaml.dump(data))
    return manifest


def _create_pkg_with_hooks(apm_modules: Path, name: str) -> None:
    """Create a package directory with a hook file."""
    pkg_dir = apm_modules / name
    hook_dir = pkg_dir / ".apm" / "hooks"
    hook_dir.mkdir(parents=True)
    (hook_dir / "pre-tool-use.json").write_text("{}")
    (pkg_dir / "apm.yml").write_text(yaml.dump({"name": name, "version": "1.0"}))


def _create_pkg_with_bin(apm_modules: Path, name: str) -> None:
    """Create a package directory with bin/ executables."""
    pkg_dir = apm_modules / name
    bin_dir = pkg_dir / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "tool").write_text("#!/bin/sh")
    (pkg_dir / "apm.yml").write_text(yaml.dump({"name": name, "version": "2.0"}))


def _isolated_config(tmp_path: Path):
    """Patch the user-config + legacy-approvals seams onto tmp_path."""
    cfg = tmp_path / "config.json"
    legacy = tmp_path / "approvals.yml"
    return (
        patch("apm_cli.security.executables._user_config_file", lambda: cfg),
        patch("apm_cli.security.executables._legacy_approvals_path", lambda: legacy),
        cfg,
    )


# ---------------------------------------------------------------------------
# _find_matching_key
# ---------------------------------------------------------------------------


class TestFindMatchingKey:
    def test_exact_match(self) -> None:
        allow = {"owner/repo#v1.0": {"hooks": True}}
        assert _find_matching_key(allow, "owner/repo#v1.0") == "owner/repo#v1.0"

    def test_prefix_match(self) -> None:
        allow = {"owner/repo#v1.0": {"hooks": True}}
        assert _find_matching_key(allow, "owner/repo") == "owner/repo#v1.0"

    def test_no_match(self) -> None:
        allow = {"other/repo#v1.0": {"hooks": True}}
        assert _find_matching_key(allow, "owner/repo") is None

    def test_empty_dict(self) -> None:
        assert _find_matching_key({}, "anything") is None


# ---------------------------------------------------------------------------
# approve_cmd
# ---------------------------------------------------------------------------


class TestApproveCmd:
    def test_no_manifest_exits_1(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(approve_cmd, [])
            assert result.exit_code != 0

    def test_no_args_shows_error(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            _write_manifest(".")
            result = runner.invoke(approve_cmd, [])
            assert result.exit_code != 0
            assert "Specify at least one package" in result.output

    def test_pending_no_packages(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            _write_manifest(".")
            result = runner.invoke(approve_cmd, ["--pending"])
            assert result.exit_code == 0
            assert "approved" in result.output.lower()

    def test_pending_with_unapproved_packages(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            _write_manifest(".")
            _create_pkg_with_hooks(Path("apm_modules"), "hook-pkg")
            result = runner.invoke(approve_cmd, ["--pending"])
            assert result.exit_code == 0
            assert "hook-pkg" in result.output

    def test_approve_all_writes_project_manifest(self) -> None:
        """apm approve --all writes to the project apm.yml executables block."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            _write_manifest(".")
            _create_pkg_with_hooks(Path("apm_modules"), "hook-pkg")
            _create_pkg_with_bin(Path("apm_modules"), "bin-pkg")

            result = runner.invoke(approve_cmd, ["--all"])
            assert result.exit_code == 0
            assert "Approved" in result.output

            from apm_cli.utils.yaml_io import load_yaml

            project_data = load_yaml(Path("apm.yml"))
            assert "executables" in project_data
            assert project_data["executables"]["allow"]

    def test_approve_specific_package_writes_project(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            _write_manifest(".")
            _create_pkg_with_hooks(Path("apm_modules"), "hook-pkg")

            result = runner.invoke(approve_cmd, ["hook-pkg"])
            assert result.exit_code == 0
            assert "Approved" in result.output

            from apm_cli.utils.yaml_io import load_yaml

            data = load_yaml(Path("apm.yml"))
            assert data["executables"]["allow"]

    def test_approve_user_scope_writes_config(self, tmp_path: Path) -> None:
        p_cfg, p_legacy, cfg = _isolated_config(tmp_path)
        runner = CliRunner()
        with runner.isolated_filesystem(), p_cfg, p_legacy:
            _write_manifest(".")
            _create_pkg_with_hooks(Path("apm_modules"), "hook-pkg")

            result = runner.invoke(approve_cmd, ["--user", "hook-pkg"])
            assert result.exit_code == 0
            assert "Approved" in result.output
            assert cfg.is_file()

            import json

            stored = json.loads(cfg.read_text())
            assert stored["executables"]["allow"]
            # The project manifest is untouched under --user.
            from apm_cli.utils.yaml_io import load_yaml

            assert "executables" not in load_yaml(Path("apm.yml"))

    def test_approve_unknown_package(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            _write_manifest(".")
            Path("apm_modules").mkdir()
            result = runner.invoke(approve_cmd, ["nonexistent"])
            assert result.exit_code == 0
            assert "not found" in result.output

    def test_approve_recommended_bulk_accepts_org_set(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            _write_manifest(".")
            _create_pkg_with_hooks(Path("apm_modules"), "hook-pkg")
            policy = ApmPolicy(executables=ExecutablesPolicy(recommend=("hook-pkg",)))

            with patch("apm_cli.commands.approve._load_org_policy", return_value=policy):
                result = runner.invoke(approve_cmd, ["--recommended"])

            assert result.exit_code == 0
            assert "Approved" in result.output
            from apm_cli.utils.yaml_io import load_yaml

            assert load_yaml(Path("apm.yml"))["executables"]["allow"]

    def test_approve_recommended_empty_set(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            _write_manifest(".")
            with patch("apm_cli.commands.approve._load_org_policy", return_value=ApmPolicy()):
                result = runner.invoke(approve_cmd, ["--recommended"])
            assert result.exit_code == 0
            assert "No org-recommended" in result.output

    def test_approve_list_shows_decisions(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            _write_manifest(".", {"executables": {"allow": {}}})
            _create_pkg_with_hooks(Path("apm_modules"), "hook-pkg")
            with patch("apm_cli.commands.approve._load_org_policy", return_value=ApmPolicy()):
                result = runner.invoke(approve_cmd, ["--list"])
            assert result.exit_code == 0
            assert "hook-pkg" in result.output


# ---------------------------------------------------------------------------
# deny_cmd
# ---------------------------------------------------------------------------


class TestDenyCmd:
    def test_deny_writes_project_deny(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            _write_manifest(".")
            _create_pkg_with_hooks(Path("apm_modules"), "hook-pkg")
            result = runner.invoke(deny_cmd, ["hook-pkg"])
            assert result.exit_code == 0
            assert "Denied" in result.output

            from apm_cli.utils.yaml_io import load_yaml

            data = load_yaml(Path("apm.yml"))
            assert data["executables"]["deny"]

    def test_deny_uninstalled_package(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            _write_manifest(".")
            Path("apm_modules").mkdir()
            result = runner.invoke(deny_cmd, ["owner/repo"])
            assert result.exit_code == 0
            assert "Denied" in result.output

    def test_deny_user_scope_writes_config(self, tmp_path: Path) -> None:
        p_cfg, p_legacy, cfg = _isolated_config(tmp_path)
        runner = CliRunner()
        with runner.isolated_filesystem(), p_cfg, p_legacy:
            _write_manifest(".")
            Path("apm_modules").mkdir()
            result = runner.invoke(deny_cmd, ["--user", "owner/repo"])
            assert result.exit_code == 0
            import json

            stored = json.loads(cfg.read_text())
            assert stored["executables"]["deny"]


# ---------------------------------------------------------------------------
# explain_cmd
# ---------------------------------------------------------------------------


class TestExplainCmd:
    def test_explain_unknown_package(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            _write_manifest(".")
            Path("apm_modules").mkdir()
            with patch("apm_cli.commands.approve._load_org_policy", return_value=ApmPolicy()):
                result = runner.invoke(explain_cmd, ["nonexistent"])
            assert result.exit_code == 0
            assert "not found" in result.output

    def test_explain_blocked_package_shows_layer_and_remedy(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            # Gate enabled (executables block present) but nothing approved.
            _write_manifest(".", {"executables": {"allow": {}}})
            _create_pkg_with_hooks(Path("apm_modules"), "hook-pkg")
            with patch("apm_cli.commands.approve._load_org_policy", return_value=ApmPolicy()):
                result = runner.invoke(explain_cmd, ["hook-pkg"])
            assert result.exit_code == 0
            assert "blocked" in result.output
            assert "default-deny" in result.output
            assert "apm approve" in result.output

    def test_explain_allowed_via_project(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            _create_pkg_with_hooks(Path("apm_modules"), "hook-pkg")
            _write_manifest(
                ".",
                {"executables": {"allow": {"hook-pkg": {"hooks": True}}}},
            )
            with patch("apm_cli.commands.approve._load_org_policy", return_value=ApmPolicy()):
                result = runner.invoke(explain_cmd, ["hook-pkg"])
            assert result.exit_code == 0
            assert "allowed" in result.output
            assert "project-allow" in result.output
