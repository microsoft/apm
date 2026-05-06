"""WS2b (#1116): parallel MCP registry batch lookup tests.

Verifies that ``validate_servers_exist`` and ``check_servers_needing_installation``
run in parallel and complete within bounded wall time.

No real network calls -- all registry HTTP is mocked.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

from apm_cli.registry.operations import MCPServerOperations


class TestParallelRegistryLookups:
    """Parallel batch lookups complete faster than serial."""

    def test_validate_servers_exist_parallel_wall_time(self) -> None:
        """3 servers each sleeping 200ms: wall time < 500ms (not 600ms serial)."""
        ops = MCPServerOperations.__new__(MCPServerOperations)
        ops.registry_client = MagicMock()

        call_count = {"n": 0}

        def slow_find(ref: str):
            import time as _t

            call_count["n"] += 1
            _t.sleep(0.2)
            return {"id": f"uuid-{ref}", "name": ref}

        ops.registry_client.find_server_by_reference = slow_find
        ops.registry_client._is_custom_url = False

        servers = ["server-a", "server-b", "server-c"]

        start = time.monotonic()
        valid, invalid = ops.validate_servers_exist(servers, max_workers=4)
        elapsed = time.monotonic() - start

        assert call_count["n"] == 3
        assert len(valid) == 3
        assert len(invalid) == 0
        # Parallel: should complete in ~200ms, not 600ms
        assert elapsed < 0.5, f"Wall time {elapsed:.3f}s >= 0.5s (not parallel)"

    def test_check_servers_needing_installation_parallel_wall_time(self) -> None:
        """3 servers each sleeping 200ms: wall time < 500ms (not 600ms serial)."""
        ops = MCPServerOperations.__new__(MCPServerOperations)
        ops.registry_client = MagicMock()

        def slow_find(ref: str):
            import time as _t

            _t.sleep(0.2)
            return {"id": f"uuid-{ref}", "name": ref}

        ops.registry_client.find_server_by_reference = slow_find

        # Mock _get_installed_server_ids to return empty sets
        ops._get_installed_server_ids = MagicMock(return_value=set())

        servers = ["server-a", "server-b", "server-c"]

        start = time.monotonic()
        result = ops.check_servers_needing_installation(
            target_runtimes=["copilot"],
            server_references=servers,
            max_workers=4,
        )
        elapsed = time.monotonic() - start

        # All need installation (none installed)
        assert set(result) == set(servers)
        # Parallel: should complete in ~200ms, not 600ms
        assert elapsed < 0.5, f"Wall time {elapsed:.3f}s >= 0.5s (not parallel)"

    def test_validate_preserves_submission_order(self) -> None:
        """Results appear in the same order as the input list."""
        ops = MCPServerOperations.__new__(MCPServerOperations)
        ops.registry_client = MagicMock()

        import random

        def jittered_find(ref: str):
            import time as _t

            _t.sleep(random.uniform(0.01, 0.05))  # noqa: S311
            # Mark "bad" as invalid
            if ref == "bad":
                return None
            return {"id": f"uuid-{ref}", "name": ref}

        ops.registry_client.find_server_by_reference = jittered_find
        ops.registry_client._is_custom_url = False

        servers = ["alpha", "bad", "gamma", "delta"]
        valid, invalid = ops.validate_servers_exist(servers, max_workers=4)

        # Order preserved within each bucket
        assert valid == ["alpha", "gamma", "delta"]
        assert invalid == ["bad"]

    def test_validate_single_server_does_not_error(self) -> None:
        """Edge case: single server still works (no executor edge cases)."""
        ops = MCPServerOperations.__new__(MCPServerOperations)
        ops.registry_client = MagicMock()
        ops.registry_client.find_server_by_reference = lambda ref: {"id": "x"}
        ops.registry_client._is_custom_url = False

        valid, invalid = ops.validate_servers_exist(["only-one"], max_workers=4)
        assert valid == ["only-one"]
        assert invalid == []
