"""Unit tests for --update auth pre-flight probe in pipeline.py (#1015)."""

import subprocess  # noqa: F401
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.install.errors import AuthenticationError


def _make_dep(host="dev.azure.com", repo_url="myorg/myproject/_git/myrepo"):
    dep = MagicMock()
    dep.host = host
    dep.repo_url = repo_url
    dep.port = None
    dep.is_azure_devops.return_value = True
    dep.explicit_scheme = None
    dep.is_insecure = False
    dep.ado_organization = "myorg"
    dep.ado_project = "myproject"
    dep.ado_repo = "myrepo"
    return dep


def _make_ctx(update_refs=True, deps=None):
    ctx = MagicMock()
    ctx.deps_to_install = deps or [_make_dep()]
    ctx.update_refs = update_refs
    return ctx


def _make_resolver(auth_scheme="basic", token="pat", git_env=None):  # noqa: S107
    resolver = MagicMock()
    dep_ctx = MagicMock()
    dep_ctx.token = token
    dep_ctx.auth_scheme = auth_scheme
    dep_ctx.git_env = git_env or {}
    resolver.resolve_for_dep.return_value = dep_ctx
    resolver.build_error_context.return_value = "    Diagnostic payload"
    return resolver


class TestUpdatePreflightRejectsBadAuth:
    """Pre-flight raises AuthenticationError when git ls-remote returns 401."""

    @patch("subprocess.run")
    def test_auth_failure_raises(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=128,
            stderr="fatal: Authentication failed (401)",
            stdout="",
        )
        from apm_cli.install.pipeline import _preflight_auth_check

        ctx = _make_ctx()
        resolver = _make_resolver()

        with pytest.raises(AuthenticationError) as exc_info:
            _preflight_auth_check(ctx, resolver, verbose=False)

        assert "No files were modified" in exc_info.value.diagnostic_context
        assert "apm.yml" in exc_info.value.diagnostic_context

    @patch("subprocess.run")
    def test_auth_failure_message_mentions_host(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=128,
            stderr="fatal: unable to access (403)",
            stdout="",
        )
        from apm_cli.install.pipeline import _preflight_auth_check

        ctx = _make_ctx()
        resolver = _make_resolver()

        with pytest.raises(AuthenticationError) as exc_info:
            _preflight_auth_check(ctx, resolver, verbose=False)

        # Bounded full-phrase assertion (see CodeQL note in test_validation_ado_bearer.py).
        assert str(exc_info.value) == "Authentication failed for dev.azure.com"


class TestUpdatePreflightPassesGoodAuth:
    """Pre-flight succeeds when git ls-remote returns rc=0."""

    @patch("subprocess.run")
    def test_good_auth_no_exception(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stderr="",
            stdout="abc123\trefs/heads/main\n",
        )
        from apm_cli.install.pipeline import _preflight_auth_check

        ctx = _make_ctx()
        resolver = _make_resolver()

        # Should not raise
        _preflight_auth_check(ctx, resolver, verbose=False)


class TestPreflightSkippedForGitHubDeps:
    """github.com deps are skipped (they use the API probe with unauth fallback)."""

    @patch("subprocess.run")
    def test_github_deps_skipped(self, mock_run):
        dep = _make_dep(host="github.com", repo_url="owner/repo")
        ctx = _make_ctx(deps=[dep])
        resolver = _make_resolver()

        from apm_cli.install.pipeline import _preflight_auth_check

        _preflight_auth_check(ctx, resolver, verbose=False)
        mock_run.assert_not_called()


class TestPreflightClustersDeduplicate:
    """Multiple deps on the same (host, org) only probe once."""

    @patch("subprocess.run")
    def test_deduplication(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

        dep1 = _make_dep(host="dev.azure.com", repo_url="myorg/projA/_git/repoA")
        dep2 = _make_dep(host="dev.azure.com", repo_url="myorg/projB/_git/repoB")
        ctx = _make_ctx(deps=[dep1, dep2])
        resolver = _make_resolver()

        from apm_cli.install.pipeline import _preflight_auth_check

        _preflight_auth_check(ctx, resolver, verbose=False)
        assert mock_run.call_count == 1


def _make_generic_dep(host="gitlab.internal.corp", repo_url="org/repo"):
    """Create a mock dep for a generic (non-GitHub, non-ADO) host."""
    dep = MagicMock()
    dep.host = host
    dep.repo_url = repo_url
    dep.port = None
    dep.is_azure_devops.return_value = False
    dep.explicit_scheme = None
    dep.is_insecure = False
    return dep


class TestPreflightGenericHostAllowsCredentialHelpers:
    """Generic hosts (GHES, GitLab, etc.) must not block credential helpers (#1082)."""

    @patch("subprocess.run")
    def test_generic_host_env_omits_credential_blocking_vars(self, mock_run):
        """The probe env for generic hosts must not contain any of the vars
        that block credential helpers: GIT_CONFIG_GLOBAL, GIT_CONFIG_NOSYSTEM,
        or GIT_ASKPASS.

        These vars prevent git from reading ~/.gitconfig (where credential
        helpers are configured), which is the primary auth mechanism for
        non-GitHub/non-ADO hosts.
        """
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

        dep = _make_generic_dep(host="ghes.corp.example.com")
        ctx = _make_ctx(deps=[dep])
        resolver = _make_resolver(token="some-token")

        from apm_cli.install.pipeline import _preflight_auth_check

        _preflight_auth_check(ctx, resolver, verbose=False)

        assert mock_run.call_count == 1
        call_env = mock_run.call_args[1]["env"]
        assert "GIT_CONFIG_GLOBAL" not in call_env
        assert "GIT_CONFIG_NOSYSTEM" not in call_env
        assert "GIT_ASKPASS" not in call_env

    @patch("subprocess.run")
    def test_generic_host_auth_failure_still_raises(self, mock_run):
        """Auth failures on generic hosts still raise AuthenticationError."""
        mock_run.return_value = MagicMock(
            returncode=128,
            stderr="fatal: Authentication failed for 'https://ghes.corp.example.com/'",
            stdout="",
        )

        dep = _make_generic_dep(host="ghes.corp.example.com")
        ctx = _make_ctx(deps=[dep])
        resolver = _make_resolver(token="some-token")

        from apm_cli.install.pipeline import _preflight_auth_check

        with pytest.raises(AuthenticationError) as exc_info:
            _preflight_auth_check(ctx, resolver, verbose=False)

        assert str(exc_info.value).startswith("Authentication failed for ghes.corp.example.com")

    @patch("subprocess.run")
    def test_ado_host_omits_credential_blocking_env(self, mock_run):
        """ADO hosts strip GIT_CONFIG_GLOBAL / GIT_CONFIG_NOSYSTEM / GIT_ASKPASS
        from the preflight probe env so Git Credential Manager can answer
        for Entra-cached ADO creds when bearer acquisition is unavailable
        (microsoft/apm#1430 -- Windows az.cmd resolution failure, sandboxed
        runs, proxy issues, etc.).

        The actual clone path remains isolated -- it builds its own clean
        env via GitAuthEnvBuilder.setup_environment(). This carve-out only
        widens the preflight PROBE leg so GCM gets a chance to authenticate
        before we surface a misleading "az not logged in" diagnostic.
        """
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

        dep = _make_dep(host="dev.azure.com", repo_url="myorg/myproject/_git/myrepo")
        ctx = _make_ctx(deps=[dep])
        # Simulate ADO git_env that carries the blocking vars (as real AuthResolver does)
        resolver = _make_resolver(
            token="ado-pat",
            git_env={
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_ASKPASS": "echo",
                "GIT_CONFIG_GLOBAL": "/dev/null",
            },
        )

        from apm_cli.install.pipeline import _preflight_auth_check

        _preflight_auth_check(ctx, resolver, verbose=False)

        call_env = mock_run.call_args[1]["env"]
        # Layer 2 of #1430 fix: ADO preflight no longer kills GCM.
        assert "GIT_CONFIG_NOSYSTEM" not in call_env
        assert "GIT_ASKPASS" not in call_env
        assert "GIT_CONFIG_GLOBAL" not in call_env


# ---------------------------------------------------------------------------
# ADO PAT->AAD bearer fallback (#1212)
# ---------------------------------------------------------------------------


def _make_ado_resolver_with_bearer(
    *, primary_returncode, primary_stderr, bearer_returncode=0, bearer_stderr=""
):
    """Build a resolver wired to delegate via execute_with_bearer_fallback.

    Mirrors the real AuthResolver protocol used by every ADO call site:
    primary_op runs first; on auth-failure signature is_auth_failure is
    True so bearer_op is invoked.
    """
    resolver = MagicMock()
    dep_ctx = MagicMock()
    dep_ctx.token = "stale-pat"
    dep_ctx.auth_scheme = "basic"
    dep_ctx.source = "ADO_APM_PAT"
    dep_ctx.git_env = {"GIT_TOKEN": "stale-pat", "GIT_CONFIG_NOSYSTEM": "1"}
    resolver.resolve_for_dep.return_value = dep_ctx
    resolver.build_error_context.return_value = "    Diagnostic payload"

    # _build_git_env returns a CLEAN bearer env (no GIT_TOKEN).
    resolver._build_git_env.return_value = {
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": "AUTHORIZATION: Bearer dummy.jwt.token",
    }

    primary_result = MagicMock(
        returncode=primary_returncode,
        stderr=primary_stderr,
        stdout="",
    )
    bearer_result = MagicMock(
        returncode=bearer_returncode,
        stderr=bearer_stderr,
        stdout="",
    )

    def _exec(dep_ref, primary_op, bearer_op, is_auth_failure):
        # Real helper: run primary, then bearer if is_auth_failure(primary).
        from apm_cli.core.auth import BearerFallbackOutcome

        po = primary_op()
        if is_auth_failure(po):
            bo = bearer_op("dummy.jwt.token")
            if bo is not None and not is_auth_failure(bo):
                return BearerFallbackOutcome(bo, True)
            return BearerFallbackOutcome(bo if bo is not None else po, True)
        return BearerFallbackOutcome(po, False)

    resolver.execute_with_bearer_fallback.side_effect = _exec
    return resolver, primary_result, bearer_result


class TestAdoBearerFallback:
    """ADO PAT->AAD bearer fallback (#1212)."""

    @patch("subprocess.run")
    def test_stale_pat_then_bearer_succeeds(self, mock_run):
        """401 on PAT followed by 0 on bearer must NOT raise."""
        resolver, primary, bearer = _make_ado_resolver_with_bearer(
            primary_returncode=128,
            primary_stderr="fatal: Authentication failed (401)",
            bearer_returncode=0,
            bearer_stderr="",
        )
        # subprocess.run is invoked inside _primary_op and _bearer_op.
        mock_run.side_effect = [primary, bearer]

        from apm_cli.install.pipeline import _preflight_auth_check

        ctx = _make_ctx()
        _preflight_auth_check(ctx, resolver, verbose=False)

        assert resolver.execute_with_bearer_fallback.called
        # _build_git_env was used for bearer env (no leak of GIT_TOKEN).
        resolver._build_git_env.assert_called_with(
            "dummy.jwt.token", scheme="bearer", host_kind="ado"
        )

    @patch("subprocess.run")
    def test_pat_and_bearer_both_fail_raises_with_bearer_signal(self, mock_run):
        """Both PAT and bearer rejected -> AuthenticationError with bearer_also_failed=True."""
        resolver, primary, bearer = _make_ado_resolver_with_bearer(
            primary_returncode=128,
            primary_stderr="fatal: Authentication failed (401)",
            bearer_returncode=128,
            bearer_stderr="fatal: Authentication failed (403)",
        )
        mock_run.side_effect = [primary, bearer]

        from apm_cli.install.pipeline import _preflight_auth_check

        ctx = _make_ctx()
        with pytest.raises(AuthenticationError):
            _preflight_auth_check(ctx, resolver, verbose=False)

        # build_error_context invoked with bearer_also_failed=True so the
        # diagnostic surfaces "az cli bearer was also rejected".
        kwargs = resolver.build_error_context.call_args.kwargs
        assert kwargs.get("bearer_also_failed") is True

    @patch("subprocess.run")
    def test_bearer_env_does_not_leak_pat(self, mock_run):
        """The bearer attempt must NOT carry GIT_TOKEN (the stale PAT)."""
        resolver, primary, bearer = _make_ado_resolver_with_bearer(
            primary_returncode=128,
            primary_stderr="fatal: Authentication failed (401)",
            bearer_returncode=0,
            bearer_stderr="",
        )
        mock_run.side_effect = [primary, bearer]

        from apm_cli.install.pipeline import _preflight_auth_check

        ctx = _make_ctx()
        _preflight_auth_check(ctx, resolver, verbose=False)

        # Second subprocess.run call is the bearer attempt.
        bearer_env = mock_run.call_args_list[1][1]["env"]
        assert "GIT_TOKEN" not in bearer_env, (
            "Stale PAT leaked into bearer env -- _build_git_env was bypassed"
        )


# ---------------------------------------------------------------------------
# SSH preflight path (_use_ssh=True)
# ---------------------------------------------------------------------------


def _make_ssh_dep(host="git.corp.internal", repo_url="org/repo", explicit_scheme="ssh"):
    """Mock dep for a generic SSH-only host. Defaults to explicit ssh:// scheme.

    Note: as of the fix for #1293, ``_preflight_auth_check`` only routes through
    the SSH probe when ``dep.explicit_scheme == "ssh"`` -- mirroring
    :meth:`TransportSelector.select`. Tokenless shorthand deps now use the HTTPS
    probe so anonymous public Gitea/Forgejo deps keep working.
    """
    dep = MagicMock()
    dep.host = host
    dep.repo_url = repo_url
    dep.port = None
    dep.is_azure_devops.return_value = False
    dep.explicit_scheme = explicit_scheme
    dep.is_insecure = False
    dep.ssh_user = None
    return dep


def _make_ssh_resolver(token=None, git_env=None):
    """Resolver for the SSH probe path. Pair with an ``explicit_scheme="ssh"`` dep."""
    resolver = MagicMock()
    dep_ctx = MagicMock()
    dep_ctx.token = token
    dep_ctx.auth_scheme = "ssh"
    dep_ctx.git_env = git_env or {}
    resolver.resolve_for_dep.return_value = dep_ctx
    resolver.build_error_context.return_value = "    Diagnostic payload"
    return resolver


class TestSshPreflightAuthRejection:
    """_preflight_auth_check raises AuthenticationError on SSH auth rejection."""

    @patch("subprocess.run")
    def test_permission_denied_raises(self, mock_run):
        """'Permission denied (publickey)' is an SSH auth failure -> raises."""
        mock_run.return_value = MagicMock(
            returncode=128,
            stderr="git@git.corp.internal: Permission denied (publickey).",
            stdout="",
        )
        from apm_cli.install.pipeline import _preflight_auth_check

        ctx = _make_ctx(deps=[_make_ssh_dep()])
        resolver = _make_ssh_resolver()

        with pytest.raises(AuthenticationError) as exc_info:
            _preflight_auth_check(ctx, resolver, verbose=False)

        assert "SSH authentication failed" in str(exc_info.value)
        assert "ssh-agent" in exc_info.value.diagnostic_context
        assert "No files were modified" in exc_info.value.diagnostic_context

    @patch("subprocess.run")
    def test_no_more_auth_methods_raises(self, mock_run):
        """'no more authentication methods to try' is a hard SSH auth rejection."""
        mock_run.return_value = MagicMock(
            returncode=128,
            stderr="Permission denied (publickey).\nfatal: Could not read from remote repository.",
            stdout="",
        )
        from apm_cli.install.pipeline import _preflight_auth_check

        ctx = _make_ctx(deps=[_make_ssh_dep()])
        resolver = _make_ssh_resolver()

        with pytest.raises(AuthenticationError):
            _preflight_auth_check(ctx, resolver, verbose=False)


class TestSshPreflightConnectivityDeferred:
    """Connectivity errors on SSH path must NOT raise AuthenticationError."""

    @patch("subprocess.run")
    def test_connection_refused_defers(self, mock_run):
        """'connection refused' is a network error, not an auth failure -- must defer."""
        mock_run.return_value = MagicMock(
            returncode=255,
            stderr="ssh: connect to host git.corp.internal port 22: Connection refused",
            stdout="",
        )
        from apm_cli.install.pipeline import _preflight_auth_check

        ctx = _make_ctx(deps=[_make_ssh_dep()])
        resolver = _make_ssh_resolver()

        # Must NOT raise; defer to the real clone phase
        _preflight_auth_check(ctx, resolver, verbose=False)

    @patch("subprocess.run")
    def test_dns_failure_defers(self, mock_run):
        """'could not resolve hostname' is a DNS failure, not an auth failure -- must defer."""
        mock_run.return_value = MagicMock(
            returncode=255,
            stderr="ssh: Could not resolve hostname git.corp.internal: nodename nor servname provided",
            stdout="",
        )
        from apm_cli.install.pipeline import _preflight_auth_check

        ctx = _make_ctx(deps=[_make_ssh_dep()])
        resolver = _make_ssh_resolver()

        # Must NOT raise; defer to the real clone phase
        _preflight_auth_check(ctx, resolver, verbose=False)


class TestSshPreflightExplicitScheme:
    """Deps with explicit ssh:// scheme use SSH probe even when a token is present."""

    @patch("subprocess.run")
    def test_explicit_ssh_scheme_uses_ssh_path(self, mock_run):
        """ssh:// scheme forces _use_ssh=True regardless of token presence."""
        mock_run.return_value = MagicMock(
            returncode=128,
            stderr="git@git.corp.internal: Permission denied (publickey).",
            stdout="",
        )
        from apm_cli.install.pipeline import _preflight_auth_check

        dep = _make_ssh_dep()
        dep.explicit_scheme = "ssh"
        ctx = _make_ctx(deps=[dep])
        # Token is present, but explicit_scheme=ssh forces SSH path
        resolver = _make_ssh_resolver(token="some-token")

        with pytest.raises(AuthenticationError) as exc_info:
            _preflight_auth_check(ctx, resolver, verbose=False)

        assert "SSH authentication failed" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Heuristic alignment with TransportSelector (regression traps for #1293 fix)
#
# Pre-fix, the heuristic forced SSH on any tokenless generic host, which
# diverges from TransportSelector.select(): for shorthand (no explicit
# scheme) the selector defaults to plain HTTPS without a token. The traps
# below assert the corrected heuristic so any future regression of
# ``_use_ssh = is_generic and (... or not dep_ctx.token)`` is caught.
# ---------------------------------------------------------------------------


def _probe_url_from_call(mock_run):
    """Extract the URL passed to the first git ls-remote subprocess call."""
    args = mock_run.call_args[0][0]
    # ["git", "ls-remote", "--heads", "--exit-code", URL]
    return args[-1]


class TestPreflightTransportMirrorsSelector:
    """Probe transport must mirror what TransportSelector.select() would choose."""

    @patch("subprocess.run")
    def test_tokenless_generic_shorthand_probes_https(self, mock_run):
        """Tokenless public Gitea/Forgejo (no scheme, no token) -> plain HTTPS probe.

        Regression trap: pre-fix this took the SSH path, breaking anonymous
        access to public dependencies on generic Git hosts that have no
        configured SSH key. TransportSelector for shorthand without a token
        returns ``[_PLAIN_HTTPS]`` (transport_selection.py:315-316), so the
        preflight must do the same.
        """
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

        dep = _make_generic_dep(host="gitea.public.example", repo_url="org/repo")
        # Shorthand: no explicit scheme.
        assert dep.explicit_scheme is None
        ctx = _make_ctx(deps=[dep])
        # Anonymous: no token.
        resolver = _make_resolver(token=None)

        from apm_cli.install.pipeline import _preflight_auth_check

        _preflight_auth_check(ctx, resolver, verbose=False)

        probe_url = _probe_url_from_call(mock_run)
        assert probe_url.startswith("https://"), (
            f"Tokenless shorthand must probe HTTPS to mirror TransportSelector; got: {probe_url}"
        )
        assert not probe_url.startswith("ssh://"), (
            "Tokenless shorthand must NOT probe SSH -- would break anonymous public deps."
        )

    @patch("subprocess.run")
    def test_tokened_generic_shorthand_probes_https(self, mock_run):
        """Token-present generic shorthand -> authenticated HTTPS probe (baseline).

        Regression trap for any future change that flips the heuristic back
        to SSH when the URL has no explicit scheme. TransportSelector for
        shorthand with a token returns ``[_AUTH_HTTPS]`` -- the preflight
        must match.
        """
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

        dep = _make_generic_dep(host="gitea.corp.example", repo_url="org/repo")
        assert dep.explicit_scheme is None
        ctx = _make_ctx(deps=[dep])
        resolver = _make_resolver(token="some-token")

        from apm_cli.install.pipeline import _preflight_auth_check

        _preflight_auth_check(ctx, resolver, verbose=False)

        probe_url = _probe_url_from_call(mock_run)
        assert probe_url.startswith("https://"), (
            f"Tokened shorthand must probe HTTPS, not SSH; got: {probe_url}"
        )
