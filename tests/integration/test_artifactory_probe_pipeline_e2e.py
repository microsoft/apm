"""Integration coverage for the Artifactory boundary-probe install pipeline.

End-to-end-ish trap for PR #1472. The unit suites in
``tests/unit/install/test_artifactory_resolver.py`` and
``tests/unit/test_artifactory_support.py`` already exercise the resolver in
isolation. This file traps the FULL install-side pipeline:

    resolve_parsed_dependency_reference
        -> _resolve_artifactory_boundary (HEAD probe, mocked)
            -> _rebuild_dep_ref (proxy-verified boundary)
                -> dependency_reference_to_yaml_entry
                    (structured git+path entry shape for apm.yml / lockfile)

The HTTP layer is the only thing mocked; everything else is real code on
the same call chain ``apm install`` would take. This is the regression
trap the apm-review-panel CEO asked for as a follow-up to #1472: a single
test that detects silent breakage anywhere in the probe-to-lockfile chain
(parse -> probe -> rebuild -> serialize) instead of only at the resolver
boundary.
"""

from __future__ import annotations

from unittest.mock import MagicMock, Mock, patch
from urllib.parse import urlparse

import pytest

from apm_cli.install.artifactory_resolver import _resolve_artifactory_boundary
from apm_cli.install.package_resolution import (
    dependency_reference_to_yaml_entry,
    resolve_parsed_dependency_reference,
)
from apm_cli.models.apm_package import DependencyReference


def _scripted_head(boundary_owner: str, boundary_repo: str):
    """Build a fake ``requests.head`` that returns 200 only for one boundary.

    Every other candidate URL gets a 404. The boundary is identified by the
    ``owner/repo`` slug appearing in the probed URL path, which is exactly
    how the resolver tells candidates apart.
    """

    def _head(url, headers=None, timeout=None, verify=None, allow_redirects=None):
        resp = MagicMock()
        marker = f"/{boundary_owner}/{boundary_repo}/"
        resp.status_code = 200 if marker in url else 404
        return resp

    return _head


class TestArtifactoryProbePipelineMode1:
    """Mode 1 (explicit FQDN) end-to-end: parse -> probe -> rebuild -> yaml entry.

    A nested-group GitLab path under the Artifactory proxy must:

    1. Parse to a shallow parse-time guess (per the new structural rule).
    2. Probe HEAD-walks candidate splits.
    3. Lock in the candidate whose archive responds 2xx.
    4. Surface as a structured ``git:`` + ``path:`` entry in apm.yml.
    """

    def test_nested_path_resolves_and_serializes_to_structured_yaml(self):
        """``group/sub/repo/skills/sec`` under an Artifactory key locks in
        the probed boundary AND emits a structured apm.yml entry whose
        ``git:`` URL embeds the proxy prefix and whose ``path:`` is the
        in-repo virtual sub-path."""
        package = "art.example.com/artifactory/apm/group/sub/repo/skills/sec"
        auth = Mock()
        auth.resolve_for_dep.return_value = Mock(token=None)

        scripted = _scripted_head(boundary_owner="sub", boundary_repo="repo")

        with patch("apm_cli.install.artifactory_resolver.requests.head", side_effect=scripted):
            dep_ref, direct_virtual_resolved = resolve_parsed_dependency_reference(
                package,
                marketplace_dep_ref=None,
                dependency_reference_cls=DependencyReference,
                try_resolve_gitlab_direct_shorthand=lambda *a, **k: None,
                auth_resolver=auth,
                verbose=False,
                resolve_artifactory_boundary=_resolve_artifactory_boundary,
            )

        # The probe rebuilt the dep_ref at the proxy-verified split, so the
        # pipeline marks it for structured-entry persistence.
        assert direct_virtual_resolved is True
        # Boundary lock-in: owner is always the first post-prefix segment
        # (group); repo grows shallow-first until a candidate responds. The
        # scripted probe answers for owner='group' + repo='sub/repo', so the
        # boundary lands there and the in-repo virtual path is 'skills/sec'.
        assert dep_ref.repo_url == "group/sub/repo"
        assert dep_ref.virtual_path == "skills/sec"

        # End-to-end serialization: the yaml entry the install pipeline will
        # write into apm.yml is the structured form (not bare shorthand).
        entry = dependency_reference_to_yaml_entry(dep_ref)
        assert "git" in entry
        assert "path" in entry
        # The git URL must keep the proxy host + the artifactory/<key> prefix
        # + the probed owner/repo so a future install reproduces the same
        # routing. Parse the URL into components and assert on each part by
        # exact match (per the repo's test convention -- URL assertions use
        # urllib.parse, never substring), so CodeQL's incomplete-URL-substring
        # heuristic stays quiet and the assertion can't be satisfied by a
        # spoofed lookalike in the host position.
        parsed = urlparse(entry["git"])
        assert parsed.hostname == "art.example.com"
        # Path is ordered: /artifactory/<key>/<probed owner>/<probed repo>...
        path_parts = [seg for seg in parsed.path.split("/") if seg]
        assert path_parts[:5] == ["artifactory", "apm", "group", "sub", "repo"]
        assert entry["path"] == "skills/sec"

    def test_unambiguous_path_skips_serialization_overhead(self):
        """A two-segment (unambiguous) FQDN dep returns ``direct_virtual_
        resolved=False``: the parse-time dep_ref is already definitive, so
        apm.yml stays in its existing shape."""
        package = "art.example.com/artifactory/apm/owner/repo"
        auth = Mock()
        auth.resolve_for_dep.return_value = Mock(token=None)

        # No HEAD calls are expected (single-candidate short-circuit), but
        # patch anyway so an accidental probe surfaces as a clear failure.
        called = []

        def _head(url, **kwargs):
            called.append(url)
            resp = MagicMock()
            resp.status_code = 200
            return resp

        with patch("apm_cli.install.artifactory_resolver.requests.head", side_effect=_head):
            dep_ref, direct_virtual_resolved = resolve_parsed_dependency_reference(
                package,
                marketplace_dep_ref=None,
                dependency_reference_cls=DependencyReference,
                try_resolve_gitlab_direct_shorthand=lambda *a, **k: None,
                auth_resolver=auth,
                verbose=False,
                resolve_artifactory_boundary=_resolve_artifactory_boundary,
            )

        assert direct_virtual_resolved is False
        assert dep_ref.repo_url == "owner/repo"
        assert called == [], "unambiguous Mode 1 dep should not HEAD-probe"


class TestArtifactoryProbePipelineMode2:
    """Mode 2 (bare shorthand under ``PROXY_REGISTRY_ONLY``) end-to-end."""

    def test_bare_shorthand_resolves_and_keeps_bare_shape(self, monkeypatch):
        """Bare nested shorthand under the proxy locks in the boundary BUT
        keeps the rebuilt ref bare (no host/artifactory_prefix) so identity
        and lockfile shape stay env-driven, not embedded in the entry."""
        monkeypatch.setenv("PROXY_REGISTRY_URL", "https://art.example.com/artifactory/apm")
        monkeypatch.setenv("PROXY_REGISTRY_ONLY", "1")

        package = "group/sub/repo/skills/sec"
        auth = Mock()
        auth.resolve_for_dep.return_value = Mock(token=None)

        scripted = _scripted_head(boundary_owner="sub", boundary_repo="repo")

        with patch("apm_cli.install.artifactory_resolver.requests.head", side_effect=scripted):
            dep_ref, direct_virtual_resolved = resolve_parsed_dependency_reference(
                package,
                marketplace_dep_ref=None,
                dependency_reference_cls=DependencyReference,
                try_resolve_gitlab_direct_shorthand=lambda *a, **k: None,
                auth_resolver=auth,
                verbose=False,
                resolve_artifactory_boundary=_resolve_artifactory_boundary,
            )

        assert direct_virtual_resolved is True
        assert dep_ref.repo_url == "group/sub/repo"
        assert dep_ref.virtual_path == "skills/sec"
        # Mode 2 ref stays bare shorthand: no artifactory_prefix embedded on
        # the rebuilt dep_ref. The proxy routing remains env-driven
        # (PROXY_REGISTRY_URL + PROXY_REGISTRY_ONLY) so the lockfile stays
        # portable across proxy URLs and host stays github.com (the dep's
        # logical home), not the proxy host.
        assert not getattr(dep_ref, "artifactory_prefix", None)
        assert dep_ref.host == "github.com"


class TestArtifactoryProbePipelineErrorSurfacing:
    """When the probe rejects every candidate, the pipeline raises a
    ValueError that names the boundary trouble in user-facing terms.

    install.py catches ValueError, routes it through the per-package
    invalid-outcomes ledger, and continues with the next package. This test
    exercises the resolver-raise path (and confirms the message wording is
    actionable, NOT silent-fallback-to-a-guess).
    """

    def test_all_404_raises_unresolved_with_bypass_hint(self):
        package = "art.example.com/artifactory/apm/group/sub/repo"
        auth = Mock()
        auth.resolve_for_dep.return_value = Mock(token=None)

        def _all_404(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 404
            return resp

        with patch("apm_cli.install.artifactory_resolver.requests.head", side_effect=_all_404):
            with pytest.raises(ValueError, match=r"//"):
                resolve_parsed_dependency_reference(
                    package,
                    marketplace_dep_ref=None,
                    dependency_reference_cls=DependencyReference,
                    try_resolve_gitlab_direct_shorthand=lambda *a, **k: None,
                    auth_resolver=auth,
                    verbose=False,
                    resolve_artifactory_boundary=_resolve_artifactory_boundary,
                )

    def test_transport_errors_raise_inconclusive_with_bypass_hint(self):
        """Every candidate raises a transport error (network unreachable /
        DNS / TLS): the resolver fails closed (does NOT silently lock in a
        guess) AND the error message names the ``//`` bypass marker so the
        user has an escape hatch when the proxy is unavailable."""
        import requests as _requests

        package = "art.example.com/artifactory/apm/group/sub/repo"
        auth = Mock()
        auth.resolve_for_dep.return_value = Mock(token=None)

        def _all_transport_err(url, **kwargs):
            raise _requests.ConnectionError("simulated DNS failure")

        with patch(
            "apm_cli.install.artifactory_resolver.requests.head",
            side_effect=_all_transport_err,
        ):
            with pytest.raises(ValueError, match=r"//") as excinfo:
                resolve_parsed_dependency_reference(
                    package,
                    marketplace_dep_ref=None,
                    dependency_reference_cls=DependencyReference,
                    try_resolve_gitlab_direct_shorthand=lambda *a, **k: None,
                    auth_resolver=auth,
                    verbose=False,
                    resolve_artifactory_boundary=_resolve_artifactory_boundary,
                )
        # The INCONCLUSIVE branch is the one this PR added the `//` hint to;
        # confirm we are on that branch (and not the UNRESOLVED branch which
        # had the hint pre-PR).
        msg = str(excinfo.value)
        assert "could not reach the proxy" in msg
