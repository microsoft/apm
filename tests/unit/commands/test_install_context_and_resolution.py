"""Comprehensive unit tests for apm_cli.commands.install.

Targets the uncovered branches in:
- _split_argv_at_double_dash
- _get_invocation_argv
- _check_package_conflicts
- _merge_packages_into_yml
- _validate_and_add_packages_to_apm_yml (key branches)
- install CLI command (key validation / early-exit branches)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.commands.install import (
    _check_package_conflicts,
    _get_invocation_argv,
    _merge_packages_into_yml,
    _split_argv_at_double_dash,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_install_logger() -> MagicMock:
    logger = MagicMock()
    logger.verbose = False
    logger.verbose_detail = MagicMock()
    logger.progress = MagicMock()
    logger.warning = MagicMock()
    logger.error = MagicMock()
    logger.success = MagicMock()
    return logger


def _make_dep_ref(
    canonical: str = "owner/repo",
    identity: str = "github.com/owner/repo",
    *,
    is_insecure: bool = False,
    is_local: bool = False,
) -> MagicMock:
    ref = MagicMock()
    ref.to_canonical.return_value = canonical
    ref.get_identity.return_value = identity
    ref.is_insecure = is_insecure
    ref.is_local = is_local
    ref.is_virtual = False
    ref.is_virtual_subdirectory = MagicMock(return_value=False)
    ref.reference = None
    return ref


# ---------------------------------------------------------------------------
# _get_invocation_argv
# ---------------------------------------------------------------------------


class TestGetInvocationArgv:
    def test_returns_sys_argv(self) -> None:
        import sys

        result = _get_invocation_argv()
        assert result is sys.argv


# ---------------------------------------------------------------------------
# _split_argv_at_double_dash
# ---------------------------------------------------------------------------


class TestSplitArgvAtDoubleDash:
    def test_no_double_dash_returns_full_argv_empty_command(self) -> None:
        clean, cmd = _split_argv_at_double_dash(["apm", "install", "owner/repo"])
        assert clean == ["apm", "install", "owner/repo"]
        assert cmd == ()

    def test_double_dash_splits_correctly(self) -> None:
        clean, cmd = _split_argv_at_double_dash(
            ["apm", "install", "--mcp", "foo", "--", "npx", "-y", "srv"]
        )
        assert clean == ["apm", "install", "--mcp", "foo"]
        assert cmd == ("npx", "-y", "srv")

    def test_double_dash_at_end_gives_empty_command(self) -> None:
        clean, cmd = _split_argv_at_double_dash(["apm", "install", "--"])
        assert clean == ["apm", "install"]
        assert cmd == ()

    def test_double_dash_at_start_gives_empty_clean(self) -> None:
        clean, cmd = _split_argv_at_double_dash(["--", "npx", "server"])
        assert clean == []
        assert cmd == ("npx", "server")

    def test_command_tuple_is_tuple_type(self) -> None:
        _, cmd = _split_argv_at_double_dash(["a", "--", "b"])
        assert isinstance(cmd, tuple)

    def test_empty_argv_returns_empty(self) -> None:
        clean, cmd = _split_argv_at_double_dash([])
        assert clean == []
        assert cmd == ()


# ---------------------------------------------------------------------------
# _check_package_conflicts
# ---------------------------------------------------------------------------


class TestCheckPackageConflicts:
    def test_empty_deps_returns_empty_set(self) -> None:
        result = _check_package_conflicts([])
        assert result == set()

    def test_string_dep_adds_identity(self) -> None:
        with patch("apm_cli.commands.install.DependencyReference") as mock_cls:
            mock_ref = MagicMock()
            mock_ref.get_identity.return_value = "github.com/owner/repo"
            mock_cls.parse.return_value = mock_ref
            result = _check_package_conflicts(["owner/repo"])
        assert "github.com/owner/repo" in result

    def test_dict_dep_uses_parse_from_dict(self) -> None:
        with patch("apm_cli.commands.install.DependencyReference") as mock_cls:
            mock_ref = MagicMock()
            mock_ref.get_identity.return_value = "github.com/owner/repo"
            mock_cls.parse_from_dict.return_value = mock_ref
            result = _check_package_conflicts([{"repo": "owner/repo"}])
        assert "github.com/owner/repo" in result

    def test_invalid_dep_entry_is_skipped(self) -> None:
        with patch("apm_cli.commands.install.DependencyReference") as mock_cls:
            mock_cls.parse.side_effect = ValueError("bad format")
            result = _check_package_conflicts(["bad-format"])
        assert result == set()

    def test_unknown_type_is_skipped(self) -> None:
        result = _check_package_conflicts([42, None, 3.14])
        assert result == set()

    def test_multiple_deps_returns_all_identities(self) -> None:
        with patch("apm_cli.commands.install.DependencyReference") as mock_cls:
            ref_a = MagicMock()
            ref_a.get_identity.return_value = "github.com/a/repo"
            ref_b = MagicMock()
            ref_b.get_identity.return_value = "github.com/b/repo"
            mock_cls.parse.side_effect = [ref_a, ref_b]
            result = _check_package_conflicts(["a/repo", "b/repo"])
        assert "github.com/a/repo" in result
        assert "github.com/b/repo" in result


# ---------------------------------------------------------------------------
# _merge_packages_into_yml
# ---------------------------------------------------------------------------


class TestMergePackagesIntoYml:
    def test_appends_packages_to_current_deps(self, tmp_path: Path) -> None:
        logger = _make_install_logger()
        apm_yml = tmp_path / "apm.yml"
        data = {"dependencies": {"apm": ["existing/pkg"]}}
        current_deps = data["dependencies"]["apm"]

        with patch("apm_cli.utils.yaml_io.dump_yaml_roundtrip") as mock_dump:
            _merge_packages_into_yml(
                ["new/pkg"],
                {},
                current_deps,
                data,
                "dependencies",
                apm_yml,
                logger=logger,
            )

        assert "new/pkg" in current_deps
        mock_dump.assert_called_once()
        logger.success.assert_called_once()

    def test_uses_apm_yml_entry_when_present(self, tmp_path: Path) -> None:
        logger = _make_install_logger()
        apm_yml = tmp_path / "apm.yml"
        data = {"dependencies": {"apm": []}}
        current_deps = data["dependencies"]["apm"]
        apm_yml_entries = {"owner/repo": {"repo": "owner/repo", "ref": "main"}}

        with patch("apm_cli.utils.yaml_io.dump_yaml_roundtrip"):
            _merge_packages_into_yml(
                ["owner/repo"],
                apm_yml_entries,
                current_deps,
                data,
                "dependencies",
                apm_yml,
                logger=logger,
            )

        assert {"repo": "owner/repo", "ref": "main"} in current_deps

    def test_write_failure_exits(self, tmp_path: Path) -> None:
        logger = _make_install_logger()
        apm_yml = tmp_path / "apm.yml"
        data = {"dependencies": {"apm": []}}
        current_deps = data["dependencies"]["apm"]

        with patch("apm_cli.utils.yaml_io.dump_yaml_roundtrip", side_effect=Exception("disk full")):
            with pytest.raises(SystemExit) as exc_info:
                _merge_packages_into_yml(
                    ["owner/repo"],
                    {},
                    current_deps,
                    data,
                    "dependencies",
                    apm_yml,
                    logger=logger,
                )
        assert exc_info.value.code == 1
        logger.error.assert_called()

    def test_dev_flag_logs_devDependencies_label(self, tmp_path: Path) -> None:
        logger = _make_install_logger()
        apm_yml = tmp_path / "apm.yml"
        data = {"devDependencies": {"apm": []}}
        current_deps = data["devDependencies"]["apm"]

        with patch("apm_cli.utils.yaml_io.dump_yaml_roundtrip"):
            _merge_packages_into_yml(
                ["owner/repo"],
                {},
                current_deps,
                data,
                "devDependencies",
                apm_yml,
                dev=True,
                logger=logger,
            )

        # verbose_detail should mention devDependencies
        call_args = [call[0][0] for call in logger.verbose_detail.call_args_list]
        assert any("devDependencies" in arg for arg in call_args)


# ---------------------------------------------------------------------------
# _validate_and_add_packages_to_apm_yml
# ---------------------------------------------------------------------------


class TestValidateAndAddPackagesToApmYml:
    """Tests for the _validate_and_add_packages_to_apm_yml function."""

    def test_missing_apm_yml_exits(self, tmp_path: Path) -> None:
        from apm_cli.commands.install import _validate_and_add_packages_to_apm_yml

        logger = _make_install_logger()
        with pytest.raises(SystemExit) as exc_info:
            _validate_and_add_packages_to_apm_yml(
                ["owner/repo"],
                logger=logger,
                manifest_path=tmp_path / "nonexistent.yml",
            )
        assert exc_info.value.code == 1
        logger.error.assert_called()

    def test_empty_packages_with_existing_yml_returns_empty(self, tmp_path: Path) -> None:
        from apm_cli.commands.install import _validate_and_add_packages_to_apm_yml

        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text("name: project\ndependencies:\n  apm: []\n")
        logger = _make_install_logger()

        with patch("apm_cli.commands.install._check_package_conflicts", return_value=set()):
            with patch("apm_cli.commands.install._resolve_package_references") as mock_resolve:
                mock_resolve.return_value = ([], [], [], {}, {}, False)
                outcome_mock = MagicMock()
                outcome_mock.all_failed = False
                with patch(
                    "apm_cli.commands.install._ValidationOutcome", return_value=outcome_mock
                ):
                    with patch("apm_cli.commands.install.persist_dependency_list_if_changed"):
                        logger.validation_summary.return_value = True
                        validated, _outcome = _validate_and_add_packages_to_apm_yml(
                            [],
                            logger=logger,
                            manifest_path=apm_yml,
                        )
        assert validated == []

    def test_dry_run_returns_validated_without_writing(self, tmp_path: Path) -> None:
        from apm_cli.commands.install import _validate_and_add_packages_to_apm_yml

        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text("name: project\ndependencies:\n  apm: []\n")
        logger = _make_install_logger()
        logger.validation_summary.return_value = True

        valid_outcomes = [("owner/repo", False)]
        with patch("apm_cli.commands.install._check_package_conflicts", return_value=set()):
            with patch("apm_cli.commands.install._resolve_package_references") as mock_resolve:
                mock_resolve.return_value = (valid_outcomes, [], ["owner/repo"], {}, {}, False)
                with patch("apm_cli.commands.install._ValidationOutcome") as mock_outcome_cls:
                    mock_outcome = MagicMock()
                    mock_outcome.all_failed = False
                    mock_outcome_cls.return_value = mock_outcome
                    validated, _outcome = _validate_and_add_packages_to_apm_yml(
                        ["owner/repo"],
                        dry_run=True,
                        logger=logger,
                        manifest_path=apm_yml,
                    )
        # Dry-run: packages returned but NOT written
        assert "owner/repo" in validated

    def test_validation_summary_false_returns_early(self, tmp_path: Path) -> None:
        from apm_cli.commands.install import _validate_and_add_packages_to_apm_yml

        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text("name: project\ndependencies:\n  apm: []\n")
        logger = _make_install_logger()
        logger.validation_summary.return_value = False  # abort

        with patch("apm_cli.commands.install._check_package_conflicts", return_value=set()):
            with patch("apm_cli.commands.install._resolve_package_references") as mock_resolve:
                mock_resolve.return_value = ([], [("owner/repo", "bad format")], [], {}, {}, False)
                with patch("apm_cli.commands.install._ValidationOutcome") as mock_outcome_cls:
                    mock_outcome = MagicMock()
                    mock_outcome.all_failed = True
                    mock_outcome_cls.return_value = mock_outcome
                    validated, _outcome = _validate_and_add_packages_to_apm_yml(
                        ["owner/repo"],
                        logger=logger,
                        manifest_path=apm_yml,
                    )
        assert validated == []

    def test_manifest_update_preserves_comments(self, tmp_path: Path) -> None:
        from apm_cli.commands.install import _validate_and_add_packages_to_apm_yml

        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text(
            "# project comment\n"
            "name: project\n"
            "# dependency section comment\n"
            "dependencies: # dependencies inline comment\n"
            "  # apm list comment\n"
            "  apm:\n"
            "    - existing/pkg # existing dependency comment\n",
            encoding="utf-8",
        )
        logger = _make_install_logger()
        logger.validation_summary.return_value = True

        with patch("apm_cli.commands.install._check_package_conflicts", return_value=set()):
            with patch("apm_cli.commands.install._resolve_package_references") as mock_resolve:
                mock_resolve.return_value = (
                    [("new/pkg", False)],
                    [],
                    ["new/pkg"],
                    {},
                    {},
                    False,
                )
                validated, _outcome = _validate_and_add_packages_to_apm_yml(
                    ["new/pkg"],
                    logger=logger,
                    manifest_path=apm_yml,
                )

        text = apm_yml.read_text(encoding="utf-8")
        assert validated == ["new/pkg"]
        assert "# project comment" in text
        assert "# dependency section comment" in text
        assert "# dependencies inline comment" in text
        assert "# apm list comment" in text
        assert "# existing dependency comment" in text
        assert "new/pkg" in text


# ---------------------------------------------------------------------------
# install CLI command -- validation / early-exit branches
# ---------------------------------------------------------------------------


class TestInstallCommandBranches:
    """Test install CLI for key error/validation branches without network."""

    def _invoke(self, args: list[str], **kwargs: object) -> None:
        from click.testing import CliRunner

        from apm_cli.commands.install import install

        runner = CliRunner(**kwargs)
        return runner.invoke(install, args, catch_exceptions=False)

    def test_frozen_and_update_raises_usage_error(self) -> None:
        from click.testing import CliRunner

        from apm_cli.commands.install import install

        runner = CliRunner()
        result = runner.invoke(install, ["--frozen", "--update"])
        assert result.exit_code != 0

    def test_ssh_and_https_together_exits(self, tmp_path: Path) -> None:
        from click.testing import CliRunner

        from apm_cli.commands.install import install

        runner = CliRunner()
        # Create minimal apm.yml so the command reaches the --ssh/--https check
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            (Path(tmp_path) / "apm.yml").write_text("name: test\ndependencies:\n  apm: []\n")
            with patch(
                "apm_cli.commands.install._split_argv_at_double_dash", return_value=([], ())
            ):
                with patch("apm_cli.commands.install._validate_registry_url", return_value=None):
                    with patch("apm_cli.commands.install._validate_mcp_conflicts"):
                        with patch(
                            "apm_cli.integration.targets.should_use_legacy_skill_paths",
                            return_value=False,
                        ):
                            with patch("apm_cli.core.scope.get_manifest_path") as mock_mp:
                                with patch("apm_cli.core.scope.get_apm_dir") as mock_apm:
                                    with patch("apm_cli.core.scope.get_deploy_root") as mock_dr:
                                        mock_mp.return_value = Path(tmp_path) / "apm.yml"
                                        mock_apm.return_value = Path(tmp_path)
                                        mock_dr.return_value = Path(tmp_path)
                                        with patch("apm_cli.commands.install.AuthResolver"):
                                            result = runner.invoke(install, ["--ssh", "--https"])
        assert result.exit_code != 0

    def test_alias_without_local_bundle_raises_usage_error(self, tmp_path: Path) -> None:
        from click.testing import CliRunner

        from apm_cli.commands.install import install

        runner = CliRunner()
        # --as with a non-existent (non-local-bundle) path should raise UsageError
        with runner.isolated_filesystem():
            result = runner.invoke(install, ["--as", "custom-name", "owner/repo"])
        assert result.exit_code != 0

    def test_skill_with_mcp_raises_usage_error(self) -> None:
        from click.testing import CliRunner

        from apm_cli.commands.install import install

        runner = CliRunner()
        with patch("apm_cli.commands.install.InstallLogger"):
            with patch(
                "apm_cli.commands.install._split_argv_at_double_dash", return_value=([], ())
            ):
                with patch("apm_cli.commands.install._validate_registry_url", return_value=None):
                    with patch("apm_cli.commands.install._validate_mcp_conflicts"):
                        result = runner.invoke(
                            install, ["--mcp", "myserver", "--skill", "my-skill"]
                        )
        # Should exit with error because --skill cannot be combined with --mcp
        assert result.exit_code != 0

    def test_no_apm_yml_no_packages_exits(self, tmp_path: Path) -> None:
        from click.testing import CliRunner

        from apm_cli.commands.install import install

        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            with patch(
                "apm_cli.commands.install._split_argv_at_double_dash", return_value=([], ())
            ):
                with patch("apm_cli.commands.install._validate_registry_url", return_value=None):
                    with patch("apm_cli.commands.install._validate_mcp_conflicts"):
                        with patch(
                            "apm_cli.integration.targets.should_use_legacy_skill_paths",
                            return_value=False,
                        ):
                            with patch("apm_cli.core.scope.get_manifest_path") as mock_mp:
                                with patch("apm_cli.core.scope.get_apm_dir") as mock_apm:
                                    with patch("apm_cli.core.scope.get_deploy_root") as mock_dr:
                                        missing = Path(tmp_path) / "apm.yml"
                                        mock_mp.return_value = missing
                                        mock_apm.return_value = Path(tmp_path)
                                        mock_dr.return_value = Path(tmp_path)
                                        with patch("apm_cli.commands.install.AuthResolver"):
                                            result = runner.invoke(install, [])
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# InstallContext defaults
# ---------------------------------------------------------------------------


class TestInstallContextDefaults:
    def test_refresh_default_is_false(self) -> None:
        from apm_cli.commands.install import InstallContext

        ctx = InstallContext(
            scope=None,
            manifest_path=Path("."),
            manifest_display="apm.yml",
            apm_dir=Path("."),
            project_root=Path("."),
            logger=None,
            auth_resolver=None,
            verbose=False,
            force=False,
            dry_run=False,
            update=False,
            dev=False,
            runtime=None,
            exclude=None,
            target=None,
            parallel_downloads=4,
            allow_insecure=False,
            allow_insecure_hosts=(),
            protocol_pref=None,
            allow_protocol_fallback=False,
            trust_transitive_mcp=False,
            no_policy=False,
            install_mode=None,
            packages=(),
        )
        assert ctx.refresh is False
        assert ctx.only_packages is None
        assert ctx.transaction is None
        assert ctx.legacy_skill_paths is False
        assert ctx.frozen is False
        assert ctx.plan_callback is None

    def test_context_is_mutable(self) -> None:
        from apm_cli.commands.install import InstallContext

        ctx = InstallContext(
            scope=None,
            manifest_path=Path("."),
            manifest_display="apm.yml",
            apm_dir=Path("."),
            project_root=Path("."),
            logger=None,
            auth_resolver=None,
            verbose=False,
            force=False,
            dry_run=False,
            update=False,
            dev=False,
            runtime=None,
            exclude=None,
            target=None,
            parallel_downloads=4,
            allow_insecure=False,
            allow_insecure_hosts=(),
            protocol_pref=None,
            allow_protocol_fallback=False,
            trust_transitive_mcp=False,
            no_policy=False,
            install_mode=None,
            packages=(),
        )
        ctx.refresh = True
        assert ctx.refresh is True
