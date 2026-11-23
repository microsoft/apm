"""Trust-policy discovery that raises a known discovery error.

``discover_trust_policy`` serves every executables-trust reader (install's
bundle preflight, ``apm approve``, MCP admission, ...). A discovery that
raises one of the owner's known discovery errors -- including a rejected
policy inheritance chain (cycle, cross-host ``extends``, excess depth) --
must route through the policy-outcome owner instead of escaping as a
traceback. A rejected chain routes as the always-block ``incomplete_chain``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from apm_cli.commands.approve import approve_cmd
from apm_cli.policy import PolicyInheritanceError

pytestmark = [pytest.mark.component, pytest.mark.promise("P-OWN-policy-outcome-routing")]

_DISCOVER = "apm_cli.policy.discovery.discover_policy_with_chain"
_CYCLE = PolicyInheritanceError("Cycle detected in policy extends chain: a -> b -> a")


def test_approve_reports_rejected_chain_as_named_error_without_writing():
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path("apm.yml").write_text("name: demo\nversion: '1.0.0'\n", encoding="utf-8")
        before = Path("apm.yml").read_bytes()
        with patch(_DISCOVER, side_effect=_CYCLE):
            result = runner.invoke(approve_cmd, ["--recommended"])
        after = Path("apm.yml").read_bytes()

    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit)
    flat = " ".join(result.output.split())
    assert "[x] Blocked by org policy: org policy inheritance chain is incomplete" in flat
    assert "Traceback" not in result.output
    assert after == before
