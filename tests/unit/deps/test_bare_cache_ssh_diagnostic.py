"""Clone-engine diagnostics for a failed SSH clone."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from apm_cli.deps.clone_engine import CloneEngine
from apm_cli.deps.transport_selection import (
    ProtocolPreference,
    TransportAttempt,
    TransportPlan,
)
from apm_cli.models.apm_package import DependencyReference

pytestmark = pytest.mark.component


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
