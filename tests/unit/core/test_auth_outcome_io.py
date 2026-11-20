"""Contracts of the auth-outcome classifier and the Azure DevOps chain owner (filesystem, process and CLI boundary cases)."""

from __future__ import annotations

import subprocess

import pytest

import apm_cli.core.token_manager as token_manager_module
from apm_cli.core.token_manager import GitHubTokenManager

pytestmark = [pytest.mark.component, pytest.mark.promise("P-OWN-host-credential-resolution")]


def test_helper_credential_keeps_username_and_request_scope(monkeypatch) -> None:
    def _fill(args, *_a, **kwargs):
        return subprocess.CompletedProcess(
            args, 0, stdout="username=bob\npassword=secret-value\n", stderr=""
        )

    monkeypatch.setattr(token_manager_module.subprocess, "run", _fill)
    credential = GitHubTokenManager.resolve_git_helper_credential(
        "dev.azure.com", path="/org/proj/_git/repo"
    )
    assert credential is not None
    assert (credential.username, credential.host, credential.path) == (
        "bob",
        "dev.azure.com",
        "org/proj/_git/repo",
    )
    assert "secret-value" not in repr(credential)
    assert credential.description == (("username", "bob"), ("password", "secret-value"))
    assert "secret-value" not in repr(credential)
