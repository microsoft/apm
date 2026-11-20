"""Factors of the lifecycle obligation set (#4099).

The vocabulary is the steward's promise model: nine factors, six modifiers
(each a single ``on`` value; ``off`` is the default every other cell already
exercises) and three partial-equivalence exception factors. Every value that
depends on a target is derived from ``KNOWN_TARGETS``
(``src/apm_cli/integration/targets.py``); no target name is written here.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from apm_cli.integration.targets import KNOWN_TARGETS

OPS = (
    "install",
    "update",
    "uninstall",
    "prune",
    "compile",
    "lock",
    "pack",
    "bundle-install",
    "mcp-install",
    "approve-deny",
    "audit",
    "marketplace",
)
SOURCES = (
    "git",
    "local-path",
    "marketplace-git",
    "marketplace-local",
    "marketplace-file",
    "local-bundle",
    "registry",
    "native-plugin",
)
PLAIN_PRIMS = ("skill", "other-file")
# The executable domain of the gate (commands/approve.py:164).
EXECUTABLE_PRIMS = ("hook", "mcp", "lsp", "bin", "canvas")
PRIMS = (*PLAIN_PRIMS, *EXECUTABLE_PRIMS)
TARGET_CHANGES = tuple(
    f"{shape}-{change}"
    for shape in ("single", "multi")
    for change in ("unchanged", "widened", "narrowed")
)
ON = "on"
MODIFIERS = ("FROZEN", "DRYRUN", "FORCE", "SELECT", "UPDATE", "NOPOLICY")


@dataclass(frozen=True)
class GateLevel:
    """One GATE value and the authority that decides it.

    ``executable-gate`` levels are decided by ``executables.allow`` and govern
    only executable primitives; the ``org-policy`` level is an org policy
    dependency deny and governs every primitive of the package. U2 maps only
    executable-gate levels of plain primitives, so the two deny levels can
    never collapse into one cell.
    """

    name: str
    authority: str | None


GATE_LEVELS = (
    GateLevel("none", None),
    GateLevel("allowed", "executable-gate"),
    GateLevel("denied-executable", "executable-gate"),
    GateLevel("denied-dependency", "org-policy"),
    GateLevel("unapproved-exec", "executable-gate"),
    GateLevel("revoked-after-deploy", "executable-gate"),
)
GATE_AUTHORITY: Mapping[str, str | None] = {level.name: level.authority for level in GATE_LEVELS}

FACTORS: Mapping[str, tuple[str, ...]] = {
    "OP": OPS,
    "SRC": SOURCES,
    "PRIM": PRIMS,
    "SCOPE": ("project", "user"),
    "LOCK": ("absent", "consistent", "stale", "newer-generator"),
    "DEPLOYED": ("absent", "clean", "edited", "unowned", "present-provenance-unknown"),
    "GATE": tuple(level.name for level in GATE_LEVELS),
    "TGT": TARGET_CHANGES,
    "NET": ("online", "offline"),
    **{modifier: (ON,) for modifier in MODIFIERS},
}
EXCEPTION_FACTORS: Mapping[str, tuple[str, ...]] = {
    "HOSTID": ("bare-default", "qualified-host-port", "proxy-logical"),
    "MCPOWN": ("copilot", "vscode", "legacy-receipt"),
    "FILEROUTE": ("deployed", "compile-staged"),
}
# Canonical factor order of every obligation id.
ORDER = (*FACTORS, *EXCEPTION_FACTORS)
# Factors a single (source, primitive, gate) unit of a package binds together.
UNIT_FACTORS = ("SRC", "PRIM", "GATE")
TRIPLES: Mapping[str, tuple[str, str, str]] = {
    "T1": ("OP", "GATE", "PRIM"),
    "T2": ("FROZEN", "SCOPE", "GATE"),
    "T3": ("OP", "SRC", "PRIM"),
    "T4": ("OP", "SCOPE", "LOCK"),
    "T5": ("DRYRUN", "OP", "SRC"),
}


def cell_id(family: str, cell: Mapping[str, str]) -> str:
    """``<family>|F=v|...`` with factors in canonical order."""
    ordered = sorted(cell.items(), key=lambda item: ORDER.index(item[0]))
    return "|".join((family, *(f"{name}={value}" for name, value in ordered)))


def parse_cell(key: str) -> tuple[str, dict[str, str]]:
    family, *parts = key.split("|")
    return family, dict(part.split("=", 1) for part in parts if "=" in part)


def compile_staged_targets() -> tuple[str, ...]:
    """Targets whose profile has no native instructions primitive.

    A file primitive routed to one of these is staged for compile instead of
    deployed (install/services.py:1055).
    """
    return tuple(
        sorted(
            name
            for name, profile in KNOWN_TARGETS.items()
            if "instructions" not in profile.primitives
        )
    )


def row_targets() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(single, multi) target lists of the replay rows, ranked by capability.

    Candidates deploy both skills and hooks; the richest profile comes first
    (ties by name), the multi list adds the next one.
    """
    ranked = sorted(
        (
            name
            for name, profile in KNOWN_TARGETS.items()
            if {"skills", "hooks"} <= set(profile.primitives)
        ),
        key=lambda name: (-len(KNOWN_TARGETS[name].primitives), name),
    )
    return (ranked[0],), tuple(ranked[:2])
