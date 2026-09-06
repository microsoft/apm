"""Resolution-staging MCP paths remain valid after publication and replay."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import tomlkit

from apm_cli.adapters.client.claude import ClaudeClientAdapter
from apm_cli.adapters.client.codex import CodexClientAdapter
from apm_cli.deps.apm_resolver import APMDependencyResolver
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.install.resolution_staging import ResolutionStagingSession
from apm_cli.integration.mcp_integrator import MCPIntegrator
from apm_cli.models.apm_package import APMPackage
from apm_cli.models.dependency.reference import DependencyReference
from apm_cli.utils.content_hash import compute_package_hash
from apm_cli.utils.staging_guard import STAGING_DIR_NAME
from apm_cli.utils.yaml_io import load_yaml

pytestmark = pytest.mark.component

_PLUGIN_ROOT = "${CLAUDE_PLUGIN_ROOT}"


def _plugin_manifest() -> dict[str, object]:
    return {
        "name": "tool",
        "version": "1.0.0",
        "mcpServers": {
            "toolsrv": {
                "command": "node",
                "args": [
                    f"{_PLUGIN_ROOT}/start.mjs",
                    f"--root={_PLUGIN_ROOT}/data",
                ],
                "env": {"TOOL_HOME": _PLUGIN_ROOT},
            }
        },
    }


def _publish_plugin(tmp_path: Path) -> tuple[Path, DependencyReference, APMPackage]:
    modules = tmp_path / "apm_modules"
    modules.mkdir()
    staging = ResolutionStagingSession(modules)
    dep_ref = DependencyReference(repo_url="acme/tool", reference="main")

    def download_callback(
        dependency: DependencyReference,
        apm_modules_dir: Path,
        parent_chain: str = "",
        parent_pkg: APMPackage | None = None,
    ) -> Path:
        del parent_chain, parent_pkg
        replacement = staging.prepare_replacement(dependency.get_install_path(apm_modules_dir))
        replacement.mkdir(parents=True)
        (replacement / "plugin.json").write_text(
            json.dumps(_plugin_manifest()),
            encoding="utf-8",
        )
        (replacement / "start.mjs").write_text("// inert fixture\n", encoding="utf-8")
        return replacement

    resolver = APMDependencyResolver(
        apm_modules_dir=modules,
        download_callback=download_callback,
        activation_callback=staging.publish_replacement,
        max_parallel=1,
    )
    package = resolver._try_load_dependency_package(dep_ref, parent_chain="acme/parent")
    assert package is not None
    staging.commit()
    return dep_ref.get_install_path(modules).resolve(), dep_ref, package


def _assert_published_server(package: APMPackage, live_path: Path) -> dict[str, object]:
    server = package.get_all_mcp_dependencies()[0]
    assert server.args == [
        f"{live_path}/start.mjs",
        f"--root={live_path}/data",
    ]
    assert server.env == {"TOOL_HOME": str(live_path)}
    assert Path(server.args[0]).is_file()
    return MCPIntegrator._build_self_defined_info(server)


def test_generated_plugin_manifest_survives_publication_and_cached_replay(
    tmp_path: Path,
) -> None:
    live_path, dep_ref, fresh_package = _publish_plugin(tmp_path)
    generated_manifest = live_path / "apm.yml"

    manifest_text = generated_manifest.read_text(encoding="utf-8")
    assert _PLUGIN_ROOT in manifest_text
    assert STAGING_DIR_NAME not in manifest_text
    fresh_server_info = _assert_published_server(fresh_package, live_path)
    published_hash = compute_package_hash(live_path)

    cached_resolver = APMDependencyResolver(
        apm_modules_dir=live_path.parents[1],
        max_parallel=1,
    )
    replayed_package = cached_resolver._try_load_dependency_package(
        dep_ref,
        parent_chain="acme/parent",
    )

    assert replayed_package is not None
    replayed_server_info = _assert_published_server(replayed_package, live_path)
    assert replayed_server_info == fresh_server_info
    assert compute_package_hash(live_path) == published_hash


def test_replayed_plugin_writes_lock_claude_and_codex_configs(tmp_path: Path) -> None:
    live_path, dep_ref, _ = _publish_plugin(tmp_path)
    cached_resolver = APMDependencyResolver(
        apm_modules_dir=live_path.parents[1],
        max_parallel=1,
    )
    replayed_package = cached_resolver._try_load_dependency_package(
        dep_ref,
        parent_chain="acme/parent",
    )
    assert replayed_package is not None
    server_info = _assert_published_server(replayed_package, live_path)
    server_cache = {"toolsrv": server_info}

    project = tmp_path / "consumer"
    (project / ".claude").mkdir(parents=True)
    claude = ClaudeClientAdapter(project_root=project)
    codex = CodexClientAdapter(project_root=project)
    assert claude.configure_mcp_server(
        "toolsrv",
        server_name="toolsrv",
        server_info_cache=server_cache,
    )
    assert codex.configure_mcp_server(
        "toolsrv",
        server_name="toolsrv",
        server_info_cache=server_cache,
    )

    lock = LockFile()
    locked_dependency = LockedDependency.from_dependency_ref(
        dep_ref=dep_ref,
        resolved_commit="a" * 40,
        depth=2,
        resolved_by="acme/parent",
    )
    locked_dependency.content_hash = compute_package_hash(live_path)
    lock.add_dependency(locked_dependency)
    lock.mcp_servers = ["toolsrv"]
    lock.mcp_configs = {"toolsrv": server_info}
    lock_path = project / "apm.lock.yaml"
    lock.write(lock_path)

    claude_config = json.loads((project / ".mcp.json").read_text(encoding="utf-8"))
    codex_config = tomlkit.parse((project / ".codex" / "config.toml").read_text(encoding="utf-8"))
    lock_data = load_yaml(lock_path)
    expected_script = f"{live_path}/start.mjs"
    expected_home = str(live_path)

    lock_server = lock_data["mcp_configs"]["toolsrv"]["_raw_stdio"]
    assert lock_server["args"][0] == expected_script
    assert lock_server["env"]["TOOL_HOME"] == expected_home
    assert lock_data["dependencies"][0]["content_hash"] == compute_package_hash(live_path)

    claude_server = claude_config["mcpServers"]["toolsrv"]
    assert claude_server["args"][0] == expected_script
    assert claude_server["env"]["TOOL_HOME"] == expected_home

    codex_server = codex_config["mcp_servers"]["toolsrv"]
    assert codex_server["args"][0] == expected_script
    assert codex_server["env"]["TOOL_HOME"] == expected_home

    durable_text = "\n".join(
        [lock_path.read_text(), json.dumps(claude_config), tomlkit.dumps(codex_config)]
    )
    assert STAGING_DIR_NAME not in durable_text
