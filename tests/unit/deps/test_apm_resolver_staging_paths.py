"""Regression tests for issue #2825.

A dependency fetched through the resolution-staging replacement path must be
recorded at its published ``apm_modules/<owner>/<repo>`` location, never at the
``.apm-resolution-staging`` path the download landed in.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from apm_cli.deps.apm_resolver import APMDependencyResolver
from apm_cli.install.resolution_staging import ResolutionStagingSession
from apm_cli.models.dependency.reference import DependencyReference
from apm_cli.utils.staging_guard import STAGING_DIR_NAME

_ROOT = "${CLAUDE_PLUGIN_ROOT}"
_MANIFEST = {
    "name": "tool",
    "version": "1.0.0",
    "dependencies": {
        "mcp": [
            {
                "name": "toolsrv",
                "registry": False,
                "transport": "stdio",
                "command": "node",
                "args": [
                    f"{_ROOT}/start.mjs",
                    f"--root={_ROOT}/data",
                    f"--search={_ROOT}/a:{_ROOT}/b",
                ],
                "env": {"TOOL_HOME": _ROOT, "TOOL_PREFIX": f"PREFIX={_ROOT}"},
            }
        ]
    },
}


def _resolve_through_staging(tmp_path: Path) -> tuple[Path, object]:
    modules = tmp_path / "apm_modules"
    modules.mkdir()
    staging = ResolutionStagingSession(modules)
    dep_ref = DependencyReference(repo_url="acme/tool", reference="main")

    def download_callback(dependency, apm_modules_dir, parent_chain="", parent_pkg=None):
        replacement = staging.prepare_replacement(dependency.get_install_path(apm_modules_dir))
        replacement.mkdir(parents=True, exist_ok=True)
        (replacement / "apm.yml").write_text(yaml.safe_dump(_MANIFEST), encoding="utf-8")
        (replacement / "start.mjs").write_text("//\n", encoding="utf-8")
        return replacement

    resolver = APMDependencyResolver(
        apm_modules_dir=modules,
        download_callback=download_callback,
        activation_callback=staging.publish_replacement,
        max_parallel=1,
    )
    package = resolver._try_load_dependency_package(dep_ref)
    return dep_ref.get_install_path(modules).resolve(), package


def test_staged_download_records_published_package_path(tmp_path: Path) -> None:
    live_path, package = _resolve_through_staging(tmp_path)

    assert package is not None
    assert package.package_path == live_path
    assert STAGING_DIR_NAME not in str(package.package_path)


def test_staged_download_records_published_mcp_args(tmp_path: Path) -> None:
    live_path, package = _resolve_through_staging(tmp_path)

    servers = package.get_all_mcp_dependencies()
    assert [server.name for server in servers] == ["toolsrv"]
    server = servers[0]
    assert server.args[0] == str(live_path / "start.mjs")
    assert Path(server.args[0]).exists()
    assert server.env["TOOL_HOME"] == str(live_path)


def test_staged_download_rebases_mid_token_and_repeated_roots(tmp_path: Path) -> None:
    live_path, package = _resolve_through_staging(tmp_path)

    server = package.get_all_mcp_dependencies()[0]
    assert server.args[1] == f"--root={live_path}/data"
    assert server.args[2] == f"--search={live_path}/a:{live_path}/b"
    assert server.env["TOOL_PREFIX"] == f"PREFIX={live_path}"


def test_staged_download_leaves_no_staging_reference(tmp_path: Path) -> None:
    _, package = _resolve_through_staging(tmp_path)

    server = package.get_all_mcp_dependencies()[0]
    rendered = [*server.args, *server.env.values(), str(package.package_path)]
    assert not [value for value in rendered if STAGING_DIR_NAME in value]
