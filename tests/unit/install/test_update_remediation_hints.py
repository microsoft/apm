"""Remediation hints name commands that work, and ref updates can repair them (#3837).

* ``PROXY_REGISTRY_ONLY`` blocks replay of a lockfile locked to direct VCS
  sources, but a ref update re-resolves through the registry instead of
  failing on the stale on-disk entries.
* Auth pre-flight failures name the command the user actually invoked.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import click
import pytest

from apm_cli.core.scope import InstallScope
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.deps.registry_proxy import RegistryConfig
from apm_cli.install.errors import InstallFailureAlreadyRendered
from apm_cli.install.pipeline import run_install_pipeline
from apm_cli.install.source_helpers import lockfile_refresh_command, ref_update_operation_label

pytestmark = pytest.mark.component


def _registry_only_config() -> RegistryConfig:
    env = {
        "PROXY_REGISTRY_URL": "https://art.example.com/artifactory/api/vcs",
        "PROXY_REGISTRY_ONLY": "1",
    }
    with patch.dict(os.environ, env, clear=True):
        return RegistryConfig.from_env()


def _run_pipeline(
    tmp_path: Path,
    errors: list[str],
    *,
    update_refs: bool,
    only_packages: list[str] | None = None,
    retained: str | None = None,
) -> None:
    lock = LockFile()
    lock.add_dependency(LockedDependency(repo_url="owner/repo", host="github.com"))
    if retained:
        lock.add_dependency(LockedDependency(repo_url=retained, host="github.com"))
    lockfile_cls = MagicMock()
    lockfile_cls.read.return_value = lock

    pkg = MagicMock()
    pkg.get_apm_dependencies.return_value = [MagicMock(repo_url="owner/repo", host="github.com")]
    pkg.get_dev_apm_dependencies.return_value = []
    pkg.get_mcp_dependencies.return_value = []
    dep = MagicMock()
    dep.get_unique_key.return_value = "owner/repo"

    def _phase(name, _phase, ctx):
        if name == "resolve":
            ctx.deps_to_install = [dep]
        elif name == "targets":
            ctx.integrators["skill"] = MagicMock()

    with (
        patch("apm_cli.install.pipeline._run_phase", side_effect=_phase),
        patch("apm_cli.install.pipeline._preflight_auth_check"),
        patch(
            "apm_cli.install.pipeline._rich_error",
            side_effect=lambda msg, **_kw: errors.append(msg),
        ),
        patch("apm_cli.deps.lockfile.LockFile", lockfile_cls),
        patch("apm_cli.deps.lockfile.get_lockfile_path", return_value=tmp_path / "x.lock"),
        patch("apm_cli.core.scope.get_deploy_root", return_value=tmp_path),
        patch("apm_cli.core.scope.get_apm_dir", return_value=tmp_path),
        patch(
            "apm_cli.install.phases.local_content._project_has_root_primitives",
            return_value=False,
        ),
        patch("apm_cli.utils.install_tui.InstallTui"),
        patch(
            "apm_cli.deps.registry_proxy.RegistryConfig.from_env",
            return_value=_registry_only_config(),
        ),
        patch(
            "apm_cli.integration.base_integrator.BaseIntegrator.normalize_managed_files",
            return_value=set(),
        ),
        patch("apm_cli.commands._helpers._update_gitignore_for_apm_modules"),
    ):
        run_install_pipeline(
            pkg,
            update_refs=update_refs,
            only_packages=only_packages,
            logger=MagicMock(),
            target="claude",
            scope=InstallScope.PROJECT,
        )


class TestRegistryOnlyLockfileConflict:
    def test_replay_blocks_and_names_apm_update(self, tmp_path: Path) -> None:
        errors: list[str] = []
        with pytest.raises(InstallFailureAlreadyRendered):
            _run_pipeline(tmp_path, errors, update_refs=False)
        assert any("direct VCS sources" in msg for msg in errors)
        assert any("Run 'apm update' to re-resolve" in msg for msg in errors)
        assert not any("install --update" in msg for msg in errors)

    def test_ref_update_re_resolves_instead_of_blocking(self, tmp_path: Path) -> None:
        errors: list[str] = []
        _run_pipeline(tmp_path, errors, update_refs=True)
        assert not any("direct VCS sources" in msg for msg in errors)

    def test_targeted_update_still_checks_entries_it_keeps(self, tmp_path: Path) -> None:
        errors: list[str] = []
        with pytest.raises(InstallFailureAlreadyRendered):
            _run_pipeline(
                tmp_path,
                errors,
                update_refs=True,
                only_packages=["owner/repo"],
                retained="other/kept",
            )
        conflicts = [msg for msg in errors if msg.strip().startswith("- ")]
        assert conflicts == ["  - other/kept (host: github.com)"]

    def test_targeted_update_re_resolves_its_selection(self, tmp_path: Path) -> None:
        errors: list[str] = []
        _run_pipeline(tmp_path, errors, update_refs=True, only_packages=["owner/repo"])
        assert not any("direct VCS sources" in msg for msg in errors)


class TestRemediationCommands:
    def test_refresh_command_follows_scope(self) -> None:
        assert lockfile_refresh_command(InstallScope.PROJECT) == "apm update"
        assert lockfile_refresh_command(InstallScope.USER) == "apm update -g"

    @pytest.mark.parametrize(
        ("command_path", "expected"),
        [
            ("apm update", "update"),
            ("apm install", "install --update"),
            ("apm deps update", "deps update"),
        ],
    )
    def test_auth_label_names_invoked_command(self, command_path: str, expected: str) -> None:
        root = click.Context(click.Group("apm"), info_name="apm")
        ctx = root
        for name in command_path.split()[1:]:
            ctx = click.Context(click.Command(name), parent=ctx, info_name=name)
        with ctx:
            assert ref_update_operation_label() == expected

    def test_auth_label_without_click_context(self) -> None:
        assert ref_update_operation_label() == "update"
