"""Pre-flight for SSH-registered github.com marketplace plugins (issue #3164).

Regression: a marketplace registered as ``ssh://git@github.com/org/repo.git``
resolves an in-repo plugin to a virtual subdirectory reference with
``explicit_scheme == "ssh"``. The pre-flight sent every GitHub-host virtual
package to the HTTPS downloader probes (raw, Contents API, anonymous HTTPS
``git ls-remote``), so a private marketplace failed validation without a
token even though the download step clones over SSH.

After the fix, the explicit SSH scheme routes the subdirectory to the
``git ls-remote`` clone-root probe, the same path non-GitHub hosts take
since #2063. HTTPS registrations keep the downloader probes.
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

import pytest

from apm_cli.install import validation
from apm_cli.install.errors import AuthenticationError
from apm_cli.marketplace.models import (
    MarketplaceManifest,
    MarketplacePlugin,
    MarketplaceSource,
)
from apm_cli.marketplace.resolver import resolve_marketplace_plugin
from apm_cli.models.apm_package import DependencyReference

SHA = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture(autouse=True)
def _no_fallback_env(monkeypatch):
    for name in ("APM_ALLOW_PROTOCOL_FALLBACK", "GITHUB_TOKEN", "GH_TOKEN", "GITHUB_APM_PAT"):
        monkeypatch.delenv(name, raising=False)


def _resolve(url: str):
    source = MarketplaceSource(name="mkt", url=url)
    manifest = MarketplaceManifest(
        name="mkt",
        plugins=(MarketplacePlugin(name="alpha", source="./plugins/alpha"),),
    )
    with (
        patch("apm_cli.marketplace.resolver.get_marketplace_by_name", return_value=source),
        patch("apm_cli.marketplace.resolver.fetch_or_cache", return_value=manifest),
    ):
        result = resolve_marketplace_plugin("alpha", "mkt")
    dep_ref = result.dependency_reference
    if dep_ref is None:
        dep_ref = DependencyReference.parse(result.canonical)
    dep_ref.reference = SHA
    return dep_ref


def _resolver_without_token():
    resolver = MagicMock()
    host_info = MagicMock(kind="github", display_name="github.com", host="github.com", port=None)
    resolver.classify_host.return_value = host_info
    ctx = MagicMock(source="none", token_type="unknown", token=None, auth_scheme="basic")
    ctx.git_env = {}
    resolver.resolve.return_value = ctx
    resolver.resolve_for_dep.return_value = ctx
    resolver.git_env_for_remote.return_value = {}
    resolver.build_error_context.return_value = "No token available."
    return resolver


def _run_result(returncode: int, stderr: str = ""):
    stdout = f"{SHA}\tHEAD\n" if returncode == 0 else ""
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _probe_urls(mock_run) -> list[str]:
    return [call.args[0][-1] for call in mock_run.call_args_list]


@pytest.mark.parametrize(
    "registered_url",
    [
        "ssh://git@github.com/org/private-mkt.git",
        "git@github.com:org/private-mkt.git",
    ],
    ids=["ssh_url", "scp_shorthand"],
)
def test_ssh_github_marketplace_subdir_probes_over_ssh_only(registered_url) -> None:
    dep_ref = _resolve(registered_url)
    assert dep_ref.host == "github.com"
    assert dep_ref.explicit_scheme == "ssh"
    assert dep_ref.is_virtual_subdirectory()

    with (
        patch.object(validation, "_validate_virtual_package", return_value=False) as https_probe,
        patch("subprocess.run", return_value=_run_result(0)) as mock_run,
    ):
        ok = validation._validate_package_exists(
            dep_ref.to_canonical(),
            auth_resolver=_resolver_without_token(),
            dep_ref=dep_ref,
        )

    https_probe.assert_not_called()
    assert ok is True
    urls = _probe_urls(mock_run)
    assert urls == ["git@github.com:org/private-mkt.git"], urls


def test_ssh_github_marketplace_subdir_ignores_available_token() -> None:
    """A reachable GitHub token must not add an HTTPS probe in strict SSH mode."""
    dep_ref = _resolve("ssh://git@github.com/org/private-mkt.git")
    resolver = _resolver_without_token()
    ctx = resolver.resolve_for_dep.return_value
    ctx.token = "ghp_example"
    ctx.source = "GITHUB_TOKEN"
    failure = _run_result(128, "fatal: Could not read from remote repository.")

    with (
        patch.object(validation, "_validate_virtual_package", return_value=True) as https_probe,
        patch("subprocess.run", return_value=failure) as mock_run,
    ):
        try:
            ok = validation._validate_package_exists(
                dep_ref.to_canonical(),
                auth_resolver=resolver,
                dep_ref=dep_ref,
            )
        except AuthenticationError:
            ok = False

    assert ok is False
    https_probe.assert_not_called()
    assert _probe_urls(mock_run) == ["git@github.com:org/private-mkt.git"]


def test_ssh_github_marketplace_subdir_auth_failure_still_fails() -> None:
    dep_ref = _resolve("ssh://git@github.com/org/private-mkt.git")
    failure = _run_result(128, "git@github.com: Permission denied (publickey).")

    with (
        patch.object(validation, "_validate_virtual_package", return_value=True) as https_probe,
        patch("subprocess.run", return_value=failure) as mock_run,
    ):
        try:
            ok = validation._validate_package_exists(
                dep_ref.to_canonical(),
                auth_resolver=_resolver_without_token(),
                dep_ref=dep_ref,
            )
        except AuthenticationError:
            ok = False

    assert ok is False
    https_probe.assert_not_called()
    urls = _probe_urls(mock_run)
    assert urls
    assert {urlparse(url).scheme or "scp" for url in urls} <= {"ssh", "scp"}, urls


def test_https_github_marketplace_subdir_keeps_downloader_probe() -> None:
    dep_ref = _resolve("https://github.com/org/public-mkt.git")
    assert dep_ref.explicit_scheme != "ssh"

    with (
        patch.object(validation, "_validate_virtual_package", return_value=True) as https_probe,
        patch.object(validation, "_validate_ado_git_package") as git_probe,
    ):
        ok = validation._validate_package_exists(
            dep_ref.to_canonical(),
            auth_resolver=_resolver_without_token(),
            dep_ref=dep_ref,
        )

    assert ok is True
    https_probe.assert_called_once()
    git_probe.assert_not_called()
