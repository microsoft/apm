"""Per-KIND data tables of the gate-removal security table (#4107).

Each module here defines ``SLICE``: the package and target that carry one
KIND in the lifecycle world, and its rows. A row names one exact obligation
id ``G|<path>|<change>|<kind>`` and the state facts of its removing step
(the first install, update, uninstall or prune after the change): the files
that carry the surface (absent after a removing step that succeeds, byte
identical after one that refuses or only plans), prefixes that must keep
their bytes either way, and an announcement override. ``tests/lifecycle/security/test_gate_removal.py``
discovers every module and replays every row; add a module or a row, never
shared code.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True)
class GateRow:
    id: str  # exact obligation id: G|<path>|<change>|<kind>
    # Snapshot root ("project" or "home") -> files that carry the gated surface.
    surface: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    # True when the surface files are shared config the removal edits, not deletes.
    shared: bool = False
    # Root -> path prefixes that must keep their bytes across the removing step.
    unchanged: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    # Regex the removing step's output must match; None uses the change default.
    announce: str | None = None
    smoke: bool = False
    # Strict expected failure: "#<issue> <neutral text>" (output defects) or
    # "S-pending (neutral issue to follow): <neutral phrase>" (gate enforcement).
    xfail: str = ""

    @property
    def parts(self) -> tuple[str, str, str]:
        _, path, change, kind = self.id.split("|")
        return path, change, kind


@dataclass(frozen=True)
class GateSlice:
    kind: str
    package: str  # world package key that carries the kind
    target: str
    rows: tuple[GateRow, ...]


def slices() -> tuple[GateSlice, ...]:
    """Every ``SLICE`` of this package, in module order."""
    found = []
    for info in sorted(pkgutil.iter_modules(__path__), key=lambda item: item.name):
        module = importlib.import_module(f"{__name__}.{info.name}")
        found.append(module.SLICE)
    return tuple(found)
