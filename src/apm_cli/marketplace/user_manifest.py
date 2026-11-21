"""Owner of the ``marketplaces:`` declaration grammar.

Each entry is a ``name`` plus a ``source`` in any form ``apm marketplace
add`` accepts, with an optional ``ref`` that mirrors ``--ref``.

* The user manifest (the ``apm.yml`` at the effective APM user root) lists
  marketplaces to register; ``apm install -g`` reconciles the list into the
  marketplace registry (see :mod:`apm_cli.marketplace.reconcile`).
* A root project ``apm.yml`` may declare the same map; it resolves that
  project's ``marketplace: <name>`` entries and is never registered (see
  :mod:`apm_cli.marketplace.project_marketplaces`, #2903).
* A dependency's ``marketplaces:`` is refused: a package can never add a
  marketplace (#3006). The resolver enforces it with
  :func:`dependency_marketplaces_error`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MARKETPLACES_KEY = "marketplaces"
_ENTRY_KEYS = ("name", "source", "ref")


@dataclass(frozen=True)
class DeclaredMarketplace:
    """One validated ``marketplaces:`` entry (user or root project manifest)."""

    name: str
    source: str
    ref: str | None = None


def _ascii(value: object) -> str:
    text = value if isinstance(value, str) else str(value)
    return ascii(text)[1:-1]


def dependency_marketplaces_error(package_name: str) -> str:
    """Actionable error for ``marketplaces:`` declared by a dependency."""
    return (
        f"Dependency '{_ascii(package_name)}' declares '{MARKETPLACES_KEY}', which only "
        "the root project's apm.yml may declare: a package cannot add marketplaces. "
        "Declare the marketplace in your project's apm.yml under 'marketplaces:', "
        "or ask the package author to remove it."
    )


def _entry_error(manifest_path: Path, index: int, detail: str) -> ValueError:
    return ValueError(
        f"Invalid {MARKETPLACES_KEY}[{index}] in {_ascii(manifest_path.as_posix())}: {detail}"
    )


def _optional_text(entry: Mapping[str, Any], key: str) -> str | None:
    value = entry.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"'{key}' must be a non-empty string")
    return value.strip()


def _parse_entry(entry: object, manifest_path: Path, index: int) -> DeclaredMarketplace:
    from .registration import is_valid_alias, parse_registration_source

    if not isinstance(entry, Mapping):
        raise _entry_error(manifest_path, index, "expected a mapping with 'name' and 'source'")
    unknown = [str(key) for key in entry if key not in _ENTRY_KEYS]
    if unknown:
        raise _entry_error(
            manifest_path,
            index,
            f"unknown key(s) {', '.join(_ascii(k) for k in unknown)}; "
            f"allowed keys are {', '.join(_ENTRY_KEYS)}",
        )
    try:
        name = _optional_text(entry, "name")
        source = _optional_text(entry, "source")
        ref = _optional_text(entry, "ref")
    except ValueError as exc:
        raise _entry_error(manifest_path, index, str(exc)) from exc
    if name is None or source is None:
        raise _entry_error(manifest_path, index, "'name' and 'source' are required")
    if not is_valid_alias(name):
        raise _entry_error(
            manifest_path,
            index,
            f"name '{_ascii(name)}' must only contain letters, digits, '.', '_' and '-'",
        )
    try:
        parsed = parse_registration_source(source, ref=ref)
    except ValueError as exc:
        raise _entry_error(
            manifest_path, index, f"source '{_ascii(source)}' is not valid: {exc}"
        ) from exc
    # A relative path would resolve against wherever 'apm install -g' runs.
    if parsed.kind == "local" and source.startswith("."):
        raise _entry_error(
            manifest_path,
            index,
            f"local source '{_ascii(source)}' must be an absolute path or start with '~/'",
        )
    return DeclaredMarketplace(name=name, source=source, ref=ref)


def parse_declared_marketplaces(
    data: Mapping[str, Any], manifest_path: Path
) -> list[DeclaredMarketplace]:
    """Return the validated ``marketplaces:`` entries of a user manifest.

    Raises:
        ValueError: The declaration is malformed or names a duplicate.
    """
    raw = data.get(MARKETPLACES_KEY)
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(
            f"Invalid '{MARKETPLACES_KEY}' in {_ascii(manifest_path.as_posix())}: "
            "expected a list of entries with 'name' and 'source', for example:\n"
            f"  {MARKETPLACES_KEY}:\n"
            "    - name: acme\n"
            "      source: acme-org/agent-marketplace"
        )
    declared: list[DeclaredMarketplace] = []
    seen: dict[str, int] = {}
    for index, entry in enumerate(raw):
        item = _parse_entry(entry, manifest_path, index)
        lower = item.name.lower()
        if lower in seen:
            raise _entry_error(
                manifest_path,
                index,
                f"duplicate name '{_ascii(item.name)}' (also {MARKETPLACES_KEY}[{seen[lower]}])",
            )
        seen[lower] = index
        declared.append(item)
    return declared


def validate_marketplaces_declaration(
    data: Mapping[str, Any], manifest_path: Path
) -> tuple[DeclaredMarketplace, ...] | None:
    """Validate a manifest's ``marketplaces:`` declaration and return it.

    Called for every parsed ``apm.yml``. Returns ``None`` when the key is
    absent. Whether a declaration counts depends on the manifest's role:
    the user manifest's is reconciled into the registry, the root project's
    resolves that project, and a dependency's is refused by the resolver.

    Raises:
        ValueError: The declaration is malformed or names a duplicate.
    """
    if MARKETPLACES_KEY not in data:
        return None
    return tuple(parse_declared_marketplaces(data, manifest_path))
