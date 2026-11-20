"""WS2b (#1116): parallel MCP registry batch lookup tests.

Verifies that ``validate_servers_exist``, ``check_servers_needing_installation``,
and ``batch_fetch_server_info`` keep every lookup in flight at the same time.
Parallelism is proven with a max-in-flight counter, not wall time, so the
result does not depend on machine speed.

No real network calls -- all registry HTTP is mocked.
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.registry.operations import MCPServerOperations

pytestmark = pytest.mark.unit

SERVERS = ["server-a", "server-b", "server-c"]


class _InFlightProbe:
    """Registry lookup stub that records how many calls overlap.

    Each call holds until ``target`` calls are in flight together. Serialized
    callers never reach the target: the first call gives up after ``timeout``
    and releases the rest, so a serial regression fails fast instead of hanging.
    """

    def __init__(self, target: int, timeout: float = 10.0) -> None:
        self._lock = threading.Lock()
        self._all_in = threading.Event()
        self._target = target
        self._timeout = timeout
        self._in_flight = 0
        self.max_in_flight = 0
        self.calls = 0

    def __call__(self, ref: str) -> dict[str, str]:
        with self._lock:
            self.calls += 1
            self._in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self._in_flight)
            if self._in_flight >= self._target:
                self._all_in.set()
        try:
            if not self._all_in.wait(self._timeout):
                self._all_in.set()
            return {"id": f"uuid-{ref}", "name": ref}
        finally:
            with self._lock:
                self._in_flight -= 1


class TestParallelRegistryLookups:
    """Batch lookups run concurrently, not one after another."""

    def test_validate_servers_exist_runs_lookups_concurrently(self) -> None:
        ops = MCPServerOperations.__new__(MCPServerOperations)
        ops.registry_client = MagicMock()
        probe = _InFlightProbe(target=len(SERVERS))
        ops.registry_client.find_server_by_reference = probe
        ops.registry_client._is_custom_url = False

        valid, invalid = ops.validate_servers_exist(SERVERS, max_workers=4)

        assert probe.calls == 3
        assert valid == SERVERS
        assert invalid == []
        assert probe.max_in_flight == 3, f"max in flight {probe.max_in_flight} (not parallel)"

    def test_check_servers_needing_installation_runs_lookups_concurrently(self) -> None:
        ops = MCPServerOperations.__new__(MCPServerOperations)
        ops.registry_client = MagicMock()
        probe = _InFlightProbe(target=len(SERVERS))
        ops.registry_client.find_server_by_reference = probe
        ops._get_installed_server_ids = MagicMock(return_value=set())

        result = ops.check_servers_needing_installation(
            target_runtimes=["copilot"],
            server_references=SERVERS,
            max_workers=4,
        )

        # All need installation (none installed)
        assert set(result) == set(SERVERS)
        assert probe.max_in_flight == 3, f"max in flight {probe.max_in_flight} (not parallel)"

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

    def test_batch_fetch_server_info_runs_lookups_concurrently(self) -> None:
        ops = MCPServerOperations.__new__(MCPServerOperations)
        ops.registry_client = MagicMock()
        probe = _InFlightProbe(target=len(SERVERS))
        ops.registry_client.find_server_by_reference = probe

        result = ops.batch_fetch_server_info(SERVERS, max_workers=4)

        assert probe.calls == 3
        assert list(result.keys()) == SERVERS
        assert all(result[ref]["id"] == f"uuid-{ref}" for ref in SERVERS)
        assert probe.max_in_flight == 3, f"max in flight {probe.max_in_flight} (not parallel)"

    def test_batch_fetch_preserves_submission_order_and_exceptions(self) -> None:
        """Results keep input order; per-ref exceptions map to None."""
        ops = MCPServerOperations.__new__(MCPServerOperations)
        ops.registry_client = MagicMock()

        import random

        def jittered_find(ref: str):
            import time as _t

            _t.sleep(random.uniform(0.01, 0.05))  # noqa: S311
            if ref == "bad":
                raise RuntimeError("lookup failed")
            return {"id": f"uuid-{ref}", "name": ref}

        ops.registry_client.find_server_by_reference = jittered_find

        servers = ["alpha", "bad", "gamma", "delta"]
        result = ops.batch_fetch_server_info(servers, max_workers=4)

        assert list(result.keys()) == servers
        assert result["alpha"]["id"] == "uuid-alpha"
        assert result["bad"] is None
        assert result["gamma"]["id"] == "uuid-gamma"
        assert result["delta"]["id"] == "uuid-delta"

    def test_batch_fetch_clamps_max_workers_to_four(self) -> None:
        """Callers cannot raise the pool past the established four-worker cap."""
        ops = MCPServerOperations.__new__(MCPServerOperations)
        ops.registry_client = MagicMock()
        ops.registry_client.find_server_by_reference = lambda ref: {"id": ref}

        with patch("concurrent.futures.ThreadPoolExecutor") as mock_pool:
            mock_pool.return_value.__enter__.return_value.map = lambda fn, refs: (
                fn(ref) for ref in refs
            )
            result = ops.batch_fetch_server_info(
                ["a", "b", "c", "d", "e"],
                max_workers=1000,
            )

        assert mock_pool.call_args.kwargs["max_workers"] == 4
        assert list(result.keys()) == ["a", "b", "c", "d", "e"]
