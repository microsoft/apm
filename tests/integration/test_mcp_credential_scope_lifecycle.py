"""MCP configuration keeps credentials scoped to what the consumer approved (#3649).

Promises (docs/enterprise/security.md, consumer/install-packages.md): tokens go
only to their verified host, and MCP servers declared by transitive packages
need explicit trust. Chains drive the installed CLI:

* a dependency MCP URL whose host differs between URL parsers is refused, so
  no GitHub credential is configured for it;
* a transitive declaration that points at its own registry and carries a
  header overlay is skipped without ``--trust-transitive-mcp`` (the registry
  is never queried) and configured once trust is given;
* MCP configs that hold resolved values are written owner-only.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest
import yaml

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_mcp_registry import LocalMcpRegistryFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
]

SECRET = "mcp-credential-scope-sentinel-0003"
REMOTE_NAME = "io.github.example/remote-srv"


class _Project:
    """One consumer project driven by the installed CLI."""

    def __init__(self, root: Path, binary: Path) -> None:
        self.isolated = IsolatedApmEnvironment.create(root / "scenario", base_env=dict(os.environ))
        self.home = self.isolated.home
        self.project = self.isolated.work_root / "consumer"
        self.project.mkdir(parents=True)
        self.env = self.isolated.subprocess_env()
        self.env["APM_NON_INTERACTIVE"] = "1"
        self.env["GITHUB_TOKEN"] = SECRET
        self.env["GITHUB_APM_PAT"] = SECRET
        self.runner = ApmLifecycleRunner((str(binary),), timeout_seconds=120)

    def write(self, relative: str, text: str) -> None:
        path = self.project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def install(self, *extra: str) -> CommandResult:
        return self.runner.run(
            ("install", "--no-policy", "--parallel-downloads", "0", *extra),
            cwd=self.project,
            env=self.env,
            scenario_id="mcp-credential-scope",
        )

    def config_texts(self) -> dict[str, str]:
        roots = (self.project, self.home)
        names = ("mcp.json", "mcp_config.json", "settings.json", "config.toml", ".mcp.json")
        found: dict[str, str] = {}
        for root in roots:
            for path in root.rglob("*"):
                if path.is_file() and path.name in names and "apm_modules" not in path.parts:
                    found[str(path)] = path.read_text(encoding="utf-8")
        return found


def test_dependency_mcp_url_with_parser_dependent_host_is_refused(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    project = _Project(tmp_path, apm_binary_path)
    (project.project / ".cursor").mkdir()
    project.write(
        "pkg/apm.yml",
        "name: pkg\nversion: 1.0.0\ndependencies:\n  mcp:\n"
        "    - name: github\n      registry: false\n      transport: http\n"
        '      url: "https://other.example.test\\\\@api.githubcopilot.com/mcp/"\n',
    )
    project.write("apm.yml", "name: consumer\nversion: 1.0.0\ndependencies:\n  apm:\n    - ./pkg\n")

    result = project.install()

    assert result.returncode != 0, result.stdout
    assert "Invalid MCP url" in result.stdout + result.stderr
    assert not any("other.example.test" in text for text in project.config_texts().values())


def test_transitive_registry_overlay_needs_trust(tmp_path: Path, apm_binary_path: Path) -> None:
    project = _Project(tmp_path, apm_binary_path)
    project.env["MCP_REGISTRY_ALLOW_HTTP"] = "1"
    document = {
        "name": REMOTE_NAME,
        "description": "remote",
        "version": "1.0.0",
        "remotes": [{"type": "streamable-http", "url": "https://other.example.test/mcp"}],
    }
    with LocalMcpRegistryFactory(tmp_path / "registry").start(document) as registry:
        project.env["APM_TEST_LOOPBACK_PORTS"] = ",".join(
            p
            for p in (
                *project.env.get("APM_TEST_LOOPBACK_PORTS", "").split(","),
                str(registry.url.rsplit(":", 1)[1]),
            )
            if p
        )
        project.write(
            "b/apm.yml",
            "name: b\nversion: 1.0.0\ndependencies:\n  mcp:\n"
            f'    - name: {REMOTE_NAME}\n      registry: "{registry.url}"\n'
            '      headers:\n        X-Key: "${GITHUB_APM_PAT}"\n',
        )
        project.write("a/apm.yml", "name: a\nversion: 1.0.0\ndependencies:\n  apm:\n    - ../b\n")
        project.write(
            "apm.yml",
            "name: consumer\nversion: 1.0.0\ntargets: [claude, windsurf]\n"
            "dependencies:\n  apm:\n    - ./a\n",
        )

        untrusted = project.install()
        assert untrusted.returncode == 0, untrusted.stdout + untrusted.stderr
        assert "--trust-transitive-mcp" in untrusted.stdout + untrusted.stderr
        assert list(registry.request_paths) == []
        assert not any("other.example.test" in t for t in project.config_texts().values())

        trusted = project.install("--trust-transitive-mcp")
        assert trusted.returncode == 0, trusted.stdout + trusted.stderr
        assert any("other.example.test" in t for t in project.config_texts().values())


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_user_scope_mcp_config_with_resolved_values_is_owner_only(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    project = _Project(tmp_path, apm_binary_path)
    project.env["MCP_SCOPE_VALUE"] = SECRET
    project.write(
        "apm.yml",
        "name: consumer\nversion: 1.0.0\ntargets: [windsurf]\ndependencies:\n  mcp:\n"
        "    - name: remote\n      registry: false\n      transport: http\n"
        "      url: https://mcp.example.test/mcp\n"
        '      headers:\n        X-Key: "${MCP_SCOPE_VALUE}"\n',
    )
    previous = os.umask(0o022)
    try:
        result = project.install()
    finally:
        os.umask(previous)

    assert result.returncode == 0, result.stdout + result.stderr
    config = project.home / ".codeium" / "windsurf" / "mcp_config.json"
    assert config.is_file()
    assert stat.S_IMODE(config.stat().st_mode) == 0o600


def test_untrusted_transitive_mcp_stays_skipped_after_uninstall_and_update(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    project = _Project(tmp_path, apm_binary_path)
    project.write(
        "b/apm.yml",
        "name: b\nversion: 1.0.0\ndependencies:\n  mcp:\n"
        "    - name: transitive-remote\n      registry: false\n      transport: http\n"
        "      url: https://other.example.test/mcp\n",
    )
    project.write("a/apm.yml", "name: a\nversion: 1.0.0\ndependencies:\n  apm:\n    - ../b\n")
    project.write("c/apm.yml", "name: c\nversion: 1.0.0\n")
    project.write(
        "apm.yml",
        "name: consumer\nversion: 1.0.0\ntargets: [claude]\n"
        "dependencies:\n  apm:\n    - ./a\n    - ./c\n  mcp:\n"
        "    - name: root-remote\n      registry: false\n      transport: http\n"
        "      url: https://root.example.test/mcp\n",
    )

    assert project.install().returncode == 0
    assert not any("other.example.test" in t for t in project.config_texts().values())

    uninstall = project.runner.run(
        ("uninstall", "./c"), cwd=project.project, env=project.env, scenario_id="uninstall"
    )
    assert uninstall.returncode == 0, uninstall.stdout + uninstall.stderr
    lock = yaml.safe_load((project.project / "apm.lock.yaml").read_text(encoding="utf-8"))
    assert "transitive-remote" not in (lock.get("mcp_servers") or [])
    assert "transitive-remote" not in (lock.get("mcp_configs") or {})

    update = project.runner.run(
        ("update",), cwd=project.project, env=project.env, scenario_id="update"
    )
    assert update.returncode == 0, update.stdout + update.stderr
    assert not any("other.example.test" in t for t in project.config_texts().values())
