"""Deterministic HTTP cache pressure between real MCP CLI transitions.

The generated install/audit model has no HTTP lookup or pressure operation.
This bounded scenario reuses its runner, isolation and state oracle; pressure
is applied through the production cache owner, not a test-only CLI switch.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlparse

import pytest

from apm_cli.cache.http_cache import HttpCache
from apm_cli.cache.paths import get_http_path
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.lifecycle_state import LifecycleStateSnapshot
from tests.utils.local_mcp_registry import LocalMcpRegistry, LocalMcpRegistryFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
]


def test_mcp_show_hit_survives_cache_pressure_without_refetch(
    tmp_path: Path, apm_binary_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A/B/A/pressure/A retains A, evicts B and preserves project state."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "http-recency", base_env=dict(os.environ))
    project = isolated.work_root / "consumer"
    project.mkdir()
    before = LifecycleStateSnapshot.capture(project)
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=60)
    factory = LocalMcpRegistryFactory(isolated.root / "registries")
    name = "io.github.apm/cache-recency"
    document = {"name": name, "description": "x" * 4096, "version": "1.0.0"}

    def show(registry: LocalMcpRegistry, transition: str) -> str:
        environment = isolated.subprocess_env()
        environment["MCP_REGISTRY_URL"] = registry.url
        environment["MCP_REGISTRY_ALLOW_HTTP"] = "1"
        environment["APM_TEST_LOOPBACK_PORTS"] = str(urlparse(registry.url).port)
        (result,) = runner.run_sequence(
            (("mcp", "show", name),),
            expected_returncodes=(0,),
            scenario_id=f"http-cache-recency-{transition}",
            cwd=project,
            env=environment,
        )
        assert name in result.stdout
        assert LifecycleStateSnapshot.capture(project) == before
        return result.stdout

    with factory.start(document) as registry_a, factory.start(document) as registry_b:
        first = show(registry_a, "cold-a")
        show(registry_b, "cold-b")
        assert registry_a.request_paths
        assert registry_b.request_paths
        requests_a = tuple(registry_a.request_paths)
        requests_b = tuple(registry_b.request_paths)
        paths_by_port: dict[int, list[Path]] = {}
        for meta_path in get_http_path(isolated.cache_root).glob("*/meta.json"):
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            port = urlparse(meta["url"]).port
            assert port is not None
            paths_by_port.setdefault(port, []).append(meta_path.parent)
        port_a, port_b = urlparse(registry_a.url).port, urlparse(registry_b.url).port
        assert set(paths_by_port) == {port_a, port_b}
        for port, paths in paths_by_port.items():
            aged = 1_000_000_000 if port == port_a else 1_000_000_100
            for path in paths:
                os.utime(path, (aged, aged))

        assert show(registry_a, "warm-a") == first
        assert tuple(registry_a.request_paths) == requests_a
        cache = HttpCache(isolated.cache_root)
        # Admit one group plus pressure, forcing precisely the cold group out.
        warm_size = sum(
            file.stat().st_size for path in paths_by_port[port_a] for file in path.iterdir()
        )
        monkeypatch.setattr("apm_cli.cache.http_cache.MAX_HTTP_CACHE_BYTES", warm_size + 5000)
        cache.store(
            "https://pressure.example.invalid/c",
            b"x" * 4096,
            headers={"Cache-Control": "max-age=3600"},
        )

        assert show(registry_a, "retained-a") == first
        assert tuple(registry_a.request_paths) == requests_a
        assert all(path.is_dir() for path in paths_by_port[port_a])
        assert all(not path.exists() for path in paths_by_port[port_b])
        assert cache.get_stats()["total_size_bytes"] <= warm_size + 5000
        show(registry_b, "evicted-b")
        assert len(registry_b.request_paths) > len(requests_b)
