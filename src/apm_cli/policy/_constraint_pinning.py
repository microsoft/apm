"""Classification of dependency constraint pinning.

A dependency is "pinned" when its declared constraint is bounded:

* Local-path deps (no version to pin).
* Registry deps that already carry a semver range (the registry
  resolver also pins via ``resolved_hash`` in the lockfile).
* Exact semver versions (``1.2.3``).
* Caret / tilde / bounded semver ranges (``^1.2.3``, ``~1.2.3``,
  ``>=1.0 <2.0``, ``1.x``).
* Literal tag refs (``v1.2.3``, ``v1.2.3-beta.1``).
* Package-qualified literal tags in the layouts the git-semver resolver
  selects from (``pkg-a-v1.2.3``, ``pkg-a--v1.2.3``), when ``pkg-a`` is
  the dependency's own package name.
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

The install pipeline rewrites ``dep.reference`` to the selected tag once
a semver range is resolved, so callers that run after that point pass
snapshots of the declared dependencies (``collect_declared_refs``) and
the classifier never judges the selected tag (#3224).
"""

from __future__ import annotations

import copy
import re
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

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

# Placeholder the git-dependency tag layouts use for the package name.
_NAME_PLACEHOLDER = "{name}"

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


def _is_package_literal_tag(spec: str, dep: DependencyReference) -> bool:
    """True when *spec* is a literal tag in a ``{name}`` layout for *dep*'s own name.

    Reuses the resolver's package-name owner, tag layouts, regex builder
    and semver parser, so a tag counts here exactly when the resolver
    could select it for this dependency. ``{name}`` is bound to this
    dependency's package name: ``pkg-b-v1.1.0`` on a ``pkg-a``
    dependency, or ``anything-v1.1.0`` on a repository named differently,
    is still a bare branch.
    """
    # Lazy imports keep the policy import graph free of resolver modules.
    from apm_cli.deps.git_semver_resolver import DEFAULT_TAG_PATTERNS
    from apm_cli.install.helpers.ref_reuse import git_semver_package_name
    from apm_cli.marketplace.semver import parse_semver
    from apm_cli.marketplace.tag_pattern import build_tag_regex

    name = git_semver_package_name(dep)
    if not name:
        return False
    for pattern in DEFAULT_TAG_PATTERNS:
        if _NAME_PLACEHOLDER not in pattern:
            continue
        match = build_tag_regex(pattern, name=name).match(spec)
        if match and parse_semver(match.group("version")) is not None:
            return True
    return False


def collect_declared_refs(
    deps: Iterable[DependencyReference],
) -> dict[str, tuple[DependencyReference, ...]]:
    """Map each direct dep's unique key to a snapshot of every declaration of it.

    Taken before resolution rewrites ``dep.reference`` to a selected tag;
    each entry is a shallow copy, so the snapshot keeps the declared ref
    (``None`` when missing) and the declaration's own package name and
    source. The unique key excludes the ref and folds repository casing,
    so one package declared twice (for example under ``dependencies`` and
    ``devDependencies``) keeps both declarations: neither a sibling
    declaration nor a selected tag can stand in for another. Pass the
    source-selected projection (``project_manifest_dependencies``) so a
    ``replace:`` rule is judged by the source and ref it selects.
    """
    declared: dict[str, tuple[DependencyReference, ...]] = {}
    for dep in deps:
        key = dep.get_unique_key()
        declared[key] = (*declared.get(key, ()), copy.copy(dep))
    return declared


def classify_unbounded_reason(dep: DependencyReference) -> UnboundedReason | None:
    """Return ``None`` if *dep*'s constraint is pinned, otherwise the reason.

    Classification order (first match wins):

    1. Local-path dep              -> ``None`` (no version surface).
    2. Registry dep + semver range -> range-shape check.
    3. Empty / missing ref         -> ``NO_REF``.
    4. 40-char hex SHA             -> ``None``.
    5. Literal ``v\\d+\\.\\d+\\.\\d+`` tag -> ``None``.
    5a. ``{name}-v...`` / ``{name}--v...`` tag for this dep's own name -> ``None``.
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
        if ref is None or not ref.strip():
            # A registry dep without a constraint is itself unbounded.
            return UnboundedReason.NO_REF
        if is_semver_range(ref):
            return _classify_range(ref)
        # Registry resolver rejects non-semver refs at parse time, but
        # defence-in-depth: treat anything else as a bare branch.
        return UnboundedReason.BARE_BRANCH

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

    # 5a. Package-qualified literal tag for this dependency's own name
    #     (``pkg-a-v1.1.0``, ``pkg-a--v1.1.0``) -> pinned. The resolver
    #     selects from exactly these layouts, and the dependency guide
    #     recommends pinning them. Any other ``...-vX.Y.Z`` string stays
    #     a bare branch below.
    if _is_package_literal_tag(spec, dep):
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
