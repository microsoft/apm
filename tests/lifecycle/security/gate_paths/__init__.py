"""Per-PATH registrations of the gate-removal security table (#4107).

Each module here defines ``PATH``: a ``PathSpec`` naming one G path (the
``<path>`` of ``G|<path>|<change>|<kind>``), how it arranges a deployed and
approved package, and the steps that must then remove it after a gate
change. ``tests/lifecycle/security/test_gate_removal.py`` discovers every
module; add a module, never shared code.

A step is a command line (``{key}`` is replaced by the package key), a
``{"run": line, "exit": code}`` pin, a ``{"pair": [audit line, frozen line]}``
probe, or an input edit ``{"edit": ...}`` that ``GateReplay`` applies:
``gate-off``, ``revoke``, ``redefine``, ``policy``, ``bundle-approve``,
``bundle-deny``, ``lifecycle`` (see ``GateReplay._edit``).
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

Step = str | Mapping[str, object]
# What a step that refuses on an out-of-date lock or a denied package prints.
FROZEN_REFUSAL = r"out of sync|does not match|denied by pattern"


def default_arrange(key: str) -> list[Step]:
    """Declare the package, approve it, deploy it."""
    return [f"apm install {key}", f"apm approve {key}", "apm install"]


@dataclass(frozen=True)
class PathSpec:
    name: str  # the G path, for example "install" or "frozen-g"
    scope: str = "project"  # "project" or "user"
    key_suffix: str = ""  # world package key variant, for example ":bundle"
    sibling: bool = False  # also install the ungated p-skill (the act steps may use it)
    arrange: Callable[[str], list[Step]] = default_arrange
    act: tuple[Step, ...] = ("apm install",)
    # Regex the removing step must print, replacing the change's default.
    announce: str | None = None
    # change -> steps that apply it on this path, replacing the shared default;
    # called with (package key, kind).
    change_steps: Mapping[str, Callable[[str, str], list[Step]]] = field(default_factory=dict)


def paths() -> dict[str, PathSpec]:
    """Every ``PATH`` of this package, by G path name."""
    found: dict[str, PathSpec] = {}
    for info in sorted(pkgutil.iter_modules(__path__), key=lambda item: item.name):
        spec = importlib.import_module(f"{__name__}.{info.name}").PATH
        assert spec.name not in found, f"G path {spec.name!r} registered twice"
        found[spec.name] = spec
    return found
