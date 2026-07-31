"""Private helpers for the deny-wins trust-resolution ladder (issue #1873).

These are extracted from ``executables.py`` to keep that facade under the
800-line guardrail. All public names are re-exported from ``executables.py``
so callers see no change; private names stay private.
"""

from __future__ import annotations

import fnmatch
from typing import TYPE_CHECKING, Any

from ._types import (
    EXEC_TYPE_BIN,
    LAYER_ORG_DENY,
    LAYER_ORG_DENY_ALL,
    LAYER_ORG_RECOMMEND,
    LAYER_PROJECT_ALLOW,
    LAYER_USER_ALLOW,
)

if TYPE_CHECKING:
    from ._types import ExecTrustContext


def _strip_version(package_key: str) -> str:
    """Return the version-blind canonical name from an approval key."""
    return package_key.split("#", 1)[0]


def normalize_bin_deploy_deny_key(value: object) -> str:
    """Normalize package identity for ``bin_deploy.deny`` storage and lookup."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        from apm_cli.models.apm_package import DependencyReference

        raw = DependencyReference.parse(raw).get_canonical_dependency_string()
    except ValueError:
        raw = raw.removesuffix(".git")
    return raw.rstrip("/").lower()


def _map_grants(
    grant_map: dict[str, dict[str, bool]] | None,
    package_key: str,
    exec_type: str,
) -> bool:
    """Return True if *grant_map* grants *exec_type* for *package_key*.

    Matches the exact key, the version-blind name, or any stored key that
    shares the same version-blind name -- so approving ``owner/repo``
    covers ``owner/repo#v1`` and vice-versa.
    """
    if not grant_map:
        return False
    name = _strip_version(package_key)
    for stored_key, entry in grant_map.items():
        if not isinstance(entry, dict):
            continue
        if (stored_key in (package_key, name) or _strip_version(stored_key) == name) and bool(
            entry.get(exec_type, False)
        ):
            return True
    return False


def _deny_glob_match(name: str, patterns: Any) -> bool:
    """Return True if *name* matches any DENY *pattern* (exact or glob).

    Deny is the org ceiling, so in v1 it supports ``fnmatch`` globs such as
    ``evil/*`` to block a whole publisher fleet-wide. Allow / recommend /
    require remain exact-match only -- widening the GRANT side with a glob is
    a larger blast radius (a typo over-trusts), whereas broad denial is
    safety-positive (#1873).
    """
    if name in patterns:
        return True
    return any(fnmatch.fnmatchcase(name, p) for p in patterns)


def _org_denies(ctx: ExecTrustContext, name: str, exec_type: str) -> tuple[bool, str | None]:
    """Return ``(denied, layer)`` for the org DENY ceiling (rule 1)."""
    if ctx.org_deny_all:
        return True, LAYER_ORG_DENY_ALL
    if exec_type == EXEC_TYPE_BIN and ctx.org_bin_deny_all:
        return True, LAYER_ORG_DENY_ALL
    if _deny_glob_match(name, ctx.org_deny):
        return True, LAYER_ORG_DENY
    if exec_type == EXEC_TYPE_BIN:
        normalized_name = normalize_bin_deploy_deny_key(name)
        if _deny_glob_match(normalized_name, ctx.org_bin_deny):
            return True, LAYER_ORG_DENY
    return False, None


def _shadowed_grants(ctx: ExecTrustContext, name: str, exec_type: str) -> tuple[str, ...]:
    """Return lower-authority grant layers overridden by a deny decision."""
    shadowed: list[str] = []
    if _map_grants(ctx.project_allow, name, exec_type):
        shadowed.append(LAYER_PROJECT_ALLOW)
    if _map_grants(ctx.user_allow, name, exec_type):
        shadowed.append(LAYER_USER_ALLOW)
    if name in ctx.org_recommend or name in ctx.org_enforce:
        shadowed.append(LAYER_ORG_RECOMMEND)
    return tuple(shadowed)
