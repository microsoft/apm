"""Regression tests for clone failure diagnostics."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

from apm_cli.deps.bare_cache import build_clone_failure_message
from apm_cli.models.apm_package import DependencyReference


def test_clone_failure_message_explains_passphrase_protected_ssh_key() -> None:
    """SSH passphrase failures must tell users how to unblock non-interactive clones."""
    plan = MagicMock()
    plan.strict = True
    plan.attempts = [MagicMock(label="SSH")]
    plan.fallback_hint = None
    auth_resolver = MagicMock()
    auth_resolver.build_error_context.return_value = ""
    last_error = subprocess.CalledProcessError(
        128,
        ["git", "clone", "ssh://github.com/owner/repo"],
        stderr=(
            b"Enter passphrase for key '/Users/alice/.ssh/id_ed25519':\n"
            b"Permission denied (publickey).\n"
        ),
    )

    message = build_clone_failure_message(
        repo_url_base="owner/repo",
        plan=plan,
        dep_ref=DependencyReference(
            repo_url="owner/repo",
            host="github.com",
            explicit_scheme="ssh",
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

    assert "SSH authentication failed" in message
    assert "ssh-add <key-file>" in message
    assert "ssh-agent" in message
    assert "token-backed HTTPS" in message
    assert "will not open a raw passphrase prompt" in message
