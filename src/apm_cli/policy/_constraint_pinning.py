"""Classification of dependency constraint pinning.

A dependency is "pinned" when its declared constraint is bounded:

* Local-path deps (no version to pin).
* Registry deps that already carry a semver range (the registry
  resolver also pins via ``resolved_hash`` in the lockfile).
* Exact semver versions (``1.2.3``).
* Caret / tilde / bounded semver ranges (``^1.2.3``, ``~1.2.3``,
  ``>=1.0 <2.0``, ``1.x``).
* Literal tag refs (``v1.2.3``, ``v1.2.3-beta.1``).
* Full 40-char SHA refs.

A dependency is "unbounded" when any of the following hold:

* ``NO_REF``           -- ref is missing / empty (tracks default branch).
* ``BARE_BRANCH``      -- ref is a branch name (anything that does not
  parse as a semver range and is not a SHA or literal tag).
* ``WILDCARD``         -- range is ``*``, ``x``, ``X``.
* ``OPEN_UPPER``       -- range opens upward without a paired upper
  bound (e.g. ``>=1.0.0`` alone).
* ``GREATER_THAN_ONLY``-- range is a bare ``>X.Y.Z``.

The classification is deterministic and operates on the declared
constraint string only; no remote calls and no subprocess.  Used by
``policy.dependencies.require_pinned_constraint`` (see
``policy_checks.py::_check_pinned_constraints``).
"""

from __future__ import annotations

import re
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apm_cli.models.dependency.reference import DependencyReference


class UnboundedReason(str, Enum):
    """Why a dependency's constraint is considered unbounded."""

    NO_REF = "no-ref"
    BARE_BRANCH = "bare-branch"
    WILDCARD = "wildcard"
    OPEN_UPPER = "open-upper"
    GREATER_THAN_ONLY = "greater-than-only"


# Full 40-char hex SHA (git object id).
_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")

# Literal version tag: ``v1.2.3`` / ``v1.2.3-rc.1`` / ``v1.2.3+meta``.
_LITERAL_TAG_RE = re.compile(r"^v\d+\.\d+\.\d+([-+][0-9A-Za-z.\-+]+)?$")

# Wildcard token in any range component.
_WILDCARD_TOKEN_RE = re.compile(r"(^|\s)[\*xX](\s|$)")

# Partial-wildcard semver component (``1.2.x`` / ``1.2.*``) inside a
# multi-component range; treated as bounded because it carries an
# implicit upper edge.
_PARTIAL_WILDCARD_RE = re.compile(r"^\d+\.\d+\.[xX*]$")


def _has_wildcard(spec: str) -> bool:
    """Detect ``*`` / ``x`` / ``X`` as a standalone token in *spec*.

    The registry-side ``X.Y.x`` partial wildcard (e.g. ``1.2.x``) does
    NOT count as unbounded -- it carries an implicit upper bound on
    the next-higher minor.  Only top-level wildcards trigger this.
    """
    stripped = spec.strip()
    return bool(_WILDCARD_TOKEN_RE.search(stripped))


def _classify_range(spec: str) -> UnboundedReason | None:
    """Inspect a syntactically-valid semver range for unbounded shape.

    Returns ``None`` when the range is bounded (caret, tilde, exact,
    paired upper bound, ``X.Y.x`` partial).
    """
    if _has_wildcard(spec):
        return UnboundedReason.WILDCARD

    parts = spec.strip().split()
    # Caret, tilde, exact, partial-wildcard ranges are always bounded.
    if len(parts) == 1:
        part = parts[0]
        if part.startswith(">="):
            return UnboundedReason.OPEN_UPPER
        if part.startswith(">"):
            return UnboundedReason.GREATER_THAN_ONLY
        return None

    # Multi-component: bounded iff at least one component pins the
    # upper edge (``<`` or ``<=``) or one is a caret/tilde/wildcard.
    has_upper = False
    has_lower_only = False
    for p in parts:
        if p.startswith(("<=", "<")):
            has_upper = True
        elif p.startswith(">=") or p.startswith(">"):
            has_lower_only = True
        elif p.startswith(("^", "~")) or _PARTIAL_WILDCARD_RE.match(p):
            has_upper = True
        else:
            # Bare exact version inside a multi-component spec is
            # treated as the upper bound (e.g. ``>=1.0 1.5.0`` is
            # nonsensical but not unbounded in shape).
            has_upper = True

    if has_lower_only and not has_upper:
        return UnboundedReason.OPEN_UPPER
    return None


def _classify_registry_ref(
    ref: str | None,
    is_semver_range,
) -> UnboundedReason | None:
    """Classify an unbounded reason for a registry dependency's constraint.

    Extracted from :func:`classify_unbounded_reason` to reduce that
    function's return count below the PLR0911 threshold.
    """
    if ref is None or not ref.strip():
        return UnboundedReason.NO_REF
    if is_semver_range(ref):
        return _classify_range(ref)
    # Registry resolver rejects non-semver refs at parse time, but
    # defence-in-depth: treat anything else as a bare branch.
    return UnboundedReason.BARE_BRANCH


def classify_unbounded_reason(dep: DependencyReference) -> UnboundedReason | None:
    """Return ``None`` if *dep*'s constraint is pinned, otherwise the reason.

    Classification order (first match wins):

    1. Local-path dep              -> ``None`` (no version surface).
    2. Registry dep + semver range -> range-shape check.
    3. Empty / missing ref         -> ``NO_REF``.
    4. 40-char hex SHA             -> ``None``.
    5. Literal ``v\\d+\\.\\d+\\.\\d+`` tag -> ``None``.
    6. Parses as semver range      -> range-shape check.
    7. Else                        -> ``BARE_BRANCH``.
    """
    # 1. Local-path deps have no constraint surface to pin.
    if getattr(dep, "is_local", False):
        return None

    ref = getattr(dep, "reference", None)
    source = getattr(dep, "source", None)

    # Lazy import: avoid pulling marketplace.semver into policy import graph.
    from apm_cli.deps.registry.semver import is_semver_range

    # 2. Registry deps: the ref IS the semver range (or a single version).
    if source == "registry":
        return _classify_registry_ref(ref, is_semver_range)

    # 3. Empty / missing ref.
    if ref is None or not ref.strip():
        return UnboundedReason.NO_REF

    spec = ref.strip()

    # 4. Full SHA -> deterministic pin (covers marketplace SHA-pinning too).
    if _SHA_RE.match(spec):
        return None

    # 5. Literal v-prefixed tag -> pinned.
    if _LITERAL_TAG_RE.match(spec):
        return None

    # 5b. Bare wildcard tokens ('*', 'x', 'X') -- handled before the
    # semver-range probe because the registry grammar rejects them as
    # standalone components, but they unambiguously express "any
    # version" and deserve a wildcard hint rather than a bare-branch one.
    if spec in {"*", "x", "X"}:
        return UnboundedReason.WILDCARD

    # 6. Semver range (includes ``^1.2.3``, ``~1.2``, ``>=1 <2``,
    #    ``1.x``, plain ``1.2.3``).
    if is_semver_range(spec):
        return _classify_range(spec)

    # 7. Anything else is a bare branch name (``main``, ``develop``,
    #    ``feature/foo``).
    return UnboundedReason.BARE_BRANCH


def is_pinned_constraint(dep: DependencyReference) -> bool:
    """Return ``True`` when *dep*'s constraint is bounded / pinned."""
    return classify_unbounded_reason(dep) is None


def humanize_reason(reason: UnboundedReason, dep: DependencyReference) -> str:
    """Render a one-line actionable hint for the given reason.

    Emitted in ``[x]``/``[!]`` policy diagnostics; ASCII only per
    ``.github/instructions/encoding.instructions.md``.
    """
    ref = getattr(dep, "reference", None) or ""
    if reason is UnboundedReason.NO_REF:
        return "no ref; resolves to default branch"
    if reason is UnboundedReason.BARE_BRANCH:
        return f"bare branch '{ref}' tracks a moving tip"
    if reason is UnboundedReason.WILDCARD:
        return f"wildcard '{ref}' matches any version"
    if reason is UnboundedReason.OPEN_UPPER:
        return "unbounded upper; pair with '<X.Y' or use a caret range"
    if reason is UnboundedReason.GREATER_THAN_ONLY:
        return f"bare '{ref}' has no upper bound; pair with '<X.Y'"
    return "unbounded constraint"
