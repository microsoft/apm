"""Unit tests for ``apm_cli.deps.host_backends``.

Tests anchor the HostBackend Protocol contract for each concrete vendor
(GitHub, GHE Cloud, GHES, ADO, Generic). Each backend is tested in
isolation -- no orchestrator, no HTTP mocks, no AuthResolver token
resolution -- to keep the seam clean.
"""

from __future__ import annotations

import os
import sys
import types
from unittest.mock import patch
from urllib.parse import urlparse

import pytest

# Stub minimal apm_cli package shape so this file runs in the legacy
# top-level test layout (sibling tests like test_github_downloader.py do
# the same trick).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from apm_cli.core.auth import AuthResolver, HostInfo
from apm_cli.deps.host_backends import (
    ADOBackend,
    GenericGitBackend,
    GHECloudBackend,
    GHESBackend,
    GitHubBackend,
    HostBackend,
    backend_for,
    backend_for_host,
)

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _dep_ref(
    *,
    host: str = "github.com",
    repo_url: str = "owner/repo",
    port: int | None = None,
    is_insecure: bool = False,
    ado_organization: str | None = None,
    ado_project: str | None = None,
    ado_repo: str | None = None,
):
    """Build a minimal DependencyReference-like SimpleNamespace."""
    return types.SimpleNamespace(
        host=host,
        repo_url=repo_url,
        port=port,
        is_insecure=is_insecure,
        ado_organization=ado_organization,
        ado_project=ado_project,
        ado_repo=ado_repo,
        is_azure_devops=lambda: ado_organization is not None,
        is_artifactory=lambda: False,
    )


def _info(host: str, kind: str, port: int | None = None) -> HostInfo:
    """Build a HostInfo directly."""
    if kind == "github":
        api_base = "https://api.github.com"
    elif kind in {"ghe_cloud", "ghes"}:
        api_base = f"https://{host}/api/v3"
    elif kind == "ado":
        api_base = "https://dev.azure.com"
    else:
        api_base = f"https://{host}/api/v3"
    return HostInfo(
        host=host,
        kind=kind,
        has_public_repos=True,
        api_base=api_base,
        port=port,
    )


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "backend_cls,host,kind",
    [
        (GitHubBackend, "github.com", "github"),
        (GHECloudBackend, "octo.ghe.com", "ghe_cloud"),
        (GHESBackend, "git.example.com", "ghes"),
        (ADOBackend, "dev.azure.com", "ado"),
        (GenericGitBackend, "gitea.example.com", "generic"),
    ],
)
def test_all_backends_satisfy_host_backend_protocol(backend_cls, host, kind):
    backend = backend_cls(host_info=_info(host, kind))
    assert isinstance(backend, HostBackend)
    assert backend.kind == kind


@pytest.mark.parametrize(
    "backend_cls,host,kind,is_family,is_generic",
    [
        (GitHubBackend, "github.com", "github", True, False),
        (GHECloudBackend, "octo.ghe.com", "ghe_cloud", True, False),
        (GHESBackend, "git.example.com", "ghes", True, False),
        (ADOBackend, "dev.azure.com", "ado", False, False),
        (GenericGitBackend, "gitea.example.com", "generic", False, True),
    ],
)
def test_capability_flags(backend_cls, host, kind, is_family, is_generic):
    backend = backend_cls(host_info=_info(host, kind))
    assert backend.is_github_family is is_family
    assert backend.is_generic is is_generic


# ---------------------------------------------------------------------------
# GitHub family clone URLs
# ---------------------------------------------------------------------------


class TestGitHubFamilyCloneUrls:
    def test_https_no_token(self):
        backend = GitHubBackend(host_info=_info("github.com", "github"))
        url = backend.build_clone_https_url(_dep_ref(), token=None)
        # build_https_clone_url omits the ".git" suffix on the unauthenticated
        # path (preserved behavior from main).
        assert url == "https://github.com/owner/repo"

    def test_https_with_token(self):
        backend = GitHubBackend(host_info=_info("github.com", "github"))
        url = backend.build_clone_https_url(_dep_ref(), token="ghp_abc")
        # x-access-token is the GH Enterprise / GH Actions compatible form.
        assert url == "https://x-access-token:ghp_abc@github.com/owner/repo.git"

    def test_https_empty_token_suppresses_credential(self):
        backend = GitHubBackend(host_info=_info("github.com", "github"))
        url = backend.build_clone_https_url(_dep_ref(), token="")
        parsed = urlparse(url)
        assert parsed.username is None and parsed.password is None
        assert parsed.hostname == "github.com"

    def test_https_bearer_scheme_does_not_embed_token(self):
        backend = GitHubBackend(host_info=_info("github.com", "github"))
        # Bearer is ADO-only; GitHub family should fall through to plain URL.
        url = backend.build_clone_https_url(_dep_ref(), token="ghp_abc", auth_scheme="bearer")
        parsed = urlparse(url)
        assert "ghp_abc" not in url
        assert parsed.username is None and parsed.password is None

    def test_ssh_url(self):
        backend = GitHubBackend(host_info=_info("github.com", "github"))
        assert backend.build_clone_ssh_url(_dep_ref()) == "git@github.com:owner/repo.git"

    def test_ghes_https(self):
        backend = GHESBackend(host_info=_info("git.acme.com", "ghes"))
        url = backend.build_clone_https_url(_dep_ref(host="git.acme.com"), token="ghp_x")
        assert url == "https://x-access-token:ghp_x@git.acme.com/owner/repo.git"

    def test_ghe_cloud_https(self):
        backend = GHECloudBackend(host_info=_info("octo.ghe.com", "ghe_cloud"))
        url = backend.build_clone_https_url(_dep_ref(host="octo.ghe.com"), token=None)
        assert url == "https://octo.ghe.com/owner/repo"

    def test_https_with_custom_port(self):
        backend = GitHubBackend(host_info=_info("github.com", "github"))
        url = backend.build_clone_https_url(_dep_ref(port=8443), token=None)
        assert ":8443" in url

    def test_http_insecure(self):
        backend = GitHubBackend(host_info=_info("github.com", "github"))
        url = backend.build_clone_http_url(_dep_ref())
        assert url.startswith("http://")
        assert url.endswith(".git")


# ---------------------------------------------------------------------------
# GitHub family Contents/Commits API
# ---------------------------------------------------------------------------


class TestGitHubFamilyApiUrls:
    def test_commits_api_github(self):
        backend = GitHubBackend(host_info=_info("github.com", "github"))
        url = backend.build_commits_api_url(_dep_ref(), "main")
        assert url == "https://api.github.com/repos/owner/repo/commits/main"

    def test_commits_api_ghe_cloud(self):
        backend = GHECloudBackend(host_info=_info("octo.ghe.com", "ghe_cloud"))
        url = backend.build_commits_api_url(_dep_ref(host="octo.ghe.com"), "main")
        assert url == "https://octo.ghe.com/api/v3/repos/owner/repo/commits/main"

    def test_commits_api_ghes(self):
        backend = GHESBackend(host_info=_info("git.acme.com", "ghes"))
        url = backend.build_commits_api_url(_dep_ref(host="git.acme.com"), "v1.0")
        assert url == "https://git.acme.com/api/v3/repos/owner/repo/commits/v1.0"

    def test_commits_api_returns_none_for_resolved_sha(self):
        backend = GitHubBackend(host_info=_info("github.com", "github"))
        # A 40-char hex SHA needs no resolution.
        sha = "a" * 40
        assert backend.build_commits_api_url(_dep_ref(), sha) is None

    def test_commits_api_returns_none_for_malformed_repo(self):
        backend = GitHubBackend(host_info=_info("github.com", "github"))
        # repo_url without a "/" cannot be split into owner/repo.
        bad = _dep_ref(repo_url="malformed-no-slash")
        assert backend.build_commits_api_url(bad, "main") is None

    def test_contents_api_returns_single_url(self):
        backend = GitHubBackend(host_info=_info("github.com", "github"))
        urls = backend.build_contents_api_urls("owner", "repo", "README.md", "main")
        assert urls == ["https://api.github.com/repos/owner/repo/contents/README.md?ref=main"]


# ---------------------------------------------------------------------------
# ADO backend
# ---------------------------------------------------------------------------


class TestADOBackend:
    def _ado_dep(self, host: str = "dev.azure.com"):
        return _dep_ref(
            host=host,
            repo_url="myorg/myproj/myrepo",
            ado_organization="myorg",
            ado_project="myproj",
            ado_repo="myrepo",
        )

    def test_https_with_pat(self):
        backend = ADOBackend(host_info=_info("dev.azure.com", "ado"))
        url = backend.build_clone_https_url(self._ado_dep(), token="ado_pat_xyz")
        # ADO embeds the PAT as basic auth.
        assert "ado_pat_xyz" in url
        assert "myorg" in url
        assert "myproj" in url

    def test_https_bearer_scheme_drops_token(self):
        backend = ADOBackend(host_info=_info("dev.azure.com", "ado"))
        url = backend.build_clone_https_url(
            self._ado_dep(), token="bearer_jwt", auth_scheme="bearer"
        )
        # Bearer goes via env, NOT in URL.
        assert "bearer_jwt" not in url
        # Still a valid ADO URL.
        assert "myorg" in url

    def test_https_empty_token(self):
        backend = ADOBackend(host_info=_info("dev.azure.com", "ado"))
        url = backend.build_clone_https_url(self._ado_dep(), token="")
        assert "@" not in url

    def test_ssh_url(self):
        backend = ADOBackend(host_info=_info("dev.azure.com", "ado"))
        url = backend.build_clone_ssh_url(self._ado_dep())
        assert url.startswith("git@ssh.dev.azure.com:")

    def test_http_clone_rejected(self):
        backend = ADOBackend(host_info=_info("dev.azure.com", "ado"))
        with pytest.raises(ValueError, match="Azure DevOps does not support plain HTTP"):
            backend.build_clone_http_url(self._ado_dep())

    def test_https_missing_org_raises_value_error(self):
        backend = ADOBackend(host_info=_info("dev.azure.com", "ado"))
        bad_dep = _dep_ref(
            host="dev.azure.com",
            repo_url="myorg/myproj/myrepo",
            ado_organization=None,
            ado_project="myproj",
            ado_repo="myrepo",
        )
        with pytest.raises(ValueError, match=r"missing ado_organization"):
            backend.build_clone_https_url(bad_dep, token="x")

    def test_ssh_missing_org_raises_value_error(self):
        backend = ADOBackend(host_info=_info("dev.azure.com", "ado"))
        bad_dep = _dep_ref(
            host="dev.azure.com",
            repo_url="myorg/myproj/myrepo",
            ado_organization=None,
            ado_project="myproj",
            ado_repo="myrepo",
        )
        with pytest.raises(ValueError, match=r"missing ado_organization"):
            backend.build_clone_ssh_url(bad_dep)

    def test_commits_api_returns_none(self):
        backend = ADOBackend(host_info=_info("dev.azure.com", "ado"))
        assert backend.build_commits_api_url(self._ado_dep(), "main") is None

    def test_contents_api_returns_empty_list(self):
        backend = ADOBackend(host_info=_info("dev.azure.com", "ado"))
        # ADO has no Contents API -- empty list signals "use ADO REST Items API".
        assert backend.build_contents_api_urls("o", "r", "f", "main") == []


# ---------------------------------------------------------------------------
# Generic git backend (GitLab, Gitea, Gogs, Bitbucket)
# ---------------------------------------------------------------------------


class TestGenericGitBackend:
    def test_https_never_embeds_token(self):
        backend = GenericGitBackend(host_info=_info("gitea.example.com", "generic"))
        # Even when a token is passed, generic hosts defer to credential
        # helpers -- we never embed the token in the URL.
        url = backend.build_clone_https_url(_dep_ref(host="gitea.example.com"), token="some_token")
        parsed = urlparse(url)
        assert "some_token" not in url
        assert parsed.username is None and parsed.password is None
        assert parsed.hostname == "gitea.example.com"

    def test_ssh_url(self):
        backend = GenericGitBackend(host_info=_info("gitea.example.com", "generic"))
        url = backend.build_clone_ssh_url(_dep_ref(host="gitea.example.com"))
        assert url == "git@gitea.example.com:owner/repo.git"

    def test_ssh_url_with_port(self):
        backend = GenericGitBackend(host_info=_info("bitbucket.example.com", "generic", port=7999))
        url = backend.build_clone_ssh_url(_dep_ref(host="bitbucket.example.com", port=7999))
        # Custom SSH port should be threaded through the URL.
        assert "7999" in url

    def test_http_insecure(self):
        backend = GenericGitBackend(host_info=_info("gitea.example.com", "generic"))
        url = backend.build_clone_http_url(_dep_ref(host="gitea.example.com"))
        assert url == "http://gitea.example.com/owner/repo.git"

    def test_http_insecure_with_port(self):
        backend = GenericGitBackend(host_info=_info("gitea.example.com", "generic"))
        url = backend.build_clone_http_url(_dep_ref(host="gitea.example.com", port=8080))
        assert url == "http://gitea.example.com:8080/owner/repo.git"

    def test_commits_api_returns_none(self):
        backend = GenericGitBackend(host_info=_info("gitea.example.com", "generic"))
        assert backend.build_commits_api_url(_dep_ref(), "main") is None

    def test_contents_api_v1_then_v3(self):
        backend = GenericGitBackend(host_info=_info("gitea.example.com", "generic"))
        urls = backend.build_contents_api_urls("o", "r", "f.md", "main")
        assert len(urls) == 2
        # v1 first (Gitea standard), v3 fallback (legacy).
        assert "/api/v1/" in urls[0]
        assert "/api/v3/" in urls[1]
        assert urls[0].endswith("?ref=main")
        assert urls[1].endswith("?ref=main")


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


class TestBackendDispatch:
    def setup_method(self):
        self.resolver = AuthResolver()

    def test_dispatch_github_dot_com(self):
        backend = backend_for(_dep_ref(host="github.com"), self.resolver)
        assert isinstance(backend, GitHubBackend)
        assert backend.host_info.host == "github.com"

    def test_dispatch_ghe_cloud(self):
        backend = backend_for(_dep_ref(host="octo.ghe.com"), self.resolver)
        assert isinstance(backend, GHECloudBackend)

    def test_dispatch_ado(self):
        backend = backend_for(_dep_ref(host="dev.azure.com"), self.resolver)
        assert isinstance(backend, ADOBackend)

    def test_dispatch_generic(self):
        backend = backend_for(_dep_ref(host="gitlab.com"), self.resolver)
        assert isinstance(backend, GenericGitBackend)

    def test_dispatch_ghes_via_github_host_env(self):
        with patch.dict(os.environ, {"GITHUB_HOST": "git.acme.com"}):
            backend = backend_for(_dep_ref(host="git.acme.com"), self.resolver)
            assert isinstance(backend, GHESBackend)
            # Regression guard: GHES api_base must be `https://{host}/api/v3`,
            # never `https://api.{host}/...` (the latter was a real bug fixed
            # earlier in the same PR; see fallback path in backend_for).
            assert backend.host_info.api_base == "https://git.acme.com/api/v3"

    def test_dispatch_uses_default_when_no_host(self):
        backend = backend_for(_dep_ref(host=None), self.resolver)
        # Default host is github.com unless overridden.
        assert isinstance(backend, (GitHubBackend, GHESBackend))

    def test_dispatch_with_none_dep_ref(self):
        backend = backend_for(None, self.resolver)
        assert isinstance(backend, (GitHubBackend, GHESBackend))

    def test_dispatch_threads_port_into_host_info(self):
        backend = backend_for(_dep_ref(host="bitbucket.example.com", port=7999), self.resolver)
        assert backend.host_info.port == 7999

    def test_backend_for_host_variant(self):
        backend = backend_for_host("github.com", self.resolver)
        assert isinstance(backend, GitHubBackend)

    def test_backend_for_host_with_port(self):
        backend = backend_for_host("bitbucket.example.com", self.resolver, port=7999)
        assert backend.host_info.port == 7999
