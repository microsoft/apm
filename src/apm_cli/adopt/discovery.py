"""Read native target locations without importing, converting, or executing content.

The profile inversion follows @chkp-roniz's discovery work in PR #2857; the
onboarding result is restricted to existing installable local package roots.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from apm_cli.compilation.root_context_protection import catalog_root_context_markers
from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
from apm_cli.core.deployment_state import LocatorKind
from apm_cli.core.target_detection import SIGNAL_WHITELIST
from apm_cli.deps.lockfile import LockFile, resolve_lockfile_path_for_read
from apm_cli.integration.targets import (
    KNOWN_TARGETS,
    active_targets,
    active_targets_user_scope,
    apply_legacy_skill_paths,
)
from apm_cli.models.dependency import DependencyReference
from apm_cli.models.validation import validate_apm_package

from .manifest_edit import local_directory, package_entry, read_manifest
from .safety import MAX_DEPTH, MAX_ENTRIES, checked_file, checked_path, checked_tree


def _inventory(root: Path, *, user_scope: bool) -> list[tuple[Path, str]]:
    """Invert scope-resolved profiles, including legacy skill roots, once per path."""
    profiles = (
        active_targets_user_scope("all", create_config=False)
        if user_scope
        else active_targets(root, "all", create_config=False)
    )
    profiles = profiles + apply_legacy_skill_paths(profiles)
    directories: dict[Path, str] = {}
    shallow: set[Path] = set()
    for profile in profiles:
        base = root / profile.root_dir
        shallow.add(base)
        for primitive, mapping in profile.primitives.items():
            directory = root / (mapping.deploy_root or profile.root_dir) / mapping.subdir
            if mapping.subdir:
                directories[directory] = primitive
            else:
                shallow.add(directory)
    found: dict[Path, str] = {}
    for _, kind, relative in SIGNAL_WHITELIST:
        if kind == "file":
            candidate = root / relative
            if candidate.exists() or candidate.is_symlink():
                found[candidate] = "native"
    for name in catalog_root_context_markers():
        candidate = root / name
        if candidate.exists() or candidate.is_symlink():
            found[candidate] = "native"
    if (root / "SKILL.md").exists():
        found[root] = "package"
    count = 0
    pending = [(directory, kind, 0) for directory, kind in directories.items()]
    pending.extend((directory, "native", MAX_DEPTH) for directory in shallow)
    visited: set[Path] = set()
    while pending:
        directory, kind, depth = pending.pop()
        if directory in visited:
            continue
        visited.add(directory)
        try:
            checked_path(directory, root)
        except ValueError:
            if directory.exists() or directory.is_symlink():
                found[directory] = "unsafe"
            continue
        if not directory.is_dir():
            continue
        if (directory / "SKILL.md").exists() or (directory / "apm.yml").exists():
            found[directory] = "package"
            continue
        for child in directory.iterdir():
            count += 1
            if count > MAX_ENTRIES:
                raise ValueError(
                    "Discovery entry limit exceeded. Narrow the project before retrying."
                )
            if child.is_symlink():
                found[child] = "unsafe"
            elif child.is_file():
                found[child] = kind
            elif child.is_dir() and depth < MAX_DEPTH:
                pending.append((child, kind, depth + 1))
            elif not child.is_dir() or kind != "native":
                found[child] = "unsafe"
    return sorted(found.items(), key=lambda item: item[0].as_posix())


def _managed_paths(consumer: Path, root: Path) -> set[Path]:
    """Read actual deployment claims through the canonical ledger codec."""
    path = resolve_lockfile_path_for_read(consumer, read_only=True)
    checked_path(path, root)
    if not path.exists():
        return set()
    checked_file(path, root)
    lockfile = LockFile.read(path)
    if lockfile is None:
        return set()
    result: set[Path] = set()
    for record in DeploymentLedgerCodec.from_lockfile(lockfile).records.values():
        if record.locator.kind is LocatorKind.URI:
            continue
        profile = KNOWN_TARGETS.get(record.locator.target, next(iter(KNOWN_TARGETS.values())))
        if record.locator.kind is LocatorKind.TARGET_RELATIVE:
            profile = profile.for_scope(user_scope=True)
            if profile is None:
                continue
        candidate = DeploymentLedgerCodec.resolve_locator(
            record.locator, project_root=root, target=profile
        )
        checked_path(candidate, root)
        result.add(candidate)
    return result


def discover(root: Path, manifest: Path, *, user_scope: bool = False) -> dict[str, Any]:
    """Build a read-only report using normal package admission and identities."""
    _, references, _ = read_manifest(manifest, root)
    consumer = manifest.parent
    declared = {local_directory(ref, consumer) for ref in references if ref.is_local}
    slots = {
        ref.get_install_path(consumer / "apm_modules"): (
            local_directory(ref, consumer) if ref.is_local else ref.get_unique_key()
        )
        for ref in references
        if not ref.is_marketplace
    }
    managed = _managed_paths(consumer, root)
    findings: list[dict[str, Any]] = []
    supported: list[tuple[dict[str, Any], Path, Path]] = []
    for path, kind in _inventory(root, user_scope=user_scope):
        finding = {
            "path": path.relative_to(root).as_posix() if path.is_relative_to(root) else str(path),
            "kind": kind,
            "status": "unsupported",
            "reason": "Loose native content is not a package. Prepare a supported package manually.",
            "dependency": None,
        }
        findings.append(finding)
        try:
            checked_path(path, root)
            if kind == "unsafe":
                raise ValueError("Unsafe path; use a regular directory within the discovery root.")
            if any(path == owned or path.is_relative_to(owned) for owned in managed) or (
                path.is_dir() and any(owned.is_relative_to(path) for owned in managed)
            ):
                finding.update(
                    status="managed", reason="Already managed by the installation ledger."
                )
                continue
            if kind != "package":
                checked_file(path, root)
                continue
            if path == consumer or consumer.is_relative_to(path):
                finding.update(
                    status="unsafe",
                    reason="Source contains the consumer; self-dependencies are refused.",
                )
                continue
            checked_tree(path, root)
            if (path / "apm.yml").exists():
                read_manifest(path / "apm.yml", root)
            admission = validate_apm_package(path, read_only=True)
            if not admission.is_valid:
                finding["reason"] = (
                    "Existing package admission rejected this directory. Prepare it manually."
                )
                continue
            entry = package_entry(path, consumer, user_scope=user_scope)
            finding["dependency"] = entry
            if path.resolve() in declared:
                finding.update(
                    status="already-declared", reason="Existing dependency and options preserved."
                )
                continue
            if any(path.is_relative_to(other) or other.is_relative_to(path) for other in declared):
                finding.update(
                    status="unsafe", reason="Package overlaps an existing dependency source."
                )
                continue
            ref = DependencyReference.parse_from_dict(entry)
            slot = ref.get_install_path(consumer / "apm_modules")
            finding.update(
                status="supported",
                reason="Existing local package; only a dependency will be added.",
            )
            supported.append((finding, path, slot))
        except (ValueError, OSError) as exc:
            finding.update(
                status="unsafe",
                reason=f"{type(exc).__name__}: package/path rejected. Repair it and retry.",
            )
    by_slot: dict[Path, list[dict[str, Any]]] = {}
    for finding, path, slot in supported:
        by_slot.setdefault(slot, []).append(finding)
        if slot in slots and slots[slot] != path.resolve():
            finding.update(
                status="unsafe", reason="Install identity collision with an existing dependency."
            )
    for group in by_slot.values():
        if len(group) > 1:
            for finding in group:
                finding.update(
                    status="unsafe",
                    reason="Install identity collision: packages share an install path.",
                )
    for index, (finding, path, _) in enumerate(supported):
        for other_finding, other, _ in supported[index + 1 :]:
            if path.is_relative_to(other) or other.is_relative_to(path):
                finding.update(
                    status="unsafe",
                    reason="Overlapping package roots cannot be onboarded together.",
                )
                other_finding.update(
                    status="unsafe",
                    reason="Overlapping package roots cannot be onboarded together.",
                )
    return {
        "scope": "global" if user_scope else "project",
        "root": str(root),
        "manifest": str(manifest),
        "findings": findings,
        "additions": [item["dependency"] for item in findings if item["status"] == "supported"],
        "applied": False,
    }
