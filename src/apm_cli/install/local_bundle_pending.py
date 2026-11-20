"""Parked local-bundle executables, for ``apm approve --pending`` (#4221).

A local bundle is not a dependency under ``apm_modules/``, so the approval
commands cannot scan it. When ``apm install <bundle>`` parks executable types
(hooks, MCP, LSP) because the exact bundle key is not approved, this module
records the slug, the key the install printed, and the parked types in the
scope's ``apm_modules/``. Readers re-check every entry against current trust,
so an approved key never shows as pending.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

_RECORD = Path("apm_modules") / ".local-bundle-pending.json"


@dataclass(frozen=True)
class PendingBundle:
    """One installed local bundle whose executables are parked."""

    slug: str
    approval_key: str
    exec_types: tuple[str, ...]


def _record_path(scope_dir: Path) -> Path:
    return Path(scope_dir) / _RECORD


def _read(scope_dir: Path) -> dict[str, dict]:
    path = _record_path(scope_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    bundles = data.get("bundles") if isinstance(data, dict) else None
    return bundles if isinstance(bundles, dict) else {}


def record_parked_executables(
    scope_dir: Path, slug: str, approval_key: str | None, parked: Iterable[str]
) -> None:
    """Record (or, with nothing *parked*, clear) one bundle's parked executable types."""
    bundles = _read(scope_dir)
    types = sorted(set(parked))
    if types and approval_key:
        bundles[slug] = {"approval_key": approval_key, "exec_types": types}
    elif slug in bundles:
        del bundles[slug]
    else:
        return
    path = _record_path(scope_dir)
    if not bundles:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"bundles": bundles}, indent=2, sort_keys=True) + "\n", "utf-8")


def pending_bundles(
    scope_dir: Path, is_approved: Callable[[str, str], bool]
) -> list[PendingBundle]:
    """Return recorded bundles with a parked type *is_approved(key, type)* still refuses."""
    found: list[PendingBundle] = []
    for slug, entry in sorted(_read(scope_dir).items()):
        if not isinstance(entry, dict):
            continue
        key = entry.get("approval_key")
        types = entry.get("exec_types")
        if not isinstance(key, str) or not isinstance(types, list):
            continue
        still = tuple(str(t) for t in types if not is_approved(key, str(t)))
        if still:
            found.append(PendingBundle(str(slug), key, still))
    return found
