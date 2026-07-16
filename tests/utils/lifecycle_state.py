"""Exact and semantic snapshots of one evolving lifecycle workspace.

For generic whole-tree hashing and diffs, use ``ArtifactSnapshot`` instead.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal, TypeAlias

from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
from apm_cli.core.deployment_state import DeploymentRecord, LocatorKind
from apm_cli.core.target_catalog import get_target_capability
from apm_cli.deps.lockfile import LEGACY_LOCKFILE_NAME, LOCKFILE_NAME, LockFile
from apm_cli.integration.hook_integrator import _APM_HOOKS_SIDECAR
from apm_cli.integration.targets import KNOWN_TARGETS
from apm_cli.utils.path_security import (
    PathTraversalError,
    ensure_path_within,
    validate_path_segments,
)
from apm_cli.utils.paths import portable_relpath
from apm_cli.utils.yaml_io import load_yaml_str

LifecycleFileKind: TypeAlias = Literal["missing", "file", "directory", "symlink"]
LifecycleFileRole: TypeAlias = Literal[
    "deployment",
    "config",
    "hook-config",
    "hook-sidecar",
    "compiled",
]
_COMPILED_NAMES = frozenset({"AGENTS.md", "CLAUDE.md", "GEMINI.md"})


@dataclass(frozen=True)
class LifecycleFileState:
    """Exact bytes and classification for one workspace-contained path."""

    relative_path: str
    kind: LifecycleFileKind
    roles: frozenset[LifecycleFileRole]
    content: bytes | None
    sha256: str | None
    link_target: str | None = None


@dataclass(frozen=True)
class LifecycleStateSnapshot:
    """Immutable exact and semantic state for a lifecycle workspace.

    Raw manifest, lockfile, and materialized file bytes prove byte-idempotency.
    ``semantic_bytes`` removes YAML presentation and lock generation time while
    preserving canonical deployment, MCP, LSP, and file-content state.
    """

    workspace_root: Path
    manifest_bytes: bytes | None
    lockfile_bytes: bytes | None
    deployment_records: tuple[DeploymentRecord, ...]
    mcp_state_bytes: bytes
    lsp_state_bytes: bytes
    files: tuple[LifecycleFileState, ...]
    semantic_bytes: bytes

    @classmethod
    def capture(
        cls,
        workspace_root: Path,
        *,
        targets: Sequence[str] = (),
        config_paths: Sequence[PurePosixPath] = (),
    ) -> LifecycleStateSnapshot:
        """Capture deterministic durable state without reading outside the workspace."""
        root = workspace_root.absolute()
        if root.is_symlink():
            raise ValueError(f"Lifecycle workspace root must not be a symlink: {root}")

        profiles = []
        for target in targets:
            capability = get_target_capability(target)
            profile = KNOWN_TARGETS.get(capability.name)
            if profile is not None:
                profiles.append(profile)

        if not root.exists():
            return cls(
                workspace_root=root,
                manifest_bytes=None,
                lockfile_bytes=None,
                deployment_records=(),
                mcp_state_bytes=_canonical_json_bytes(_empty_mcp_state()),
                lsp_state_bytes=_canonical_json_bytes(_empty_lsp_state()),
                files=(),
                semantic_bytes=_canonical_json_bytes(
                    {
                        "manifest": None,
                        "lock": None,
                        "files": [],
                    }
                ),
            )
        if not root.is_dir():
            raise ValueError(f"Lifecycle workspace root must be a directory: {root}")

        manifest_bytes = _read_optional_file(root / "apm.yml")
        lock_path = _lockfile_path(root)
        lockfile_bytes = _read_optional_file(lock_path) if lock_path is not None else None
        lock = (
            LockFile.from_yaml(lockfile_bytes.decode("utf-8"))
            if lockfile_bytes is not None
            else None
        )
        records = (
            tuple(
                record
                for _key, record in sorted(
                    lock.deployment_ledger.records.items(),
                )
            )
            if lock is not None
            else ()
        )
        target_relative = tuple(
            record.locator.key
            for record in records
            if record.locator.kind is LocatorKind.TARGET_RELATIVE
        )
        if target_relative:
            raise ValueError(
                "Lifecycle snapshots require explicit bounded roots for "
                f"target-relative deployments: {sorted(target_relative)}"
            )
        mcp_state = _mcp_state(lock)
        lsp_state = _lsp_state(lock)

        roles_by_path: dict[str, set[LifecycleFileRole]] = {}
        for record in records:
            if record.locator.kind is LocatorKind.PROJECT_RELATIVE:
                _add_role(
                    roles_by_path,
                    record.locator.value,
                    "deployment",
                )
        for relative_path in config_paths:
            _add_role(roles_by_path, relative_path.as_posix(), "config")
        for profile in profiles:
            if profile.hooks_config_display:
                hook_path = PurePosixPath(profile.hooks_config_display)
                _add_role(roles_by_path, hook_path.as_posix(), "config")
                _add_role(roles_by_path, hook_path.as_posix(), "hook-config")
                sidecar = hook_path.parent / _APM_HOOKS_SIDECAR
                _add_role(roles_by_path, sidecar.as_posix(), "config")
                _add_role(roles_by_path, sidecar.as_posix(), "hook-sidecar")
            for generated in profile.generated_files:
                relative = PurePosixPath(profile.root_dir) / generated
                _add_role(roles_by_path, relative.as_posix(), "compiled")

        if profiles:
            for relative_path in _compiled_paths(root):
                _add_role(roles_by_path, relative_path, "compiled")

        files = tuple(
            _capture_file(root, relative_path, frozenset(roles_by_path[relative_path]))
            for relative_path in sorted(roles_by_path)
        )
        manifest_semantic = (
            load_yaml_str(manifest_bytes.decode("utf-8")) if manifest_bytes is not None else None
        )
        lock_semantic = _lock_semantic(lock)
        semantic_bytes = _canonical_json_bytes(
            {
                "manifest": manifest_semantic,
                "lock": lock_semantic,
                "files": [
                    {
                        "path": file.relative_path,
                        "kind": file.kind,
                        "roles": sorted(file.roles),
                        "sha256": file.sha256,
                        "link_target": file.link_target,
                    }
                    for file in files
                ],
            }
        )
        return cls(
            workspace_root=root,
            manifest_bytes=manifest_bytes,
            lockfile_bytes=lockfile_bytes,
            deployment_records=records,
            mcp_state_bytes=_canonical_json_bytes(mcp_state),
            lsp_state_bytes=_canonical_json_bytes(lsp_state),
            files=files,
            semantic_bytes=semantic_bytes,
        )

    def file(self, relative_path: str) -> LifecycleFileState:
        """Return a tracked file state, failing clearly for untracked paths."""
        for file in self.files:
            if file.relative_path == relative_path:
                return file
        tracked = ", ".join(file.relative_path for file in self.files) or "<none>"
        raise KeyError(
            f"Lifecycle snapshot path {relative_path!r} is not tracked; tracked paths: {tracked}"
        )


def _add_role(
    roles_by_path: dict[str, set[LifecycleFileRole]],
    relative_path: str,
    role: LifecycleFileRole,
) -> None:
    validate_path_segments(
        relative_path,
        context="lifecycle snapshot path",
        reject_empty=True,
    )
    path = PurePosixPath(relative_path)
    if path.is_absolute() or "\\" in relative_path or PureWindowsPath(relative_path).drive:
        raise ValueError(f"Lifecycle snapshot path must be relative POSIX: {relative_path}")
    roles_by_path.setdefault(path.as_posix(), set()).add(role)


def _capture_file(
    root: Path,
    relative_path: str,
    roles: frozenset[LifecycleFileRole],
) -> LifecycleFileState:
    path = root.joinpath(*PurePosixPath(relative_path).parts)
    try:
        ensure_path_within(path.parent, root)
    except PathTraversalError as exc:
        raise ValueError(f"Refusing lifecycle snapshot path outside workspace: {path}") from exc
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return LifecycleFileState(
            relative_path=relative_path,
            kind="missing",
            roles=roles,
            content=None,
            sha256=None,
        )
    except NotADirectoryError as exc:
        raise ValueError(f"Lifecycle snapshot path ancestor is not a directory: {path}") from exc
    if stat.S_ISLNK(metadata.st_mode):
        return LifecycleFileState(
            relative_path=relative_path,
            kind="symlink",
            roles=roles,
            content=None,
            sha256=None,
            link_target=os.readlink(path),
        )
    if stat.S_ISDIR(metadata.st_mode):
        return LifecycleFileState(
            relative_path=relative_path,
            kind="directory",
            roles=roles,
            content=None,
            sha256=None,
        )
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"Unsupported lifecycle snapshot file type: {path}")
    content = path.read_bytes()
    return LifecycleFileState(
        relative_path=relative_path,
        kind="file",
        roles=roles,
        content=content,
        sha256=hashlib.sha256(content).hexdigest(),
    )


def _read_optional_file(path: Path) -> bytes | None:
    if path.is_symlink():
        raise ValueError(f"Lifecycle state file must be a regular file: {path}")
    if not path.exists():
        return None
    if not path.is_file():
        raise ValueError(f"Lifecycle state file must be a regular file: {path}")
    return path.read_bytes()


def _lockfile_path(root: Path) -> Path | None:
    current = root / LOCKFILE_NAME
    if current.exists() or current.is_symlink():
        return current
    legacy = root / LEGACY_LOCKFILE_NAME
    return legacy if legacy.exists() or legacy.is_symlink() else None


def _compiled_paths(root: Path) -> tuple[str, ...]:
    paths: list[str] = []
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        dirnames[:] = [name for name in dirnames if not (directory_path / name).is_symlink()]
        for filename in filenames:
            if filename in _COMPILED_NAMES:
                paths.append(portable_relpath(directory_path / filename, root))
    return tuple(sorted(paths))


def _empty_mcp_state() -> dict[str, object]:
    return {
        "servers": [],
        "configs": {},
        "target_servers": {},
        "provenance": {},
    }


def _mcp_state(lock: LockFile | None) -> dict[str, object]:
    if lock is None:
        return _empty_mcp_state()
    return {
        "servers": sorted(lock.mcp_servers),
        "configs": dict(sorted(lock.mcp_configs.items())),
        "target_servers": {
            target: sorted(servers) for target, servers in sorted(lock.mcp_target_servers.items())
        },
        "provenance": dict(sorted(lock.mcp_config_provenance.items())),
    }


def _empty_lsp_state() -> dict[str, object]:
    return {"servers": [], "configs": {}}


def _lsp_state(lock: LockFile | None) -> dict[str, object]:
    if lock is None:
        return _empty_lsp_state()
    return {
        "servers": sorted(lock.lsp_servers),
        "configs": dict(sorted(lock.lsp_configs.items())),
    }


def _lock_semantic(lock: LockFile | None) -> Mapping[str, object] | None:
    if lock is None:
        return None
    dependencies = [dependency.to_dict() for dependency in lock.get_package_dependencies()]
    return {
        "lockfile_version": lock.lockfile_version,
        "apm_version": lock.apm_version,
        "dependencies": dependencies,
        "deployments": DeploymentLedgerCodec.rows(lock.deployment_ledger),
        "mcp": _mcp_state(lock),
        "lsp": _lsp_state(lock),
        "local_deployed_files": sorted(lock.local_deployed_files),
        "local_deployed_file_hashes": dict(sorted(lock.local_deployed_file_hashes.items())),
    }


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
