"""Marketplace output selector parsing helpers.

Extracted from parse_helpers to keep that module under 400 lines.
"""

from __future__ import annotations

from typing import Any

from ...utils.path_security import PathTraversalError, validate_path_segments
from ..errors import MarketplaceYmlError
from ..output_profiles import MARKETPLACE_OUTPUTS, known_output_names
from .class_ import MarketplaceOutputSpec


def _validate_output_name(name: str) -> str:
    """Validate one marketplace output name and return it stripped."""
    stripped = name.strip()
    known = known_output_names()
    if stripped not in known:
        raise MarketplaceYmlError(
            f"Unknown marketplace output '{stripped}'. "
            f"Permitted outputs: {', '.join(sorted(known))}"
        )
    return stripped


def _parse_output_map_entry(name: str, value: Any) -> MarketplaceOutputSpec:
    """Parse one entry from the map-form outputs block."""
    path = MARKETPLACE_OUTPUTS[name].default_output
    path_explicit = False
    if value is None:
        return MarketplaceOutputSpec(name=name, path=path, path_explicit=path_explicit)
    if not isinstance(value, dict):
        raise MarketplaceYmlError(f"'outputs.{name}' must be a mapping or null")
    unknown = set(value.keys()) - {"path"}
    if unknown:
        raise MarketplaceYmlError(
            f"Unknown key(s) in 'outputs.{name}': {', '.join(sorted(unknown))}"
        )
    raw_path = value.get("path")
    if raw_path is not None:
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise MarketplaceYmlError(f"'outputs.{name}.path' must be a non-empty string")
        path = raw_path.strip()
        path_explicit = True
        try:
            validate_path_segments(path, context=f"outputs.{name}.path")
        except PathTraversalError as exc:
            raise MarketplaceYmlError(str(exc)) from exc
    return MarketplaceOutputSpec(name=name, path=path, path_explicit=path_explicit)


def _parse_outputs_map(
    raw: dict[str, Any],
) -> tuple[tuple[str, ...], tuple[MarketplaceOutputSpec, ...]]:
    """Parse the new map-form outputs block."""
    outputs: list[str] = []
    specs: list[MarketplaceOutputSpec] = []
    seen: set[str] = set()
    for key, value in raw.items():
        if not isinstance(key, str) or not key.strip():
            raise MarketplaceYmlError("'outputs' map keys must be non-empty strings")
        name = _validate_output_name(key)
        if name in seen:
            raise MarketplaceYmlError(f"Duplicate marketplace output '{name}'")
        seen.add(name)
        outputs.append(name)
        specs.append(_parse_output_map_entry(name, value))
    if not outputs:
        raise MarketplaceYmlError("'outputs' must contain at least one marketplace output")
    return tuple(outputs), tuple(specs)


def _append_outputs_deprecation_warning(
    outputs_list: list[str], warnings_sink: list[str] | None
) -> None:
    """Append the list-form outputs deprecation warning when requested."""
    if warnings_sink is None:
        return
    names_str = ", ".join(outputs_list)
    map_lines = "\n".join(f"        {name}: {{}}" for name in outputs_list)
    warnings_sink.append(
        f"outputs: [{names_str}] is deprecated; use the map form:\n\n"
        f"      outputs:\n{map_lines}\n\n"
        "    The list form will be removed in v0.15."
    )


def _parse_outputs(
    raw: Any,
    warnings_sink: list[str] | None = None,
) -> tuple[tuple[str, ...], tuple[MarketplaceOutputSpec, ...]]:
    """Parse the marketplace output selector."""
    if raw is None:
        default_spec = MarketplaceOutputSpec(
            name="claude",
            path=MARKETPLACE_OUTPUTS["claude"].default_output,
            path_explicit=False,
        )
        return ("claude",), (default_spec,)
    if isinstance(raw, dict):
        return _parse_outputs_map(raw)

    raw_items = [raw] if isinstance(raw, str) else raw if isinstance(raw, list) else None
    if raw_items is None:
        raise MarketplaceYmlError("'outputs' must be a string, list, or mapping")

    outputs_list: list[str] = []
    specs_list: list[MarketplaceOutputSpec] = []
    seen_set: set[str] = set()
    for index, item in enumerate(raw_items):
        if not isinstance(item, str) or not item.strip():
            raise MarketplaceYmlError(f"'outputs[{index}]' must be a non-empty string")
        output = _validate_output_name(item)
        if output in seen_set:
            raise MarketplaceYmlError(f"Duplicate marketplace output '{output}'")
        seen_set.add(output)
        outputs_list.append(output)
        specs_list.append(
            MarketplaceOutputSpec(
                name=output,
                path=MARKETPLACE_OUTPUTS[output].default_output,
                path_explicit=False,
            )
        )

    if not outputs_list:
        raise MarketplaceYmlError("'outputs' must contain at least one marketplace output")
    _append_outputs_deprecation_warning(outputs_list, warnings_sink)
    return tuple(outputs_list), tuple(specs_list)
