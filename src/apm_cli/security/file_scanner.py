"""File scanning for content integrity checks.

Extracted from ``commands/audit.py`` so the policy module can call these
without importing from the command layer.

Two scopes, because the two signals need different ones:

* :func:`scan_lockfile_packages` -- lockfile-driven. Required for anything
  compared against a recorded baseline (per-file hashes) and for per-package
  queries.
* :func:`scan_deployed_trees` -- deploy-tree-driven. Hidden-Unicode detection
  needs no baseline, so restricting it to recorded files would exempt every
  deployed file the lockfile omits (issue #2379).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Literal

import tomllib

from ..deps.lockfile import LockFile, get_lockfile_path
from ..integration.base_integrator import BaseIntegrator
from ..integration.hook_integrator import native_hook_config
from ..integration.hook_native_formats import HookContentEntry, inspect_native_hooks
from ..integration.targets import TargetProfile
from ..security.content_scanner import ContentScanner, ScanFinding
from .primitive_discovery import (
    PrimitiveSurface,
    iter_surface_files,
    primitive_surfaces,
    safe_surface_path,
)


@dataclass(frozen=True)
class CoverageEntry:
    """Discovery and check evidence, never an ownership or hash assertion."""

    file: str
    target: str
    kind: str
    pointer: str
    tracked: bool
    status: Literal["checked", "not-applicable", "incomplete"]
    diagnostic: str | None = None
    strippable: bool = False


@dataclass(frozen=True)
class FileScanResult:
    """Findings plus the exact repository-relative files examined."""

    findings_by_file: dict[str, list[ScanFinding]]
    scanned_files: frozenset[str]
    inventory: tuple[CoverageEntry, ...] = ()

    @property
    def incomplete(self) -> tuple[CoverageEntry, ...]:
        """Return recognized content whose prompt coverage could not be established."""
        return tuple(entry for entry in self.inventory if entry.status == "incomplete")

    @property
    def protected_files(self) -> frozenset[str]:
        """Keep shared/structured content out of whole-file Unicode remediation."""
        return frozenset(entry.file for entry in self.inventory if not entry.strippable)

    def merged(self, other: FileScanResult) -> FileScanResult:
        """Union scopes, treating ``self`` as the authoritative first scope."""
        return _merge_results((self, other))


def _merge_results(results: Iterable[FileScanResult]) -> FileScanResult:
    """Union scan evidence in linear time, retaining the first scope's metadata."""
    findings: dict[str, list[ScanFinding]] = {}
    scanned: set[str] = set()
    inventory: dict[tuple[str, str], CoverageEntry] = {}
    for result in results:
        for path, items in result.findings_by_file.items():
            findings.setdefault(path, items)
        scanned.update(result.scanned_files)
        for entry in result.inventory:
            inventory.setdefault((entry.file, entry.pointer), entry)
    return FileScanResult(findings, frozenset(scanned), tuple(inventory.values()))


def _empty_scan() -> FileScanResult:
    """Return an empty independent scan result."""
    return FileScanResult({}, frozenset())


def _is_safe_lockfile_path(rel_path: str, project_root: Path, *, user_scope: bool = False) -> bool:
    """Return True if a relative path from the lockfile is safe to read.

    Reuses the same logic as ``BaseIntegrator.validate_deploy_path``
    (no ``..``, allowed prefix, resolves within root).
    """
    return BaseIntegrator.validate_deploy_path(rel_path, project_root, user_scope=user_scope)


def _scan_directory_result(dir_path: Path, base_label: str) -> FileScanResult:
    """Recursively scan a directory and label exact project-relative paths."""
    from ..security.gate import REPORT_POLICY, SecurityGate

    verdict = SecurityGate.scan_files(dir_path, policy=REPORT_POLICY)
    return FileScanResult(
        findings_by_file={
            f"{base_label}/{rel_path}": file_findings
            for rel_path, file_findings in verdict.findings_by_file.items()
        },
        scanned_files=frozenset(f"{base_label}/{rel_path}" for rel_path in verdict.scanned_files),
    )


def _scan_files_in_dir(
    dir_path: Path,
    base_label: str,
) -> tuple[dict[str, list[ScanFinding]], int]:
    """Compatibility wrapper returning findings and unique file count."""
    result = _scan_directory_result(dir_path, base_label)
    return result.findings_by_file, len(result.scanned_files)


def _minimal_governed_prefixes(prefixes: set[str]) -> tuple[str, ...]:
    """Return non-empty prefixes with nested roots removed."""
    normalized = {prefix.rstrip("/") for prefix in prefixes if prefix.rstrip("/")}
    accepted: list[PurePosixPath] = []
    for prefix in sorted(normalized, key=lambda value: (len(PurePosixPath(value).parts), value)):
        candidate = PurePosixPath(prefix)
        if any(candidate == parent or candidate.is_relative_to(parent) for parent in accepted):
            continue
        accepted.append(candidate)
    return tuple(path.as_posix() for path in accepted)


def _label(path: Path, surface: PrimitiveSurface, project_root: Path) -> str:
    """Label external paths without disclosing an absolute user configuration root."""
    if path.is_relative_to(project_root):
        return path.relative_to(project_root).as_posix()
    return f"{surface.target}:{path.relative_to(surface.root).as_posix()}"


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject ambiguous native configuration instead of silently dropping fields."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate native configuration key")
        result[key] = value
    return result


def _content_entries(path: Path, surface: PrimitiveSurface) -> tuple[HookContentEntry, ...]:
    """Extract documented prompt-bearing content, not arbitrary string values."""
    if surface.kind == "executable":
        return (HookContentEntry(""),)
    text = path.read_text(encoding="utf-8")
    if surface.kind == "hooks":
        config = native_hook_config(surface.target)
        return inspect_native_hooks(
            json.loads(text, object_pairs_hook=_reject_duplicate_keys),
            surface.format_id,
            container=config.event_container_key if config else "hooks",
            prompt_types=config.prompt_handler_types if config else (),
            named_containers=config.named_containers if config else False,
            shared=surface.shared,
            nested_handlers=config.nested_handlers if config else None,
        )
    if surface.prompt_fields:
        document = tomllib.loads(text)
        return tuple(
            HookContentEntry(f"/{field}", prompt=document[field])
            if isinstance(document.get(field), str)
            else HookContentEntry(f"/{field}", error="missing or non-string native prompt field")
            for field in surface.prompt_fields
        )
    if path.suffix in {".md", ".mdc"}:
        return (HookContentEntry("", prompt=text),)
    return (HookContentEntry("", error="unsupported recognized primitive format"),)


def _scan_primitive(
    path: Path,
    surface: PrimitiveSurface,
    label: str,
    *,
    tracked: bool,
    cache: dict[PrimitiveSurface, FileScanResult] | None = None,
) -> FileScanResult:
    """Check one recognized surface and retain non-applicability/coverage evidence."""
    key = replace(
        surface,
        path=path,
        pattern=None,
        target=surface.target if surface.kind == "hooks" else "",
    )
    try:
        if not safe_surface_path(surface, path):
            return _empty_scan()
        if cache is not None and key in cache:
            cached = cache[key]
            return FileScanResult(
                {
                    label: [
                        replace(finding, file=label)
                        for findings in cached.findings_by_file.values()
                        for finding in findings
                    ]
                }
                if cached.findings_by_file
                else {},
                frozenset({label}) if cached.scanned_files else frozenset(),
                tuple(
                    replace(entry, file=label, target=surface.target, tracked=tracked)
                    for entry in cached.inventory
                ),
            )
        entries = _content_entries(path, surface)
    except (OSError, UnicodeError, ValueError) as exc:
        entries = (
            HookContentEntry("", error=f"cannot interpret primitive ({type(exc).__name__})"),
        )
    inventory: list[CoverageEntry] = []
    findings: list[ScanFinding] = []
    for entry in entries:
        status = (
            "incomplete"
            if entry.error
            else "checked"
            if entry.prompt is not None
            else "not-applicable"
        )
        inventory.append(
            CoverageEntry(
                label,
                surface.target,
                surface.kind,
                entry.pointer,
                tracked,
                status,
                entry.error,
                strippable=(
                    not surface.shared
                    and not entry.pointer
                    and status == "checked"
                    and not surface.external
                    and not surface.user_scope
                ),
            )
        )
        if entry.prompt is not None:
            for finding in ContentScanner.scan_text(entry.prompt, filename=label):
                findings.append(
                    replace(
                        finding,
                        pointer=entry.pointer,
                    )
                    if entry.pointer
                    else finding
                )
    result = FileScanResult(
        {label: findings} if findings else {},
        frozenset({label})
        if any(entry.status == "checked" for entry in inventory)
        else frozenset(),
        tuple(inventory),
    )
    if cache is not None:
        cache[key] = result
    return result


def _scan_deployed_trees(
    project_root: Path,
    targets: Sequence[TargetProfile] = (),
    *,
    user_scope: bool = False,
    cache: dict[PrimitiveSurface, FileScanResult] | None = None,
) -> FileScanResult:
    """Discover recognized primitives without consulting a deployment baseline."""
    from ..integration.targets import resolve_targets

    scoped = (
        list(targets)
        if targets
        else resolve_targets(project_root, user_scope=user_scope, create_config=False)
    )
    results: list[FileScanResult] = []
    cache = {} if cache is None else cache
    for surface in primitive_surfaces(project_root, scoped, user_scope=user_scope):
        try:
            for path in iter_surface_files(surface):
                results.append(
                    _scan_primitive(
                        path,
                        surface,
                        _label(path, surface, project_root),
                        tracked=False,
                        cache=cache,
                    )
                )
        except OSError as exc:
            label = _label(surface.path, surface, project_root)
            results.append(
                FileScanResult(
                    {},
                    frozenset(),
                    (
                        CoverageEntry(
                            label,
                            surface.target,
                            surface.kind,
                            "",
                            False,
                            "incomplete",
                            f"cannot discover primitives ({type(exc).__name__})",
                        ),
                    ),
                )
            )
    return _merge_results(results)


def scan_deployed_trees(project_root: Path) -> tuple[dict[str, list[ScanFinding]], int]:
    """Scan governed deploy trees and return findings plus unique file count."""
    result = _scan_deployed_trees(project_root)
    return result.findings_by_file, len(result.scanned_files)


def _scan_claimed_files(
    project_root: Path,
    claims: dict[str, str],
    package_filter: str | None,
    targets: Sequence[TargetProfile] = (),
    *,
    user_scope: bool = False,
    cache: dict[PrimitiveSurface, FileScanResult] | None = None,
) -> FileScanResult:
    """Scan one canonical deployment-claim projection."""
    from ..integration.targets import KNOWN_TARGETS

    # Catalog profiles classify historical claims; only selected profiles are
    # enumerated by automatic discovery.
    profiles = [
        profile
        for profile in KNOWN_TARGETS.values()
        if profile.name not in {target.name for target in targets}
        and not profile.user_root_resolver
    ] + list(reversed(targets))
    surfaces = primitive_surfaces(project_root, profiles, user_scope=user_scope)
    results: list[FileScanResult] = []
    cache = {} if cache is None else cache
    for rel_path, owner in claims.items():
        if package_filter and owner != package_filter:
            continue

        safe_path = rel_path.rstrip("/")
        abs_path = project_root / rel_path
        if "://" in rel_path:
            decoded = next(
                (
                    target.decode_external_locator(rel_path, target.managed_deploy_root)
                    for target in targets
                    if target.managed_deploy_root is not None
                    and any(rel_path.startswith(scheme) for scheme in target.lockfile_uri_schemes)
                ),
                None,
            )
            if decoded is None:
                continue
            abs_path = decoded
        candidates = [
            s
            for s in surfaces
            if s.contains(abs_path) or (s.pattern is not None and abs_path.is_relative_to(s.path))
        ]
        try:
            if candidates and not safe_surface_path(candidates[0], abs_path):
                continue
            is_directory = abs_path.is_dir()
        except OSError as exc:
            if candidates:
                candidate = candidates[0]
                results.append(
                    FileScanResult(
                        {},
                        frozenset(),
                        (
                            CoverageEntry(
                                rel_path,
                                candidate.target,
                                candidate.kind,
                                "",
                                True,
                                "incomplete",
                                f"cannot inspect recorded primitive ({type(exc).__name__})",
                            ),
                        ),
                    )
                )
            continue
        surface = next(
            (s for s in candidates if s.contains(abs_path) or is_directory),
            None,
        )
        external = surface is not None and surface.root != project_root
        if not external and not _is_safe_lockfile_path(
            safe_path, project_root, user_scope=user_scope
        ):
            continue
        if not abs_path.exists():
            continue

        if surface is None:
            surface = PrimitiveSurface(
                project_root,
                abs_path,
                "*.md" if is_directory else None,
                "recorded",
                "document" if abs_path.suffix in {".md", ".mdc"} or is_directory else "executable",
                "recorded",
                user_scope=user_scope,
            )
        if is_directory:
            directory_surfaces = [
                replace(candidate, path=abs_path)
                if candidate.pattern and abs_path.is_relative_to(candidate.path)
                else candidate
                for candidate in surfaces
                if candidate.path.is_relative_to(abs_path)
                or (candidate.pattern and abs_path.is_relative_to(candidate.path))
            ] or [surface]
            for directory_surface in directory_surfaces:
                try:
                    for path in iter_surface_files(directory_surface):
                        results.append(
                            _scan_primitive(
                                path,
                                directory_surface,
                                _label(path, directory_surface, project_root),
                                tracked=True,
                                cache=cache,
                            )
                        )
                except OSError as exc:
                    results.append(
                        FileScanResult(
                            {},
                            frozenset(),
                            (
                                CoverageEntry(
                                    rel_path,
                                    directory_surface.target,
                                    directory_surface.kind,
                                    "",
                                    True,
                                    "incomplete",
                                    f"cannot discover recorded primitives ({type(exc).__name__})",
                                ),
                            ),
                        )
                    )
            continue
        results.append(
            _scan_primitive(
                abs_path,
                surface,
                _label(abs_path, surface, project_root),
                tracked=True,
                cache=cache,
            )
        )

    return _merge_results(results)


def _scan_lockfile_packages(
    project_root: Path,
    package_filter: str | None = None,
    lockfile: LockFile | None = None,
) -> FileScanResult:
    """Collect the exact lockfile-driven scan result."""
    lock = lockfile if lockfile is not None else LockFile.read(get_lockfile_path(project_root))
    if lock is None:
        return _empty_scan()

    from ..core.deployment_ledger import DeploymentLedgerCodec

    claims = DeploymentLedgerCodec.legacy_deployed_file_claims(lock)
    return _scan_claimed_files(project_root, claims, package_filter)


def scan_lockfile_packages(
    project_root: Path,
    package_filter: str | None = None,
    lockfile: LockFile | None = None,
) -> tuple[dict[str, list[ScanFinding]], int]:
    """Scan deployed files tracked in apm.lock.yaml.

    Args:
        lockfile: An already-parsed lockfile for ``project_root``. When
            provided, the on-disk lockfile is not re-read.

    Returns:
        Findings grouped by path and the exact unique number of files scanned.
    """
    lock = lockfile if lockfile is not None else LockFile.read(get_lockfile_path(project_root))
    if lock is None:
        return {}, 0

    from ..core.deployment_ledger import DeploymentLedgerCodec

    claims = DeploymentLedgerCodec.legacy_deployed_file_claims(lock)
    result = _scan_claimed_files(project_root, claims, package_filter)
    return result.findings_by_file, len(result.scanned_files)


def scan_project_files(
    project_root: Path,
    *,
    package_filter: str | None = None,
    lockfile: LockFile | None = None,
    include_deployed_trees: bool,
    targets: Sequence[TargetProfile] = (),
) -> tuple[dict[str, list[ScanFinding]], int]:
    """Union lockfile and deploy-tree scopes with exact unique accounting."""
    result = scan_project_result(
        project_root,
        package_filter=package_filter,
        lockfile=lockfile,
        include_deployed_trees=include_deployed_trees,
        targets=targets,
    )
    return result.findings_by_file, len(result.scanned_files)


def scan_project_result(
    project_root: Path,
    *,
    package_filter: str | None = None,
    lockfile: LockFile | None = None,
    include_deployed_trees: bool = True,
    targets: Sequence[TargetProfile] = (),
    user_scope: bool = False,
) -> FileScanResult:
    """Return findings, discovery inventory and explicit incomplete coverage."""
    from ..core.deployment_ledger import DeploymentLedgerCodec
    from ..core.scope import get_workspace_deploy_root
    from ..install.drift import _read_apm_yml_target
    from ..integration.targets import resolve_targets

    deploy_root = get_workspace_deploy_root(project_root)
    user_scope = user_scope or deploy_root != project_root.resolve()
    scoped = (
        tuple(targets)
        if targets
        else tuple(
            resolve_targets(
                deploy_root,
                user_scope=user_scope,
                explicit_target=_read_apm_yml_target(project_root),
                create_config=False,
            )
        )
    )
    lock = lockfile if lockfile is not None else LockFile.read(get_lockfile_path(project_root))
    claims = DeploymentLedgerCodec.legacy_deployed_file_claims(lock) if lock else {}
    cache: dict[PrimitiveSurface, FileScanResult] = {}
    result = _scan_claimed_files(
        deploy_root, claims, package_filter, scoped, user_scope=user_scope, cache=cache
    )
    if include_deployed_trees:
        result = result.merged(
            _scan_deployed_trees(deploy_root, scoped, user_scope=user_scope, cache=cache)
        )
    return result
