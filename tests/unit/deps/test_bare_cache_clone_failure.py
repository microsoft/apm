"""Regression tests for clone failure diagnostics."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from apm_cli.deps.bare_cache import build_clone_failure_message
from apm_cli.deps.clone_engine import CloneEngine
from apm_cli.deps.transport_selection import (
    ProtocolPreference,
    TransportAttempt,
    TransportPlan,
)
from apm_cli.models.apm_package import DependencyReference


def _clone_failure_message(
    *,
    stderr: bytes,
    attempt_scheme: str,
    command_scheme: str | None = None,
) -> str:
    """Build a clone failure message for an SSH diagnostic scenario."""
    command_scheme = command_scheme or attempt_scheme
    plan = TransportPlan(
        attempts=[
            TransportAttempt(
                scheme=attempt_scheme,
                use_token=attempt_scheme == "https",
                label=attempt_scheme.upper(),
            )
        ],
        strict=True,
    )
    auth_resolver = MagicMock()
    auth_resolver.build_error_context.return_value = ""
    last_error = subprocess.CalledProcessError(
        128,
        ["git", "clone", f"{command_scheme}://github.com/owner/repo"],
        stderr=stderr,
    )

    return build_clone_failure_message(
        repo_url_base="owner/repo",
        plan=plan,
        dep_ref=DependencyReference(
            repo_url="owner/repo",
            host="github.com",
            explicit_scheme=attempt_scheme,
        ),
        dep_host="github.com",
        is_ado=False,
        is_generic=False,
        has_ado_token=False,
        has_token=False,
        auth_resolver=auth_resolver,
        configured_github_host="github.com",
        default_host_fn=lambda: "github.com",
        last_error=last_error,
        last_attempt_scheme=attempt_scheme,
        sanitize_git_error=lambda value: value,
    )


def test_clone_failure_message_explains_passphrase_protected_ssh_key() -> None:
    """SSH passphrase failures must tell users how to unblock non-interactive clones."""
    message = _clone_failure_message(
        stderr=(
            b"Enter passphrase for key '/Users/alice/.ssh/id_ed25519':\n"
            b"Permission denied (publickey).\n"
        ),
        attempt_scheme="ssh",
    )

    assert "SSH key authentication failed" in message
    assert "load the key into ssh-agent" in message
    assert "ssh-add <key-file>" in message
    assert "start ssh-agent" in message
    assert "Verify that the key is available to SSH" in message
    assert "dedicated deploy key" in message
    assert "token-backed HTTPS" in message
    assert "does not open an interactive passphrase prompt" in message


def test_clone_failure_message_explains_explicit_ssh_publickey_failure() -> None:
    """Explicit SSH publickey denials should get the same non-interactive guidance."""
    message = _clone_failure_message(
        stderr=b"Permission denied (publickey).\n",
        attempt_scheme="ssh",
    )

    assert "SSH key authentication failed" in message
    assert "load the key into ssh-agent" in message
    assert "ssh-add <key-file>" in message
    assert "start ssh-agent" in message
    assert "token-backed HTTPS" in message


def test_clone_failure_message_does_not_echo_captured_ssh_stderr() -> None:
    """Classification input must not become user-visible output."""
    message = _clone_failure_message(
        stderr=(
            b"Enter passphrase for key '/Users/alice/.ssh/id_secret':\n"
            b"Permission denied (publickey).\n"
        ),
        attempt_scheme="ssh",
    )

    assert "SSH key authentication failed" in message
    assert "/Users/alice/.ssh/id_secret" not in message
    assert "Permission denied (publickey)" not in message


def test_clone_failure_message_omits_ssh_diagnostic_for_https_token_failure() -> None:
    """HTTPS credential failures must retain their existing auth diagnostic."""
    message = _clone_failure_message(
        stderr=b"remote: HTTP Basic: Access denied\nfatal: Authentication failed\n",
        attempt_scheme="https",
    )

    assert "SSH key authentication failed" not in message
    assert "ssh-add <key-file>" not in message


def test_clone_failure_message_omits_ssh_diagnostic_for_https_passphrase_text() -> None:
    """Passphrase-like server text must not override the actual HTTPS transport."""
    message = _clone_failure_message(
        stderr=b"remote: Enter passphrase for key enrollment\nfatal: Authentication failed\n",
        attempt_scheme="https",
    )

    assert "SSH key authentication failed" not in message
    assert "ssh-add <key-file>" not in message


def test_clone_failure_message_omits_ssh_diagnostic_for_host_key_failure() -> None:
    """Host-key verification has different remediation from key authentication."""
    message = _clone_failure_message(
        stderr=b"Host key verification failed.\nfatal: Could not read from remote repository.\n",
        attempt_scheme="ssh",
    )

    assert "SSH key authentication failed" not in message
    assert "ssh-add <key-file>" not in message


def test_clone_failure_message_omits_ssh_diagnostic_for_network_failure() -> None:
    """SSH network failures must not be presented as key authentication failures."""
    message = _clone_failure_message(
        stderr=b"ssh: Could not resolve hostname example.invalid: Name or service not known\n",
        attempt_scheme="ssh",
    )

    assert "SSH key authentication failed" not in message
    assert "ssh-add <key-file>" not in message


def test_clone_engine_threads_failed_ssh_scheme_into_diagnostic(tmp_path: Path) -> None:
    """CloneEngine must classify the transport that produced the captured error."""
    plan = TransportPlan(
        attempts=[TransportAttempt(scheme="ssh", use_token=False, label="SSH")],
        strict=True,
    )
    host = MagicMock()
    host._transport_selector.select.return_value = plan
    host._protocol_pref = ProtocolPreference.SSH
    host._allow_fallback = False
    host._resolve_dep_token.return_value = None
    host._resolve_dep_auth_ctx.return_value = None
    host._build_noninteractive_git_env.return_value = {}
    host._build_repo_url.return_value = "ssh://git@github.com/owner/repo"
    host._sanitize_git_error.side_effect = lambda value: value
    host.auth_resolver.build_error_context.return_value = ""
    host.has_ado_token = False
    engine = CloneEngine(host)

    def _fail_clone(_url: str, _env: dict[str, str], _target: Path) -> None:
        raise subprocess.CalledProcessError(
            128,
            ["git", "clone", "ssh://git@github.com/owner/repo"],
            stderr=b"Enter passphrase for key '/home/alice/.ssh/id_secret':\n",
        )

    dep_ref = DependencyReference.parse("ssh://git@github.com/owner/repo")
    with pytest.raises(RuntimeError) as exc_info:
        engine.execute(
            dep_ref.repo_url,
            tmp_path / "repo",
            dep_ref=dep_ref,
            clone_action=_fail_clone,
        )

    message = str(exc_info.value)
    assert "SSH key authentication failed" in message
    assert "/home/alice/.ssh/id_secret" not in message
