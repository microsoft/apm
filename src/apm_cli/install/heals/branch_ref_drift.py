"""Always-on heal: re-download when a branch ref's remote SHA has
advanced past the lockfile-recorded SHA.

Without this heal, ``lockfile_match=True`` would short-circuit via the
content-hash fallback (typical for virtual packages where install_path
is not a git repo), producing a 3-way inconsistency: resolved_commit
advances in the lockfile, content_hash and on-disk content stay stale.

See PR #1158 root-cause analysis. The L94 comment in
``_resolve_download_strategy`` ("Branches always fetch latest")
documented the intent; this heal is the enforcement.
"""

from __future__ import annotations

from .base import HealContext, HealMessageLevel


class BranchRefDriftHeal:
    name = "branch_ref_drift"
    order = 10
    exclusive_group = "branch_drift"

    def applies(self, hctx: HealContext) -> bool:
        from apm_cli.models.apm_package import GitReferenceType

        if not hctx.lockfile_match or hctx.update_refs:
            return False
        if hctx.resolved_ref is None:
            return False
        if hctx.resolved_ref.ref_type != GitReferenceType.BRANCH:
            return False
        # Guard against non-git resolution paths (e.g. Artifactory proxy
        # may return a ResolvedReference whose resolved_commit is None or
        # the "cached" sentinel). Without this guard a None comparison
        # below would mis-classify drift, and execute() would crash on
        # `resolved_commit[:8]`.
        remote_sha = hctx.resolved_ref.resolved_commit
        if remote_sha in (None, "", "cached"):
            return False
        if hctx.existing_lockfile is None:
            return False
        locked = hctx.existing_lockfile.get_dependency(hctx.package_key)
        if locked is None:
            return False
        if locked.resolved_commit in (None, "", "cached"):
            return False
        return remote_sha != locked.resolved_commit

    def execute(self, hctx: HealContext) -> None:
        locked = hctx.existing_lockfile.get_dependency(hctx.package_key)
        hctx.lockfile_match = False
        hctx.ref_changed = True
        # Tell FreshDependencySource that a content_hash change for this
        # dep is LEGITIMATE (caused by upstream branch advancing past the
        # lockfile-recorded SHA), not a supply-chain attack. Without
        # this the supply-chain hard-block at sources.py would abort the
        # install with sys.exit(1) before the repaired lockfile is
        # written.
        hctx.add_bypass_key(hctx.package_key)
        hctx.emit(
            HealMessageLevel.INFO,
            f"  Branch ref drift: {hctx.package_key} remote @"
            f"{hctx.resolved_ref.resolved_commit[:8]} != locked @"
            f"{locked.resolved_commit[:8]} -- forcing re-download",
        )
