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
from textwrap import dedent
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from apm_cli.commands.install import (
    _merge_packages_into_yml,
    _resolve_package_references,
)
from apm_cli.core.command_logger import CommandLogger
from apm_cli.models.apm_package import APMPackage, clear_apm_yml_cache
from apm_cli.models.dependency.reference import DependencyReference

pytestmark = pytest.mark.component

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


# ---------------------------------------------------------------------------
# _split_argv_at_double_dash
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _check_package_conflicts
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _merge_packages_into_yml
# ---------------------------------------------------------------------------


class TestMergePackagesIntoYml:
    def test_appends_packages_to_current_deps(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        logger = CommandLogger("install")
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
        assert capsys.readouterr().out.count("[*] Updated apm.yml with 1 new package(s)") == 1

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
        apm_yml.write_text("name: project\nversion: 1.0.0\ndependencies:\n  apm: []\n")
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
        assert apm_yml.read_text() == "name: project\nversion: 1.0.0\ndependencies:\n  apm: []\n"

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


class TestInstallUpdatePackageScoping:
    def test_update_existing_named_package_scopes_apm_install_to_that_package(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """--update for an already-declared package must not integrate every dep."""
        from apm_cli.commands.install import install
        from apm_cli.models.results import InstallResult

        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(
            "name: test\n"
            "version: 0.1.0\n"
            "dependencies:\n"
            "  apm:\n"
            "    - owner/target\n"
            "    - owner/other\n",
            encoding="utf-8",
        )

        service_requests = []

        def capture_request(_self, request):
            service_requests.append(request)
            return InstallResult(installed_count=1, diagnostics=None)

        with (
            patch(
                "apm_cli.commands.install._get_invocation_argv",
                return_value=["apm", "install", "--update", "owner/target"],
            ),
            patch("apm_cli.commands.install._validate_package_exists", return_value=True),
            patch("apm_cli.install.service.InstallService.run", capture_request),
        ):
            result = CliRunner().invoke(install, ["--update", "owner/target"])

        assert result.exit_code == 0, result.output
        assert service_requests[0].only_packages == ["owner/target"]


class TestResolvePackageReferencesGitLabDirectShorthandPersistence:
    """Virtual probe results must serialize as git+path for apm.yml merge."""

    _PKG = "git.epam.com/epm-ease/apm-registry/agents/ai-run-ba-flow"

    @patch("apm_cli.commands.install._try_resolve_gitlab_direct_shorthand")
    @patch("apm_cli.commands.install._validate_package_exists", return_value=True)
    def test_virtual_shorthand_populates_apm_yml_entries_object_form(
        self, mock_validate, mock_try_resolve
    ):
        resolved = DependencyReference.from_gitlab_shorthand_probe(
            "git.epam.com",
            "epm-ease/apm-registry",
            "agents/ai-run-ba-flow",
            None,
        )
        mock_try_resolve.return_value = resolved
        parsed_stub = MagicMock()
        parsed_stub.is_virtual = False

        with patch("apm_cli.commands.install.DependencyReference") as mock_dep_cls:
            mock_dep_cls.parse.return_value = parsed_stub
            mock_dep_cls.is_local_path.return_value = False
            mock_dep_cls.needs_gitlab_direct_shorthand_probing.return_value = True

            existing = set()
            _v, _inv, validated, _mkt, entries, _chg = _resolve_package_references(
                [self._PKG],
                [],
                existing,
            )

        canonical = "git.epam.com/epm-ease/apm-registry/agents/ai-run-ba-flow"
        assert validated == [canonical]
        assert entries[canonical] == {
            "git": "https://git.epam.com/epm-ease/apm-registry",
            "path": "agents/ai-run-ba-flow",
        }

    def test_generated_entry_round_trips_via_from_apm_yml(self, tmp_path):
        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text(
            dedent(
                """
                name: wire-test
                version: "0.0.1"
                dependencies:
                  apm:
                    - git: https://git.epam.com/epm-ease/apm-registry
                      path: agents/ai-run-ba-flow
                """
            ).lstrip(),
            encoding="utf-8",
        )
        clear_apm_yml_cache()
        pkg = APMPackage.from_apm_yml(apm_yml)
        deps = pkg.get_apm_dependencies()
        assert len(deps) == 1
        ref = deps[0]
        assert ref.host == "git.epam.com"
        assert ref.repo_url == "epm-ease/apm-registry"
        assert ref.virtual_path == "agents/ai-run-ba-flow"
        assert ref.is_virtual is True
