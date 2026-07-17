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
_LOCAL_BUNDLE_OWNER = "local-bundle"
_SHA256_PREFIX = "sha256:"
_LOWER_HEX = frozenset("0123456789abcdef")


def _require_local_bundle_hash(path: str, content_hash: str | None) -> None:
    """Reject imperative provenance without one canonical SHA-256 digest."""
    digest = (
        content_hash.removeprefix(_SHA256_PREFIX)
        if isinstance(content_hash, str) and content_hash.startswith(_SHA256_PREFIX)
        else ""
    )
    if len(digest) != 64 or any(character not in _LOWER_HEX for character in digest):
        raise ValueError(
            f"Local bundle deployment {path!r} requires a canonical sha256:<hex> content hash"
        )


class DeploymentLedgerCodec:
    """Translate canonical deployment records to and from lockfile views."""

    @staticmethod
    def from_lockfile(lockfile: LockFile) -> DeploymentLedger:
        """Read canonical rows or synthesize them from legacy ownership views."""
        current = getattr(lockfile, "deployment_ledger", None)
        if current is not None and (
            current.records or getattr(lockfile, "_deployments_present", False)
        ):
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
        for runtime, servers in getattr(lockfile, "mcp_target_servers", {}).items():
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
        lockfile._deployments_present = True
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
        prior_ledger = DeploymentLedgerCodec.from_lockfile(lockfile)
        prior_bundle_paths = DeploymentLedgerCodec.local_bundle_paths(lockfile)
        if owner == ".":
            lockfile.local_deployed_files = list(files)
            lockfile.local_deployed_file_hashes = dict(hashes)
        else:
            dependency = lockfile.dependencies[owner]
            dependency.deployed_files = list(files)
            dependency.deployed_file_hashes = dict(hashes)
        DeploymentLedgerCodec._rebuild_from_legacy(
            lockfile,
            prior_bundle_paths,
            prior_ledger=prior_ledger,
        )

    @staticmethod
    def record_local_bundle_files(
        lockfile: LockFile,
        files: list[str],
        hashes: dict[str, str],
    ) -> None:
        """Persist imperative bundle files without making them replayable source."""
        prior_bundle_paths = DeploymentLedgerCodec.local_bundle_paths(lockfile)
        for path in files:
            _require_local_bundle_hash(path, hashes.get(path))
        merged_files = sorted(set(lockfile.local_deployed_files).union(files))
        merged_hashes = dict(lockfile.local_deployed_file_hashes)
        merged_hashes.update(hashes)
        DeploymentLedgerCodec.replace_legacy_owner(
            lockfile,
            ".",
            merged_files,
            merged_hashes,
        )

        DeploymentLedgerCodec._mark_local_bundle_paths(
            lockfile,
            prior_bundle_paths.union(files),
        )
        DeploymentLedgerCodec.apply_to_lockfile(lockfile.deployment_ledger, lockfile)

    @staticmethod
    def local_bundle_paths(lockfile: LockFile) -> frozenset[str]:
        """Return paths whose active provenance is an imperative local bundle."""
        ledger = DeploymentLedgerCodec.from_lockfile(lockfile)
        paths: set[str] = set()
        for record in ledger.records.values():
            if record.active_owner != _LOCAL_BUNDLE_OWNER:
                continue
            path = DeploymentLedgerCodec._legacy_value(record.locator)
            _require_local_bundle_hash(path, record.content_hash)
            paths.add(path)
        return frozenset(paths)

    @staticmethod
    def replace_mcp_target_servers(
        lockfile: LockFile,
        target_servers: dict[str, list[str]],
    ) -> None:
        """Update the MCP compatibility view and invalidate its projection."""
        lockfile.mcp_target_servers = {
            runtime: list(servers) for runtime, servers in target_servers.items()
        }
        lockfile._mcp_target_servers_present = True
        DeploymentLedgerCodec.refresh_from_legacy(lockfile)

    @staticmethod
    def replace_context_local_files(context: Any, files: list[str]) -> None:
        """Route transitional install-context ownership mutation through one owner."""
        context.local_deployed_files = list(files)

    @staticmethod
    def refresh_from_legacy(lockfile: LockFile) -> None:
        """Rebuild canonical rows after a compatibility view mutates in place."""
        prior_ledger = DeploymentLedgerCodec.from_lockfile(lockfile)
        prior_bundle_paths = DeploymentLedgerCodec.local_bundle_paths(lockfile)
        DeploymentLedgerCodec._rebuild_from_legacy(
            lockfile,
            prior_bundle_paths,
            prior_ledger=prior_ledger,
        )

    @staticmethod
    def invalidate_legacy_projection(lockfile: LockFile) -> None:
        """Invalidate compatibility rows while preserving imperative provenance."""
        prior_bundle_paths = DeploymentLedgerCodec.local_bundle_paths(lockfile)
        lockfile.deployment_ledger = DeploymentLedger(records={})
        lockfile._deployments_present = False
        if prior_bundle_paths:
            DeploymentLedgerCodec._rebuild_from_legacy(lockfile, prior_bundle_paths)

    @staticmethod
    def rename_local_deployed_path(
        lockfile: LockFile,
        old_value: str,
        new_value: str,
    ) -> None:
        """Rename one local path while preserving imperative provenance."""
        if old_value not in lockfile.local_deployed_files:
            return
        prior_bundle_paths = DeploymentLedgerCodec.local_bundle_paths(lockfile)
        was_local_bundle = old_value in prior_bundle_paths
        lockfile.local_deployed_files = [
            value for value in lockfile.local_deployed_files if value != old_value
        ]
        if new_value not in lockfile.local_deployed_files:
            lockfile.local_deployed_files.append(new_value)
        if old_value in lockfile.local_deployed_file_hashes:
            old_hash = lockfile.local_deployed_file_hashes.pop(old_value)
            lockfile.local_deployed_file_hashes.setdefault(new_value, old_hash)
        if was_local_bundle:
            renamed_bundle_paths = (prior_bundle_paths - {old_value}) | {new_value}
            DeploymentLedgerCodec._rebuild_from_legacy(
                lockfile,
                frozenset(renamed_bundle_paths),
            )
        else:
            lockfile.deployment_ledger = DeploymentLedger(records={})
            lockfile._deployments_present = False

    @staticmethod
    def _rebuild_from_legacy(
        lockfile: LockFile,
        prior_bundle_paths: frozenset[str],
        *,
        prior_ledger: DeploymentLedger | None = None,
    ) -> None:
        """Rebuild compatibility rows and restore surviving bundle provenance."""
        lockfile.deployment_ledger = DeploymentLedger(records={})
        lockfile._deployments_present = False
        lockfile.deployment_ledger = DeploymentLedgerCodec.from_lockfile(lockfile)
        lockfile._deployments_present = True
        if prior_ledger is not None:
            previous_by_locator = {
                (
                    record.locator.kind,
                    record.locator.value,
                    record.locator.runtime,
                    record.locator.scope,
                ): record
                for record in prior_ledger.records.values()
            }
            records: dict[str, DeploymentRecord] = {}
            for record in lockfile.deployment_ledger.records.values():
                locator = record.locator
                previous = previous_by_locator.get(
                    (locator.kind, locator.value, locator.runtime, locator.scope)
                )
                if previous is None:
                    records[locator.key] = record
                    continue
                active_owner = (
                    previous.active_owner
                    if previous.active_owner in record.owners
                    else record.active_owner
                )
                preserved = DeploymentRecord(
                    locator=previous.locator,
                    owners=record.owners,
                    active_owner=active_owner,
                    content_hash=record.content_hash,
                )
                records[preserved.locator.key] = preserved
            lockfile.deployment_ledger = DeploymentLedger(records=records)
        surviving_paths = prior_bundle_paths.intersection(lockfile.local_deployed_files)
        DeploymentLedgerCodec._mark_local_bundle_paths(lockfile, surviving_paths)

    @staticmethod
    def _mark_local_bundle_paths(
        lockfile: LockFile,
        bundle_paths: frozenset[str],
    ) -> None:
        """Mark existing ledger rows as hash-audited imperative bundle output."""
        if not bundle_paths:
            return
        records = dict(lockfile.deployment_ledger.records)
        marked_paths: set[str] = set()
        for key, record in records.items():
            path = DeploymentLedgerCodec._legacy_value(record.locator)
            if path not in bundle_paths:
                continue
            _require_local_bundle_hash(path, record.content_hash)
            owners = tuple(owner for owner in record.owners if owner != _LOCAL_BUNDLE_OWNER)
            records[key] = DeploymentRecord(
                locator=record.locator,
                owners=(*owners, _LOCAL_BUNDLE_OWNER),
                active_owner=_LOCAL_BUNDLE_OWNER,
                content_hash=record.content_hash,
            )
            marked_paths.add(path)
        missing = bundle_paths - marked_paths
        if missing:
            raise ValueError(f"Local bundle deployment rows are missing: {sorted(missing)}")
        lockfile.deployment_ledger = DeploymentLedger(records=records)
        lockfile._deployments_present = True

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
        DeploymentLedgerCodec.validate_rows(rows)
        records: dict[str, DeploymentRecord] = {}
        for row in rows:
            owners = tuple(str(owner) for owner in row.get("owners", ()) if owner)
            active_owner = str(row.get("active_owner") or "")
            locator = DeploymentLocator(
                kind=LocatorKind(str(row["kind"])),
                target=str(row["target"]),
                value=str(row["value"]),
                runtime=str(row["runtime"]) if row.get("runtime") is not None else None,
                scope=str(row["scope"]),
            )
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
    def validate_rows(rows: Any) -> None:
        """Reject any malformed deployment row before ledger construction."""
        if not isinstance(rows, list):
            raise ValueError("Lockfile deployments must be a list")
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise ValueError(f"Lockfile deployment row {index} must be a mapping")
            try:
                LocatorKind(str(row["kind"]))
                target = row["target"]
                value = row["value"]
                scope = row["scope"]
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"Lockfile deployment row {index} has invalid locator") from exc
            if not all(isinstance(item, str) and item for item in (target, value, scope)):
                raise ValueError(
                    f"Lockfile deployment row {index} locator fields must be non-empty strings"
                )
            owners = row.get("owners")
            active_owner = row.get("active_owner")
            if (
                not isinstance(owners, list)
                or not owners
                or not all(isinstance(owner, str) and owner for owner in owners)
                or not isinstance(active_owner, str)
                or active_owner not in owners
            ):
                raise ValueError(f"Lockfile deployment row {index} has invalid owners")
            content_hash = row.get("content_hash")
            if content_hash is not None and not isinstance(content_hash, str):
                raise ValueError(
                    f"Lockfile deployment row {index} content_hash must be a string or null"
                )

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
