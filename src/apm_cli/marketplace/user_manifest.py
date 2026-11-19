"""Owner of the user-manifest ``marketplaces:`` declaration.

The user manifest (the ``apm.yml`` at the effective APM user root) may list
marketplaces to register. Each entry is a ``name`` plus a ``source`` in any
form ``apm marketplace add`` accepts, with an optional ``ref`` that mirrors
``--ref``. ``apm install -g`` reconciles the list into the marketplace
registry (see :mod:`apm_cli.marketplace.reconcile`).

Project and package manifests cannot declare marketplaces: registering a
marketplace is user-level discovery configuration, never something a
project or a dependency may impose.
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
    """One validated ``marketplaces:`` entry from the user manifest."""

    name: str
    source: str
    ref: str | None = None


def user_manifest_path() -> Path:
    """Return the user manifest path at the effective APM user root."""
    from ..core.scope import InstallScope, get_manifest_path

    return get_manifest_path(InstallScope.USER)


def is_user_manifest(manifest_path: Path) -> bool:
    """Return True when *manifest_path* is the user manifest."""
    try:
        return manifest_path.resolve() == user_manifest_path().resolve()
    except OSError:
        return False


def _ascii(value: object) -> str:
    text = value if isinstance(value, str) else str(value)
    return ascii(text)[1:-1]


def outside_user_manifest_error(manifest_path: Path) -> str:
    """Actionable error for ``marketplaces:`` outside the user manifest."""
    return (
        f"'{MARKETPLACES_KEY}' is not allowed in {_ascii(manifest_path.as_posix())}. "
        "Project and package manifests cannot declare marketplaces. "
        f"Move the entries to the user manifest ({_ascii(user_manifest_path().as_posix())}) "
        "and run 'apm install -g', or register one with 'apm marketplace add SOURCE --name NAME'."
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


def validate_marketplaces_declaration(data: Mapping[str, Any], manifest_path: Path) -> None:
    """Reject ``marketplaces:`` outside the user manifest; validate it inside.

    Called for every parsed ``apm.yml`` (project root, dependency and user
    manifest), so neither a project nor a package can declare marketplaces.

    Raises:
        ValueError: The key appears outside the user manifest or is malformed.
    """
    if MARKETPLACES_KEY not in data:
        return
    if not is_user_manifest(manifest_path):
        raise ValueError(outside_user_manifest_error(manifest_path))
    parse_declared_marketplaces(data, manifest_path)
