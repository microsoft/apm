"""Behavioral receipts for OpenAPM security requirements (req-sc-*).

Credential tests run in an isolated HOME with an empty global git config and
a fake GITHUB_APM_PAT; redirect tests use real loopback HTTP servers.
"""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import io
import json
import os
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from apm_cli.core.auth import AuthResolver, _redact_secrets
from apm_cli.core.token_manager import GitHubTokenManager
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.deps.registry.extractor import UnsafeTarballError, extract_tarball
from apm_cli.install.phases.integrate import _run_executable_approval_prompt
from apm_cli.models.dependency.reference import DependencyReference
from apm_cli.policy.policy_checks import (
    _check_required_executable_untrusted,
    _check_required_packages_deployed,
)
from apm_cli.policy.schema import DependencyPolicy, ExecutablesPolicy
from apm_cli.security import executables
from apm_cli.security.executables import ExecutableDeclaration
from apm_cli.utils.http_credentials import (
    CredentialScopedSession,
    RedirectRefusedError,
    origin_bounded_request,
)

_FAKE_PAT = "ghp_" + "Q" * 36


@pytest.fixture
def isolated_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith(("GIT_", "GCM_", "ADO_", "GITHUB_", "GH_", "GITLAB_", "APM_")):
            monkeypatch.delenv(name, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    git_config = tmp_path / "gitconfig"
    git_config.write_text("", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(git_config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    monkeypatch.setenv("GITHUB_APM_PAT", _FAKE_PAT)
    monkeypatch.setattr(
        GitHubTokenManager, "resolve_credential_from_gh_cli", lambda *_a, **_k: None
    )


# --- req-sc-003: credentials stay in their host class ------------------------


class _Recorder(BaseHTTPRequestHandler):
    location: str | None = None
    seen: list[dict[str, str]]

    def log_message(self, *_args) -> None:
        pass

    def do_GET(self) -> None:
        type(self).seen.append(dict(self.headers.items()))
        if type(self).location:
            self.send_response(302)
            self.send_header("Location", type(self).location)
        else:
            self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()


@contextlib.contextmanager
def _server(location: str | None = None):
    handler = type("_Handler", (_Recorder,), {"location": location, "seen": []})
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], handler.seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


_CREDENTIALS = {"Authorization": f"Bearer {_FAKE_PAT}", "PRIVATE-TOKEN": _FAKE_PAT}


@pytest.mark.req("req-sc-003")
def test_cross_host_redirect_drops_every_credential_header() -> None:
    """A 302 from 127.0.0.1 to localhost reaches the target with no credential."""
    with _server() as (target_port, target_seen):
        with _server(f"http://localhost:{target_port}/archive") as (origin_port, origin_seen):
            session = CredentialScopedSession()
            response = session.get(
                f"http://127.0.0.1:{origin_port}/archive", headers=_CREDENTIALS, timeout=10
            )

    assert response.status_code == 200
    assert origin_seen[0]["Authorization"] == f"Bearer {_FAKE_PAT}"
    assert len(target_seen) == 1
    assert {"Authorization", "PRIVATE-TOKEN"}.isdisjoint(target_seen[0])


@pytest.mark.req("req-sc-003")
def test_origin_bound_fetch_never_contacts_another_host() -> None:
    """The bounded fetch refuses the cross-host hop instead of sending anything there."""
    with _server() as (target_port, target_seen):
        with _server(f"http://localhost:{target_port}/archive") as (origin_port, _seen):
            base = f"http://127.0.0.1:{origin_port}"
            with pytest.raises(RedirectRefusedError) as refused:
                origin_bounded_request(
                    requests.Session(),
                    "GET",
                    f"{base}/archive",
                    base_url=base,
                    headers=_CREDENTIALS,
                    timeout=10,
                )

    assert refused.value.reason == "outside-origin"
    assert target_seen == []


@pytest.mark.req("req-sc-003")
@pytest.mark.usefixtures("isolated_credentials")
@pytest.mark.parametrize(
    "host", ["gitlab.com", "evil-github.com", "github.com.evil.example", "bitbucket.org"]
)
def test_github_credential_is_never_resolved_for_another_host(host: str) -> None:
    """The github.com token resolves for github.com only, named by its source."""
    resolver = AuthResolver()
    github = resolver.resolve("github.com")
    assert (github.token, github.source) == (_FAKE_PAT, "GITHUB_APM_PAT")

    other = resolver.resolve(host)
    assert other.token != _FAKE_PAT
    assert other.source != "GITHUB_APM_PAT"


# --- req-sc-010: interactive approvals stay out of apm.yml --------------------


@pytest.mark.req("req-sc-010")
def test_interactive_approval_is_persisted_per_user_never_in_apm_yml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    manifest = project / "apm.yml"
    manifest.write_text("name: app\nversion: 1.0.0\n", encoding="utf-8")
    before = manifest.read_bytes()
    user_config = tmp_path / "home" / ".apm" / "config.json"
    monkeypatch.setattr(executables, "_user_config_file", lambda: user_config)
    monkeypatch.setattr(executables, "_legacy_approvals_path", lambda: tmp_path / "legacy.yml")
    for name in ("APM_NON_INTERACTIVE", "CI"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("click.confirm", lambda *_a, **_k: True)
    monkeypatch.chdir(project)
    declaration = ExecutableDeclaration(
        package_key="acme/tools", package_name="tools", bin_count=1, hook_count=1
    )
    ctx = SimpleNamespace(blocked_executables=[declaration], policy_fetch=None, logger=None)

    _run_executable_approval_prompt(ctx)

    assert manifest.read_bytes() == before
    assert sorted(p.name for p in project.iterdir()) == ["apm.yml"]
    stored = json.loads(user_config.read_text(encoding="utf-8"))["executables"]["allow"]
    assert stored["acme/tools"] == {"hooks": True, "bin": True}
    assert not user_config.resolve().is_relative_to(project.resolve())


# --- req-sc-012: required presence comes from the lock, not deployment --------


@pytest.mark.req("req-sc-012")
def test_parked_required_package_satisfies_presence_and_is_reported_separately() -> None:
    deps = [DependencyReference.parse("acme/tools")]
    parked = LockedDependency(
        repo_url="acme/tools",
        host="github.com",
        resolved_commit="a" * 40,
        exec_status="gated_pending_approval",
    )
    lock = LockFile(dependencies={"acme/tools": parked})
    require = DependencyPolicy(require=("acme/tools",))

    presence = _check_required_packages_deployed(deps, lock, require)
    assert presence.passed, presence.message
    assert parked.deployed_files == []

    withheld = _check_required_executable_untrusted(
        deps, lock, ExecutablesPolicy(require=("acme/tools",))
    )
    assert not withheld.passed
    assert withheld.name != presence.name
    assert "acme/tools" in " ".join([withheld.message, *withheld.details])

    missing = _check_required_packages_deployed(deps, LockFile(), require)
    assert not missing.passed
    assert "acme/tools" in " ".join(missing.details)


# --- passing clauses of req-sc-002 / req-sc-007 (ids report their xfails) -----


def _tarball(*members: tuple[str, bytes | None, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name, link, data in members:
            info = tarfile.TarInfo(name)
            if link is not None:
                info.type = tarfile.SYMTYPE
                info.linkname = link.decode()
                tar.addfile(info)
            else:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    return gzip.compress(buffer.getvalue())


@pytest.mark.parametrize(
    "bad",
    [("../escape.txt", None, b"x"), ("/abs.txt", None, b"x"), ("link", b"/etc/passwd", b"")],
    ids=["dotdot", "absolute", "symlink"],
)
def test_unsafe_archive_entry_is_rejected(tmp_path: Path, bad: tuple) -> None:
    data = _tarball(bad)
    with pytest.raises(UnsafeTarballError):
        extract_tarball(data, hashlib.sha256(data).hexdigest(), tmp_path / "out")
    assert not (tmp_path / "escape.txt").exists()


def test_token_text_is_redacted_from_diagnostics() -> None:
    message = f"fatal: could not read from https://x-access-token:{_FAKE_PAT}@github.com/a/b"
    assert _FAKE_PAT not in _redact_secrets(message)
