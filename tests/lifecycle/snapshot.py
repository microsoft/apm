"""Lifecycle world snapshots: gate surfaces and in-place restore (#4031, #4093).

``LifecycleSurfaceRoots`` names the durable roots outside the workspace that
gate decisions read or write; ``capture_surfaces`` fills the optional gate
fields of ``LifecycleStateSnapshot`` from them.

Deployment receipts bind to a deployment root's inode, so a generated model
cannot replay from a fresh ``copytree`` of its world. ``restore_tree`` makes a
destination byte-equal to a pristine copy while every directory present on
both sides is synchronized in place rather than recreated.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from apm_cli.utils.paths import portable_relpath
from apm_cli.utils.yaml_io import load_yaml_str


def restore_tree(source: Path, destination: Path) -> None:
    """Make *destination* byte-equal to *source*, keeping surviving directories."""
    destination.mkdir(exist_ok=True)
    wanted = {entry.name: entry for entry in os.scandir(source)}
    for entry in os.scandir(destination):
        if entry.name not in wanted:
            _remove_path(Path(entry.path))
    for name, entry in wanted.items():
        target = destination / name
        if entry.is_dir(follow_symlinks=False):
            if target.is_symlink() or (target.exists() and not target.is_dir()):
                _remove_path(target)
            restore_tree(Path(entry.path), target)
            continue
        if target.is_symlink() or target.exists():
            _remove_path(target)
        if entry.is_symlink():
            target.symlink_to(os.readlink(entry.path))
        else:
            shutil.copy2(entry.path, target)
    shutil.copystat(source, destination)


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


@dataclass(frozen=True)
class LifecycleSurfaceRoots:
    """Durable roots outside the workspace that gate decisions read or write.

    ``deploy_root`` is the project root at project scope and ``$HOME`` at user
    scope; ``apm_home`` is ``$APM_HOME`` (it may live outside ``$HOME``).
    """

    deploy_root: Path
    apm_home: Path
    cache_root: Path
    user_scope: bool = False


# Native Agent Plugin activation surfaces and catalogs, per scope.
_PROJECT_NATIVE_REGISTRIES = (
    ".github/copilot/settings.local.json",
    ".github/copilot/settings.json",
    "apm_modules/.github/plugin/marketplace.json",
    "apm_modules/.github/plugin/apm-registration.json",
)
_USER_NATIVE_REGISTRIES = (".copilot/settings.json",)
_USER_HOME_NATIVE_REGISTRIES = (
    "apm_modules/.github/plugin/marketplace.json",
    "apm_modules/.github/plugin/apm-registration.json",
)
# Per-adapter MCP configuration files APM may write, per scope.
_PROJECT_MCP_CONFIGS = (
    ".mcp.json",
    ".vscode/mcp.json",
    ".cursor/mcp.json",
    ".codex/config.toml",
    "opencode.json",
)
_USER_MCP_CONFIGS = (
    ".copilot/mcp-config.json",
    ".claude.json",
    ".codex/config.toml",
    ".cursor/mcp.json",
)


def capture_surfaces(workspace_root: Path, surfaces: LifecycleSurfaceRoots) -> dict[str, tuple]:
    """Capture approval stores, policy cache, native registries and MCP configs."""
    # lifecycle_state imports this module; import its byte helpers lazily.
    from tests.utils.lifecycle_state import _canonical_json_bytes, _read_optional_file

    manifest = _read_optional_file(workspace_root / "apm.yml")
    manifest_data = load_yaml_str(manifest.decode("utf-8")) if manifest is not None else None
    user_config = _read_optional_file(surfaces.apm_home / "config.json")
    user_data = json.loads(user_config.decode("utf-8")) if user_config is not None else None
    approval_stores = (
        ("manifest", _canonical_json_bytes(_executables_block(manifest_data))),
        ("user-config", _canonical_json_bytes(_executables_block(user_data))),
    )
    policy_root = surfaces.cache_root / "policy_v1"
    policy_cache = tuple(
        (portable_relpath(path, policy_root), hashlib.sha256(path.read_bytes()).hexdigest())
        for path in sorted(policy_root.rglob("*") if policy_root.is_dir() else ())
        if path.is_file()
    )
    if surfaces.user_scope:
        registries = [
            *((f"deploy:{rel}", surfaces.deploy_root / rel) for rel in _USER_NATIVE_REGISTRIES),
            *((f"apm-home:{rel}", surfaces.apm_home / rel) for rel in _USER_HOME_NATIVE_REGISTRIES),
        ]
        mcp_configs = [(f"deploy:{rel}", surfaces.deploy_root / rel) for rel in _USER_MCP_CONFIGS]
    else:
        registries = [(f"deploy:{rel}", workspace_root / rel) for rel in _PROJECT_NATIVE_REGISTRIES]
        mcp_configs = [(f"deploy:{rel}", workspace_root / rel) for rel in _PROJECT_MCP_CONFIGS]
    return {
        "approval_stores": approval_stores,
        "policy_cache": policy_cache,
        "native_registries": tuple((label, _read_surface(path)) for label, path in registries),
        "mcp_config_files": tuple((label, _read_surface(path)) for label, path in mcp_configs),
    }


def _executables_block(data: object) -> object:
    return data.get("executables") if isinstance(data, dict) else None


def _read_surface(path: Path) -> bytes | None:
    return path.read_bytes() if path.is_file() and not path.is_symlink() else None
