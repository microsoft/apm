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

from typing import TYPE_CHECKING, Any

from ..deps.lockfile import LockedDependency, LockFile

if TYPE_CHECKING:
    from ..policy.schema import IntegrityPolicy
    from .context import InstallContext


def require_hashes_enabled(policy: IntegrityPolicy | None) -> bool:
    """Return the canonical enablement decision for dependency hash policy."""
    return policy is not None and policy.require_hashes


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


def unhashed_dependency_names(deps: list[LockedDependency]) -> str:
    """Name unhashed entries by their dependency key, as apm.yml and the lockfile do.

    A key such as ``owner/repo`` is shown as is: the URL redactor would render
    it as an invalid registry URL. A key that is itself a URL (a repo_url that
    may carry ``user:token@`` credentials) is redacted first.
    """
    from .mcp.registry import _redact_url_credentials

    keys = (dep.get_unique_key() for dep in deps)
    return ", ".join(sorted(_redact_url_credentials(key) if "://" in key else key for key in keys))


def unhashed_dependencies_advisory(
    deps: list[LockedDependency], policy: IntegrityPolicy | None
) -> str | None:
    """Return one advisory naming unhashed entries when hashes are not required.

    With ``require_hashes`` enabled the fail-closed check names them instead.
    """
    if require_hashes_enabled(policy):
        return None
    missing = unhashed_dependencies(deps)
    if not missing:
        return None
    names = unhashed_dependency_names(missing)
    return (
        f"Locked dependencies without a content hash, verified by commit only: {names}. "
        "Set security.integrity.require_hashes: true in org policy to require hashes."
    )


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
    names = unhashed_dependency_names(missing)
    raise RuntimeError(
        "security.integrity.require_hashes is enabled but these locked "
        f"dependencies have no content hash (fail-closed): {names}. "
        "Re-run the install so the lockfile records a hash for every entry."
    )


def enforce_installed_hash_policy(ctx: InstallContext, lockfile: LockFile | None) -> None:
    """Enforce ``require_hashes`` against the lockfile this install would commit."""
    if getattr(ctx, "no_policy", False):
        return
    policy_fetch = getattr(ctx, "policy_fetch", None)
    policy = getattr(policy_fetch, "policy", None) if policy_fetch else None
    from ..deps.lockfile import get_lockfile_path

    enforce_policy_hashes(policy, lockfile, get_lockfile_path(ctx.apm_dir))


def enforce_policy_hashes(policy: Any, lockfile: LockFile | None, lockfile_path: Any) -> None:
    """Enforce *policy*'s ``require_hashes`` against *lockfile* (read at *lockfile_path*)."""
    if policy is None or not require_hashes_enabled(policy.security.integrity):
        return

    from .phases.policy_gate import PolicyViolationError

    if lockfile is None:
        from ..core.remediation import CONTENT_HASH_LOCK_RESTORE

        raise PolicyViolationError(
            "security.integrity.require_hashes is enabled but the lockfile at "
            f"{lockfile_path} could not be read (missing or corrupt); failing closed. "
            f"{CONTENT_HASH_LOCK_RESTORE.bind(lockfile=lockfile_path).sentence()}"
        )
    try:
        enforce_require_hashes(lockfile.get_package_dependencies(), enabled=True)
    except RuntimeError as exc:
        raise PolicyViolationError(str(exc)) from exc
