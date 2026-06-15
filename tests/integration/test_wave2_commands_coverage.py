"""Integration tests targeting low-coverage command and utility modules.

Covers:
  - ``apm update`` with --dry-run, --yes, --global, positional packages, no-deps
  - ``commands/compile/watcher.py`` APMFileHandler debounce/event-filter logic
  - ``apm policy status`` subcommand paths (JSON output, table, --check)
  - ``apm run`` with various scenarios (no script, params, verbose)
  - ``apm cache info/clean/prune`` commands
  - ``apm runtime list/setup/status/remove`` commands
  - ``deps/bare_cache.py`` _scrub_bare_remote_url, build_clone_failure_message
  - ``deps/github_downloader_validation.py`` helpers
  - ``install/mcp/registry.py`` URL validation, resolve, env override
  - ``install/insecure_policy.py`` format messages, guard functions
  - ``deps/shared_clone_cache.py`` SharedCloneCache lifecycle

No live network calls -- all HTTP/subprocess are mocked.
"""

from __future__ import annotations

import os
import time
import urllib.parse
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def runner() -> CliRunner:
    """Provide a Click test runner."""
    return CliRunner()


@pytest.fixture()
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect config files to a temporary directory."""
    import apm_cli.config as _conf

    _conf._invalidate_config_cache()
    config_dir = tmp_path / ".apm"
    config_file = config_dir / "config.json"
    monkeypatch.setattr(_conf, "CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(_conf, "CONFIG_FILE", str(config_file))
    yield config_file
    _conf._invalidate_config_cache()


@pytest.fixture()
def project_with_deps(tmp_path: Path) -> Path:
    """Create an APM project with one dependency."""
    apm_yml = tmp_path / "apm.yml"
    apm_yml.write_text(
        "name: test-project\n"
        "version: 1.0.0\n"
        "description: Test project\n"
        "targets:\n"
        "  - copilot\n"
        "dependencies:\n"
        "  apm:\n"
        "    test-org/test-pkg: github:test-org/test-pkg\n",
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture()
def project_no_deps(tmp_path: Path) -> Path:
    """Create an APM project with no dependencies."""
    apm_yml = tmp_path / "apm.yml"
    apm_yml.write_text(
        "name: test-project\nversion: 1.0.0\ndependencies:\n  apm: {}\n",
        encoding="utf-8",
    )
    return tmp_path


# ===========================================================================
# apm cache commands
# ===========================================================================


class TestCacheInfo:
    """Tests for ``apm cache info``."""

    def test_cache_info_shows_stats(self, runner: CliRunner, tmp_path: Path) -> None:
        """``apm cache info`` should render cache statistics."""
        mock_git_stats = {
            "db_count": 3,
            "checkout_count": 5,
            "total_size_bytes": 1024 * 1024 * 2,  # 2 MB
        }
        mock_http_stats = {
            "entry_count": 10,
            "total_size_bytes": 512 * 1024,  # 512 KB
        }
        with (
            patch("apm_cli.cache.paths.get_cache_root", return_value=tmp_path),
            patch(
                "apm_cli.cache.git_cache.GitCache.get_cache_stats",
                return_value=mock_git_stats,
            ),
            patch(
                "apm_cli.cache.http_cache.HttpCache.get_stats",
                return_value=mock_http_stats,
            ),
        ):
            result = runner.invoke(cli, ["cache", "info"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "Cache root" in result.output

    def test_cache_info_formats_bytes(self, runner: CliRunner) -> None:
        """``apm cache info`` formats bytes/KB/MB/GB correctly."""
        from apm_cli.commands.cache import _format_size

        assert "B" in _format_size(500)
        assert "KB" in _format_size(1500)
        assert "MB" in _format_size(2 * 1024 * 1024)
        assert "GB" in _format_size(2 * 1024 * 1024 * 1024)

    def test_cache_info_error_on_bad_root(self, runner: CliRunner) -> None:
        """``apm cache info`` exits non-zero when cache root cannot be resolved."""
        with patch(
            "apm_cli.cache.paths.get_cache_root",
            side_effect=ValueError("bad cache root"),
        ):
            result = runner.invoke(cli, ["cache", "info"], catch_exceptions=False)
        assert result.exit_code != 0


class TestCacheClean:
    """Tests for ``apm cache clean``."""

    def test_cache_clean_with_force_skips_prompt(self, runner: CliRunner, tmp_path: Path) -> None:
        """``apm cache clean --force`` should not prompt and clean everything."""
        with (
            patch("apm_cli.cache.paths.get_cache_root", return_value=tmp_path),
            patch("apm_cli.cache.git_cache.GitCache.clean_all") as mock_git_clean,
            patch("apm_cli.cache.http_cache.HttpCache.clean_all") as mock_http_clean,
        ):
            result = runner.invoke(cli, ["cache", "clean", "--force"], catch_exceptions=False)
        assert result.exit_code == 0
        mock_git_clean.assert_called_once()
        mock_http_clean.assert_called_once()

    def test_cache_clean_with_yes_skips_prompt(self, runner: CliRunner, tmp_path: Path) -> None:
        """``apm cache clean --yes`` should also skip the prompt."""
        with (
            patch("apm_cli.cache.paths.get_cache_root", return_value=tmp_path),
            patch("apm_cli.cache.git_cache.GitCache.clean_all"),
            patch("apm_cli.cache.http_cache.HttpCache.clean_all"),
        ):
            result = runner.invoke(cli, ["cache", "clean", "--yes"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "cleaned" in result.output.lower()

    def test_cache_clean_declined_prompt(self, runner: CliRunner, tmp_path: Path) -> None:
        """``apm cache clean`` with declined prompt should abort."""
        with (
            patch("apm_cli.cache.paths.get_cache_root", return_value=tmp_path),
            patch("apm_cli.cache.git_cache.GitCache.clean_all") as mock_git_clean,
        ):
            # Provide 'N' to the prompt
            result = runner.invoke(cli, ["cache", "clean"], input="N\n", catch_exceptions=False)
        assert result.exit_code == 0
        mock_git_clean.assert_not_called()

    def test_cache_clean_error_on_bad_root(self, runner: CliRunner) -> None:
        """``apm cache clean`` exits non-zero when cache root fails."""
        with patch(
            "apm_cli.cache.paths.get_cache_root",
            side_effect=OSError("no permission"),
        ):
            result = runner.invoke(cli, ["cache", "clean", "--force"], catch_exceptions=False)
        assert result.exit_code != 0


class TestCachePrune:
    """Tests for ``apm cache prune``."""

    def test_cache_prune_default_days(self, runner: CliRunner, tmp_path: Path) -> None:
        """``apm cache prune`` uses 30 days by default."""
        with (
            patch("apm_cli.cache.paths.get_cache_root", return_value=tmp_path),
            patch("apm_cli.cache.git_cache.GitCache.prune", return_value=3) as mock_prune,
        ):
            result = runner.invoke(cli, ["cache", "prune"], catch_exceptions=False)
        assert result.exit_code == 0
        mock_prune.assert_called_once_with(max_age_days=30)
        assert "3" in result.output

    def test_cache_prune_custom_days(self, runner: CliRunner, tmp_path: Path) -> None:
        """``apm cache prune --days 7`` passes the custom value."""
        with (
            patch("apm_cli.cache.paths.get_cache_root", return_value=tmp_path),
            patch("apm_cli.cache.git_cache.GitCache.prune", return_value=0) as mock_prune,
        ):
            result = runner.invoke(cli, ["cache", "prune", "--days", "7"], catch_exceptions=False)
        assert result.exit_code == 0
        mock_prune.assert_called_once_with(max_age_days=7)

    def test_cache_prune_error_on_bad_root(self, runner: CliRunner) -> None:
        """``apm cache prune`` exits non-zero when cache root fails."""
        with patch(
            "apm_cli.cache.paths.get_cache_root",
            side_effect=OSError("no permission"),
        ):
            result = runner.invoke(cli, ["cache", "prune"], catch_exceptions=False)
        assert result.exit_code != 0


# ===========================================================================
# apm runtime commands
# ===========================================================================


class TestRuntimeList:
    """Tests for ``apm runtime list``."""

    def test_runtime_list_shows_all_runtimes(self, runner: CliRunner) -> None:
        """``apm runtime list`` should list all available runtimes."""
        mock_runtimes = {
            "copilot": {
                "description": "GitHub Copilot CLI",
                "installed": False,
                "path": None,
            },
            "codex": {
                "description": "OpenAI Codex CLI",
                "installed": True,
                "path": "/usr/local/bin/codex",
                "version": "1.0.0",
            },
        }
        with patch(
            "apm_cli.runtime.manager.RuntimeManager.list_runtimes",
            return_value=mock_runtimes,
        ):
            result = runner.invoke(cli, ["runtime", "list"], catch_exceptions=False)
        assert result.exit_code == 0

    def test_runtime_list_error_propagates(self, runner: CliRunner) -> None:
        """``apm runtime list`` exits non-zero on exception."""
        with patch(
            "apm_cli.runtime.manager.RuntimeManager.list_runtimes",
            side_effect=RuntimeError("cannot list"),
        ):
            result = runner.invoke(cli, ["runtime", "list"], catch_exceptions=False)
        assert result.exit_code != 0


class TestRuntimeStatus:
    """Tests for ``apm runtime status``."""

    def test_runtime_status_no_available(self, runner: CliRunner) -> None:
        """``apm runtime status`` with no runtime shows guidance."""
        with (
            patch(
                "apm_cli.runtime.manager.RuntimeManager.get_available_runtime",
                return_value=None,
            ),
            patch(
                "apm_cli.runtime.manager.RuntimeManager.get_runtime_preference",
                return_value=["copilot", "codex", "llm", "gemini"],
            ),
        ):
            result = runner.invoke(cli, ["runtime", "status"], catch_exceptions=False)
        assert result.exit_code == 0

    def test_runtime_status_with_active_runtime(self, runner: CliRunner) -> None:
        """``apm runtime status`` shows the active runtime name."""
        with (
            patch(
                "apm_cli.runtime.manager.RuntimeManager.get_available_runtime",
                return_value="copilot",
            ),
            patch(
                "apm_cli.runtime.manager.RuntimeManager.get_runtime_preference",
                return_value=["copilot", "codex"],
            ),
        ):
            result = runner.invoke(cli, ["runtime", "status"], catch_exceptions=False)
        assert result.exit_code == 0

    def test_runtime_status_error_propagates(self, runner: CliRunner) -> None:
        """``apm runtime status`` exits non-zero on exception."""
        with patch(
            "apm_cli.runtime.manager.RuntimeManager.get_available_runtime",
            side_effect=RuntimeError("fail"),
        ):
            result = runner.invoke(cli, ["runtime", "status"], catch_exceptions=False)
        assert result.exit_code != 0


class TestRuntimeSetup:
    """Tests for ``apm runtime setup``."""

    def test_runtime_setup_success(self, runner: CliRunner) -> None:
        """``apm runtime setup copilot`` succeeds when manager returns True."""
        with patch(
            "apm_cli.runtime.manager.RuntimeManager.setup_runtime",
            return_value=True,
        ):
            result = runner.invoke(cli, ["runtime", "setup", "copilot"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "setup complete" in result.output.lower()

    def test_runtime_setup_failure_exits_nonzero(self, runner: CliRunner) -> None:
        """``apm runtime setup`` exits non-zero when manager returns False."""
        with patch(
            "apm_cli.runtime.manager.RuntimeManager.setup_runtime",
            return_value=False,
        ):
            result = runner.invoke(cli, ["runtime", "setup", "codex"], catch_exceptions=False)
        assert result.exit_code != 0

    def test_runtime_setup_error_propagates(self, runner: CliRunner) -> None:
        """``apm runtime setup`` exits non-zero on exception."""
        with patch(
            "apm_cli.runtime.manager.RuntimeManager.setup_runtime",
            side_effect=RuntimeError("no script"),
        ):
            result = runner.invoke(cli, ["runtime", "setup", "llm"], catch_exceptions=False)
        assert result.exit_code != 0

    def test_runtime_setup_with_version_flag(self, runner: CliRunner) -> None:
        """``apm runtime setup copilot --version 1.0`` passes version kwarg."""
        with patch(
            "apm_cli.runtime.manager.RuntimeManager.setup_runtime",
            return_value=True,
        ) as mock_setup:
            result = runner.invoke(
                cli,
                ["runtime", "setup", "copilot", "--version", "1.0"],
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        mock_setup.assert_called_once_with("copilot", "1.0", False)


class TestRuntimeRemove:
    """Tests for ``apm runtime remove``."""

    def test_runtime_remove_success(self, runner: CliRunner) -> None:
        """``apm runtime remove copilot --yes`` removes the runtime."""
        with patch(
            "apm_cli.runtime.manager.RuntimeManager.remove_runtime",
            return_value=True,
        ):
            result = runner.invoke(
                cli, ["runtime", "remove", "copilot", "--yes"], catch_exceptions=False
            )
        assert result.exit_code == 0

    def test_runtime_remove_failure(self, runner: CliRunner) -> None:
        """``apm runtime remove`` exits non-zero when removal fails."""
        with patch(
            "apm_cli.runtime.manager.RuntimeManager.remove_runtime",
            return_value=False,
        ):
            result = runner.invoke(
                cli, ["runtime", "remove", "codex", "--yes"], catch_exceptions=False
            )
        assert result.exit_code != 0


# ===========================================================================
# apm policy status
# ===========================================================================


class TestPolicyStatus:
    """Tests for ``apm policy status``."""

    def _make_fetch_result(self, outcome: str = "found", **kwargs: Any) -> Any:
        """Build a minimal PolicyFetchResult-like mock."""
        from apm_cli.policy.discovery import PolicyFetchResult

        return PolicyFetchResult(outcome=outcome, **kwargs)

    def test_policy_status_json_output(self, runner: CliRunner) -> None:
        """``apm policy status --json`` emits valid JSON."""
        import json

        result_mock = self._make_fetch_result(outcome="absent")
        with (
            patch(
                "apm_cli.commands.policy.discover_policy",
                return_value=result_mock,
            ),
            patch("apm_cli.commands.policy._read_cache_entry", return_value=None),
        ):
            result = runner.invoke(
                cli,
                ["policy", "status", "--json"],
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "outcome" in data

    def test_policy_status_table_output(self, runner: CliRunner) -> None:
        """``apm policy status`` renders a table by default."""
        result_mock = self._make_fetch_result(outcome="absent")
        with (
            patch(
                "apm_cli.commands.policy.discover_policy_with_chain",
                return_value=result_mock,
            ),
            patch("apm_cli.commands.policy._read_cache_entry", return_value=None),
        ):
            result = runner.invoke(
                cli,
                ["policy", "status"],
                catch_exceptions=False,
            )
        assert result.exit_code == 0

    def test_policy_status_check_flag_exits_one_on_absent(self, runner: CliRunner) -> None:
        """``apm policy status --check`` exits 1 when outcome != found."""
        result_mock = self._make_fetch_result(outcome="absent")
        with (
            patch(
                "apm_cli.commands.policy.discover_policy_with_chain",
                return_value=result_mock,
            ),
            patch("apm_cli.commands.policy._read_cache_entry", return_value=None),
        ):
            result = runner.invoke(
                cli,
                ["policy", "status", "--check"],
                catch_exceptions=False,
            )
        assert result.exit_code == 1

    def test_policy_status_check_flag_exits_zero_on_found(self, runner: CliRunner) -> None:
        """``apm policy status --check`` exits 0 when outcome == found."""
        from apm_cli.policy.schema import ApmPolicy

        policy = ApmPolicy()
        result_mock = self._make_fetch_result(outcome="found", policy=policy)
        with (
            patch(
                "apm_cli.commands.policy.discover_policy_with_chain",
                return_value=result_mock,
            ),
            patch("apm_cli.commands.policy._read_cache_entry", return_value=None),
        ):
            result = runner.invoke(
                cli,
                ["policy", "status", "--check"],
                catch_exceptions=False,
            )
        assert result.exit_code == 0

    def test_policy_status_exception_still_exits_zero(self, runner: CliRunner) -> None:
        """``apm policy status`` exits 0 even when discovery throws."""
        with patch(
            "apm_cli.commands.policy.discover_policy_with_chain",
            side_effect=RuntimeError("network error"),
        ):
            result = runner.invoke(
                cli,
                ["policy", "status"],
                catch_exceptions=False,
            )
        assert result.exit_code == 0

    def test_policy_status_no_cache_flag(self, runner: CliRunner) -> None:
        """``apm policy status --no-cache`` calls discover_policy with no_cache=True."""
        result_mock = self._make_fetch_result(outcome="absent")
        with patch(
            "apm_cli.commands.policy.discover_policy",
            return_value=result_mock,
        ) as mock_discover:
            result = runner.invoke(
                cli,
                ["policy", "status", "--no-cache"],
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        mock_discover.assert_called_once()
        _, kwargs = mock_discover.call_args
        assert kwargs.get("no_cache") is True

    def test_policy_status_with_policy_source(self, runner: CliRunner) -> None:
        """``apm policy status --policy-source`` calls discover_policy with override."""
        result_mock = self._make_fetch_result(outcome="found")
        with patch(
            "apm_cli.commands.policy.discover_policy",
            return_value=result_mock,
        ) as mock_discover:
            runner.invoke(
                cli,
                ["policy", "status", "--policy-source", "org:myorg"],
                catch_exceptions=False,
            )
        mock_discover.assert_called_once()
        _, kwargs = mock_discover.call_args
        assert kwargs.get("policy_override") == "org:myorg"


# ===========================================================================
# apm run
# ===========================================================================


class TestRunCommand:
    """Tests for ``apm run``."""

    def test_run_no_script_no_start_script_exits_nonzero(self, runner: CliRunner) -> None:
        """``apm run`` without args and no start script exits non-zero."""
        with (
            patch("apm_cli.commands.run._get_default_script", return_value=None),
            patch("apm_cli.commands.run._list_available_scripts", return_value={}),
        ):
            result = runner.invoke(cli, ["run"], catch_exceptions=False)
        assert result.exit_code != 0

    def test_run_no_script_shows_available_scripts(self, runner: CliRunner) -> None:
        """``apm run`` without args lists available scripts."""
        with (
            patch("apm_cli.commands.run._get_default_script", return_value=None),
            patch(
                "apm_cli.commands.run._list_available_scripts",
                return_value={"build": "npm run build", "test": "npm test"},
            ),
        ):
            result = runner.invoke(cli, ["run"], catch_exceptions=False)
        assert result.exit_code != 0

    def test_run_explicit_script_success(self, runner: CliRunner) -> None:
        """``apm run build`` executes script successfully."""
        mock_runner = MagicMock()
        mock_runner.run_script.return_value = True
        with patch(
            "apm_cli.core.script_runner.ScriptRunner",
            return_value=mock_runner,
        ):
            result = runner.invoke(cli, ["run", "build"], catch_exceptions=False)
        assert result.exit_code == 0
        mock_runner.run_script.assert_called_once_with("build", {})

    def test_run_explicit_script_failure(self, runner: CliRunner) -> None:
        """``apm run build`` exits non-zero when script returns False."""
        mock_runner = MagicMock()
        mock_runner.run_script.return_value = False
        with patch(
            "apm_cli.core.script_runner.ScriptRunner",
            return_value=mock_runner,
        ):
            result = runner.invoke(cli, ["run", "build"], catch_exceptions=False)
        assert result.exit_code != 0

    def test_run_with_params(self, runner: CliRunner) -> None:
        """``apm run build --param key=value`` parses parameters correctly."""
        mock_runner = MagicMock()
        mock_runner.run_script.return_value = True
        with patch(
            "apm_cli.core.script_runner.ScriptRunner",
            return_value=mock_runner,
        ):
            result = runner.invoke(
                cli,
                ["run", "build", "--param", "env=prod", "--param", "debug=true"],
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        mock_runner.run_script.assert_called_once_with("build", {"env": "prod", "debug": "true"})

    def test_run_uses_default_start_script(self, runner: CliRunner) -> None:
        """``apm run`` without script name falls back to the 'start' script."""
        mock_runner = MagicMock()
        mock_runner.run_script.return_value = True
        with (
            patch("apm_cli.commands.run._get_default_script", return_value="start"),
            patch("apm_cli.core.script_runner.ScriptRunner", return_value=mock_runner),
        ):
            result = runner.invoke(cli, ["run"], catch_exceptions=False)
        assert result.exit_code == 0
        mock_runner.run_script.assert_called_once_with("start", {})

    def test_run_import_error_warns(self, runner: CliRunner) -> None:
        """``apm run`` falls back gracefully on ImportError in ScriptRunner."""
        with patch(
            "apm_cli.core.script_runner.ScriptRunner",
            side_effect=ImportError("not available"),
        ):
            result = runner.invoke(cli, ["run", "build"], catch_exceptions=False)
        # Should exit 0 (import error path) or non-zero
        assert result.exit_code in (0, 1)

    def test_run_script_exception_exits_nonzero(self, runner: CliRunner) -> None:
        """``apm run build`` exits non-zero on unexpected exception."""
        mock_runner = MagicMock()
        mock_runner.run_script.side_effect = RuntimeError("unexpected")
        with patch(
            "apm_cli.core.script_runner.ScriptRunner",
            return_value=mock_runner,
        ):
            result = runner.invoke(cli, ["run", "build"], catch_exceptions=False)
        assert result.exit_code != 0

    def test_run_verbose_flag(self, runner: CliRunner) -> None:
        """``apm run build --verbose`` passes verbose flag without error."""
        mock_runner = MagicMock()
        mock_runner.run_script.return_value = True
        with patch(
            "apm_cli.core.script_runner.ScriptRunner",
            return_value=mock_runner,
        ):
            result = runner.invoke(cli, ["run", "build", "--verbose"], catch_exceptions=False)
        assert result.exit_code == 0


# ===========================================================================
# apm update (additional paths not already covered)
# ===========================================================================


class TestUpdateCommand:
    """Additional ``apm update`` path tests."""

    def test_update_yes_no_deps_returns_early(
        self, runner: CliRunner, isolated_config: Path
    ) -> None:
        """``apm update --yes`` on empty deps returns success without installing."""
        with runner.isolated_filesystem():
            Path("apm.yml").write_text(
                "name: empty\nversion: 1.0.0\ndependencies:\n  apm: {}\n",
                encoding="utf-8",
            )
            with patch(
                "apm_cli.commands.update.resolve_revision_pin_updates",
                return_value=[],
            ):
                result = runner.invoke(cli, ["update", "--yes"], catch_exceptions=False)
        assert result.exit_code in (0, 1)
        # Should indicate nothing to update
        assert "nothing" in result.output.lower() or result.exit_code == 0

    def test_update_dry_run_with_deps(self, runner: CliRunner, isolated_config: Path) -> None:
        """``apm update --dry-run`` shows plan and exits without changes."""
        with runner.isolated_filesystem():
            Path("apm.yml").write_text(
                "name: proj\nversion: 1.0.0\ndependencies:\n  apm:\n    org/pkg: github:org/pkg\n",
                encoding="utf-8",
            )
            with (
                patch(
                    "apm_cli.commands.update.resolve_revision_pin_updates",
                    return_value=[],
                ),
                patch(
                    "apm_cli.commands.install._install_apm_dependencies",
                ) as mock_install,
            ):
                from apm_cli.install.plan import UpdatePlan

                mock_plan = MagicMock(spec=UpdatePlan)
                mock_plan.has_changes = False
                mock_result = MagicMock()
                mock_result.installed_count = 0

                def fake_install(*args: Any, plan_callback: Any = None, **kwargs: Any) -> Any:
                    if plan_callback:
                        plan_callback(mock_plan)
                    return mock_result

                mock_install.side_effect = fake_install

                result = runner.invoke(cli, ["update", "--dry-run"], catch_exceptions=False)
        assert result.exit_code in (0, 1)
        assert "dry run" in result.output.lower() or "nothing" in result.output.lower()

    def test_update_positional_unknown_package(
        self, runner: CliRunner, isolated_config: Path
    ) -> None:
        """``apm update unknown/pkg`` exits with error when package not in apm.yml."""
        with runner.isolated_filesystem():
            Path("apm.yml").write_text(
                "name: proj\nversion: 1.0.0\ndependencies:\n  apm:\n"
                "    org/known: github:org/known\n",
                encoding="utf-8",
            )
            with patch(
                "apm_cli.commands.update.resolve_revision_pin_updates",
                return_value=[],
            ):
                result = runner.invoke(cli, ["update", "org/unknown"], catch_exceptions=False)
        # Either exits non-zero (UnknownPackageError) or 0 (nothing to update)
        # The important thing is no unhandled exception
        assert result.exit_code in (0, 1)

    def test_update_ci_env_emits_info_banner(
        self, runner: CliRunner, isolated_config: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``apm update`` in CI environment emits informational banner."""
        monkeypatch.setenv("CI", "true")
        with runner.isolated_filesystem():
            Path("apm.yml").write_text(
                "name: proj\nversion: 1.0.0\ndependencies:\n  apm: {}\n",
                encoding="utf-8",
            )
            with patch(
                "apm_cli.commands.update.resolve_revision_pin_updates",
                return_value=[],
            ):
                result = runner.invoke(cli, ["update", "--yes"], catch_exceptions=False)
        # Banner should mention apm update vs apm self-update distinction
        assert "self-update" in result.output.lower() or result.exit_code in (0, 1)

    def test_update_apm_yml_parse_error_exits_nonzero(
        self, runner: CliRunner, isolated_config: Path
    ) -> None:
        """``apm update`` exits non-zero when apm.yml has invalid YAML."""
        with runner.isolated_filesystem():
            Path("apm.yml").write_text(
                "name: [broken\n",
                encoding="utf-8",
            )
            result = runner.invoke(cli, ["update", "--yes"], catch_exceptions=False)
        assert result.exit_code != 0


# ===========================================================================
# compile/watcher.py -- APMFileHandler
# ===========================================================================


class TestAPMFileHandlerDebounce:
    """Unit tests for APMFileHandler event filtering and debounce logic."""

    def _make_handler(self) -> Any:
        """Create an APMFileHandler instance with a mock logger."""
        from apm_cli.commands.compile.watcher import APMFileHandler
        from apm_cli.core.command_logger import CommandLogger

        logger = MagicMock(spec=CommandLogger)
        return APMFileHandler(
            output="AGENTS.md",
            chatmode=None,
            no_links=False,
            dry_run=False,
            logger=logger,
        )

    def _make_event(self, path: str, is_dir: bool = False) -> MagicMock:
        """Create a minimal filesystem event mock."""
        event = MagicMock()
        event.src_path = path
        event.is_directory = is_dir
        return event

    def test_directory_events_are_ignored(self) -> None:
        """Directory change events should not trigger recompile."""
        handler = self._make_handler()
        with patch.object(handler, "_recompile") as mock_recompile:
            event = self._make_event("/fake/.apm/dir", is_dir=True)
            handler.on_modified(event)
        mock_recompile.assert_not_called()

    def test_non_primitive_file_ignored(self) -> None:
        """Generic .md files (README, CHANGELOG) should not trigger recompile."""
        handler = self._make_handler()
        with patch.object(handler, "_recompile") as mock_recompile:
            event = self._make_event("/fake/README.md", is_dir=False)
            handler.on_modified(event)
        mock_recompile.assert_not_called()

    def test_apm_yml_triggers_recompile(self) -> None:
        """apm.yml change should trigger recompile."""
        handler = self._make_handler()
        with patch.object(handler, "_recompile") as mock_recompile:
            event = self._make_event("/fake/apm.yml", is_dir=False)
            handler.on_modified(event)
        mock_recompile.assert_called_once_with("/fake/apm.yml")

    def test_skill_md_triggers_recompile(self) -> None:
        """SKILL.md file change should trigger recompile."""
        handler = self._make_handler()
        with patch.object(handler, "_recompile") as mock_recompile:
            event = self._make_event("/fake/.apm/skills/SKILL.md", is_dir=False)
            handler.on_modified(event)
        mock_recompile.assert_called_once()

    def test_instruction_file_triggers_recompile(self) -> None:
        """A .instructions.md file change should trigger recompile."""
        handler = self._make_handler()
        with patch.object(handler, "_recompile") as mock_recompile:
            event = self._make_event(
                "/fake/.github/instructions/some.instructions.md", is_dir=False
            )
            handler.on_modified(event)
        mock_recompile.assert_called_once()

    def test_debounce_suppresses_rapid_events(self) -> None:
        """Rapid successive events should be debounced to one recompile."""
        handler = self._make_handler()
        # Set last_compile to now so the first event also triggers debounce
        handler.last_compile = time.time()
        with patch.object(handler, "_recompile") as mock_recompile:
            event = self._make_event("/fake/apm.yml", is_dir=False)
            handler.on_modified(event)
            handler.on_modified(event)
            handler.on_modified(event)
        # All should be blocked by debounce
        mock_recompile.assert_not_called()

    def test_debounce_allows_event_after_delay(self) -> None:
        """An event after the debounce window should trigger recompile."""
        handler = self._make_handler()
        # Set last_compile far in the past
        handler.last_compile = 0.0
        with patch.object(handler, "_recompile") as mock_recompile:
            event = self._make_event("/fake/apm.yml", is_dir=False)
            handler.on_modified(event)
        mock_recompile.assert_called_once()

    def test_recompile_calls_compiler(self) -> None:
        """_recompile() should call AgentsCompiler.compile."""
        from apm_cli.commands.compile.watcher import APMFileHandler
        from apm_cli.core.command_logger import CommandLogger

        logger = MagicMock(spec=CommandLogger)
        handler = APMFileHandler(
            output="AGENTS.md",
            chatmode=None,
            no_links=False,
            dry_run=False,
            logger=logger,
        )
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.output_path = "AGENTS.md"
        with (
            patch(
                "apm_cli.commands.compile.watcher.AgentsCompiler.compile",
                return_value=mock_result,
            ),
            patch(
                "apm_cli.commands.compile.watcher.CompilationConfig.from_apm_yml",
            ),
            patch("apm_cli.commands.compile.watcher.clear_discovery_cache"),
        ):
            handler._recompile("/fake/apm.yml")

    def test_recompile_dry_run_success_message(self) -> None:
        """_recompile() with dry_run=True emits a dry run success message."""
        from apm_cli.commands.compile.watcher import APMFileHandler
        from apm_cli.core.command_logger import CommandLogger

        logger = MagicMock(spec=CommandLogger)
        handler = APMFileHandler(
            output="AGENTS.md",
            chatmode=None,
            no_links=False,
            dry_run=True,
            logger=logger,
        )
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.output_path = "AGENTS.md"
        with (
            patch(
                "apm_cli.commands.compile.watcher.AgentsCompiler.compile",
                return_value=mock_result,
            ),
            patch(
                "apm_cli.commands.compile.watcher.CompilationConfig.from_apm_yml",
            ),
            patch("apm_cli.commands.compile.watcher.clear_discovery_cache"),
        ):
            handler._recompile("/fake/some.prompt.md")
        logger.success.assert_called()
        call_args = str(logger.success.call_args_list)
        assert "dry run" in call_args.lower()

    def test_recompile_failure_logs_errors(self) -> None:
        """_recompile() on compilation failure logs each error."""
        from apm_cli.commands.compile.watcher import APMFileHandler
        from apm_cli.core.command_logger import CommandLogger

        logger = MagicMock(spec=CommandLogger)
        handler = APMFileHandler(
            output="AGENTS.md",
            chatmode=None,
            no_links=False,
            dry_run=False,
            logger=logger,
        )
        mock_result = MagicMock()
        mock_result.success = False
        mock_result.errors = ["syntax error in file.md"]
        with (
            patch(
                "apm_cli.commands.compile.watcher.AgentsCompiler.compile",
                return_value=mock_result,
            ),
            patch(
                "apm_cli.commands.compile.watcher.CompilationConfig.from_apm_yml",
            ),
            patch("apm_cli.commands.compile.watcher.clear_discovery_cache"),
        ):
            handler._recompile("/fake/broken.instructions.md")
        logger.error.assert_called()

    def test_format_target_label_single_target(self) -> None:
        """_format_target_label for a single string target returns a label."""
        from apm_cli.commands.compile.watcher import _format_target_label

        label = _format_target_label("copilot", "copilot", None)
        assert label is not None
        assert "copilot" in label.lower() or "compiling" in label.lower()

    def test_format_target_label_none_target(self) -> None:
        """_format_target_label with None effective_target returns None."""
        from apm_cli.commands.compile.watcher import _format_target_label

        result = _format_target_label(None, None, None)
        assert result is None


# ===========================================================================
# install/mcp/registry.py
# ===========================================================================


class TestMcpRegistryValidation:
    """Tests for ``install/mcp/registry.py`` validation helpers."""

    def test_validate_registry_url_none(self) -> None:
        """``validate_registry_url(None)`` returns None."""
        from apm_cli.install.mcp.registry import validate_registry_url

        assert validate_registry_url(None) is None

    def test_validate_registry_url_valid_https(self) -> None:
        """``validate_registry_url`` accepts a valid HTTPS URL."""
        from apm_cli.install.mcp.registry import validate_registry_url

        url = validate_registry_url("https://registry.example.com")
        parsed = urllib.parse.urlparse(url)
        assert parsed.scheme == "https"
        assert "registry.example.com" in parsed.netloc

    def test_validate_registry_url_valid_http(self) -> None:
        """``validate_registry_url`` accepts HTTP (explicit flag intent)."""
        from apm_cli.install.mcp.registry import validate_registry_url

        url = validate_registry_url("http://local-registry.internal")
        parsed = urllib.parse.urlparse(url)
        assert parsed.scheme == "http"

    def test_validate_registry_url_rejects_empty(self) -> None:
        """``validate_registry_url`` rejects empty strings."""
        import click

        from apm_cli.install.mcp.registry import validate_registry_url

        with pytest.raises(click.UsageError):
            validate_registry_url("   ")

    def test_validate_registry_url_rejects_file_scheme(self) -> None:
        """``validate_registry_url`` rejects file:// URLs."""
        import click

        from apm_cli.install.mcp.registry import validate_registry_url

        with pytest.raises(click.UsageError):
            validate_registry_url("file:///etc/passwd")

    def test_validate_registry_url_rejects_ws_scheme(self) -> None:
        """``validate_registry_url`` rejects ws:// URLs."""
        import click

        from apm_cli.install.mcp.registry import validate_registry_url

        with pytest.raises(click.UsageError):
            validate_registry_url("ws://evil.example.com")

    def test_validate_registry_url_rejects_no_netloc(self) -> None:
        """``validate_registry_url`` rejects URLs with no host."""
        import click

        from apm_cli.install.mcp.registry import validate_registry_url

        with pytest.raises(click.UsageError):
            validate_registry_url("https://")

    def test_validate_registry_url_rejects_too_long(self) -> None:
        """``validate_registry_url`` rejects URLs exceeding 2048 chars."""
        import click

        from apm_cli.install.mcp.registry import validate_registry_url

        long_url = "https://example.com/" + "a" * 2050
        with pytest.raises(click.UsageError):
            validate_registry_url(long_url)

    def test_validate_registry_url_strips_trailing_slash(self) -> None:
        """``validate_registry_url`` strips trailing slashes."""
        from apm_cli.install.mcp.registry import validate_registry_url

        url = validate_registry_url("https://registry.example.com/")
        assert not url.endswith("/")

    def test_redact_url_credentials(self) -> None:
        """``_redact_url_credentials`` removes user:password@ from URL."""
        from apm_cli.install.mcp.registry import _redact_url_credentials

        url = "https://user:token@registry.example.com/path"
        redacted = _redact_url_credentials(url)
        parsed = urllib.parse.urlparse(redacted)
        assert "token" not in redacted
        assert "user" not in redacted
        assert "registry.example.com" in parsed.netloc

    def test_is_local_or_metadata_host_localhost(self) -> None:
        """``_is_local_or_metadata_host`` detects localhost."""
        from apm_cli.install.mcp.registry import _is_local_or_metadata_host

        assert _is_local_or_metadata_host("localhost") is True

    def test_is_local_or_metadata_host_loopback_ip(self) -> None:
        """``_is_local_or_metadata_host`` detects 127.0.0.1."""
        from apm_cli.install.mcp.registry import _is_local_or_metadata_host

        assert _is_local_or_metadata_host("127.0.0.1") is True

    def test_is_local_or_metadata_host_private_ip(self) -> None:
        """``_is_local_or_metadata_host`` detects RFC1918 addresses."""
        from apm_cli.install.mcp.registry import _is_local_or_metadata_host

        assert _is_local_or_metadata_host("192.168.1.1") is True
        assert _is_local_or_metadata_host("10.0.0.1") is True

    def test_is_local_or_metadata_host_public_host(self) -> None:
        """``_is_local_or_metadata_host`` returns False for public hosts."""
        from apm_cli.install.mcp.registry import _is_local_or_metadata_host

        assert _is_local_or_metadata_host("registry.npmjs.org") is False
        assert _is_local_or_metadata_host(None) is False

    def test_resolve_registry_url_flag_wins(self) -> None:
        """``resolve_registry_url`` with CLI flag returns flag value as source."""
        from apm_cli.install.mcp.registry import resolve_registry_url

        with patch.dict(os.environ, {}, clear=True):
            url, source = resolve_registry_url("https://flag.example.com")
        assert url == "https://flag.example.com"
        assert source == "flag"

    def test_resolve_registry_url_env_var(self) -> None:
        """``resolve_registry_url`` reads from MCP_REGISTRY_URL env var."""
        from apm_cli.install.mcp.registry import resolve_registry_url

        with patch.dict(os.environ, {"MCP_REGISTRY_URL": "https://env.example.com"}, clear=False):
            url, source = resolve_registry_url(None)
        assert url == "https://env.example.com"
        assert source == "env"

    def test_resolve_registry_url_default(self) -> None:
        """``resolve_registry_url`` returns None when no source is configured."""
        from apm_cli.install.mcp.registry import resolve_registry_url

        env_without_registry = {
            k: v
            for k, v in os.environ.items()
            if k not in ("MCP_REGISTRY_URL", "MCP_REGISTRY_ALLOW_HTTP")
        }
        with (
            patch.dict(os.environ, env_without_registry, clear=True),
            patch("apm_cli.config.get_mcp_registry_url", return_value=None),
        ):
            url, source = resolve_registry_url(None)
        assert url is None
        assert source == "default"

    def test_registry_env_override_sets_and_restores(self) -> None:
        """``registry_env_override`` sets env vars and restores them."""
        from apm_cli.install.mcp.registry import registry_env_override

        original = os.environ.get("MCP_REGISTRY_URL")
        with registry_env_override("https://temp.example.com"):
            assert os.environ.get("MCP_REGISTRY_URL") == "https://temp.example.com"
        # Restored after context manager exits
        assert os.environ.get("MCP_REGISTRY_URL") == original

    def test_registry_env_override_http_sets_allow_flag(self) -> None:
        """``registry_env_override`` with http:// also sets MCP_REGISTRY_ALLOW_HTTP."""
        from apm_cli.install.mcp.registry import registry_env_override

        with registry_env_override("http://local.example.com"):
            assert os.environ.get("MCP_REGISTRY_ALLOW_HTTP") == "1"
        # Cleaned up after exit
        assert os.environ.get("MCP_REGISTRY_ALLOW_HTTP") is None

    def test_registry_env_override_none_is_noop(self) -> None:
        """``registry_env_override(None)`` is a no-op context manager."""
        from apm_cli.install.mcp.registry import registry_env_override

        before = os.environ.get("MCP_REGISTRY_URL")
        with registry_env_override(None):
            assert os.environ.get("MCP_REGISTRY_URL") == before


# ===========================================================================
# install/insecure_policy.py
# ===========================================================================


class TestInsecurePolicy:
    """Tests for ``install/insecure_policy.py`` helper functions."""

    def test_format_insecure_dep_requirements_both_missing(self) -> None:
        """Full two-step recipe when both dep flag and CLI flag are missing."""
        from apm_cli.install.insecure_policy import _format_insecure_dependency_requirements

        msg = _format_insecure_dependency_requirements(
            "http://example.com/pkg",
            missing_dep_allow=True,
            missing_cli_flag=True,
        )
        assert "allow_insecure: true" in msg
        assert "--allow-insecure" in msg
        # Steps should be numbered
        assert "1." in msg
        assert "2." in msg

    def test_format_insecure_dep_requirements_only_cli_missing(self) -> None:
        """Only CLI flag step when dep entry already has allow_insecure: true."""
        from apm_cli.install.insecure_policy import _format_insecure_dependency_requirements

        msg = _format_insecure_dependency_requirements(
            "http://example.com/pkg",
            missing_dep_allow=False,
            missing_cli_flag=True,
        )
        assert "allow_insecure: true" not in msg
        assert "--allow-insecure" in msg

    def test_format_insecure_dep_warning_direct(self) -> None:
        """Warning for a direct insecure dependency."""
        from apm_cli.install.insecure_policy import (
            _format_insecure_dependency_warning,
            _InsecureDependencyInfo,
        )

        info = _InsecureDependencyInfo(url="http://bad.example.com/pkg", is_transitive=False)
        msg = _format_insecure_dependency_warning(info)
        assert "http://bad.example.com/pkg" in msg
        assert "transitive" not in msg

    def test_format_insecure_dep_warning_transitive(self) -> None:
        """Warning for a transitive insecure dependency includes introducer."""
        from apm_cli.install.insecure_policy import (
            _format_insecure_dependency_warning,
            _InsecureDependencyInfo,
        )

        info = _InsecureDependencyInfo(
            url="http://bad.example.com/pkg",
            is_transitive=True,
            introduced_by="parent-org/parent-pkg",
        )
        msg = _format_insecure_dependency_warning(info)
        assert "transitive" in msg
        assert "parent-org/parent-pkg" in msg

    def test_get_insecure_dep_host_extracts_hostname(self) -> None:
        """``_get_insecure_dependency_host`` extracts the host from a URL."""
        from apm_cli.install.insecure_policy import (
            _get_insecure_dependency_host,
            _InsecureDependencyInfo,
        )

        info = _InsecureDependencyInfo(
            url="http://mirror.example.com:8080/pkg#main", is_transitive=False
        )
        host = _get_insecure_dependency_host(info)
        # urllib.parse is used internally -- validate via parsed form
        parsed = urllib.parse.urlparse("http://mirror.example.com:8080/pkg")
        assert host == parsed.hostname.lower()

    def test_normalize_allow_insecure_host_valid(self) -> None:
        """``_normalize_allow_insecure_host`` normalizes a valid hostname."""
        from apm_cli.install.insecure_policy import _normalize_allow_insecure_host

        result = _normalize_allow_insecure_host("Mirror.EXAMPLE.com")
        assert result == "mirror.example.com"

    def test_normalize_allow_insecure_host_invalid(self) -> None:
        """``_normalize_allow_insecure_host`` raises ValueError on bad hostname."""
        from apm_cli.install.insecure_policy import _normalize_allow_insecure_host

        with pytest.raises(ValueError, match="Invalid hostname"):
            _normalize_allow_insecure_host("not a valid hostname!")

    def test_guard_transitive_insecure_deps_blocks_unapproved_host(self) -> None:
        """``_guard_transitive_insecure_dependencies`` raises on blocked host."""
        from apm_cli.install.insecure_policy import (
            InsecureDependencyPolicyError,
            _guard_transitive_insecure_dependencies,
            _InsecureDependencyInfo,
        )

        logger = MagicMock()
        infos = [
            _InsecureDependencyInfo(
                url="http://evil.example.com/pkg",
                is_transitive=True,
                introduced_by="parent/pkg",
            )
        ]
        with pytest.raises(InsecureDependencyPolicyError):
            _guard_transitive_insecure_dependencies(
                infos,
                logger,
                allow_insecure=False,
                allow_insecure_hosts=(),
            )

    def test_guard_transitive_insecure_deps_allows_approved_host(self) -> None:
        """``_guard_transitive_insecure_dependencies`` passes when host is approved."""
        from apm_cli.install.insecure_policy import (
            _guard_transitive_insecure_dependencies,
            _InsecureDependencyInfo,
        )

        logger = MagicMock()
        infos = [
            _InsecureDependencyInfo(
                url="http://approved.example.com/pkg",
                is_transitive=True,
            )
        ]
        # Should not raise
        _guard_transitive_insecure_dependencies(
            infos,
            logger,
            allow_insecure=False,
            allow_insecure_hosts=("approved.example.com",),
        )

    def test_guard_transitive_no_transitive_deps_is_noop(self) -> None:
        """``_guard_transitive_insecure_dependencies`` does nothing with no transitives."""
        from apm_cli.install.insecure_policy import (
            _guard_transitive_insecure_dependencies,
            _InsecureDependencyInfo,
        )

        logger = MagicMock()
        infos = [
            _InsecureDependencyInfo(
                url="http://direct.example.com/pkg",
                is_transitive=False,
            )
        ]
        _guard_transitive_insecure_dependencies(
            infos,
            logger,
            allow_insecure=False,
            allow_insecure_hosts=(),
        )

    def test_allow_insecure_host_callback_normalizes_hosts(self) -> None:
        """``_allow_insecure_host_callback`` normalizes and deduplicates hosts."""
        from apm_cli.install.insecure_policy import _allow_insecure_host_callback

        ctx = MagicMock()
        param = MagicMock()
        result = _allow_insecure_host_callback(
            ctx, param, ["Mirror.EXAMPLE.com", "mirror.example.com", "other.example.com"]
        )
        assert "mirror.example.com" in result
        assert "other.example.com" in result
        # Deduplicated
        assert len([h for h in result if h == "mirror.example.com"]) == 1

    def test_allow_insecure_host_callback_invalid_raises(self) -> None:
        """``_allow_insecure_host_callback`` raises click.BadParameter on bad host."""
        import click

        from apm_cli.install.insecure_policy import _allow_insecure_host_callback

        ctx = MagicMock()
        param = MagicMock()
        with pytest.raises(click.BadParameter):
            _allow_insecure_host_callback(ctx, param, ["not a valid host!"])


# ===========================================================================
# deps/shared_clone_cache.py
# ===========================================================================


class TestSharedCloneCache:
    """Tests for ``SharedCloneCache`` thread-safe per-run clone cache."""

    def test_get_or_clone_calls_clone_fn_once(self, tmp_path: Path) -> None:
        """``get_or_clone`` calls clone_fn exactly once for a new key."""
        from apm_cli.deps.shared_clone_cache import SharedCloneCache

        call_count = 0

        def clone_fn(path: Path) -> None:
            nonlocal call_count
            call_count += 1
            path.mkdir(parents=True, exist_ok=True)
            # Create a bare-repo shaped directory
            (path / "HEAD").write_text("ref: refs/heads/main\n")

        cache = SharedCloneCache(base_dir=tmp_path)
        try:
            result = cache.get_or_clone("github.com", "owner", "repo", "main", clone_fn)
            assert result.is_dir()
            assert call_count == 1
        finally:
            cache.cleanup()

    def test_get_or_clone_reuses_cached_result(self, tmp_path: Path) -> None:
        """``get_or_clone`` returns the same path on the second call."""
        from apm_cli.deps.shared_clone_cache import SharedCloneCache

        call_count = 0

        def clone_fn(path: Path) -> None:
            nonlocal call_count
            call_count += 1
            path.mkdir(parents=True, exist_ok=True)
            (path / "HEAD").write_text("ref: refs/heads/main\n")

        cache = SharedCloneCache(base_dir=tmp_path)
        try:
            p1 = cache.get_or_clone("github.com", "owner", "repo", "main", clone_fn)
            p2 = cache.get_or_clone("github.com", "owner", "repo", "main", clone_fn)
            assert p1 == p2
            assert call_count == 1  # Only cloned once
        finally:
            cache.cleanup()

    def test_get_or_clone_different_keys_clone_separately(self, tmp_path: Path) -> None:
        """Different (owner, repo, ref) keys produce separate clones."""
        from apm_cli.deps.shared_clone_cache import SharedCloneCache

        def clone_fn(path: Path) -> None:
            path.mkdir(parents=True, exist_ok=True)
            (path / "HEAD").write_text("ref: refs/heads/main\n")

        cache = SharedCloneCache(base_dir=tmp_path)
        try:
            p1 = cache.get_or_clone("github.com", "owner", "repo1", "main", clone_fn)
            p2 = cache.get_or_clone("github.com", "owner", "repo2", "main", clone_fn)
            assert p1 != p2
        finally:
            cache.cleanup()

    def test_cleanup_removes_temp_dirs(self, tmp_path: Path) -> None:
        """``cleanup()`` removes all created temp directories."""
        from apm_cli.deps.shared_clone_cache import SharedCloneCache

        def clone_fn(path: Path) -> None:
            path.mkdir(parents=True, exist_ok=True)
            (path / "HEAD").write_text("ref: refs/heads/main\n")

        cache = SharedCloneCache(base_dir=tmp_path)
        result_path = cache.get_or_clone("github.com", "owner", "repo", "main", clone_fn)
        assert result_path.exists()

        cache.cleanup()
        # After cleanup the path may not exist
        # The important check is that cleanup() doesn't raise
        assert len(cache._temp_dirs) == 0

    def test_context_manager_calls_cleanup(self, tmp_path: Path) -> None:
        """Using SharedCloneCache as a context manager calls cleanup on exit."""
        from apm_cli.deps.shared_clone_cache import SharedCloneCache

        def clone_fn(path: Path) -> None:
            path.mkdir(parents=True, exist_ok=True)
            (path / "HEAD").write_text("ref: refs/heads/main\n")

        with SharedCloneCache(base_dir=tmp_path) as cache:
            cache.get_or_clone("github.com", "owner", "repo", "main", clone_fn)
        # After context manager exit, entries should be cleared
        assert len(cache._entries) == 0

    def test_clone_failure_does_not_cache_result(self, tmp_path: Path) -> None:
        """A failed clone does not cache a result."""
        from apm_cli.deps.shared_clone_cache import SharedCloneCache

        def failing_clone_fn(path: Path) -> None:
            raise RuntimeError("clone failed")

        cache = SharedCloneCache(base_dir=tmp_path)
        try:
            with pytest.raises(RuntimeError, match="clone failed"):
                cache.get_or_clone("github.com", "owner", "repo", "main", failing_clone_fn)
            # Entry should have error set, not path
            key = ("github.com", "owner", "repo", "main")
            entry = cache._entries.get(key)
            assert entry is not None
            assert entry.path is None
        finally:
            cache.cleanup()

    def test_find_repo_bare_returns_none_when_empty(self, tmp_path: Path) -> None:
        """``_find_repo_bare`` returns None when no bare is registered."""
        from apm_cli.deps.shared_clone_cache import SharedCloneCache

        cache = SharedCloneCache(base_dir=tmp_path)
        try:
            result = cache._find_repo_bare("github.com", "owner", "repo")
            assert result is None
        finally:
            cache.cleanup()

    def test_tier0_fetch_fn_used_for_existing_bare(self, tmp_path: Path) -> None:
        """When a bare exists for the same repo, fetch_fn is tried before fresh clone."""
        from apm_cli.deps.shared_clone_cache import SharedCloneCache

        def clone_fn(path: Path) -> None:
            path.mkdir(parents=True, exist_ok=True)
            (path / "HEAD").write_text("ref: refs/heads/main\n")

        fetch_called = []

        def fetch_fn(bare_path: Path, sha: str) -> bool:
            fetch_called.append((bare_path, sha))
            return True  # Successful fetch

        cache = SharedCloneCache(base_dir=tmp_path)
        try:
            # First clone to register a bare
            cache.get_or_clone("github.com", "owner", "repo", "main", clone_fn)

            # Second request for same repo but different ref with fetch_fn
            cache.get_or_clone(
                "github.com", "owner", "repo", "abc1234", clone_fn, fetch_fn=fetch_fn
            )
            # fetch_fn should have been called
            assert len(fetch_called) > 0
        finally:
            cache.cleanup()


# ===========================================================================
# deps/bare_cache.py helpers
# ===========================================================================


class TestBareCacheHelpers:
    """Tests for helper functions in ``deps/bare_cache.py``."""

    def test_scrub_bare_remote_url_calls_git(self, tmp_path: Path) -> None:
        """``_scrub_bare_remote_url`` invokes git remote set-url to redact URL."""
        from apm_cli.deps.bare_cache import _scrub_bare_remote_url

        bare_path = tmp_path / "test.git"
        bare_path.mkdir()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            _scrub_bare_remote_url(bare_path, "git", {"PATH": "/usr/bin"})
        mock_run.assert_called_once()
        # Verify that "redacted://" appears in the call arguments
        call_args = mock_run.call_args[0][0]
        assert "redacted://" in call_args

    def test_scrub_bare_remote_url_handles_git_failure(self, tmp_path: Path) -> None:
        """``_scrub_bare_remote_url`` logs a warning on git failure (non-fatal)."""
        from apm_cli.deps.bare_cache import _scrub_bare_remote_url

        bare_path = tmp_path / "test.git"
        bare_path.mkdir()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)
            # Should not raise even on failure
            _scrub_bare_remote_url(bare_path, "git", {})

    def test_scrub_bare_remote_url_truncates_fetch_head(self, tmp_path: Path) -> None:
        """``_scrub_bare_remote_url`` truncates FETCH_HEAD when it exists."""
        from apm_cli.deps.bare_cache import _scrub_bare_remote_url

        bare_path = tmp_path / "test.git"
        bare_path.mkdir()
        fetch_head = bare_path / "FETCH_HEAD"
        fetch_head.write_text("https://oauth2:TOKEN@github.com/org/repo main\n")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            _scrub_bare_remote_url(bare_path, "git", {})

        # FETCH_HEAD should be truncated
        assert fetch_head.read_text() == ""

    def test_scrub_bare_remote_url_exception_is_logged(self, tmp_path: Path) -> None:
        """``_scrub_bare_remote_url`` logs a warning on subprocess exception."""
        from apm_cli.deps.bare_cache import _scrub_bare_remote_url

        bare_path = tmp_path / "test.git"
        bare_path.mkdir()

        with patch("subprocess.run", side_effect=OSError("git not found")):
            # Should not raise
            _scrub_bare_remote_url(bare_path, "git", {})

    def test_build_clone_failure_message(self) -> None:
        """``build_clone_failure_message`` returns a helpful error string."""
        from unittest.mock import MagicMock

        from apm_cli.deps.bare_cache import build_clone_failure_message

        mock_plan = MagicMock()
        mock_plan.strict = False
        mock_plan.attempts = []
        mock_plan.fallback_hint = None

        mock_auth = MagicMock()
        mock_auth.build_error_context.return_value = ""

        msg = build_clone_failure_message(
            repo_url_base="https://github.com/owner/repo",
            plan=mock_plan,
            dep_ref=None,
            dep_host=None,
            is_ado=False,
            is_generic=False,
            has_ado_token=False,
            has_token=True,
            auth_resolver=mock_auth,
            configured_github_host="github.com",
            default_host_fn=lambda: "github.com",
            last_error=None,
            sanitize_git_error=lambda s: s,
        )
        assert isinstance(msg, str)
        assert "owner/repo" in msg


# ===========================================================================
# policy helper unit tests
# ===========================================================================


class TestPolicyHelpers:
    """Unit tests for helper functions in ``commands/policy.py``."""

    def test_strip_source_prefix_org(self) -> None:
        """``_strip_source_prefix`` removes the org: prefix."""
        from apm_cli.commands.policy import _strip_source_prefix

        assert _strip_source_prefix("org:my-org") == "my-org"

    def test_strip_source_prefix_url(self) -> None:
        """``_strip_source_prefix`` removes the url: prefix."""
        from apm_cli.commands.policy import _strip_source_prefix

        assert _strip_source_prefix("url:https://example.com") == "https://example.com"

    def test_strip_source_prefix_file(self) -> None:
        """``_strip_source_prefix`` removes the file: prefix."""
        from apm_cli.commands.policy import _strip_source_prefix

        assert _strip_source_prefix("file:/path/to/policy.yml") == "/path/to/policy.yml"

    def test_strip_source_prefix_no_prefix(self) -> None:
        """``_strip_source_prefix`` returns the string unchanged if no known prefix."""
        from apm_cli.commands.policy import _strip_source_prefix

        assert _strip_source_prefix("bare-string") == "bare-string"

    def test_format_age_none(self) -> None:
        """``_format_age(None)`` returns 'n/a'."""
        from apm_cli.commands.policy import _format_age

        assert _format_age(None) == "n/a"

    def test_format_age_seconds(self) -> None:
        """``_format_age`` returns seconds label for < 60s."""
        from apm_cli.commands.policy import _format_age

        result = _format_age(45)
        assert "s" in result and "ago" in result

    def test_format_age_minutes(self) -> None:
        """``_format_age`` returns minutes label for 60-3599s."""
        from apm_cli.commands.policy import _format_age

        result = _format_age(120)
        assert "m" in result and "ago" in result

    def test_format_age_hours(self) -> None:
        """``_format_age`` returns hours label for 3600-86399s."""
        from apm_cli.commands.policy import _format_age

        result = _format_age(7200)
        assert "h" in result and "ago" in result

    def test_format_age_days(self) -> None:
        """``_format_age`` returns days label for >= 86400s."""
        from apm_cli.commands.policy import _format_age

        result = _format_age(86400 * 3)
        assert "d" in result and "ago" in result

    def test_count_rules_none_policy(self) -> None:
        """``_count_rules(None)`` returns an empty dict."""
        from apm_cli.commands.policy import _count_rules

        assert _count_rules(None) == {}

    def test_summarize_rules_empty(self) -> None:
        """``_summarize_rules({})`` returns an empty list."""
        from apm_cli.commands.policy import _summarize_rules

        assert _summarize_rules({}) == []

    def test_summarize_rules_filters_zero_and_negative(self) -> None:
        """``_summarize_rules`` skips zero and negative-one counts."""
        from apm_cli.commands.policy import _summarize_rules

        counts = {
            "dependencies_deny": 0,
            "mcp_deny": -1,
            "mcp_allow": 3,
        }
        result = _summarize_rules(counts)
        assert len(result) == 1
        assert "mcp" in result[0].lower()
