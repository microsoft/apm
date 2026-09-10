"""Bounded package-source admission shared by preparation and revalidation."""

import os
import stat
from pathlib import Path

from apm_cli.contracts.imports import read_project_manifest
from apm_cli.contracts.models import ContractError, ContractLimits, ContractSource, LeafContract
from apm_cli.install.target_filter import resolve_effective_package_targets
from apm_cli.integration.targets import KNOWN_TARGETS
from apm_cli.models.dependency.reference import DependencyReference
from apm_cli.models.dependency.selection import parse_dependency_entry
from apm_cli.utils.content_hash import compute_package_hash, verify_package_hash
from apm_cli.utils.path_security import ensure_path_within, has_symlink_component


def bounded_tree(root: Path, limits: ContractLimits) -> tuple[str, ...]:
    """Reject unsafe or unbounded trees before any whole-package hash or copy."""
    if not root.is_dir() or root.is_symlink():
        raise ContractError("Package source must be a regular directory.", code="invalid_source")
    pending = [root]
    count = total = 0
    files = []
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                count += 1
                if count > limits.baseline_files:
                    raise ContractError(
                        "Package tree exceeds the entry limit.", code="source_limit"
                    )
                path = Path(entry.path)
                if entry.is_symlink():
                    raise ContractError(
                        "Package trees cannot contain symlinks.", code="source_escape"
                    )
                ensure_path_within(path, root)
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    pending.append(path)
                elif stat.S_ISREG(info.st_mode):
                    total += info.st_size
                    if info.st_size > limits.file_bytes or total > limits.baseline_bytes:
                        raise ContractError("Package bytes exceed the limit.", code="source_limit")
                    files.append(path.relative_to(root).as_posix())
                else:
                    raise ContractError("Package contains a special file.", code="invalid_source")
    return tuple(sorted(files))


def source_hash(root: Path, limits: ContractLimits) -> str:
    """Delegate content identity only after bounding the traversed tree."""
    bounded_tree(root, limits)
    return compute_package_hash(root)


def validate_reference(dependency: DependencyReference) -> None:
    """Limit acquisition to ordinary local directories and Git packages."""
    if (
        dependency.source not in {None, "git", "local"}
        or dependency.is_marketplace
        or dependency.is_virtual_file()
        or dependency.is_parent_repo_inheritance
        or dependency.skill_subset
    ):
        raise ContractError(
            "Use a local APM directory or Git package reference; registry, marketplace, "
            "parent inheritance and single-file sources are unsupported.",
            code="unsupported_source",
        )


def package_dependency(
    root: Path,
    contract: LeafContract,
    limits: ContractLimits,
    *,
    allow_missing_manifest: bool = False,
) -> DependencyReference | None:
    """Admit only the selected direct dependency and no automatic activation."""
    package, data, _ = read_project_manifest(root, limits, allow_missing=allow_missing_manifest)
    for key in (
        "scripts",
        "hooks",
        "mcp",
        "plugins",
        "plugin",
        "executables",
        "bin",
        "mcpServers",
        "lspServers",
        "execute",
        "registries",
    ):
        if data.get(key):
            raise ContractError(
                f"Package {key} activation is unsupported. Use a self-contained contract package.",
                code="unsupported_package",
            )
    if any(
        (root / name).exists() for name in ("hooks", "plugin.json", ".claude-plugin", ".mcp.json")
    ):
        raise ContractError(
            "Package activation resources are unsupported.", code="unsupported_package"
        )
    allowed = resolve_effective_package_targets(
        [KNOWN_TARGETS["copilot"]], None, package, None, package.name
    )
    if not allowed.targets:
        raise ContractError("Package targets exclude Copilot.", code="unsupported_package")
    declarations = []
    for dependencies in (package.dependencies, package.dev_dependencies):
        for kind, values in (dependencies or {}).items():
            if values and kind != "apm":
                raise ContractError(
                    "Only one direct APM skill dependency is supported.", code="unsupported_import"
                )
            declarations.extend(values or [])
    if len(declarations) != len(contract.imports):
        raise ContractError(
            "Package dependencies must be exactly the one imported skill, or empty without imports.",
            code="unsupported_import",
        )
    if not declarations:
        return None
    dependency = parse_dependency_entry(declarations[0])
    if not dependency.is_parent_repo_inheritance:
        validate_reference(dependency)
    restricted = resolve_effective_package_targets(
        [KNOWN_TARGETS["copilot"]], dependency.target_subset, package, None, package.name
    )
    if not restricted.targets:
        raise ContractError(
            "Imported dependency targets exclude Copilot.", code="unsupported_import"
        )
    return dependency


def validate_source(
    source: ContractSource, contract: LeafContract, *, limits: ContractLimits
) -> None:
    """A prepared source is not trusted: recheck package bytes and supported shape."""
    ensure_path_within(contract.path, source.root)
    if has_symlink_component(source.root, contract.path):
        raise ContractError("Selected package source contains a symlink.", code="source_escape")
    bounded_tree(source.root, limits)
    expected_hash = source.prepared_hash or source.package_hash
    if expected_hash is None or not verify_package_hash(source.root, expected_hash):
        raise ContractError(
            "Prepared package content changed. Prepare again.", code="source_changed"
        )
    package_dependency(source.root, contract, limits)
