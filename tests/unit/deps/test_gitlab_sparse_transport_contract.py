"""Hermetic protocol, authentication, and reuse contracts for issue #2938."""

from __future__ import annotations

import os
import socket
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

import pytest

from apm_cli.core.auth import AuthResolver
from apm_cli.deps.git_file_transport import (
    GitFileTransportError,
    GitFileTransportSecurityError,
)
from apm_cli.deps.github_downloader import GitHubPackageDownloader
from apm_cli.deps.transport_selection import ProtocolPreference
from apm_cli.models.apm_package import DependencyReference
from apm_cli.utils.path_security import PathTraversalError
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = pytest.mark.component

_TOKEN = "glpat-2938-dummy-secret"
_REPORTED_URL = "ssh://git@gitlab-ssh.example.com:2222/owner/repo.git"


def _dep(url: str = _REPORTED_URL) -> DependencyReference:
    """Parse the reported object form without replacing parser or URL builders."""
    return DependencyReference.parse_from_dict(
        {"git": url, "path": "agents/spec.agent.md", "type": "gitlab"}
    )


def _components(url: str) -> tuple[str, str | None, str | None, int | None, str]:
    """Compare SCP and URL forms using parsed components."""
    if "://" not in url:
        authority, path = url.split(":", 1)
        url = f"ssh://{authority}/{path}"
    parsed = urlparse(url)
    return parsed.scheme, parsed.username, parsed.hostname, parsed.port, parsed.path


@pytest.fixture
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Isolate credentials/config and reject network-capable subprocesses."""
    env = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=os.environ)
    real_run = subprocess.run

    def local_only(command: list[str], *args: object, **kwargs: object) -> object:
        if Path(command[0]).stem != "git" or not (
            "config" in command or ("ls-remote" in command and "--get-url" in command)
        ):
            raise AssertionError(f"Unexpected subprocess: {command}")
        return real_run(command, *args, **kwargs)

    with patch.dict(os.environ, env.subprocess_env(), clear=True):
        monkeypatch.setattr(tempfile, "tempdir", str(env.temp_root))
        monkeypatch.setenv("APM_GITLAB_HOSTS", "gitlab-ssh.example.com")
        monkeypatch.setattr(subprocess, "run", local_only)
        monkeypatch.setattr(
            socket, "create_connection", Mock(side_effect=AssertionError("network forbidden"))
        )
        monkeypatch.setattr(
            socket, "getaddrinfo", Mock(side_effect=AssertionError("DNS forbidden"))
        )
        monkeypatch.setattr(socket, "socket", Mock(side_effect=AssertionError("socket forbidden")))
        monkeypatch.setattr(
            "requests.sessions.Session.request",
            Mock(side_effect=AssertionError("HTTP forbidden")),
        )
        yield


@pytest.fixture
def downloader(isolated: None) -> Iterator[GitHubPackageDownloader]:
    """Use real policy/auth owners, but replace sparse materialization at its seam."""
    owner = GitHubPackageDownloader(
        auth_resolver=AuthResolver(allow_external_fallback=False),
        allow_fallback=False,
    )
    yield owner
    owner._strategies._git_file_transport_finalizer()


def _capture(owner: GitHubPackageDownloader, outcomes: list[object]) -> tuple[list, Mock]:
    """Capture each prepared remote/environment while supplying bounded outcomes."""
    attempts: list = []

    def factory(dep: DependencyReference, ref: str, **kwargs: object) -> Mock:
        remote = kwargs["build_repo_url_fn"](dep.repo_url, dep_ref=dep)
        attempts.append((remote, kwargs["git_env"], ref))
        outcome = outcomes[len(attempts) - 1]
        transport = Mock()
        transport.fetch_file.side_effect = outcome if isinstance(outcome, Exception) else None
        transport.fetch_file.return_value = outcome
        return transport

    owner._strategies._git_file_transport_factory = factory
    response = Mock(content=b"REST", status_code=200)
    api = Mock(return_value=response)
    owner._resilient_get = api
    return attempts, api


@pytest.mark.parametrize(
    ("url", "preference", "expected"),
    [
        (_REPORTED_URL, "https", ("ssh", "git", "gitlab-ssh.example.com", 2222, "/owner/repo.git")),
        (
            "git@gitlab.com:group/repo.git",
            "https",
            ("ssh", "git", "gitlab.com", None, "/group/repo.git"),
        ),
        (
            "ssh://git@gitlab.com/group/repo.git",
            "https",
            ("ssh", "git", "gitlab.com", None, "/group/repo.git"),
        ),
        (
            "ssh://alice@gitlab.com:2200/group/sub/repo.git",
            "https",
            ("ssh", "alice", "gitlab.com", 2200, "/group/sub/repo.git"),
        ),
        (
            "https://gitlab.com/group/repo.git",
            "ssh",
            ("https", None, "gitlab.com", None, "/group/repo.git"),
        ),
        (
            "http://gitlab.com/group/repo.git",
            "ssh",
            ("http", None, "gitlab.com", None, "/group/repo.git"),
        ),
        ("gitlab.com/group/repo", "ssh", ("ssh", "git", "gitlab.com", None, "/group/repo.git")),
        ("gitlab.com/group/repo", "https", ("https", None, "gitlab.com", None, "/group/repo.git")),
    ],
    ids=[
        "reported",
        "scp",
        "ssh-default",
        "custom-user",
        "https-explicit",
        "http-explicit",
        "ssh-preference",
        "https-preference",
    ],
)
def test_manifest_transport_components(
    downloader: GitHubPackageDownloader, url: str, preference: str, expected: tuple
) -> None:
    """P1/P2: manifest intent wins over the shorthand preference."""
    downloader._protocol_pref = ProtocolPreference.from_str(preference)
    downloader.auth_resolver = AuthResolver(allow_external_fallback=True)
    attempts, api = _capture(downloader, [b"Git"])
    with patch(
        "apm_cli.core.token_manager.GitHubTokenManager.resolve_credential_from_git",
        return_value=None,
    ) as credential_fill:
        assert downloader._download_github_file(_dep(url), "agents/spec.agent.md", "v1") == b"Git"
    assert _components(attempts[0][0]) == expected, "P1/P2 manifest remote fidelity"
    assert attempts[0][2] == "v1"
    if expected[0] in ("ssh", "http"):
        credential_fill.assert_not_called()
    api.assert_not_called()


@pytest.mark.parametrize("token", ["", _TOKEN], ids=["without-pat", "with-pat"])
def test_strict_ssh_failure_never_rest(
    downloader: GitHubPackageDownloader, monkeypatch: pytest.MonkeyPatch, token: str
) -> None:
    """P3/P7: PAT presence cannot authorize an SSH-to-REST downgrade."""
    monkeypatch.setenv("GITLAB_APM_PAT", token)
    failure = GitFileTransportError(
        f"SSH authentication rejected https://oauth2:{_TOKEN}@gitlab.com/owner/repo.git"
    )
    attempts, api = _capture(downloader, [failure])
    raised = None
    try:
        downloader._download_github_file(_dep(), "agents/spec.agent.md", "release/one")
    except RuntimeError as exc:
        raised = exc
    assert raised is not None, "P3 strict SSH must fail without REST"
    assert raised.__cause__ is failure
    assert "agents/spec.agent.md" in str(raised)
    assert "release/one" in str(raised)
    assert "SSH" in str(raised)
    assert _TOKEN not in str(raised), "P7 failure diagnostics must redact credentials"
    assert len(attempts) == 1
    assert _components(attempts[0][0])[0] == "ssh"
    api.assert_not_called()


@pytest.mark.parametrize("scheme", ["ssh", "http"])
def test_unmanaged_transports_strip_pat(
    downloader: GitHubPackageDownloader, monkeypatch: pytest.MonkeyPatch, scheme: str
) -> None:
    """P7: plaintext/SSH children inherit neither PATs nor managed auth headers."""
    monkeypatch.setenv("GITLAB_APM_PAT", _TOKEN)
    attempts, api = _capture(downloader, [b"Git"])
    dep = _dep(f"{scheme}://gitlab.com/group/repo.git")
    assert downloader._download_github_file(dep, "agents/spec.agent.md", "main") == b"Git"
    remote, env, _ = attempts[0]
    assert urlparse(remote).password is None
    assert _TOKEN not in repr((remote, env)), "P7 credentials must not enter SSH/HTTP attempts"
    assert not {"GIT_TOKEN", "GITLAB_APM_PAT", "GITLAB_TOKEN"} & env.keys(), (
        "P7 child token variables must be removed"
    )
    assert all(
        env.get(f"GIT_CONFIG_VALUE_{index}", "") == ""
        for index in range(int(env.get("GIT_CONFIG_COUNT", "0")))
        if "extraheader" in env.get(f"GIT_CONFIG_KEY_{index}", "").lower()
    )
    api.assert_not_called()


@pytest.mark.parametrize("successful_attempt", [0, 1, None])
def test_opt_in_fallback_order_port_and_warning(
    downloader: GitHubPackageDownloader, successful_attempt: int | None
) -> None:
    """P4: opt-in preserves port, short-circuits success, and warns once."""
    downloader._allow_fallback = True
    outcomes = [GitFileTransportError("unavailable"), GitFileTransportError("unavailable")]
    if successful_attempt is not None:
        outcomes[successful_attempt] = b"Git"
    attempts, api = _capture(downloader, outcomes)
    with patch("apm_cli.deps.download_strategies._rich_warning") as warning:
        result = downloader._download_github_file(_dep(), "agents/spec.agent.md", "main")
        assert result == (b"REST" if successful_attempt is None else b"Git")
        if successful_attempt == 0:
            assert downloader._download_github_file(_dep(), "other.md", "main") == b"Git"
    assert [_components(remote)[0] for remote, _, _ in attempts] == (
        ["ssh"] if successful_attempt == 0 else ["ssh", "https"]
    )
    assert {_components(remote)[2:4] for remote, _, _ in attempts} == {
        ("gitlab-ssh.example.com", 2222)
    }
    notices = [call.args[0] for call in warning.call_args_list]
    assert len([notice for notice in notices if notice.startswith("Custom port ")]) == 1
    assert len(notices) == (1 if successful_attempt == 0 else 2)
    assert api.call_count == (1 if successful_attempt is None else 0)


@pytest.mark.parametrize("scheme", ["ssh", "https"])
@pytest.mark.parametrize("allow_fallback", [False, True])
@pytest.mark.parametrize("first_fails", [False, True])
def test_protocol_switch_warning_matches_executed_attempts(
    downloader: GitHubPackageDownloader, scheme: str, allow_fallback: bool, first_fails: bool
) -> None:
    """Warn only after failure actually advances an opt-in cross-protocol attempt."""
    downloader._allow_fallback = allow_fallback
    first = GitFileTransportError("unavailable") if first_fails else b"Git"
    attempts, api = _capture(downloader, [first, b"Git"])
    dep = _dep(f"{scheme}://gitlab.com/group/repo.git")
    with patch("apm_cli.deps.download_strategies._rich_warning") as warning:
        if first_fails and not allow_fallback:
            if scheme == "ssh":
                with pytest.raises(RuntimeError, match="REST is not authorized"):
                    downloader._download_github_file(dep, "agents/spec.agent.md", "main")
            else:
                assert downloader._download_github_file(dep, "agents/spec.agent.md") == b"REST"
        else:
            assert downloader._download_github_file(dep, "agents/spec.agent.md") == b"Git"
    switched = first_fails and allow_fallback
    assert len(attempts) == (2 if switched else 1)
    messages = [call.args[0] for call in warning.call_args_list]
    labels = ("SSH", "plain HTTPS") if scheme == "ssh" else ("plain HTTPS", "SSH")
    expected = (
        [
            f"Protocol fallback: {labels[0]} GitLab sparse fetch of group/repo "
            f"failed; retrying with {labels[1]}."
        ]
        if switched
        else []
    )
    assert messages == expected, "P11 warn exactly when the executed protocol changes"
    assert api.call_count == int(first_fails and not allow_fallback and scheme == "https")


def test_https_rest_preserves_headers_endpoint_and_ref(
    downloader: GitHubPackageDownloader, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P5/P7: an admitted HTTPS failure retains scoped REST compatibility."""
    monkeypatch.setenv("GITLAB_APM_PAT", _TOKEN)
    attempts, api = _capture(downloader, [GitFileTransportError("unavailable")] * 2)
    dep = _dep("https://gitlab.com/group/sub/repo.git")
    assert downloader._download_github_file(dep, "agents/a b.md", "release/one") == b"REST"
    parsed = urlparse(api.call_args.args[0])
    assert (parsed.scheme, parsed.hostname, parsed.port) == ("https", "gitlab.com", None)
    assert (
        parsed.path == "/api/v4/projects/group%2Fsub%2Frepo/repository/files/agents%2Fa%20b.md/raw"
    )
    assert parse_qs(parsed.query) == {"ref": ["release/one"]}
    assert api.call_args.kwargs["headers"]["PRIVATE-TOKEN"] == _TOKEN
    assert urlparse(attempts[0][0]).username is None
    assert any(
        value.startswith("Authorization: Basic ")
        for key, value in attempts[0][1].items()
        if key.startswith("GIT_CONFIG_VALUE_")
    )


def test_http_failure_has_no_rest(downloader: GitHubPackageDownloader) -> None:
    """P5: explicit HTTP never silently upgrades to HTTPS REST."""
    attempts, api = _capture(downloader, [GitFileTransportError("unavailable")])
    with pytest.raises(RuntimeError):
        downloader._download_github_file(_dep("http://gitlab.com/g/r.git"), "x.md", "main")
    assert [_components(remote)[0] for remote, _, _ in attempts] == ["http"]
    api.assert_not_called()


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("programming defect"),
        OSError("local disk failure"),
        PathTraversalError("escape"),
        GitFileTransportSecurityError("unsafe ref"),
    ],
)
def test_non_transport_failures_are_terminal(
    downloader: GitHubPackageDownloader, failure: Exception
) -> None:
    """P6: only a typed Git failure may advance even an opted-in plan."""
    downloader._allow_fallback = True
    attempts, api = _capture(downloader, [failure, b"unauthorized retry"])
    raised = None
    try:
        downloader._download_github_file(_dep(), "x.md", "main")
    except Exception as exc:
        raised = exc
    assert raised is failure, "P6 non-transport failures must remain terminal"
    assert len(attempts) == 1, "P6 non-transport failures must not advance the Git plan"
    api.assert_not_called()


def test_cache_reuse_revalidates_policy(downloader: GitHubPackageDownloader) -> None:
    """P8: a previously successful checkout cannot bypass rewrite validation."""
    attempts, api = _capture(downloader, [b"Git"])
    assert downloader._download_github_file(_dep(), "first.md", "main") == b"Git"
    with patch(
        "apm_cli.deps.download_strategies.validate_git_url_rewrite_safety",
        side_effect=GitFileTransportSecurityError("unsafe rewrite"),
    ):
        with pytest.raises(GitFileTransportSecurityError, match="unsafe rewrite"):
            downloader._download_github_file(_dep(), "second.md", "main")
    assert len(attempts) == 1
    api.assert_not_called()


@pytest.mark.parametrize(
    "rewrite",
    ["ssh://mirror@gitlab.com:2222/owner/repo.git", "file:///local/repo.git"],
    ids=["ssh-mirror", "local-mirror"],
)
@pytest.mark.parametrize("token", ["", _TOKEN], ids=["without-pat", "with-pat"])
def test_effective_rewrite_never_authorizes_rest(
    downloader: GitHubPackageDownloader,
    monkeypatch: pytest.MonkeyPatch,
    rewrite: str,
    token: str,
) -> None:
    """P8: real insteadOf probing cannot turn nominal HTTPS into REST permission."""
    url = "https://gitlab.com/owner/repo.git"
    monkeypatch.setenv("GITLAB_APM_PAT", token)
    downloader.auth_resolver = AuthResolver(allow_external_fallback=True)
    subprocess.run(
        ["git", "config", "--global", f"url.{rewrite}.insteadOf", url],
        check=True,
        capture_output=True,
        timeout=10,
    )
    attempts, api = _capture(downloader, [GitFileTransportError("mirror unavailable")])
    with patch(
        "apm_cli.core.token_manager.GitHubTokenManager.resolve_credential_from_git",
        return_value=None,
    ) as credential_fill:
        with pytest.raises(RuntimeError, match="mirror unavailable"):
            downloader._download_github_file(_dep(url), "x.md", "main")
    credential_fill.assert_not_called()
    assert len(attempts) == 1
    assert _components(attempts[0][0]) == _components(url)
    assert _TOKEN not in repr(attempts[0][1])
    api.assert_not_called()


def test_rewrite_probe_error_is_terminal(downloader: GitHubPackageDownloader) -> None:
    """P6: a failed local rewrite probe must not authorize any transport."""
    downloader._allow_fallback = True
    attempts, api = _capture(downloader, [])
    with patch.object(
        downloader._transport_selector._resolver,
        "resolve",
        side_effect=RuntimeError("rewrite probe failed"),
    ):
        with pytest.raises(RuntimeError, match="rewrite probe failed"):
            downloader._download_github_file(_dep(), "x.md", "main")
    assert attempts == []
    api.assert_not_called()


@pytest.mark.parametrize(
    ("other_url", "other_ref", "other_effective", "other_auth"),
    [
        ("ssh://alice@gitlab.com/g/r.git", "main", None, "native"),
        ("https://gitlab.com/g/r.git", "main", None, "native"),
        ("ssh://git@gitlab.com:2222/g/r.git", "main", None, "native"),
        ("ssh://git@gitlab.com/g/r.git", "release", None, "native"),
        ("ssh://git@gitlab.com/g/r.git", "main", "file:///mirror/repo.git", "native"),
        ("ssh://git@gitlab.com/g/r.git", "main", None, "managed"),
    ],
    ids=["user", "protocol", "port", "ref", "effective-mirror", "auth-mode"],
)
def test_prepared_identity_separates_checkouts(
    downloader: GitHubPackageDownloader,
    other_url: str,
    other_ref: str,
    other_effective: str | None,
    other_auth: str,
) -> None:
    """P10: only identical prepared attempts share a live sparse transport."""
    attempts, _ = _capture(downloader, [b"one", b"two"])
    delegate = downloader._strategies
    url = "ssh://git@gitlab.com/g/r.git"
    common = dict(requested_url=url, effective_url=url, git_env={}, auth_mode="native")
    assert delegate._download_gitlab_file_via_git(_dep(url), "a", "main", **common) == b"one"
    assert delegate._download_gitlab_file_via_git(_dep(url), "b", "main", **common) == b"one"
    assert (
        delegate._download_gitlab_file_via_git(
            _dep(other_url),
            "c",
            other_ref,
            requested_url=other_url,
            effective_url=other_effective or other_url,
            git_env={},
            auth_mode=other_auth,
        )
        == b"two"
    ), "P10 distinct prepared identities must not reuse a checkout"
    assert len(attempts) == 2, "P10 distinct prepared identities need separate transports"
    assert len(delegate._git_file_transports) == 2


def test_failed_eviction_preserves_replacement(downloader: GitHubPackageDownloader) -> None:
    """P10: a delayed failed caller cannot evict a newer transport at its key."""
    delegate = downloader._strategies
    key = delegate._git_file_transport_key(_dep(), "main", _REPORTED_URL, _REPORTED_URL, "native")
    failed, replacement = Mock(), Mock()
    delegate._git_file_transports[key] = replacement
    delegate._discard_git_file_transport(key, failed)
    assert delegate._git_file_transports[key] is replacement
    failed.close.assert_called_once()
    replacement.close.assert_not_called()
