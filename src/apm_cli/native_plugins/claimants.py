"""Plugin-name claimant selection for native activation (OpenAPM req-tg-013).

Among admitted dependencies that declare the same plugin name at one scope
(compared under case folding), a directly declared dependency wins over a
transitive one; two claimants at the same precedence fail. A name the
previous lock activated for one dependency is never silently repointed to a
different transitive claimant.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .errors import NativePluginError


@dataclass(frozen=True)
class Claimant:
    """One admitted dependency that claims a native plugin name."""

    name: str
    direct: bool


def select_claimants(
    claimants: Mapping[str, Claimant], prior_owners: Mapping[str, str]
) -> tuple[frozenset[str], list[str]]:
    """Return the dependency keys that win their plugin name, and notes for the rest.

    *prior_owners* maps a case-folded plugin name to the dependency key the
    previous lock activated it for. Raises :class:`NativePluginError` when two
    claimants share a name at the same precedence.
    """
    groups: dict[str, list[str]] = {}
    for key, claimant in claimants.items():
        groups.setdefault(claimant.name.casefold(), []).append(key)
    winners: set[str] = set()
    notes: list[str] = []
    for folded, keys in sorted(groups.items()):
        direct = sorted(key for key in keys if claimants[key].direct)
        pool = direct or sorted(keys)
        name = claimants[pool[0]].name
        if len(pool) > 1:
            raise NativePluginError(
                f"Native plugin name '{name}' is claimed by {' and '.join(pool)} at the same "
                "precedence. Rename one plugin, or exclude one dependency from its native targets."
            )
        winner = pool[0]
        for loser in sorted(set(keys) - {winner}):
            notes.append(
                f"Native plugin '{name}' is declared directly by {winner} and transitively by "
                f"{loser}; activating {winner} and skipping {loser}."
            )
        owner = prior_owners.get(folded)
        if owner is not None and owner != winner and not claimants[winner].direct:
            notes.append(
                f"Native plugin '{name}' was activated for {owner}; it is not repointed to the "
                f"transitive dependency {winner}. Uninstall {owner} first if this is intended."
            )
            continue
        winners.add(winner)
    return frozenset(winners), notes
