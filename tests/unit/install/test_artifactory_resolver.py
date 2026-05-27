"""Regression-trap tests for ``apm_cli.install.artifactory_resolver``.

This file extends the broader Artifactory-boundary coverage in
``tests/unit/test_artifactory_support.py`` with focused regression traps for
the security and audience-separation contracts that a refactor could
silently break:

* ``allow_redirects=False`` on every HEAD probe (token-leak guard against
  proxy-issued cross-host redirects).
* Mode 2 ``Authorization`` header is sourced from
  :class:`apm_cli.deps.registry_proxy.RegistryConfig` (the proxy bearer),
  NOT from :class:`apm_cli.core.auth.AuthResolver` (which would hand back
  the github.com PAT and leak it to the proxy host).
* 403 -- like 401 -- is classified as ``AUTH``; mixed 401/404 demotes the
  candidate set to ``MISSING`` so the user-facing error stays accurate.

These are paired with mutation-break gates in the originating shepherd
session: each test was confirmed to FAIL when the contract under test was
intentionally removed in the source.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, Mock, patch

import pytest

from apm_cli.install.artifactory_resolver import _resolve_artifactory_boundary
from apm_cli.models.apm_package import DependencyReference

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_head_recorder(status_code: int = 404):
    """Build a fake ``requests.head`` that records every call.

    Returns ``(recorder_list, fake_head)``.  Each entry in ``recorder_list``
    is the keyword-argument dict the resolver passed to ``requests.head``.
    """
    recorded: list[dict] = []

    def _head(url, headers=None, timeout=None, verify=None, allow_redirects=None):
        recorded.append(
            {
                "url": url,
                "headers": dict(headers or {}),
                "timeout": timeout,
                "verify": verify,
                "allow_redirects": allow_redirects,
            }
        )
        resp = MagicMock()
        resp.status_code = status_code
        return resp

    return recorded, _head


# ---------------------------------------------------------------------------
# allow_redirects=False mutation-break guard
# ---------------------------------------------------------------------------


class TestRedirectLeakGuard:
    """``allow_redirects=False`` must hold for every probe.

    Flipping it to ``True`` would let the proxy redirect the resolver to a
    different host while the Bearer token rides along -- a cross-host token
    leak.  This is the single highest-severity invariant in the module.
    """

    def test_every_probe_disables_redirects(self):
        """Probe every candidate of an ambiguous Mode 1 dep; assert each call
        was made with ``allow_redirects=False``."""
        package = "art.example.com/artifactory/apm/group/sub/repo"
        auth = Mock()
        auth.resolve_for_dep.return_value = Mock(token=None)

        recorded, fake_head = _make_head_recorder(status_code=404)
        with patch("apm_cli.install.artifactory_resolver.requests.head", side_effect=fake_head):
            with pytest.raises(ValueError):
                _resolve_artifactory_boundary(package, auth, verbose=False)

        # Sanity: the resolver actually probed something (otherwise the
        # assertion below would pass vacuously and the mutation-break gate
        # would be useless).
        assert recorded, "expected at least one HEAD probe"
        for call in recorded:
            assert call["allow_redirects"] is False, (
                "allow_redirects must be False on every probe to prevent "
                "cross-host Bearer-token leakage via proxy-issued redirects"
            )


# ---------------------------------------------------------------------------
# Mode 2 token-audience guard
# ---------------------------------------------------------------------------


class TestModeTwoTokenAudience:
    """Mode 2 (bare shorthand under ``PROXY_REGISTRY_ONLY``) must source the
    probe ``Authorization`` header from :class:`RegistryConfig.from_env`,
    NOT from :class:`AuthResolver` (which would return the github.com PAT
    and leak it to the proxy host).
    """

    def test_authorization_header_is_proxy_bearer_not_github_pat(self):
        """Both a github.com PAT (via AuthResolver) and a proxy bearer (via
        ``PROXY_REGISTRY_TOKEN``) are set.  The HEAD probe must carry the
        proxy bearer, never the PAT.
        """
        package = "group/sub/repo"
        with patch.dict(
            os.environ,
            {
                "PROXY_REGISTRY_URL": "https://art.example.com/artifactory/apm",
                "PROXY_REGISTRY_ONLY": "1",
                "PROXY_REGISTRY_TOKEN": "proxy-bearer-PROXY",
            },
            clear=True,
        ):
            dep = DependencyReference.parse(package)

            # Wire AuthResolver to hand back a github.com PAT.  If the
            # resolver wrongly consults it, this token will end up in the
            # outgoing Authorization header.
            auth = Mock()
            auth.resolve_for_dep.return_value = Mock(token="github-pat-LEAK")

            recorded, fake_head = _make_head_recorder(status_code=404)
            with patch(
                "apm_cli.install.artifactory_resolver.requests.head",
                side_effect=fake_head,
            ):
                with pytest.raises(ValueError):
                    _resolve_artifactory_boundary(package, auth, verbose=False, dep_ref=dep)

        assert recorded, "expected at least one HEAD probe"
        for call in recorded:
            auth_header = call["headers"].get("Authorization", "")
            assert "github-pat-LEAK" not in auth_header, (
                "Mode 2 must not carry the github.com PAT to the proxy host"
            )
            assert auth_header == "Bearer proxy-bearer-PROXY", (
                f"Mode 2 Authorization must come from RegistryConfig (got {auth_header!r})"
            )
        # And the github PAT path must never have been consulted.
        auth.resolve_for_dep.assert_not_called()


# ---------------------------------------------------------------------------
# 401 vs 403 vs other-4xx error discrimination
# ---------------------------------------------------------------------------


class TestErrorDiscrimination:
    """401/403 -> AUTH; any non-auth 4xx demotes the candidate set to MISSING."""

    def _run_with_status_map(self, status_for_url):
        package = "art.example.com/artifactory/apm/group/sub/repo"
        auth = Mock()
        auth.resolve_for_dep.return_value = Mock(token="t")

        def _head(url, headers=None, timeout=None, verify=None, allow_redirects=None):
            resp = MagicMock()
            resp.status_code = status_for_url(url)
            return resp

        with patch("apm_cli.install.artifactory_resolver.requests.head", side_effect=_head):
            with pytest.raises(ValueError) as excinfo:
                _resolve_artifactory_boundary(package, auth, verbose=False)
        return str(excinfo.value)

    def test_all_403_raises_auth_specific_error(self):
        """403 must classify as AUTH, same as 401."""
        msg = self._run_with_status_map(lambda _url: 403)
        assert "authentication problem" in msg

    def test_mixed_401_and_404_demotes_to_unresolved(self):
        """If any candidate returns a non-auth 4xx, the set is MISSING -- the
        error must be the unresolved-boundary one, not the auth-specific one.
        Otherwise a real 404 on one of several candidates would be misreported
        as an auth failure and the user would chase the wrong fix.
        """
        # First call (shallow candidate) returns 401, all others 404.
        state = {"calls": 0}

        def status_for(url: str) -> int:
            state["calls"] += 1
            return 401 if state["calls"] == 1 else 404

        msg = self._run_with_status_map(status_for)
        assert "did not resolve" in msg
        assert "authentication problem" not in msg

    def test_429_is_not_auth(self):
        """A non-auth status code (e.g. 429 Too Many Requests) returned on
        the HEAD probe must not be classified as AUTH; the response code
        only means ``MISSING`` because the resolver could not get an
        existence proof.  Guards against accidental broadening of the
        AUTH set beyond {401, 403}.
        """
        msg = self._run_with_status_map(lambda _url: 429)
        assert "did not resolve" in msg
        assert "authentication problem" not in msg


# ---------------------------------------------------------------------------
# INCONCLUSIVE: every URL shape raised a transport error -- fail closed
# ---------------------------------------------------------------------------


class TestInconclusiveFailsClosed:
    """Network failures (DNS / TLS / timeout) on every probe must raise a
    network-specific error, not silently mis-classify as MISSING.

    Without this guard, a transient network outage during an ambiguous-boundary
    install could lock the dependency onto the wrong owner/repo split, or
    surface a misleading "missing repo" error that sends the user chasing the
    wrong fix.  Convergent finding from devx-ux-expert and
    supply-chain-security-expert in the apm-review-panel pass.
    """

    def test_all_transport_errors_raise_network_specific_error(self):
        """``requests.RequestException`` from every URL shape -> ValueError
        mentioning network reachability, not the missing-repo phrasing.
        """
        import requests

        package = "art.example.com/artifactory/apm/group/sub/repo"
        auth = Mock()
        auth.resolve_for_dep.return_value = Mock(token="t")

        def _head(url, headers=None, timeout=None, verify=None, allow_redirects=None):
            raise requests.ConnectionError("name resolution failed")

        with patch("apm_cli.install.artifactory_resolver.requests.head", side_effect=_head):
            with pytest.raises(ValueError) as excinfo:
                _resolve_artifactory_boundary(package, auth, verbose=False)

        msg = str(excinfo.value)
        assert "could not reach the proxy" in msg
        assert "name resolution failed" in msg
        # Must NOT surface as a missing-repo error -- that would send the user
        # chasing the wrong fix when the real issue is the network.
        assert "did not resolve to a reachable repository archive" not in msg

    def test_partial_transport_failure_does_not_fail_closed(self):
        """If some URL shapes 404 but the network was reached at least once,
        the candidate classifies as MISSING (not INCONCLUSIVE) so the install
        keeps walking the candidate list rather than aborting early.
        """
        import requests

        package = "art.example.com/artifactory/apm/group/sub/repo"
        auth = Mock()
        auth.resolve_for_dep.return_value = Mock(token="t")

        state = {"calls": 0}

        def _head(url, headers=None, timeout=None, verify=None, allow_redirects=None):
            state["calls"] += 1
            # First call fails with transport error; subsequent calls return 404.
            if state["calls"] == 1:
                raise requests.ConnectionError("transient")
            resp = MagicMock()
            resp.status_code = 404
            return resp

        with patch("apm_cli.install.artifactory_resolver.requests.head", side_effect=_head):
            with pytest.raises(ValueError) as excinfo:
                _resolve_artifactory_boundary(package, auth, verbose=False)

        msg = str(excinfo.value)
        # Per-URL-shape transport errors are tolerated as long as at least one
        # URL shape per candidate produced an HTTP response.  Because every
        # candidate produced a 404 on at least one URL shape, the resolver
        # reports the unresolved-boundary error -- not the network error.
        assert "did not resolve" in msg
        assert "could not reach the proxy" not in msg
