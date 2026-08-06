"""Marketplace manifest validation.

Provides validation functions for marketplace.json integrity checking.
Used by ``apm marketplace validate``.

Validation runs in two layers:

1. **Structural** (``validate_raw_marketplace_structure``): operates on the raw
   JSON dict before permissive parsing.  Catches type-level violations such as
   ``plugins`` being a non-list that the parser would silently coerce to ``[]``.

2. **Business-rule** (``validate_marketplace``, ``validate_plugin_schema``,
   ``validate_no_duplicate_names``): operates on parsed
   ``MarketplaceManifest`` / ``MarketplacePlugin`` objects.  The JSON parser
   (``models.py``) already drops entries that are structurally unrecognizable;
   these validators enforce additional business rules on the successfully
   parsed entries.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from .models import MarketplaceManifest, MarketplacePlugin

_PYTHON_TO_JSON_TYPE: dict[str, str] = {
    "str": "string",
    "int": "number",
    "float": "number",
    "bool": "boolean",
    "NoneType": "null",
    "list": "array",
    "dict": "object",
}


def _json_type_name(value: Any) -> str:
    """Return the JSON-schema type name for a Python value."""
    python_name = type(value).__name__
    return _PYTHON_TO_JSON_TYPE.get(python_name, python_name)


@dataclass
class ValidationResult:
    """Result of a single validation check."""

    check_name: str
    passed: bool
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def validate_raw_marketplace_structure(data: Any) -> ValidationResult:
    """Check the raw marketplace.json dict for structural invariants.

    This runs BEFORE the permissive parser (``parse_marketplace_json``) so
    that type-level violations -- e.g. ``plugins`` being a string instead of
    a list -- are surfaced rather than silently coerced to safe defaults.

    Args:
        data: The raw parsed JSON value.  Expected to be a ``dict``.

    Returns:
        ValidationResult: ``check_name="Structure"``, failed when invariants
        are violated.
    """
    errors: list[str] = []

    if not isinstance(data, dict):
        errors.append(f"marketplace.json root must be an object, got {_json_type_name(data)!r}")
        return ValidationResult(check_name="Structure", passed=False, errors=errors)

    raw_plugins = data.get("plugins", [])
    if not isinstance(raw_plugins, list):
        errors.append(f"'plugins' field must be an array, got {_json_type_name(raw_plugins)!r}")

    return ValidationResult(
        check_name="Structure",
        passed=len(errors) == 0,
        errors=errors,
    )


def validate_marketplace(
    manifest: MarketplaceManifest,
) -> list[ValidationResult]:
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
