"""Unit tests for the cross-protocol-fallback port warning (issue #786).

When the user opts into cross-protocol fallback via
``--allow-protocol-fallback`` / ``APM_ALLOW_PROTOCOL_FALLBACK=1`` AND a
dependency carries a custom port AND the plan attempts both SSH and
HTTPS, APM emits a ``[!]`` warning before the first clone attempt. The
same port is reused across schemes, which is incorrect for servers that
serve SSH and HTTPS on different ports (e.g. Bitbucket Datacenter: SSH
7999, HTTPS 7990). The warning names the offending dependency
(``{host}/{repo}``), names the planned initial + fallback schemes,
lists two remediations (pin the URL scheme, or drop
``--allow-protocol-fallback`` to fail fast), and links to the docs.
"""

import os
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch  # noqa: F401

import pytest  # noqa: F401
from git.exc import GitCommandError

from apm_cli.deps.github_downloader import GitHubPackageDownloader
from apm_cli.models.apm_package import DependencyReference


def _make_downloader():
    with (
        patch.dict(os.environ, {}, clear=True),
        patch(
            "apm_cli.core.token_manager.GitHubTokenManager.resolve_credential_from_git",
            return_value=None,
        ),
    ):
        return GitHubPackageDownloader()


def _run_clone_capture_warnings(dep, allow_fallback=False):
    """Run _clone_with_fallback and return every ``_rich_warning`` call.

    Returns a list of (message, symbol) tuples, one per call. Clones
    always fail so any chained attempts run.
    """
    dl = _make_downloader()
    dl.auth_resolver._cache.clear()
    if allow_fallback:
        dl._allow_fallback = True

    def _fake_clone(url, *a, **kw):
        raise GitCommandError("clone", "failed")

    captured = []

    def _capture(message, symbol=None):
        captured.append((message, symbol))

    with (
        patch.dict(os.environ, {}, clear=True),
        patch(
            "apm_cli.core.token_manager.GitHubTokenManager.resolve_credential_from_git",
            return_value=None,
        ),
        patch("apm_cli.deps.github_downloader.Repo") as MockRepo,
        patch("apm_cli.deps.github_downloader._rich_warning", side_effect=_capture),
    ):
        MockRepo.clone_from.side_effect = _fake_clone
        target = Path(tempfile.mkdtemp())
        try:
            dl._clone_with_fallback(dep.repo_url, target, dep_ref=dep)
        except (RuntimeError, GitCommandError):
            pass
        finally:
            import shutil

            shutil.rmtree(target, ignore_errors=True)
    return captured


class TestProtocolFallbackPortWarning:
    """Issue #786: warn once per dep when fallback reuses a custom port."""

    def _port_warnings(self, calls):
        return [m for m, _s in calls if "Custom port" in m]

    def test_warning_fires_on_ssh_url_with_port_when_fallback_allowed(self):
        """ssh:// URL with port + allow_fallback => plan has SSH and HTTPS =>
        exactly one warning naming the offender, both schemes, both
        remediations, and the docs URL."""
        dep = DependencyReference.parse("ssh://git@bitbucket.example.com:7999/project/repo.git")
        assert dep.port == 7999

        calls = _run_clone_capture_warnings(dep, allow_fallback=True)
        port_warnings = self._port_warnings(calls)
        assert len(port_warnings) == 1, (
            f"expected exactly one port warning, got {len(port_warnings)}: {port_warnings!r}"
        )
        msg = port_warnings[0]
        # Assertions anchor on the emitted format ("Custom port ... on
        # host/repo:" + "See: https://...") instead of bare host/URL
        # substrings, so the test both documents the wire format and avoids
        # CodeQL's "incomplete URL substring sanitization" false positive.
        assert "Custom port 7999 on bitbucket.example.com/project/repo:" in msg, (
            f"warning must name the offender in 'Custom port {{port}} on "
            f"{{host}}/{{repo}}:' form: {msg!r}"
        )
        assert "SSH" in msg and "HTTPS" in msg, f"warning must name both planned schemes: {msg!r}"
        assert "Pin the URL scheme" in msg, (
            f"warning must offer the 'pin the URL scheme' remediation: {msg!r}"
        )
        assert "--allow-protocol-fallback" in msg, (
            f"warning must offer the 'drop --allow-protocol-fallback' escape "
            f"hatch as an alternative: {msg!r}"
        )
        assert "See: https://microsoft.github.io/apm/" in msg, (
            f"warning must link to the public docs via the 'See: ' prefix: {msg!r}"
        )

    def test_warning_fires_on_https_url_with_port_when_fallback_allowed(self):
        """https:// URL with port + allow_fallback => plan has HTTPS and SSH =>
        warning fires, naming the offender and schemes."""
        dep = DependencyReference.parse("https://git.company.internal:8443/team/repo.git")
        assert dep.port == 8443

        calls = _run_clone_capture_warnings(dep, allow_fallback=True)
        port_warnings = self._port_warnings(calls)
        assert len(port_warnings) == 1, f"expected exactly one port warning, got: {port_warnings!r}"
        msg = port_warnings[0]
        assert "Custom port 8443 on git.company.internal/team/repo:" in msg, (
            f"warning must name the offender in 'Custom port {{port}} on "
            f"{{host}}/{{repo}}:' form: {msg!r}"
        )
        assert "SSH" in msg and "HTTPS" in msg, f"warning must name both planned schemes: {msg!r}"

    def test_warning_silent_in_strict_mode_even_with_custom_port(self):
        """Strict mode (default) only plans one attempt; the warning must not
        fire even when a custom port is set. This is the whole point of
        strict-by-default (#778)."""
        dep = DependencyReference.parse("ssh://git@bitbucket.example.com:7999/project/repo.git")
        assert dep.port == 7999

        calls = _run_clone_capture_warnings(dep, allow_fallback=False)
        port_warnings = self._port_warnings(calls)
        assert port_warnings == [], (
            f"strict mode must not emit the port warning, got: {port_warnings!r}"
        )

    def test_warning_silent_when_no_port_set(self):
        """Shorthand with no port => no warning, even under allow_fallback."""
        dep = DependencyReference.parse("owner/repo")
        assert dep.port is None

        calls = _run_clone_capture_warnings(dep, allow_fallback=True)
        port_warnings = self._port_warnings(calls)
        assert port_warnings == [], (
            f"no-port deps must not emit the port warning, got: {port_warnings!r}"
        )

    def test_warning_fires_once_not_per_attempt(self):
        """Regression guard: the warning must be emitted once per dep (before
        the attempt loop), not per clone attempt in the plan."""
        dep = DependencyReference.parse("ssh://git@bitbucket.example.com:7999/project/repo.git")
        calls = _run_clone_capture_warnings(dep, allow_fallback=True)
        port_warnings = self._port_warnings(calls)
        assert len(port_warnings) == 1, (
            f"warning must fire exactly once per dep, got {len(port_warnings)}"
        )

    def test_warning_dedup_across_multiple_clone_calls_for_same_dep(self):
        """Regression guard: a single install run can call
        ``_clone_with_fallback`` multiple times for the same dep (ref-
        resolution clone, then the actual dep clone). The warning must still
        fire only once per (host, repo, port) across all those calls."""
        dep = DependencyReference.parse("ssh://git@bitbucket.example.com:7999/project/repo.git")
        dl = _make_downloader()
        dl.auth_resolver._cache.clear()
        dl._allow_fallback = True

        def _fake_clone(url, *a, **kw):
            raise GitCommandError("clone", "failed")

        captured = []

        def _capture(message, symbol=None):
            captured.append((message, symbol))

        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "apm_cli.core.token_manager.GitHubTokenManager.resolve_credential_from_git",
                return_value=None,
            ),
            patch("apm_cli.deps.github_downloader.Repo") as MockRepo,
            patch("apm_cli.deps.github_downloader._rich_warning", side_effect=_capture),
        ):
            MockRepo.clone_from.side_effect = _fake_clone
            for _ in range(3):
                target = Path(tempfile.mkdtemp())
                try:
                    dl._clone_with_fallback(dep.repo_url, target, dep_ref=dep)
                except (RuntimeError, GitCommandError):
                    pass
                finally:
                    import shutil

                    shutil.rmtree(target, ignore_errors=True)

        port_warnings = [m for m, _s in captured if "Custom port" in m]
        assert len(port_warnings) == 1, (
            f"warning must dedup across repeated _clone_with_fallback calls "
            f"for the same dep, got {len(port_warnings)}: {port_warnings!r}"
        )

    def test_warning_fires_again_for_different_dep(self):
        """Dedup must be per-dep, not global: a second dep with a different
        (host, repo, port) identity gets its own warning."""
        dep_a = DependencyReference.parse("ssh://git@bitbucket.example.com:7999/project/repo.git")
        dep_b = DependencyReference.parse("https://git.other.example:8443/team/repo.git")
        dl = _make_downloader()
        dl.auth_resolver._cache.clear()
        dl._allow_fallback = True

        def _fake_clone(url, *a, **kw):
            raise GitCommandError("clone", "failed")

        captured = []

        def _capture(message, symbol=None):
            captured.append((message, symbol))

        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "apm_cli.core.token_manager.GitHubTokenManager.resolve_credential_from_git",
                return_value=None,
            ),
            patch("apm_cli.deps.github_downloader.Repo") as MockRepo,
            patch("apm_cli.deps.github_downloader._rich_warning", side_effect=_capture),
        ):
            MockRepo.clone_from.side_effect = _fake_clone
            for dep in (dep_a, dep_a, dep_b, dep_b):
                target = Path(tempfile.mkdtemp())
                try:
                    dl._clone_with_fallback(dep.repo_url, target, dep_ref=dep)
                except (RuntimeError, GitCommandError):
                    pass
                finally:
                    import shutil

                    shutil.rmtree(target, ignore_errors=True)

        port_warnings = [m for m, _s in captured if "Custom port" in m]
        assert len(port_warnings) == 2, (
            f"expected one warning per distinct dep identity (2), "
            f"got {len(port_warnings)}: {port_warnings!r}"
        )
        assert any("7999" in m for m in port_warnings), port_warnings
        assert any("8443" in m for m in port_warnings), port_warnings

    def test_warning_dedup_normalises_hostname_casing(self):
        """Issue #800: DNS hostnames are case-insensitive per RFC 4343, so
        two deps that differ only in host casing denote the same identity.
        The dedup key must lowercase the host axis to match the
        ``AuthResolver._cache`` convention.

        Note: ``DependencyReference.parse`` already lowercases the host on
        URL-string inputs, so this test must build the deps via the
        dataclass constructor directly to reach the dedup key with casing
        preserved -- that is the code path the dedup-key fix defends."""
        dep_lower = DependencyReference(
            repo_url="project/repo",
            host="bitbucket.example.com",
            port=7999,
        )
        dep_mixed = DependencyReference(
            repo_url="project/repo",
            host="Bitbucket.Example.com",
            port=7999,
        )
        assert dep_mixed.host == "Bitbucket.Example.com", (
            "direct constructor must preserve host casing, else this "
            "test cannot reproduce the #800 regression"
        )
        dl = _make_downloader()
        dl.auth_resolver._cache.clear()
        dl._allow_fallback = True

        def _fake_clone(url, *a, **kw):
            raise GitCommandError("clone", "failed")

        captured = []

        def _capture(message, symbol=None):
            captured.append((message, symbol))

        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "apm_cli.core.token_manager.GitHubTokenManager.resolve_credential_from_git",
                return_value=None,
            ),
            patch("apm_cli.deps.github_downloader.Repo") as MockRepo,
            patch("apm_cli.deps.github_downloader._rich_warning", side_effect=_capture),
        ):
            MockRepo.clone_from.side_effect = _fake_clone
            for dep in (dep_lower, dep_mixed):
                target = Path(tempfile.mkdtemp())
                try:
                    dl._clone_with_fallback(dep.repo_url, target, dep_ref=dep)
                except (RuntimeError, GitCommandError):
                    pass
                finally:
                    import shutil

                    shutil.rmtree(target, ignore_errors=True)

        port_warnings = [m for m, _s in captured if "Custom port" in m]
        assert len(port_warnings) == 1, (
            f"case-differing hostnames must dedup to one warning, "
            f"got {len(port_warnings)}: {port_warnings!r}"
        )

    def test_host_normalisation_does_not_collapse_distinct_repos(self):
        """Guard against over-normalisation: even when host casing is
        folded, the dedup key must still discriminate on repo path, so
        two deps sharing host+port but differing on repo still warn
        twice. Built via the direct constructor for the same reason as
        ``test_warning_dedup_normalises_hostname_casing``."""
        dep_a = DependencyReference(
            repo_url="project/repo-a",
            host="bitbucket.example.com",
            port=7999,
        )
        dep_b = DependencyReference(
            repo_url="project/repo-b",
            host="Bitbucket.Example.com",
            port=7999,
        )
        dl = _make_downloader()
        dl.auth_resolver._cache.clear()
        dl._allow_fallback = True

        def _fake_clone(url, *a, **kw):
            raise GitCommandError("clone", "failed")

        captured = []

        def _capture(message, symbol=None):
            captured.append((message, symbol))

        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "apm_cli.core.token_manager.GitHubTokenManager.resolve_credential_from_git",
                return_value=None,
            ),
            patch("apm_cli.deps.github_downloader.Repo") as MockRepo,
            patch("apm_cli.deps.github_downloader._rich_warning", side_effect=_capture),
        ):
            MockRepo.clone_from.side_effect = _fake_clone
            for dep in (dep_a, dep_b):
                target = Path(tempfile.mkdtemp())
                try:
                    dl._clone_with_fallback(dep.repo_url, target, dep_ref=dep)
                except (RuntimeError, GitCommandError):
                    pass
                finally:
                    import shutil

                    shutil.rmtree(target, ignore_errors=True)

        port_warnings = [m for m, _s in captured if "Custom port" in m]
        assert len(port_warnings) == 2, (
            f"same host+port with distinct repos must still warn "
            f"separately, got {len(port_warnings)}: {port_warnings!r}"
        )
