"""Org policy mcp.transport.allow judges the effective MCP transport (#3764).

Promise (enterprise/policy-reference.md): ``mcp.transport.allow`` applies to
the transport APM writes, including registry servers that declare no
``transport:`` in apm.yml. A blocked server is rejected before any MCP
config or manifest write, and ``apm audit --ci`` reaches the same verdict.

The installed CLI runs against a local MCP Registry v0.1 endpoint and an org
policy seeded into the policy cache from a local file; no network is used.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_mcp_registry import LocalMcpRegistry, LocalMcpRegistryFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
]

_POLICY_SOURCE = "dev.azure.com/contoso/apm/apm-policy"
_POLICY_REMOTE = "https://dev.azure.com/contoso/project/_git/transport-policy"
_POLICY = {
    "name": "transport-policy",
    "version": "1",
    "enforcement": "block",
    "mcp": {"transport": {"allow": ["stdio", "http"]}},
}
_SSE_NAME = "io.example/sse-server"
_SSE_HOST = "sse.example.test"
_HTTP_NAME = "io.example/http-server"
_HTTP_HOST = "http.example.test"
_MCP_CONFIG_NAMES = ("mcp.json", "mcp_config.json", "settings.json", "config.toml", ".mcp.json")


def _server(name: str, transport: str, host: str) -> dict[str, object]:
    return {
        "name": name,
        "description": name,
        "version": "1.0.0",
        "remotes": [{"type": transport, "url": f"https://{host}/mcp"}],
    }


class _Project:
    """One governed consumer project driven by the installed CLI."""

    def __init__(self, root: Path, binary: Path, registry: LocalMcpRegistry) -> None:
        self.isolated = IsolatedApmEnvironment.create(root / "scenario", base_env=dict(os.environ))
        self.root = self.isolated.work_root / "consumer"
        self.root.mkdir(parents=True)
        self.env = self.isolated.subprocess_env()
        self.env["APM_NON_INTERACTIVE"] = "1"
        self.env["MCP_REGISTRY_URL"] = registry.url
        self.env["MCP_REGISTRY_ALLOW_HTTP"] = "1"
        port = registry.url.rsplit(":", 1)[1]
        ports = [p for p in self.env.get("APM_TEST_LOOPBACK_PORTS", "").split(",") if p]
        self.env["APM_TEST_LOOPBACK_PORTS"] = ",".join([*ports, port])
        self.runner = ApmLifecycleRunner((str(binary),), timeout_seconds=120)
        for argv in (
            ("git", "init", "--initial-branch=main"),
            ("git", "remote", "add", "origin", _POLICY_REMOTE),
        ):
            subprocess.run(
                argv, cwd=self.root, env=self.env, capture_output=True, check=True, timeout=30
            )
        # A leaf that extends a parent makes discovery persist the merged
        # policy in the cache, which the auto-discovering install then reads.
        (self.root / "org").mkdir()
        dump_yaml(dict(_POLICY), self.root / "org" / "apm-policy.yml")
        leaf = self.root.joinpath(*_POLICY_SOURCE.split("/"))
        leaf.parent.mkdir(parents=True)
        dump_yaml({"name": "transport-leaf", "extends": "org/apm-policy.yml"}, leaf)

    def write_manifest(self, mcp: list[str | dict[str, str]] | None = None) -> bytes:
        manifest: dict[str, object] = {
            "name": "consumer",
            "version": "1.0.0",
            "targets": ["copilot"],
        }
        if mcp:
            manifest["dependencies"] = {"mcp": mcp}
        dump_yaml(manifest, self.root / "apm.yml")
        return (self.root / "apm.yml").read_bytes()

    def run(self, *args: str, scenario: str) -> CommandResult:
        return self.runner.run(args, cwd=self.root, env=self.env, scenario_id=scenario)

    def audit(self, scenario: str) -> tuple[CommandResult, dict[str, dict[str, object]]]:
        result = self.run(
            "audit",
            "--ci",
            "--no-drift",
            "--no-fail-fast",
            "--format",
            "json",
            "--policy",
            _POLICY_SOURCE,
            "--no-cache",
            scenario=scenario,
        )
        payload = json.loads(result.stdout)
        return result, {c["name"]: c for c in payload["checks"]}

    def mcp_config_texts(self) -> str:
        texts: list[str] = []
        for base in (self.root, self.isolated.home):
            for path in base.rglob("*"):
                if (
                    path.is_file()
                    and path.name in _MCP_CONFIG_NAMES
                    and "apm_modules" not in path.parts
                ):
                    texts.append(path.read_text(encoding="utf-8"))
        return "\n".join(texts)


def test_registry_sse_remote_blocked_before_any_write(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    factory = LocalMcpRegistryFactory(tmp_path / "registry")
    with factory.start(_server(_SSE_NAME, "sse", _SSE_HOST)) as registry:
        project = _Project(tmp_path, apm_binary_path, registry)
        manifest_before = project.write_manifest()
        seeded, _checks = project.audit("transport-policy-seed")
        assert seeded.returncode == 0, seeded.stdout + seeded.stderr

        # --registry wins over a default registry that cannot answer.
        project.env["MCP_REGISTRY_URL"] = "http://127.0.0.1:9"
        flagged = project.run(
            "install",
            "--mcp",
            _SSE_NAME,
            "--registry",
            registry.url,
            scenario="transport-policy-mcp-registry-flag",
        )
        flagged_output = " ".join((flagged.stdout + flagged.stderr).split())
        assert flagged.returncode != 0, flagged_output
        assert "transport 'sse' not in allowed [stdio, streamable-http]" in flagged_output
        assert "unreachable" not in flagged_output
        assert (project.root / "apm.yml").read_bytes() == manifest_before
        project.env["MCP_REGISTRY_URL"] = registry.url

        direct = project.run("install", "--mcp", _SSE_NAME, scenario="transport-policy-mcp-blocked")
        direct_output = " ".join((direct.stdout + direct.stderr).split())
        assert direct.returncode != 0, direct_output
        assert "transport 'sse' not in allowed [stdio, streamable-http]" in direct_output
        assert (project.root / "apm.yml").read_bytes() == manifest_before
        assert _SSE_HOST not in project.mcp_config_texts()

        project.write_manifest([_SSE_NAME])
        install = project.run("install", scenario="transport-policy-install-blocked")
        install_output = " ".join((install.stdout + install.stderr).split())
        assert install.returncode != 0, install_output
        assert "transport 'sse' not in allowed [stdio, streamable-http]" in install_output
        assert _SSE_HOST not in project.mcp_config_texts()

        audit, checks = project.audit("transport-policy-audit-blocked")
        assert audit.returncode == 1, audit.stdout + audit.stderr
        assert checks["mcp-transport"]["passed"] is False
        assert checks["mcp-transport"]["details"] == [
            f"{_SSE_NAME}: transport 'sse' not in allowed [stdio, streamable-http]"
        ]


def test_registry_streamable_http_remote_passes(tmp_path: Path, apm_binary_path: Path) -> None:
    factory = LocalMcpRegistryFactory(tmp_path / "registry")
    with factory.start(_server(_HTTP_NAME, "streamable-http", _HTTP_HOST)) as registry:
        project = _Project(tmp_path, apm_binary_path, registry)
        project.write_manifest()
        seeded, _checks = project.audit("transport-policy-compliant-seed")
        assert seeded.returncode == 0, seeded.stdout + seeded.stderr

        project.write_manifest([_HTTP_NAME])
        install = project.run("install", scenario="transport-policy-install-compliant")
        assert install.returncode == 0, install.stdout + install.stderr
        assert _HTTP_HOST in project.mcp_config_texts()

        audit, checks = project.audit("transport-policy-audit-compliant")
        assert audit.returncode == 0, audit.stdout + audit.stderr
        assert checks["mcp-transport"]["passed"] is True


def test_unreachable_registry_blocks_with_remedy(tmp_path: Path, apm_binary_path: Path) -> None:
    factory = LocalMcpRegistryFactory(tmp_path / "registry")
    registry = factory.start(_server(_SSE_NAME, "sse", _SSE_HOST))
    project = _Project(tmp_path, apm_binary_path, registry)
    manifest_before = project.write_manifest()
    seeded, _checks = project.audit("transport-policy-unreachable-seed")
    assert seeded.returncode == 0, seeded.stdout + seeded.stderr
    registry.close()

    direct = project.run("install", "--mcp", _SSE_NAME, scenario="transport-policy-unreachable")
    output = " ".join((direct.stdout + direct.stderr).split())
    assert direct.returncode != 0, output
    assert f"{_SSE_NAME} -- effective transport unknown (MCP registry" in output
    assert "is unreachable); cannot check it against allowed" in output
    assert (project.root / "apm.yml").read_bytes() == manifest_before
    assert _SSE_HOST not in project.mcp_config_texts()

    # A declared transport does not stand in for the registry: install would
    # write what the registry serves, so it is still unknown, before any write.
    declared = project.run(
        "install",
        "--mcp",
        _SSE_NAME,
        "--transport",
        "http",
        scenario="transport-policy-unreachable-declared",
    )
    declared_output = " ".join((declared.stdout + declared.stderr).split())
    assert declared.returncode != 0, declared_output
    assert "effective transport unknown" in declared_output
    assert "A declared 'transport:' cannot stand in" in declared_output
    assert (project.root / "apm.yml").read_bytes() == manifest_before

    project.write_manifest([_SSE_NAME])
    audit, checks = project.audit("transport-policy-unreachable-audit")
    assert audit.returncode == 1, audit.stdout + audit.stderr
    assert "is unreachable" in checks["mcp-transport"]["details"][0]

    # audit --ci does not judge a declared registry transport offline either.
    project.write_manifest([{"name": _SSE_NAME, "transport": "http"}])
    declared_audit, checks = project.audit("transport-policy-unreachable-declared-audit")
    assert checks["mcp-transport"]["passed"] is False, declared_audit.stdout
    assert "effective transport unknown" in checks["mcp-transport"]["details"][0]

    # enforcement: warn downgrades the unknown transport to a reported warning.
    dump_yaml(dict(_POLICY, enforcement="warn"), project.root / "org" / "apm-policy.yml")
    warned, checks = project.audit("transport-policy-unreachable-warn-audit")
    assert checks["mcp-transport"]["passed"] is True, warned.stdout
    assert checks["mcp-transport"]["message"].endswith("(enforcement: warn)")
    assert "effective transport unknown" in checks["mcp-transport"]["details"][0]
