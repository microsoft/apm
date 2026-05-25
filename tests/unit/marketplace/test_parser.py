"""Unit coverage for the URL-first marketplace source parser.

Covers:
- local absolute / relative / ``file://`` / ``~/`` paths
- Windows local-path cases (drive letter, .\\, ~\\)
- SCP-like ``git@host:org/repo.git`` SSH
- HTTPS to untrusted host classified as kind=git (was: rejected pre-PR)
- single-segment input -> ValueError
- existing GitHub/GitLab cases still pass
"""

from __future__ import annotations

from urllib.parse import urlsplit

import pytest

from apm_cli.commands.marketplace import _parse_marketplace_source


@pytest.mark.parametrize(
    "raw",
    [
        "/srv/marketplaces/agent-forge",
        "./relative/path",
        "../up/path",
        "~/code/marketplace",
        "~",
        "file:///srv/marketplaces/agent-forge.git",
    ],
)
def test_local_paths_classified_as_local(raw: str) -> None:
    url, kind, host = _parse_marketplace_source(raw, host_flag=None)
    assert kind == "local"
    assert url.startswith("file://")
    assert host is None


@pytest.mark.parametrize(
    "raw",
    [
        r"C:\repos\mkt",
        r"C:/repos/mkt",
        r".\local",
    ],
)
def test_windows_paths_classified_as_local(raw: str) -> None:
    url, kind, _host = _parse_marketplace_source(raw, host_flag=None)
    assert kind == "local"
    assert url.startswith("file://") or url.startswith("file:") or url.startswith(("./", "../"))


def test_scp_ssh_url_classified_as_git() -> None:
    url, kind, host = _parse_marketplace_source(
        "git@gitea.example.com:org/repo.git", host_flag=None
    )
    assert kind == "git"
    # SCP-style remains as-is (no scheme); assert exact form to avoid
    # arbitrary-substring matches CodeQL flags as URL-sanitization weakness.
    assert url == "git@gitea.example.com:org/repo.git"
    assert host == "gitea.example.com"


def test_https_untrusted_host_classified_as_git() -> None:
    """Previously rejected: HTTPS to a host APM doesn't classify as github/gitlab now flows through."""
    url, kind, host = _parse_marketplace_source(
        "https://gitea.example.com/org/repo.git", host_flag=None
    )
    assert kind == "git"
    assert url == "https://gitea.example.com/org/repo.git"
    assert host == "gitea.example.com"


def test_https_github_classified_as_github() -> None:
    url, kind, _host = _parse_marketplace_source("https://github.com/owner/repo", host_flag=None)
    assert kind == "github"
    parsed = urlsplit(url)
    assert parsed.hostname == "github.com"
    assert parsed.path == "/owner/repo"


def test_owner_repo_shorthand_classified_as_github_by_default() -> None:
    url, kind, _host = _parse_marketplace_source("owner/repo", host_flag=None)
    assert kind == "github"
    parsed = urlsplit(url)
    assert parsed.hostname == "github.com"
    assert parsed.path.rstrip("/") == "/owner/repo"


def test_host_owner_repo_shorthand_uses_host_flag() -> None:
    url, kind, _host = _parse_marketplace_source("ghe.contoso.com/team/repo", host_flag=None)
    # GHES classification depends on env; the key invariant is that the host is preserved.
    parsed = urlsplit(url)
    assert parsed.hostname == "ghe.contoso.com"
    assert parsed.path.rstrip("/") == "/team/repo"
    assert kind in ("github", "git")


def test_single_segment_input_rejected() -> None:
    with pytest.raises(ValueError):
        _parse_marketplace_source("repo-with-no-slash", host_flag=None)


def test_explicit_host_flag_combined_with_owner_repo() -> None:
    url, _kind, _host = _parse_marketplace_source("owner/repo", host_flag="ghes.example.com")
    parsed = urlsplit(url)
    assert parsed.hostname == "ghes.example.com"
    assert parsed.path.rstrip("/") == "/owner/repo"


def test_https_ado_url_classified_as_git() -> None:
    """ADO is no longer rejected at the parser layer."""
    url, kind, host = _parse_marketplace_source(
        "https://dev.azure.com/contoso/eng/_git/agent-forge", host_flag=None
    )
    # ADO classification routes through "git" kind so subprocess-git fetcher handles it.
    assert kind == "git"
    # Use urlsplit().hostname for exact host match (CodeQL: avoid substring sanitization).
    assert urlsplit(url).hostname == "dev.azure.com"
    assert host == "dev.azure.com"
