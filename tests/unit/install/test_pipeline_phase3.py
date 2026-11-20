"""Phase-3 unit tests for ``apm_cli.install.pipeline``.

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

import re
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.install.errors import AuthenticationError, FrozenInstallError
from apm_cli.install.pipeline import _preflight_auth_check

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
    dep.source_reference = dep
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

    def _single_attempt(_host, operation, **_kwargs):
        return operation(dep_ctx.token, dict(dep_ctx.git_env))

    resolver.try_with_fallback.side_effect = _single_attempt
    return resolver


# ---------------------------------------------------------------------------
# _run_phase: basic contracts
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _preflight_auth_check: github host skipped
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _preflight_auth_check: timeout sentinel continues
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _preflight_auth_check: non-auth failure continues
# ---------------------------------------------------------------------------


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
        assert re.search(r"\bdev\.azure\.com\b", str(exc_info.value))


# ---------------------------------------------------------------------------
# _preflight_auth_check: generic host prunes isolation env keys
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _preflight_auth_check: verbose mode calls logger trace
# ---------------------------------------------------------------------------


# Trace may or may not fire depending on host type; main assertion is no exception.


# ---------------------------------------------------------------------------
# _preflight_auth_check: deduplication via seen set
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# run_install_pipeline: early returns
# ---------------------------------------------------------------------------


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

    @pytest.mark.parametrize("error_type", [AuthenticationError, FrozenInstallError])
    def test_typed_install_error_re_raised(self, error_type: type[Exception]) -> None:
        """Typed install refusals propagate instead of becoming generic RuntimeError."""
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
                raise error_type("install refused")

            mock_phase.side_effect = _phase_side

            with pytest.raises(error_type, match="install refused"):
                run_install_pipeline(pkg, target="claude")
