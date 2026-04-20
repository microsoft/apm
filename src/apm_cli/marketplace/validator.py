"""Marketplace manifest validation.

Provides validation functions for marketplace.json integrity checking.
Used by ``apm marketplace validate`` and potentially by ``apm marketplace publish``.

All validators operate on parsed ``MarketplaceManifest`` / ``MarketplacePlugin``
objects. The JSON parser (``models.py``) already drops entries that are
structurally unrecognizable; these validators enforce additional business
rules on the successfully parsed entries.
"""

from dataclasses import dataclass, field
from typing import List, Sequence

from .models import MarketplaceManifest, MarketplacePlugin


@dataclass
class ValidationResult:
    """Result of a single validation check."""

    check_name: str
    passed: bool
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


def validate_marketplace(
    manifest: MarketplaceManifest,
) -> List[ValidationResult]:
    """Run all validation checks on a marketplace manifest.

    Returns a list of ``ValidationResult`` objects, one per check.
    """
    plugins = manifest.plugins
    return [
        validate_plugin_schema(plugins),
        validate_no_duplicate_names(plugins),
    ]


def validate_plugin_schema(
    plugins: Sequence[MarketplacePlugin],
) -> ValidationResult:
    """Check all plugins have required fields (name, source)."""
    errors: List[str] = []
    for plugin in plugins:
        if not plugin.name or not plugin.name.strip():
            errors.append("Plugin entry has empty name")
        if plugin.source is None:
            errors.append(
                f"Plugin '{plugin.name}' is missing required field 'source'"
            )
    return ValidationResult(
        check_name="Schema",
        passed=len(errors) == 0,
        errors=errors,
    )


def validate_no_duplicate_names(
    plugins: Sequence[MarketplacePlugin],
) -> ValidationResult:
    """Check no two plugins share the same name (case-insensitive)."""
    errors: List[str] = []
    seen: dict = {}
    for plugin in plugins:
        lower = plugin.name.strip().lower()
        if lower in seen:
            errors.append(
                f"Duplicate plugin name: '{plugin.name}' "
                f"(conflicts with '{seen[lower]}')"
            )
        else:
            seen[lower] = plugin.name
    return ValidationResult(
        check_name="Names",
        passed=len(errors) == 0,
        errors=errors,
    )
