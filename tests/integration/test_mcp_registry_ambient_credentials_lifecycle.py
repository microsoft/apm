"""The MCP registry client never attaches ambient credentials (#4088).

PROMISE: auth.credentials_isolated -- credentials stay scoped to their intended
host; a ``~/.netrc`` entry for the registry host is not sent to the registry
(source: docs/src/content/docs/enterprise/registry-proxy.md).
CELL: OP=mcp-show|mcp-search SRC=registry NET=loopback-registry
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_mcp_registry import LocalMcpRegistryFactory

pytestmark = [
    pytest.mark.promise("P-LAW-auth.credentials_isolated", "P-OWN-credential-origin-scope"),
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
    pytest.mark.skipif(os.name == "nt", reason="POSIX netrc permissions"),
]

_NAME = "io.github.apm/netrc-probe"


def test_registry_requests_carry_no_netrc_credentials(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "world", base_env=dict(os.environ))
    project = isolated.work_root / "consumer"
    project.mkdir()
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=60)
    factory = LocalMcpRegistryFactory(isolated.root / "registries")

    with factory.start({"name": _NAME, "description": "probe", "version": "1.0.0"}) as registry:
        host = urlparse(registry.url).hostname
        netrc = isolated.home / ".netrc"
        netrc.write_text(f"machine {host}\nlogin ambient\npassword s3cret\n", encoding="ascii")
        netrc.chmod(0o600)
        environment = isolated.subprocess_env()
        environment["MCP_REGISTRY_URL"] = registry.url
        environment["MCP_REGISTRY_ALLOW_HTTP"] = "1"
        environment["APM_TEST_LOOPBACK_PORTS"] = str(urlparse(registry.url).port)
        environment["NO_PROXY"] = host or ""
        for args in (("mcp", "show", _NAME), ("mcp", "search", "netrc")):
            result = runner.run(args, cwd=project, env=environment)
            assert result.returncode == 0, result.stdout + result.stderr
            assert "s3cret" not in result.stdout + result.stderr

        assert registry.request_paths, "the registry was never contacted"
        assert list(registry.request_authorizations) == [None] * len(registry.request_paths)
