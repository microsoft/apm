"""Marketplace manifest validation.

Provides validation functions for marketplace.json integrity checking.
Used by ``apm marketplace validate``.

The tolerant JSON parser (``models.py``) retains structural diagnostics for
this module to surface. The remaining validators enforce business rules on
successfully parsed ``MarketplacePlugin`` entries.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field

from .models import MarketplaceManifest, MarketplacePlugin


@dataclass
class ValidationResult:
    """Result of a single validation check."""

    check_name: str
    passed: bool
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def validate_marketplace(
    manifest: MarketplaceManifest,
) -> list[ValidationResult]:
    """Run all validation checks on a marketplace manifest.

    Returns a list of ``ValidationResult`` objects, one per check.
    """
    return [
        validate_marketplace_structure(manifest),
        validate_plugin_schema(manifest.plugins),
        validate_no_duplicate_names(manifest.plugins),
    ]


def validate_marketplace_structure(manifest: MarketplaceManifest) -> ValidationResult:
    """Report raw manifest structure errors retained by the tolerant parser."""
    return ValidationResult(
        check_name="Structure",
        passed=not manifest.structural_errors,
        errors=list(manifest.structural_errors),
    )


def validate_plugin_schema(
    plugins: Sequence[MarketplacePlugin],
) -> ValidationResult:
    """Check all plugins have required fields (name, source)."""
    errors: list[str] = []
    for plugin in plugins:
        if not plugin.name or not plugin.name.strip():
            errors.append("Plugin entry has empty name")
        if plugin.source is None:
            errors.append(f"Plugin '{plugin.name}' is missing required field 'source'")
    return ValidationResult(
        check_name="Schema",
        passed=len(errors) == 0,
        errors=errors,
    )


def validate_no_duplicate_names(
    plugins: Sequence[MarketplacePlugin],
) -> ValidationResult:
    """Check no two plugins share the same name (case-insensitive)."""
    errors: list[str] = []
    seen: dict = {}
    for plugin in plugins:
        lower = plugin.name.strip().lower()
        if lower in seen:
            errors.append(
                f"Duplicate plugin name: '{plugin.name}' (conflicts with '{seen[lower]}')"
            )
        else:
            seen[lower] = plugin.name
    return ValidationResult(
        check_name="Names",
        passed=len(errors) == 0,
        errors=errors,
    )
