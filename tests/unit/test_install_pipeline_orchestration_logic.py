"""Unit tests for apm_cli.install.pipeline (pure logic cases)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# _run_phase
# ---------------------------------------------------------------------------


class TestRunPhase:
    def test_non_verbose_calls_phase(self):
        from apm_cli.install.pipeline import _run_phase

        ctx = MagicMock()
        ctx.verbose = False
        ctx.logger = MagicMock()
        phase = MagicMock()
        phase.run.return_value = "result"
        result = _run_phase("myphase", phase, ctx)
        phase.run.assert_called_once_with(ctx)
        assert result == "result"

    def test_verbose_calls_phase_and_logs(self):
        from apm_cli.install.pipeline import _run_phase

        ctx = MagicMock()
        ctx.verbose = True
        ctx.logger = MagicMock()
        phase = MagicMock()
        phase.run.return_value = "done"
        result = _run_phase("myphase", phase, ctx)
        assert result == "done"
        ctx.logger.verbose_detail.assert_called_once()

    def test_verbose_phase_exception_propagates(self):
        from apm_cli.install.pipeline import _run_phase

        ctx = MagicMock()
        ctx.verbose = True
        ctx.logger = MagicMock()
        phase = MagicMock()
        phase.run.side_effect = RuntimeError("phase failed")
        with pytest.raises(RuntimeError, match="phase failed"):
            _run_phase("myphase", phase, ctx)

    def test_no_logger_falls_through(self):
        from apm_cli.install.pipeline import _run_phase

        ctx = MagicMock()
        ctx.verbose = True
        ctx.logger = None
        phase = MagicMock()
        phase.run.return_value = 42
        result = _run_phase("myphase", phase, ctx)
        assert result == 42


# ---------------------------------------------------------------------------
# run_install_pipeline -- empty deps short-circuit
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# run_install_pipeline -- plan_callback
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# run_install_pipeline -- exception wrapping
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _preflight_auth_check
# ---------------------------------------------------------------------------


class TestPreflightAuthCheck:
    def _make_ctx(self, deps=None):
        ctx = MagicMock()
        ctx.deps_to_install = deps or []
        ctx.verbose = False
        ctx.logger = MagicMock()
        return ctx

    def test_skips_github_hosts(self):
        from apm_cli.install.pipeline import _preflight_auth_check

        dep = MagicMock()
        dep.host = "github.com"
        dep.repo_url = "org/repo"
        ctx = self._make_ctx(deps=[dep])
        auth = MagicMock()

        with patch("apm_cli.utils.github_host.is_github_hostname", return_value=True):
            # Should complete without raising
            _preflight_auth_check(ctx, auth, verbose=False)

    def test_skips_deps_without_host(self):
        from apm_cli.install.pipeline import _preflight_auth_check

        dep = MagicMock()
        dep.host = None
        dep.repo_url = "org/repo"
        ctx = self._make_ctx(deps=[dep])
        auth = MagicMock()

        # No exception expected
        _preflight_auth_check(ctx, auth, verbose=False)

    def test_auth_success_does_not_raise(self):
        from apm_cli.install.pipeline import _preflight_auth_check

        dep = MagicMock()
        dep.host = "ado.example.com"
        dep.repo_url = "org/repo"
        dep.is_azure_devops.return_value = True
        ctx = self._make_ctx(deps=[dep])

        dep_ctx = MagicMock()
        dep_ctx.auth_scheme = "basic"
        dep_ctx.source = "ADO_APM_PAT"
        dep_ctx.token = "tok"
        dep_ctx.git_env = {}
        auth = MagicMock()
        auth.resolve_for_dep.return_value = dep_ctx
        auth._build_git_env.return_value = {}
        auth.try_with_fallback.side_effect = lambda _host, op, **_kw: op(dep_ctx.token, {})

        proc = MagicMock()
        proc.returncode = 0
        proc.stderr = ""

        with (
            patch("apm_cli.utils.github_host.is_github_hostname", return_value=False),
            patch("apm_cli.utils.github_host.is_azure_devops_hostname", return_value=True),
            patch("apm_cli.utils.github_host.is_ado_auth_failure_signal", return_value=False),
            patch("subprocess.run", return_value=proc),
            patch(
                "apm_cli.deps.github_downloader.GitHubPackageDownloader._build_repo_url",
                return_value="https://ado.example.com/org/repo.git",
            ),
        ):
            _preflight_auth_check(ctx, auth, verbose=False)
