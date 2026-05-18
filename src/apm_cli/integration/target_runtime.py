"""Target profiles for multi-tool integration.

Each target tool (Copilot, Claude, Cursor, ...) describes where APM
primitives should land.  Adding a new target means adding an entry to
``KNOWN_TARGETS`` -- no new classes required.

Resolver invariant (#820): both :func:`active_targets` and
:func:`active_targets_user_scope` accept ``Union[str, List[str]]`` for
``explicit_target`` but treat the two shapes identically -- string inputs
are wrapped to a one-element list before the resolution loop.  Validity
is enforced *upstream* by
:func:`apm_cli.core.target_detection.parse_target_field`, which is the
shared gatekeeper for both ``--target`` and ``apm.yml``'s ``target:``
field.  Unknown tokens never reach these functions in normal flow; if
one does, it falls through the loop without matching any profile and
the result is an empty list (no silent ``[copilot]`` fallback).
"""

from __future__ import annotations

import sys
from pathlib import Path

from .targets import PrimitiveMapping, TargetProfile, _flag_gated

_PATH_TYPE = Path
from ._known_targets import KNOWN_TARGETS, RUNTIME_TO_CANONICAL_TARGET


def get_integration_prefixes(targets=None) -> tuple:
    """Return all known target root prefixes as a tuple.

    Used by ``BaseIntegrator.validate_deploy_path`` so the allow-list
    stays in sync with registered targets.

    When *targets* is provided, prefixes are derived from those
    (already scope-resolved) profiles.  Otherwise falls back to
    ``KNOWN_TARGETS`` for backward compatibility.

    Includes prefixes from ``deploy_root`` overrides (e.g. ``.agents/``
    for Codex skills) so cross-root paths pass security validation.
    """
    source = targets if targets is not None else KNOWN_TARGETS.values()
    prefixes: list[str] = []
    seen: set[str] = set()
    for t in source:
        # Dynamic-root targets (cowork) use cowork:// prefix in lockfile.
        # Check the *capability* (user_root_resolver is not None) rather
        # than the *run-time state* (resolved_deploy_root is not None).
        # The static KNOWN_TARGETS registry always has resolved_deploy_root
        # = None (the resolver fires only on per-install copies created by
        # for_scope()), but cleanup code passes targets=None which falls
        # back to the static registry.  Using the capability flag ensures
        # cowork:// entries pass prefix validation during cleanup/uninstall.
        if t.user_root_resolver is not None:
            from apm_cli.integration.copilot_cowork_paths import COWORK_LOCKFILE_PREFIX

            if COWORK_LOCKFILE_PREFIX not in seen:
                seen.add(COWORK_LOCKFILE_PREFIX)
                prefixes.append(COWORK_LOCKFILE_PREFIX)
            continue
        if t.prefix not in seen:
            seen.add(t.prefix)
            prefixes.append(t.prefix)
        for m in t.primitives.values():
            if m.deploy_root is not None:
                dp = f"{m.deploy_root}/"
                if dp not in seen:
                    seen.add(dp)
                    prefixes.append(dp)
    return tuple(prefixes)


from ._target_resolver import (
    active_targets,
    active_targets_user_scope,
    resolve_targets,
)
