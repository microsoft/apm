"""Lockfile codec for canonical deployment ownership state."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from apm_cli.core.deployment_state import (
    DeploymentLedger,
    DeploymentLocator,
    DeploymentRecord,
    LocatorKind,
)
from apm_cli.utils.path_security import PathTraversalError, ensure_path_within
from apm_cli.utils.paths import portable_relpath

if TYPE_CHECKING:
    from apm_cli.core.scope import InstallScope
    from apm_cli.deps.lockfile import LockFile
    from apm_cli.integration.targets import TargetProfile


_LEGACY_TARGET_PREFIXES = {
    ".github/": "copilot",
    ".claude/": "claude",
    ".cursor/": "cursor",
    ".windsurf/": "windsurf",
    ".kiro/": "kiro",
    ".gemini/": "gemini",
    ".codex/": "codex",
    ".opencode/": "opencode",
    ".agents/": "agents",
}


class DeploymentLedgerCodec:
    """Translate canonical deployment records to and from lockfile views."""

    @staticmethod
    def from_lockfile(lockfile: LockFile) -> DeploymentLedger:
        """Read canonical rows or synthesize them from legacy ownership views."""
        current = getattr(lockfile, "deployment_ledger", None)
        if current is not None and current.records:
            return current

        records: dict[str, DeploymentRecord] = {}
        for owner, dependency in lockfile.dependencies.items():
            if owner == ".":
                continue
            DeploymentLedgerCodec._add_legacy_paths(
                records,
                owner,
                dependency.deployed_files,
                dependency.deployed_file_hashes,
            )
        DeploymentLedgerCodec._add_legacy_paths(
            records,
            ".",
            lockfile.local_deployed_files,
            lockfile.local_deployed_file_hashes,
        )
        for runtime, servers in lockfile.mcp_target_servers.items():
            for server in servers:
                locator = DeploymentLocator(
                    kind=LocatorKind.URI,
                    target="mcp",
                    value=server,
                    runtime=runtime,
                    scope="project",
                )
                records[locator.key] = DeploymentRecord(
                    locator=locator,
                    owners=(".",),
                    active_owner=".",
                    content_hash=None,
                )
        return DeploymentLedger(records=records)

    @staticmethod
    def apply_to_lockfile(
        ledger: DeploymentLedger,
        lockfile: LockFile,
        *,
        write_legacy_views: bool = True,
    ) -> None:
        """Apply the ledger once and optionally maintain one-cycle legacy views."""
        lockfile.deployment_ledger = ledger
        if not write_legacy_views:
            return

        dependency_files: dict[str, list[str]] = {
            owner: [] for owner in lockfile.dependencies if owner != "."
        }
        dependency_hashes: dict[str, dict[str, str]] = {
            owner: {} for owner in lockfile.dependencies if owner != "."
        }
        local_files: list[str] = []
        local_hashes: dict[str, str] = {}
        mcp_targets: dict[str, list[str]] = {}

        for record in ledger.records.values():
            locator = record.locator
            if locator.target == "mcp" and locator.runtime:
                mcp_targets.setdefault(locator.runtime, []).append(locator.value)
                continue
            path = DeploymentLedgerCodec._legacy_value(locator)
            for owner in record.owners:
                if owner == ".":
                    local_files.append(path)
                    if record.content_hash is not None:
                        local_hashes[path] = record.content_hash
                elif owner in dependency_files:
                    dependency_files[owner].append(path)
                    if record.content_hash is not None:
                        dependency_hashes[owner][path] = record.content_hash

        for owner, dependency in lockfile.dependencies.items():
            if owner == ".":
                continue
            dependency.deployed_files = sorted(dict.fromkeys(dependency_files[owner]))
            dependency.deployed_file_hashes = dict(sorted(dependency_hashes[owner].items()))
        lockfile.local_deployed_files = sorted(dict.fromkeys(local_files))
        lockfile.local_deployed_file_hashes = dict(sorted(local_hashes.items()))
        lockfile.mcp_target_servers = {
            runtime: sorted(dict.fromkeys(servers))
            for runtime, servers in sorted(mcp_targets.items())
        }

    @staticmethod
    def replace_legacy_owner(
        lockfile: LockFile,
        owner: str,
        files: list[str],
        hashes: dict[str, str],
    ) -> None:
        """Update one compatibility ownership view and invalidate its projection."""
        if owner == ".":
            lockfile.local_deployed_files = list(files)
            lockfile.local_deployed_file_hashes = dict(hashes)
        else:
            dependency = lockfile.dependencies[owner]
            dependency.deployed_files = list(files)
            dependency.deployed_file_hashes = dict(hashes)
        lockfile.deployment_ledger = DeploymentLedger(records={})

    @staticmethod
    def replace_mcp_target_servers(
        lockfile: LockFile,
        target_servers: dict[str, list[str]],
    ) -> None:
        """Update the MCP compatibility view and invalidate its projection."""
        lockfile.mcp_target_servers = {
            runtime: list(servers) for runtime, servers in target_servers.items()
        }
        lockfile.deployment_ledger = DeploymentLedger(records={})

    @staticmethod
    def replace_context_local_files(context: Any, files: list[str]) -> None:
        """Route transitional install-context ownership mutation through one owner."""
        context.local_deployed_files = list(files)

    @staticmethod
    def locator_for_path(
        path: Path | str,
        *,
        project_root: Path,
        target: TargetProfile,
        runtime: str | None = None,
        scope: InstallScope,
    ) -> DeploymentLocator:
        """Encode a path without forcing external roots into project strings."""
        scope_value = getattr(scope, "value", str(scope))
        if isinstance(path, str) and "://" in path:
            return DeploymentLocator(
                kind=LocatorKind.URI,
                target=target.name,
                value=path,
                runtime=runtime,
                scope=scope_value,
            )

        candidate = Path(path).resolve()
        root = project_root.resolve()
        try:
            project_path = ensure_path_within(candidate, root)
        except PathTraversalError:
            project_path = None
        if project_path is not None:
            return DeploymentLocator(
                kind=LocatorKind.PROJECT_RELATIVE,
                target=target.name,
                value=portable_relpath(project_path, root),
                runtime=runtime,
                scope=scope_value,
            )

        deploy_root = target.managed_deploy_root
        if deploy_root is not None:
            resolved_deploy_root = deploy_root.resolve()
            try:
                target_path = ensure_path_within(candidate, resolved_deploy_root)
            except PathTraversalError:
                target_path = None
            if target_path is not None:
                return DeploymentLocator(
                    kind=LocatorKind.TARGET_RELATIVE,
                    target=target.name,
                    value=portable_relpath(target_path, resolved_deploy_root),
                    runtime=runtime,
                    scope=scope_value,
                )

        raise RuntimeError(
            f"Cannot encode deployment path {candidate!r}: path is outside "
            "the project and target managed roots."
        )

    @staticmethod
    def resolve_locator(
        locator: DeploymentLocator,
        *,
        project_root: Path,
        target: TargetProfile,
    ) -> Path | str:
        """Resolve a persisted locator through its owning target profile."""
        if locator.kind == LocatorKind.URI:
            return locator.value
        if locator.kind == LocatorKind.PROJECT_RELATIVE:
            return ensure_path_within(project_root / locator.value, project_root)
        deploy_root = target.managed_deploy_root
        if deploy_root is None:
            raise RuntimeError(f"Target {target.name!r} has no managed root for {locator.key}")
        return ensure_path_within(deploy_root / locator.value, deploy_root)

    @staticmethod
    def rows(ledger: DeploymentLedger) -> list[dict[str, Any]]:
        """Return deterministic additive lockfile rows."""
        rows: list[dict[str, Any]] = []
        for key in sorted(ledger.records):
            record = ledger.records[key]
            locator = record.locator
            rows.append(
                {
                    "kind": locator.kind.value,
                    "target": locator.target,
                    "value": locator.value,
                    "runtime": locator.runtime,
                    "scope": locator.scope,
                    "owners": list(record.owners),
                    "active_owner": record.active_owner,
                    "content_hash": record.content_hash,
                }
            )
        return rows

    @staticmethod
    def from_rows(rows: Any) -> DeploymentLedger:
        """Parse additive rows, rejecting malformed entries."""
        records: dict[str, DeploymentRecord] = {}
        if not isinstance(rows, list):
            return DeploymentLedger(records={})
        for row in rows:
            if not isinstance(row, dict):
                continue
            owners = tuple(str(owner) for owner in row.get("owners", ()) if owner)
            active_owner = str(row.get("active_owner") or "")
            if not owners or not active_owner:
                continue
            try:
                locator = DeploymentLocator(
                    kind=LocatorKind(str(row["kind"])),
                    target=str(row["target"]),
                    value=str(row["value"]),
                    runtime=str(row["runtime"]) if row.get("runtime") is not None else None,
                    scope=str(row["scope"]),
                )
            except (KeyError, TypeError, ValueError):
                continue
            records[locator.key] = DeploymentRecord(
                locator=locator,
                owners=owners,
                active_owner=active_owner,
                content_hash=(
                    str(row["content_hash"]) if row.get("content_hash") is not None else None
                ),
            )
        return DeploymentLedger(records=records)

    @staticmethod
    def _add_legacy_paths(
        records: dict[str, DeploymentRecord],
        owner: str,
        paths: list[str],
        hashes: dict[str, str],
    ) -> None:
        for value in paths:
            locator = DeploymentLedgerCodec._legacy_locator(value)
            prior = records.get(locator.key)
            owners = list(prior.owners) if prior else []
            if owner in owners:
                owners.remove(owner)
            owners.append(owner)
            records[locator.key] = DeploymentRecord(
                locator=locator,
                owners=tuple(owners),
                active_owner=owner,
                content_hash=hashes.get(value) or (prior.content_hash if prior else None),
            )

    @staticmethod
    def _legacy_locator(value: str) -> DeploymentLocator:
        if "://" in value:
            target = value.split("://", 1)[0].removesuffix("-db")
            kind = LocatorKind.URI
        else:
            target = next(
                (
                    name
                    for prefix, name in _LEGACY_TARGET_PREFIXES.items()
                    if value.startswith(prefix)
                ),
                "legacy",
            )
            kind = LocatorKind.PROJECT_RELATIVE
        return DeploymentLocator(
            kind=kind,
            target=target,
            value=value,
            runtime=None,
            scope="project",
        )

    @staticmethod
    def _legacy_value(locator: DeploymentLocator) -> str:
        return locator.value
