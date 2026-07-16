"""Regression tests for clone failure diagnostics."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

from apm_cli.deps.bare_cache import build_clone_failure_message
from apm_cli.models.apm_package import DependencyReference


def _clone_failure_message(
    *,
    stderr: bytes,
    explicit_scheme: str | None,
) -> str:
    """Build a clone failure message for an SSH diagnostic scenario."""
    plan = MagicMock()
    plan.strict = True
    plan.attempts = [MagicMock(label="SSH")]
    plan.fallback_hint = None
    auth_resolver = MagicMock()
    auth_resolver.build_error_context.return_value = ""
    last_error = subprocess.CalledProcessError(
        128,
        ["git", "clone", "ssh://github.com/owner/repo"],
        stderr=stderr,
    )

    return build_clone_failure_message(
        repo_url_base="owner/repo",
        plan=plan,
        dep_ref=DependencyReference(
            repo_url="owner/repo",
            host="github.com",
            explicit_scheme=explicit_scheme,
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
        sanitize_git_error=lambda value: value,
    )


def test_clone_failure_message_explains_passphrase_protected_ssh_key() -> None:
    """SSH passphrase failures must tell users how to unblock non-interactive clones."""
    message = _clone_failure_message(
        stderr=(
            b"Enter passphrase for key '/Users/alice/.ssh/id_ed25519':\n"
            b"Permission denied (publickey).\n"
        ),
        explicit_scheme="ssh",
    )

    assert "SSH authentication failed" in message
    assert "ssh-add <key-file>" in message
    assert "ssh-agent" in message
    assert "token-backed HTTPS" in message
    assert "will not open a raw passphrase prompt" in message


def test_clone_failure_message_explains_explicit_ssh_publickey_failure() -> None:
    """Explicit SSH publickey denials should get the same non-interactive guidance."""
    message = _clone_failure_message(
        stderr=b"Permission denied (publickey).\n",
        explicit_scheme="ssh",
    )

    assert "SSH authentication failed" in message
    assert "ssh-add <key-file>" in message
    assert "token-backed HTTPS" in message


def test_clone_failure_message_omits_ssh_diagnostic_for_non_ssh_publickey_text() -> None:
    """Publickey text on a non-SSH dependency must not produce SSH-only guidance."""
    message = _clone_failure_message(
        stderr=b"Permission denied (publickey).\n",
        explicit_scheme=None,
    )

    assert "SSH authentication failed" not in message
    assert "ssh-add <key-file>" not in message
