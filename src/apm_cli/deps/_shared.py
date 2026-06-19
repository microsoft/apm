"""Shared helpers for APM package download and validation."""

from __future__ import annotations

from pathlib import Path


def _validate_and_load_package(validation_result, target_path: Path, dep_ref) -> object:
    """Check *validation_result*, clean up *target_path* on failure, and return the package.

    Args:
        validation_result: Result from ``validate_apm_package(target_path)``.
        target_path: Destination directory; removed on validation failure.
        dep_ref: Dependency reference (for error messages and ``source`` assignment).

    Returns:
        The :class:`~apm_cli.models.apm_package.APMPackage` from the validation
        result (with ``source`` already set to ``dep_ref.to_github_url()``).

    Raises:
        RuntimeError: If the package is invalid or metadata is missing.
    """
    from ..utils.file_ops import robust_rmtree

    if not validation_result.is_valid:
        if target_path.exists():
            robust_rmtree(target_path, ignore_errors=True)
        error_msg = f"Invalid APM package {dep_ref.repo_url}:\n"
        for error in validation_result.errors:
            error_msg += f"  - {error}\n"
        raise RuntimeError(error_msg.strip())

    if not validation_result.package:
        raise RuntimeError(
            f"Package validation succeeded but no package metadata found for {dep_ref.repo_url}"
        )

    package = validation_result.package
    package.source = dep_ref.to_github_url()
    return package
