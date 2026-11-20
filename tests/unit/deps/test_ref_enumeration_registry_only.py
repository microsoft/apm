"""Registry-only ref enumeration for outdated and view versions."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.component


def _mode_off_env() -> dict[str, str]:
    env = dict(os.environ)
    env.pop("PROXY_REGISTRY_ONLY", None)
    env.pop("ARTIFACTORY_ONLY", None)
    return env


_SHA = "0123456789abcdef0123456789abcdef01234567"


class TestViewVersionsRegistryOnly:
    """``apm view <pkg> versions`` must not ls-remote in registry-only mode."""

    def _display(self, tmp_path):
        from apm_cli.commands import view

        logger = MagicMock()
        with (
            patch.object(view, "_effective_default_registry", return_value=None),
            patch.object(view, "GitHubPackageDownloader") as downloader_cls,
            patch.object(view, "AuthResolver"),
        ):
            downloader_cls.return_value.list_remote_refs.return_value = []
            try:
                view.display_versions("myorg/my-package", logger, project_root=tmp_path)
                exit_code = None
            except SystemExit as exc:
                exit_code = exc.code
        return logger, downloader_cls, exit_code

    def test_aborts_with_actionable_error_and_no_ls_remote(self, tmp_path):
        with patch.dict(os.environ, {"PROXY_REGISTRY_ONLY": "1"}):
            logger, downloader_cls, exit_code = self._display(tmp_path)

        assert exit_code == 1
        downloader_cls.return_value.list_remote_refs.assert_not_called()
        message = logger.error.call_args.args[0]
        assert "registry-only mode" in message
        assert "exact version" in message
        assert "unset PROXY_REGISTRY_ONLY" in message

    def test_without_the_flag_remote_refs_are_listed(self, tmp_path):
        with patch.dict(os.environ, _mode_off_env(), clear=True):
            logger, downloader_cls, exit_code = self._display(tmp_path)

        assert exit_code is None
        downloader_cls.return_value.list_remote_refs.assert_called_once()
        logger.error.assert_not_called()


class TestOutdatedRegistryOnly:
    """``apm outdated`` must not ls-remote and must name the remedy."""

    def _run_outdated(self, tmp_path, monkeypatch):
        from click.testing import CliRunner

        from apm_cli.cli import cli
        from apm_cli.deps.lockfile import LockedDependency, LockFile
        from apm_cli.models.apm_package import clear_apm_yml_cache

        monkeypatch.chdir(tmp_path)
        clear_apm_yml_cache()
        (tmp_path / "apm.yml").write_text(
            "name: consumer\nversion: 1.0.0\n"
            "dependencies:\n  apm:\n    - myorg/my-package#v1.4.0\n",
            encoding="utf-8",
        )
        lock = LockFile()
        lock.dependencies = {
            "myorg/my-package": LockedDependency(
                repo_url="myorg/my-package",
                resolved_ref="v1.4.0",
                resolved_commit=_SHA,
            ),
        }
        lock.write(tmp_path / "apm.lock.yaml")
        with (
            patch("apm_cli.deps.github_downloader.git") as git_module,
            patch("apm_cli.core.auth.AuthResolver"),
        ):
            git_module.cmd.Git.return_value.ls_remote.return_value = ""
            result = CliRunner().invoke(cli, ["outdated", "-j", "0"])
        return result, git_module

    def test_reports_unknown_with_remedy_and_no_ls_remote(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PROXY_REGISTRY_ONLY", "1")
        result, git_module = self._run_outdated(tmp_path, monkeypatch)

        assert result.exit_code == 0, result.output
        git_module.cmd.Git.assert_not_called()
        assert "unknown" in result.output
        assert "PROXY_REGISTRY_ONLY is set" in " ".join(result.output.split())

    def test_without_the_flag_ls_remote_runs_and_no_hint(self, tmp_path, monkeypatch):
        monkeypatch.delenv("PROXY_REGISTRY_ONLY", raising=False)
        monkeypatch.delenv("ARTIFACTORY_ONLY", raising=False)
        result, git_module = self._run_outdated(tmp_path, monkeypatch)

        assert result.exit_code == 0, result.output
        git_module.cmd.Git.assert_called()
        assert "PROXY_REGISTRY_ONLY is set" not in " ".join(result.output.split())
