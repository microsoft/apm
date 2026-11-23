"""Refused MCP retirements record the claims a run wrote, on a real lockfile (#4792)."""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.core.command_logger import InstallLogger

pytestmark = [pytest.mark.component, pytest.mark.promise("P-OWN-mcp-declaration-scope")]


def test_refused_wildcard_legacy_cleanup_records_concrete_runtime_claims(tmp_path: Path) -> None:
    """An untargeted legacy cleanup (runtime None) refused mid-way still writes the lock (#4792).

    The wildcard expands to concrete runtimes, so no ``None`` key reaches the
    lockfile and every runtime keeps the stale claim it may not have cleaned.
    """
    from apm_cli.deps.lockfile import LockFile
    from apm_cli.factory import ClientFactory
    from apm_cli.install.mcp.retirement import record_refused_retirement

    lock_path = tmp_path / "apm.lock.yaml"
    LockFile().write(lock_path)
    notes = {"name": "notes", "registry": False, "transport": "http", "url": "http://a"}
    new = {**notes, "name": "new"}
    managed = {"claude": {"new"}, "codex": {"new"}}

    record_refused_retirement(
        lock_path,
        InstallLogger(verbose=False),
        managed_target_servers=managed,
        old_target_servers={None: {"notes"}},
        removed={},
        planned=[(None, {"notes"}, "cursor")],
        servers=({"new"}, {"notes"}),
        configs=({"new": new}, {"notes": notes}),
        provenance=({}, {}),
        native_hashes=({}, {}),
    )

    claims = LockFile.read(lock_path).mcp_target_servers
    runtimes = set(ClientFactory.supported_clients()) - {"cursor"}
    assert set(claims) == runtimes
    assert claims["claude"] == ["new", "notes"]
    assert claims["codex"] == ["new", "notes"]
    assert all(claims[runtime] == ["notes"] for runtime in runtimes - {"claude", "codex"})
    assert LockFile.read(lock_path).mcp_configs["notes"] == notes
