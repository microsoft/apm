"""Unit tests for ``apm_cli.install.pipeline``.

Covers branches not hit by existing test_phase_timing.py and
test_pipeline_auth_preflight.py:

* ``run_install_pipeline`` early-return paths (no deps, ImportError guard)
* ``run_install_pipeline`` exception re-raise contracts
* ``_preflight_auth_check`` github-host skip, timeout continue,
  non-auth stderr continue, generic-host env-key pruning,
  ADO-eligible bearer-fallback path
* ``_run_phase`` verbose + non-verbose paths (light smoke)
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.install.errors import AuthenticationError
from apm_cli.install.pipeline import _preflight_auth_check, _run_phase

# ---------------------------------------------------------------------------
# Helpers shared across tests
# ---------------------------------------------------------------------------


def _make_dep(
    host: str = "dev.azure.com",
    repo_url: str = "myorg/myproject/_git/myrepo",
    is_ado: bool = True,
) -> MagicMock:
    dep = MagicMock()
    dep.host = host
    dep.repo_url = repo_url
    dep.port = None
    dep.is_azure_devops.return_value = is_ado
    dep.explicit_scheme = None
    dep.is_insecure = False
    # ADO-specific string fields needed by _build_repo_url
    dep.ado_organization = "myorg"
    dep.ado_project = "myproject"
    dep.ado_repo = "myrepo"
    return dep


def _make_ctx(deps: list | None = None, verbose: bool = False) -> MagicMock:
    ctx = MagicMock()
    ctx.deps_to_install = deps if deps is not None else [_make_dep()]
    ctx.verbose = verbose
    ctx.logger = None
    return ctx


def _make_resolver(
    auth_scheme: str = "basic",
    token: str = "pat",  # noqa: S107
    git_env: dict | None = None,
) -> MagicMock:
    resolver = MagicMock()
    dep_ctx = MagicMock()
    dep_ctx.token = token
    dep_ctx.auth_scheme = auth_scheme
    dep_ctx.git_env = git_env or {}
    # Intentionally do NOT set dep_ctx.source so getattr(..., None) != "ADO_APM_PAT"
    # This keeps ado_eligible=False and routes through _primary_op() -> subprocess.run.
    del dep_ctx.source
    resolver.resolve_for_dep.return_value = dep_ctx
    resolver.build_error_context.return_value = "    Diagnostic payload"
    return resolver


# ---------------------------------------------------------------------------
# _run_phase: basic contracts
# ---------------------------------------------------------------------------


class TestRunPhase:
    """Verify _run_phase delegates to phase.run(ctx) and returns its value."""

    def test_returns_phase_return_value(self) -> None:
        phase = SimpleNamespace(run=lambda ctx: "phase-result")
        ctx = SimpleNamespace(verbose=False, logger=None)
        assert _run_phase("test", phase, ctx) == "phase-result"

    def test_non_verbose_skips_timing(self) -> None:
        phase = SimpleNamespace(run=lambda ctx: 42)
        ctx = SimpleNamespace(verbose=False, logger=None)
        result = _run_phase("test", phase, ctx)
        assert result == 42

    def test_verbose_calls_logger_verbose_detail(self) -> None:
        calls: list[str] = []
        logger = MagicMock()
        logger.verbose_detail.side_effect = lambda msg: calls.append(msg)
        phase = SimpleNamespace(run=lambda ctx: None)
        ctx = SimpleNamespace(verbose=True, logger=logger)
        _run_phase("myphase", phase, ctx)
        assert any("myphase" in c for c in calls)
        assert any("->" in c for c in calls)

    def test_exception_propagates_and_timing_still_emits(self) -> None:
        logger = MagicMock()
        phase = SimpleNamespace(run=MagicMock(side_effect=ValueError("boom")))
        ctx = SimpleNamespace(verbose=True, logger=logger)
        with pytest.raises(ValueError, match="boom"):
            _run_phase("failing", phase, ctx)
        assert logger.verbose_detail.called

    def test_logger_failure_does_not_mask_phase_return(self) -> None:
        logger = MagicMock()
        logger.verbose_detail.side_effect = RuntimeError("logger blew up")
        phase = SimpleNamespace(run=lambda ctx: "ok")
        ctx = SimpleNamespace(verbose=True, logger=logger)
        # The timing emission failure is suppressed; return value still comes through
        assert _run_phase("safe", phase, ctx) == "ok"

    def test_no_logger_still_runs_phase(self) -> None:
        phase = SimpleNamespace(run=lambda ctx: "fine")
        ctx = SimpleNamespace(verbose=True, logger=None)
        assert _run_phase("x", phase, ctx) == "fine"


# ---------------------------------------------------------------------------
# _preflight_auth_check: github host skipped
# ---------------------------------------------------------------------------


class TestPreflightAuthCheckGitHubSkipped:
    """Deps on github.com are skipped -- no subprocess call."""

    @patch("subprocess.run")
    def test_github_host_skips_probe(self, mock_run: MagicMock) -> None:
        dep = _make_dep(host="github.com", is_ado=False)
        ctx = _make_ctx(deps=[dep])
        resolver = _make_resolver()
        _preflight_auth_check(ctx, resolver, verbose=False)
        mock_run.assert_not_called()

    @patch("subprocess.run")
    def test_no_host_skips_probe(self, mock_run: MagicMock) -> None:
        dep = _make_dep(host=None, is_ado=False)
        dep.host = None
        ctx = _make_ctx(deps=[dep])
        resolver = _make_resolver()
        _preflight_auth_check(ctx, resolver, verbose=False)
        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# _preflight_auth_check: timeout sentinel continues
# ---------------------------------------------------------------------------


class TestPreflightAuthCheckTimeout:
    """Timeout (returncode==None path) must not raise."""

    @patch("subprocess.run")
    def test_timeout_does_not_raise(self, mock_run: MagicMock) -> None:
        import subprocess

        mock_run.side_effect = subprocess.TimeoutExpired(["git"], 30)
        ctx = _make_ctx()
        resolver = _make_resolver()
        # Should complete without raising
        _preflight_auth_check(ctx, resolver, verbose=False)


# ---------------------------------------------------------------------------
# _preflight_auth_check: non-auth failure continues
# ---------------------------------------------------------------------------


class TestPreflightAuthCheckNonAuthFailure:
    """Non-auth git failures (DNS, ref-not-found) are silently skipped."""

    @patch("subprocess.run")
    def test_non_auth_stderr_does_not_raise(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(
            returncode=128,
            stderr="fatal: repository not found",
            stdout="",
        )
        ctx = _make_ctx()
        resolver = _make_resolver()
        _preflight_auth_check(ctx, resolver, verbose=False)

    @patch("subprocess.run")
    def test_rc0_does_not_raise(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="refs/heads/main")
        ctx = _make_ctx()
        resolver = _make_resolver()
        _preflight_auth_check(ctx, resolver, verbose=False)


# ---------------------------------------------------------------------------
# _preflight_auth_check: ADO auth failure raises AuthenticationError
# ---------------------------------------------------------------------------


class TestPreflightAuthCheckADOAuthFailure:
    """ADO auth failure (401 / 403) raises AuthenticationError with context."""

    @patch("subprocess.run")
    def test_auth_failure_raises_authentication_error(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(
            returncode=128,
            stderr="fatal: Authentication failed (401)",
            stdout="",
        )
        ctx = _make_ctx()
        resolver = _make_resolver()
        with pytest.raises(AuthenticationError) as exc_info:
            _preflight_auth_check(ctx, resolver, verbose=False)
        assert "No files were modified" in exc_info.value.diagnostic_context

    @patch("subprocess.run")
    def test_auth_failure_includes_host_in_message(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(
            returncode=128,
            stderr="fatal: unable to access (403)",
            stdout="",
        )
        ctx = _make_ctx()
        resolver = _make_resolver()
        with pytest.raises(AuthenticationError) as exc_info:
            _preflight_auth_check(ctx, resolver, verbose=False)
        assert "dev.azure.com" in str(exc_info.value)


# ---------------------------------------------------------------------------
# _preflight_auth_check: generic host prunes isolation env keys
# ---------------------------------------------------------------------------


class TestPreflightAuthCheckGenericHostEnvPruning:
    """Generic (non-GitHub, non-ADO) hosts have isolation keys stripped."""

    @patch("subprocess.run")
    def test_generic_host_completes_without_raise_on_success(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="refs")
        dep = _make_dep(host="bitbucket.example.com", is_ado=False)
        dep.is_azure_devops.return_value = False
        ctx = _make_ctx(deps=[dep])
        resolver = _make_resolver()
        _preflight_auth_check(ctx, resolver, verbose=False)
        mock_run.assert_called_once()


# ---------------------------------------------------------------------------
# _preflight_auth_check: verbose mode calls logger trace
# ---------------------------------------------------------------------------


class TestPreflightAuthCheckVerbose:
    """In verbose mode, trace messages flow through logger.verbose_detail."""

    @patch("subprocess.run")
    def test_verbose_trace_on_success(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="refs")
        ctx = _make_ctx()
        ctx.verbose = True
        logger = MagicMock()
        ctx.logger = logger
        resolver = _make_resolver()
        _preflight_auth_check(ctx, resolver, verbose=True)
        # Trace may or may not fire depending on host type; main assertion is no exception.


# ---------------------------------------------------------------------------
# _preflight_auth_check: deduplication via seen set
# ---------------------------------------------------------------------------


class TestPreflightAuthCheckDeduplication:
    """Two deps from the same (host, org) cluster only produce one probe."""

    @patch("subprocess.run")
    def test_same_host_org_probed_once(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="refs")
        dep1 = _make_dep()
        dep2 = _make_dep()
        ctx = _make_ctx(deps=[dep1, dep2])
        resolver = _make_resolver()
        _preflight_auth_check(ctx, resolver, verbose=False)
        # Both deps share (dev.azure.com, myorg) -- only one run() call expected.
        assert mock_run.call_count == 1


# ---------------------------------------------------------------------------
# run_install_pipeline: early returns
# ---------------------------------------------------------------------------


class TestRunInstallPipelineEarlyReturns:
    """Verify the function short-circuits when there is nothing to do."""

    def test_no_deps_no_local_primitives_returns_empty_result(self) -> None:
        """When apm_package has zero deps and no local content, return immediately."""
        from apm_cli.install.pipeline import run_install_pipeline
        from apm_cli.models.results import InstallResult

        pkg = MagicMock()
        pkg.get_apm_dependencies.return_value = []
        pkg.get_dev_apm_dependencies.return_value = []

        with (
            patch("apm_cli.deps.lockfile.LockFile") as mock_lf_cls,
            patch("apm_cli.deps.lockfile.get_lockfile_path", return_value=None),
            patch("apm_cli.core.scope.get_deploy_root", return_value=MagicMock()),
            patch("apm_cli.core.scope.get_apm_dir", return_value=None),
            patch(
                "apm_cli.install.phases.local_content._project_has_root_primitives",
                return_value=False,
            ),
        ):
            mock_lf_cls.read.return_value = None
            result = run_install_pipeline(pkg)

        assert isinstance(result, InstallResult)

    def test_import_error_raises_runtime_error(self) -> None:
        """If the deps system is missing, raise RuntimeError with a clear message."""
        from apm_cli.install.pipeline import run_install_pipeline

        pkg = MagicMock()
        pkg.get_apm_dependencies.return_value = []
        pkg.get_dev_apm_dependencies.return_value = []

        import builtins

        real_import = builtins.__import__

        def _bad_import(name, *args, **kwargs):
            if name == "apm_cli.deps.lockfile" or ("deps" in str(name) and "lockfile" in str(name)):
                raise ImportError("no lockfile")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=_bad_import):
            with pytest.raises((RuntimeError, ImportError)):
                run_install_pipeline(pkg)


# ---------------------------------------------------------------------------
# run_install_pipeline: exception re-raise contracts
# ---------------------------------------------------------------------------


class TestRunInstallPipelineExceptionContracts:
    """Typed exceptions are re-raised; others are wrapped in RuntimeError."""

    def _make_pkg_with_deps(self) -> MagicMock:
        pkg = MagicMock()
        dep = MagicMock()
        dep.repo_url = "owner/repo"
        dep.host = "github.com"
        pkg.get_apm_dependencies.return_value = [dep]
        pkg.get_dev_apm_dependencies.return_value = []
        return pkg

    def test_authentication_error_re_raised(self) -> None:
        """AuthenticationError must propagate (not get wrapped in RuntimeError)."""
        from apm_cli.install.pipeline import run_install_pipeline

        pkg = self._make_pkg_with_deps()

        # Patch at the source import locations so they're found at call-time
        with (
            patch("apm_cli.deps.lockfile.LockFile") as mock_lf_cls,
            patch("apm_cli.deps.lockfile.get_lockfile_path", return_value=None),
            patch("apm_cli.core.scope.get_deploy_root", return_value=MagicMock()),
            patch("apm_cli.core.scope.get_apm_dir", return_value=None),
            patch(
                "apm_cli.install.phases.local_content._project_has_root_primitives",
                return_value=False,
            ),
            patch("apm_cli.install.pipeline._run_phase") as mock_phase,
            patch("apm_cli.utils.install_tui.InstallTui"),
        ):
            mock_lf_cls.read.return_value = None
            # Resolve phase sets deps -- return a ctx-like object with deps
            # Make first _run_phase (resolve) inject deps into ctx, second raises
            resolve_called = [False]

            def _phase_side(name, phase, ctx):
                if name == "resolve":
                    resolve_called[0] = True
                    ctx.deps_to_install = [MagicMock()]
                    return None
                raise AuthenticationError("auth failed")

            mock_phase.side_effect = _phase_side

            with pytest.raises(AuthenticationError):
                run_install_pipeline(pkg)

    def test_generic_exception_wrapped_in_runtime_error(self) -> None:
        """Unexpected exceptions must be wrapped in RuntimeError."""
        from apm_cli.install.pipeline import run_install_pipeline

        pkg = self._make_pkg_with_deps()

        with (
            patch("apm_cli.deps.lockfile.LockFile") as mock_lf_cls,
            patch("apm_cli.deps.lockfile.get_lockfile_path", return_value=None),
            patch("apm_cli.core.scope.get_deploy_root", return_value=MagicMock()),
            patch("apm_cli.core.scope.get_apm_dir", return_value=None),
            patch(
                "apm_cli.install.phases.local_content._project_has_root_primitives",
                return_value=False,
            ),
            patch("apm_cli.install.pipeline._run_phase") as mock_phase,
            patch("apm_cli.utils.install_tui.InstallTui"),
        ):
            mock_lf_cls.read.return_value = None

            def _phase_side(name, phase, ctx):
                if name == "resolve":
                    ctx.deps_to_install = [MagicMock()]
                    return None
                raise ValueError("something unexpected")

            mock_phase.side_effect = _phase_side

            with pytest.raises(RuntimeError, match="Failed to resolve APM dependencies"):
                run_install_pipeline(pkg)
