"""Deploy-store staging at the lockfile phase.

The lockfile phase stages each deployed file's bytes and one
package-bound render record per verified dependency; the deploy store
commits them only once the install commits.

Exposes:

- ``stage_deployed_file()`` -- stage one deployed file's bytes and
  return its canonical digest.
- ``stage_package_records()`` -- stage the render records for the
  dependencies verified and deployed this run.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

from apm_cli.cache.deploy_store import stage as stage_deployed_bytes
from apm_cli.utils.content_hash import canonical_digest

if TYPE_CHECKING:
    from apm_cli.deps.lockfile import LockFile
    from apm_cli.install.context import InstallContext


def stage_deployed_file(full: Path, data: bytes) -> str:
    """Stage *data* deployed at *full* and return its canonical digest."""
    digest = canonical_digest(data)
    # Stored by the deploy store only once the install commits.
    stage_deployed_bytes(full, digest, hashlib.sha256(data).hexdigest(), data.isascii())
    return digest


def _package_bodies() -> dict:
    """Per-package output lines the integrators captured this run (lane C renderer)."""
    try:
        from apm_cli.install.diff_apply import _BODIES
    except ImportError:
        return {}
    return _BODIES


def stage_package_records(ctx: InstallContext, lockfile: LockFile) -> None:
    """Stage one package-bound render record per dependency verified and deployed this run.

    A record binds the exact bytes this run deployed to the package's
    verified content hash, so a later command can replay them without
    trusting the lockfile alone. Only a dependency whose content hash was
    verified this run, and whose every recorded file the integrators
    produced or claimed and this run hashed, gets one.
    """
    from apm_cli.cache.deploy_store import (
        package_record_key,
        plain_file_mode,
        stage_record,
        staged_file,
    )
    from apm_cli.policy.outcome_routing import effective_policy_fingerprint
    from apm_cli.version import get_version

    plain_mode = plain_file_mode()
    fetch = getattr(ctx, "policy_fetch", None)
    policy = effective_policy_fingerprint(
        fetch, disabled=fetch is None and bool(getattr(ctx, "no_policy", False))
    )
    bodies = _package_bodies()
    previous = getattr(ctx, "existing_lockfile", None)
    version = get_version()
    root = ctx.project_root
    for dep_key, dep in lockfile.dependencies.items():
        verified = ctx.package_hashes.get(dep_key)
        lock_hashes = dict(getattr(dep, "deployed_file_hashes", None) or {})
        produced = set(ctx.package_deployed_files.get(dep_key) or ())
        if not verified or dep.content_hash != verified or not lock_hashes:
            continue
        files: dict[str, list[str]] = {}
        ascii_only = True
        for rel, value in lock_hashes.items():
            staged = staged_file(root / rel) if rel in produced else None
            if staged is None or f"sha256:{staged[0]}" != value or staged[1] is None:
                break
            if _file_mode(root / rel) != plain_mode:
                # A replay writes plain files: a mode it cannot reproduce takes the full run.
                break
            files[rel] = [staged[0], staged[1]]
            ascii_only = ascii_only and staged[2]
        else:
            prior = previous.dependencies.get(dep_key) if previous is not None else None
            fresh = prior is None or not getattr(prior, "deployed_files", None)
            body = bodies.get(dep_key) if fresh else None
            key = package_record_key(
                dep.repo_url,
                dep.resolved_commit,
                dep.content_hash,
                ctx.targets,
                version,
                ctx.scope,
            )
            if body is None:
                # A converged re-run prints no render body; keep the one the
                # first render of these exact files recorded under this key.
                body = _recorded_body(dep, ctx, version, files)
            record = {"v": 1, "files": files, "ascii_only": ascii_only, "body": body}
            if policy is not None:
                # The effective policy this install gated with (#4940 drift replay binding).
                record["policy"] = policy
            stage_record(key, record)


def _file_mode(path: Path) -> int | None:
    try:
        return path.stat().st_mode & 0o7777
    except OSError:
        return None


def _recorded_body(dep: Any, ctx: Any, version: str, files: dict) -> list[str] | None:
    """The body an earlier record of this exact package, setup and files carries."""
    from apm_cli.cache.deploy_store import DeployStore

    try:
        store = DeployStore.open_default()
        record = (
            store.package_record(
                dep.repo_url,
                dep.resolved_commit,
                dep.content_hash,
                ctx.targets,
                version,
                ctx.scope,
            )
            if store is not None
            else None
        )
    except Exception:
        return None
    if not record or record.get("files") != files:
        return None
    return record.get("body")
