"""Unit coverage for the URL-first marketplace source parser.

Covers:
- local absolute / relative / ``file://`` / ``~/`` paths
- Windows local-path cases (drive letter, .\\, ~\\)
- SCP-like ``git@host:org/repo.git`` and full ``ssh://`` URLs
- HTTPS to untrusted host classified as kind=git (was: rejected pre-PR)
- single-segment input -> ValueError
- existing GitHub/GitLab cases still pass
"""

from __future__ import annotations

from urllib.parse import urlsplit

import pytest

from apm_cli.commands.marketplace import _parse_marketplace_source
from apm_cli.marketplace.source_identity import parse_marketplace_source


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


def test_scp_ssh_url_rejects_query_instead_of_falling_back_to_shorthand() -> None:
    with pytest.raises(ValueError, match="SSH URLs cannot include queries"):
        _parse_marketplace_source(
            "git@gitea.example.com:org/repo.git?ref=main",
            host_flag=None,
        )


def test_direct_parser_rejects_invalid_host_flag() -> None:
    with pytest.raises(ValueError, match="Invalid host"):
        _parse_marketplace_source("owner/repo", host_flag="not a host")


@pytest.mark.parametrize(
    ("raw", "expected_port"),
    [
        ("ssh://git@gitea.example.com/org/repo.git", None),
        ("ssh://git@gitea.example.com:2222/org/repo.git", 2222),
    ],
)
def test_ssh_protocol_url_classified_as_git(raw: str, expected_port: int | None) -> None:
    url, kind, host = _parse_marketplace_source(raw, host_flag=None)

    parsed = urlsplit(url)
    assert kind == "git"
    assert url == raw
    assert parsed.scheme == "ssh"
    assert parsed.hostname == "gitea.example.com"
    assert parsed.port == expected_port
    assert parsed.path == "/org/repo.git"
    assert host == "gitea.example.com"


def test_ssh_protocol_url_normalizes_mixed_case_scheme() -> None:
    url, kind, host = _parse_marketplace_source(
        "SSH://git@gitea.example.com:2222/org/repo.git", host_flag=None
    )

    parsed = urlsplit(url)
    assert kind == "git"
    assert url == "ssh://git@gitea.example.com:2222/org/repo.git"
    assert parsed.scheme == "ssh"
    assert parsed.hostname == "gitea.example.com"
    assert parsed.port == 2222
    assert host == "gitea.example.com"


def test_ssh_protocol_url_on_known_host_uses_git_fetcher() -> None:
    url, kind, host = _parse_marketplace_source(
        "ssh://git@github.com:2222/org/repo.git", host_flag=None
    )

    assert kind == "git"
    assert url == "ssh://git@github.com:2222/org/repo.git"
    assert host == "github.com"


@pytest.mark.parametrize(
    "raw",
    [
        "ssh://git@[2001:db8::1]:2222/Team/Repo.git",
        "ssh://git@[2001:DB8::1]/Team/Repo.git",
    ],
)
def test_ssh_protocol_url_preserves_validated_transport_identity(raw: str) -> None:
    identity = parse_marketplace_source(raw)

    assert identity.url == raw
    assert identity.kind == "git"
    assert identity.host == "2001:db8::1"


@pytest.mark.parametrize(
    "raw",
    [
        "ssh://git@gitea.example.com/org/repo.git?ref=main",
        "ssh://git@gitea.example.com/org/repo.git#main",
        "git@gitea.example.com:org/repo.git#main",
        "ssh://git@gitea.example.com/org/%2frepo.git",
        "ssh://git@gitea.example.com/org/%5Crepo.git",
        "ssh://git@gitea.example.com/org/%zzrepo.git",
        "ssh://git@gitea.example.com/org/%00repo.git",
        "ssh://git%40user@gitea.example.com/org/repo.git",
        "ssh://git@gitea.example.com%2fevil/org/repo.git",
        "ssh://git@gitea.example.com%3a2222/org/repo.git",
    ],
)
def test_ssh_protocol_url_rejects_ambiguous_or_unsafe_components(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_marketplace_source(raw)


@pytest.mark.parametrize(
    "raw",
    [
        "ssh://git@/org/repo.git",
        "ssh://git@gitea.example.com",
        "ssh://git@gitea.example.com:not-a-port/org/repo.git",
        "ssh://git@gitea.example.com:0/org/repo.git",
        "ssh://git@gitea.example.com:65536/org/repo.git",
        "ssh://%2Doption@gitea.example.com/org/repo.git",
        "ssh://git@gitea.example.com/org/%2e%2e/repo.git",
        "ssh://git@gitea.example.com/org/%2e%2e%2frepo.git",
        "ssh://git@gitea.example.com/org/%252e%252e%252frepo.git",
    ],
)
def test_invalid_ssh_protocol_url_rejected(raw: str) -> None:
    with pytest.raises(ValueError):
        _parse_marketplace_source(raw, host_flag=None)


def test_password_bearing_ssh_protocol_url_rejected_without_echoing_secret() -> None:
    raw = "ssh://git:placeholder-password@gitea.example.com:2222/org/repo.git"

    with pytest.raises(ValueError) as exc_info:
        _parse_marketplace_source(raw, host_flag=None)

    assert "password" in str(exc_info.value).lower()
    assert "placeholder-password" not in str(exc_info.value)


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


def test_https_ado_url_classified_as_ado() -> None:
    """ADO stays on its HTTPS REST fetch path after source admission."""
    url, kind, host = _parse_marketplace_source(
        "https://dev.azure.com/contoso/eng/_git/agent-forge", host_flag=None
    )
    assert kind == "ado"
    # Use urlsplit().hostname for exact host match (CodeQL: avoid substring sanitization).
    assert urlsplit(url).hostname == "dev.azure.com"
    assert host == "dev.azure.com"


def test_https_ado_url_preserves_encoded_path_presentation() -> None:
    url, kind, host = _parse_marketplace_source(
        "https://dev.azure.com/contoso/My%20Projects/_git/agent-forge",
        host_flag=None,
    )

    parsed = urlsplit(url)
    assert kind == "ado"
    assert host == "dev.azure.com"
    assert parsed.path == "/contoso/My%20Projects/_git/agent-forge"


@pytest.mark.parametrize(
    "raw",
    [
        "https://dev.azure.com/contoso/%/_git/repo",
        "https://dev.azure.com/contoso/%FF/_git/repo",
        "https://dev.azure.com/contoso/%2F/_git/repo",
        "https://dev.azure.com/contoso/%252E%252E/_git/repo",
    ],
)
def test_https_ado_url_rejects_unsafe_encoded_path(raw: str) -> None:
    with pytest.raises(ValueError):
        _parse_marketplace_source(raw, host_flag=None)
