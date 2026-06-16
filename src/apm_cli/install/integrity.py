"""Fail-closed enforcement for ``security.integrity.require_hashes``.

When the policy enables ``require_hashes``, every non-local lockfile entry MUST
carry a content hash. A missing or empty hash is treated as a FAILURE, never a
silent pass -- an unhashed entry could let a tampered lockfile redirect a
download without detection.

This module only asserts hash-presence on the already-built lockfile entries;
it does NOT add a second filesystem hash pass (the install pipeline already
computes and records hashes via the lockfile phase). Local deps are exempt,
mirroring :func:`apm_cli.deps.registry_proxy.RegistryProxy.find_missing_hashes`
-- local packages are verified through ``deployed_file_hashes`` rather than a
package ``content_hash``.
"""

from __future__ import annotations

from ..deps.lockfile import LockedDependency


def unhashed_dependencies(
    deps: list[LockedDependency],
) -> list[LockedDependency]:
    """Return non-local lockfile entries that lack a usable ``content_hash``.

    An entry is flagged when its ``content_hash`` is ``None`` or empty. Entries
    whose ``source`` is ``"local"`` are skipped (they are not hash-anchored on a
    package digest).
    """
    flagged: list[LockedDependency] = []
    for dep in deps:
        if dep.source == "local":
            continue
        if not dep.content_hash:
            flagged.append(dep)
    return flagged


def enforce_require_hashes(deps: list[LockedDependency], *, enabled: bool) -> None:
    """Fail closed when ``require_hashes`` is on and any entry lacks a hash.

    When *enabled* is ``False`` this is a no-op, preserving today's default
    behavior. When ``True`` and one or more non-local entries are missing a
    content hash, a :class:`RuntimeError` is raised naming the offending
    dependencies.
    """
    if not enabled:
        return
    missing = unhashed_dependencies(deps)
    if not missing:
        return
    from .mcp.registry import _redact_url_credentials

    names = ", ".join(sorted(_redact_url_credentials(d.repo_url) for d in missing))
    raise RuntimeError(
        "security.integrity.require_hashes is enabled but these locked "
        f"dependencies have no content hash (fail-closed): {names}. "
        "Re-run the install so the lockfile records a hash for every entry."
    )
