"""Hermetic integration tests for issue #1013: credential-env contract.

These tests exercise the full validation code path with real
:class:`AuthResolver`, :class:`DependencyReference`, and
:class:`GitHubPackageDownloader` instances.  Only ``subprocess.run``
(the ``git ls-remote`` call) is mocked to avoid network dependency.

A true end-to-end test against a real private Bitbucket server is
infeasible in CI (no private instance available).  The tests verify the
**credential-env contract** instead: the env dict passed to
``git ls-remote`` must not contain config-isolation keys that block
credential helpers for HTTPS connections.

The parity test confirms the direct ``apm install`` validation path and
the manifest-driven ``_build_validation_attempts`` path produce
equivalent credential environments for the same generic HTTPS URL.
"""

from __future__ import annotations

import os
import subprocess
from unittest.mock import patch

import pytest

from apm_cli.core.auth import AuthResolver
from apm_cli.core.token_manager import GitHubTokenManager
from apm_cli.deps.github_downloader import GitHubPackageDownloader
from apm_cli.deps.github_downloader_validation import _build_validation_attempts
from apm_cli.install import validation
from apm_cli.models.apm_package import DependencyReference

pytestmark = [pytest.mark.integration]

# ── Shared constants ──────────────────────────────────────────────────

_HTTPS_URL = "https://bitbucket.example.internal/scm/team/repo.git"
_HTTP_URL = "http://bitbucket.example.internal/scm/team/repo.git"

# Env vars to scrub so the resolver finds no pre-existing tokens.
_TOKEN_VARS = ("GITHUB_APM_PAT", "GITHUB_TOKEN", "GH_TOKEN", "ADO_APM_PAT")


# ── Helpers ───────────────────────────────────────────────────────────


def _ok_completed_process(*args, **kwargs):
    """Fake ``subprocess.run`` result simulating a successful ``git ls-remote``."""
    return subprocess.CompletedProcess(
        args=args[0] if args else [],
        returncode=0,
        stdout="abc123\trefs/heads/main\n",
        stderr="",
    )


def _clean_env() -> dict[str, str]:
    """Return a copy of ``os.environ`` with token vars removed."""
    return {k: v for k, v in os.environ.items() if k not in _TOKEN_VARS}


def _make_resolver() -> AuthResolver:
    """Build a real ``AuthResolver`` (no mocks on the resolver itself)."""
    return AuthResolver()


# ── Context managers ──────────────────────────────────────────────────
# Reusable patches shared across multiple tests.

_NO_GIT_CRED = lambda: patch.object(  # noqa: E731
    GitHubTokenManager, "resolve_credential_from_git", return_value=None
)
_NO_GH_CLI = lambda: patch.object(  # noqa: E731
    GitHubTokenManager, "resolve_credential_from_gh_cli", return_value=None
)


# =====================================================================
# 1. Full-flow: HTTPS generic host
# =====================================================================


class TestHttpsGenericHostEnv:
    """Validate that the env for a generic HTTPS host allows credential
    helpers to operate (no config isolation, no GIT_ASKPASS)."""

    def test_https_env_allows_credential_helpers(self):
        """Direct ``apm install <https-url>`` must not block credential
        helpers for a generic HTTPS host."""
        captured_env: dict[str, str] = {}

        def _capture_run(*args, **kwargs):
            env = kwargs.get("env") or {}
            captured_env.update(env)
            return _ok_completed_process(*args, **kwargs)

        with (
            patch.dict(os.environ, _clean_env(), clear=True),
            _NO_GIT_CRED(),
            _NO_GH_CLI(),
            patch("subprocess.run", side_effect=_capture_run),
        ):
            resolver = _make_resolver()
            validation._validate_package_exists(
                _HTTPS_URL,
                verbose=False,
                auth_resolver=resolver,
            )

        # Credential helpers must be reachable.
        assert captured_env.get("GIT_CONFIG_GLOBAL") != "/dev/null", (
            "GIT_CONFIG_GLOBAL=/dev/null blocks credential helpers"
        )
        assert captured_env.get("GIT_CONFIG_NOSYSTEM") != "1", (
            "GIT_CONFIG_NOSYSTEM=1 blocks system credential helpers"
        )
        # Interactive prompts are still suppressed.
        assert captured_env.get("GIT_TERMINAL_PROMPT") == "0", (
            "GIT_TERMINAL_PROMPT must be 0 to prevent interactive prompts"
        )
        # GIT_ASKPASS must NOT be present (popped by noninteractive_env).
        assert "GIT_ASKPASS" not in captured_env, (
            "GIT_ASKPASS should be absent so credential helpers can work"
        )


# =====================================================================
# 2. Full-flow: HTTP insecure generic host
# =====================================================================


class TestHttpInsecureGenericHostEnv:
    """Validate that the env for a plain HTTP host enforces full config
    isolation and credential-helper suppression."""

    def test_http_env_enforces_config_isolation(self):
        """Direct ``apm install <http-url>`` must lock down credential
        helpers for insecure plaintext transport."""
        captured_env: dict[str, str] = {}

        def _capture_run(*args, **kwargs):
            env = kwargs.get("env") or {}
            captured_env.update(env)
            return _ok_completed_process(*args, **kwargs)

        with (
            patch.dict(os.environ, _clean_env(), clear=True),
            _NO_GIT_CRED(),
            _NO_GH_CLI(),
            patch("subprocess.run", side_effect=_capture_run),
        ):
            resolver = _make_resolver()
            validation._validate_package_exists(
                _HTTP_URL,
                verbose=False,
                auth_resolver=resolver,
            )

        # Config isolation must be active.
        assert captured_env.get("GIT_CONFIG_NOSYSTEM") == "1", (
            "GIT_CONFIG_NOSYSTEM must be 1 for insecure HTTP"
        )
        # GIT_ASKPASS must be set to suppress credential helpers.
        assert captured_env.get("GIT_ASKPASS") == "echo", (
            "GIT_ASKPASS must be 'echo' for insecure HTTP"
        )
        # credential.helper override via GIT_CONFIG_COUNT.
        assert captured_env.get("GIT_CONFIG_COUNT") == "1"
        assert captured_env.get("GIT_CONFIG_KEY_0") == "credential.helper"
        assert captured_env.get("GIT_CONFIG_VALUE_0") == ""


# =====================================================================
# 3. Parity: direct install vs manifest _build_validation_attempts
# =====================================================================


class TestCredentialEnvParity:
    """The env produced by the direct install validation path must match
    the env from ``_build_validation_attempts`` for the same generic
    HTTPS URL."""

    def test_direct_and_manifest_env_are_equivalent(self):
        """Both paths must produce an env that does NOT isolate config
        and does NOT suppress credential helpers for generic HTTPS."""
        # ── Path A: direct install validation ──
        captured_direct_env: dict[str, str] = {}

        def _capture_run(*args, **kwargs):
            env = kwargs.get("env") or {}
            captured_direct_env.update(env)
            return _ok_completed_process(*args, **kwargs)

        with (
            patch.dict(os.environ, _clean_env(), clear=True),
            _NO_GIT_CRED(),
            _NO_GH_CLI(),
            patch("subprocess.run", side_effect=_capture_run),
        ):
            resolver = _make_resolver()
            validation._validate_package_exists(
                _HTTPS_URL,
                verbose=False,
                auth_resolver=resolver,
            )

        # ── Path B: _build_validation_attempts ──
        with (
            patch.dict(os.environ, _clean_env(), clear=True),
            _NO_GIT_CRED(),
            _NO_GH_CLI(),
        ):
            resolver_b = _make_resolver()
            dep_ref = DependencyReference.parse(_HTTPS_URL)
            downloader = GitHubPackageDownloader(auth_resolver=resolver_b)
            if dep_ref.host:
                downloader.github_host = dep_ref.host

            attempts = _build_validation_attempts(downloader, dep_ref, log=lambda msg: None)

        # For a generic host with no token, only the "plain HTTPS w/
        # credential helper" attempt should be present.
        plain_attempts = [a for a in attempts if "credential helper" in a.label.lower()]
        assert len(plain_attempts) == 1, (
            f"Expected exactly one credential-helper attempt, got {len(plain_attempts)}: "
            f"{[a.label for a in attempts]}"
        )
        manifest_env = plain_attempts[0].env

        # ── Compare credential-relevant keys ──
        keys_of_interest = (
            "GIT_CONFIG_GLOBAL",
            "GIT_CONFIG_NOSYSTEM",
            "GIT_ASKPASS",
            "GIT_TERMINAL_PROMPT",
        )

        for key in keys_of_interest:
            direct_val = captured_direct_env.get(key)
            manifest_val = manifest_env.get(key)
            assert direct_val == manifest_val, (
                f"Env key {key} differs: direct={direct_val!r}, manifest={manifest_val!r}"
            )

        # Explicit contract assertions.
        assert captured_direct_env.get("GIT_CONFIG_GLOBAL") != "/dev/null"
        assert manifest_env.get("GIT_CONFIG_GLOBAL") != "/dev/null"
        assert captured_direct_env.get("GIT_CONFIG_NOSYSTEM") != "1"
        assert manifest_env.get("GIT_CONFIG_NOSYSTEM") != "1"
        assert "GIT_ASKPASS" not in captured_direct_env
        assert "GIT_ASKPASS" not in manifest_env
        assert captured_direct_env.get("GIT_TERMINAL_PROMPT") == "0"
        assert manifest_env.get("GIT_TERMINAL_PROMPT") == "0"


# =====================================================================
# 4. Credential helper simulation
# =====================================================================


class TestCredentialHelperSimulation:
    """Even when ``git credential fill`` returns a token for the host,
    the validation env for a generic host must still allow native
    credential helpers (no config isolation).

    For generic hosts ``_resolve_dep_token`` returns ``None`` regardless
    of what the ``AuthResolver`` discovers, because generic hosts rely
    on git's own credential helpers rather than APM-managed tokens.
    The subprocess env must therefore remain non-isolated so those
    helpers can inject credentials into ``git ls-remote``."""

    def test_credential_fill_does_not_trigger_config_isolation(self):
        captured_env: dict[str, str] = {}

        def _capture_run(*args, **kwargs):
            env = kwargs.get("env") or {}
            captured_env.update(env)
            return _ok_completed_process(*args, **kwargs)

        with (
            patch.dict(os.environ, _clean_env(), clear=True),
            patch.object(
                GitHubTokenManager,
                "resolve_credential_from_git",
                return_value="fake-credential-token-abc123",
            ),
            _NO_GH_CLI(),
            patch("subprocess.run", side_effect=_capture_run),
        ):
            resolver = _make_resolver()
            validation._validate_package_exists(
                _HTTPS_URL,
                verbose=False,
                auth_resolver=resolver,
            )

        # Even with a credential-fill token, the generic-host branch
        # does NOT use it directly; it delegates to native git helpers.
        # The env must therefore remain non-isolated.
        assert captured_env.get("GIT_CONFIG_GLOBAL") != "/dev/null", (
            "GIT_CONFIG_GLOBAL=/dev/null blocks credential helpers"
        )
        assert captured_env.get("GIT_CONFIG_NOSYSTEM") != "1", (
            "GIT_CONFIG_NOSYSTEM=1 blocks system credential helpers"
        )
        assert captured_env.get("GIT_TERMINAL_PROMPT") == "0", "GIT_TERMINAL_PROMPT must be 0"
        assert "GIT_ASKPASS" not in captured_env, (
            "GIT_ASKPASS should be absent so credential helpers can work"
        )
